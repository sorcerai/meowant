# Detection Pipeline Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore per-cat litterbox attribution (broken since the camera/Gemma rebuild) so real Ucok/Garfield visits get credited and the false "no box use" health alerts stop.

**Architecture:** Fix the cascade at its causes: (1) a circuit-breaker on the labeler so an agy/Gemini outage fails *fast* to local Gemma instead of burning a 240s timeout per frame; (2) stop feeding bowl-camera frames into the litterbox ID labeler; (3) make the per-cat no-go alarm honest when attribution is degraded; then (4) the 0-frame capture cause and (5) gallery-ref re-bootstrap as follow-ups gated on a mid-plan checkpoint.

**Tech Stack:** Python 3, stdlib + `requests` (already used), pytest (scoped to `tests/` via `pytest.ini`).

## Global Constraints

- Tracker is `bd` (NOT TodoWrite); knowledge via `bd remember`. Beads: `mun` (T1), `33i` (T2), `j29` (T3), `86p` (T4), `y2h` (T5).
- `config.json` is gitignored — never commit it; never print its secrets (`local_key`, tokens).
- Tests: `python -m pytest -q` (pytest.ini scopes to `tests/`). Suite is currently green at 302.
- Daemon reload ONLY via `launchctl kickstart -k gui/$(id -u)/com.meowant.daemon` — never stop/start. The daemon runs from the working tree (uncommitted edits are live after reload).
- `meowantd.log` is sandbox-permission-restricted for bulk reads; do not depend on grepping it in tests.
- store.py: all DB access through `with store._lock:`; new tables go in `SCHEMA`, column adds in `_MIGRATIONS`; epoch→ISO via `store._iso`.
- Labeler sentinels (mw/labeler.py): `NONE="none"` (confident empty box), `ERROR="error"` (backend failed — must be retried, never retired as no-cat).
- The three cats: `("Ucok", "Garfield", "Ella")`. Per-cat no-go thresholds (live in health_watch): `Ucok=8h, Ella=24h, Garfield=24h`.

---

### Task 1: FallbackLabeler circuit-breaker + lower agy timeout (`mun`)

**Root cause:** `FallbackLabeler.predict_visit` runs the primary (`AgyLabeler`) on every frame first; agy is timing out at 240s/call, so a 12-frame visit burns ~48 min before any fallback. The labeler can't keep up → recent visits get 0 predictions → unattributed.

**Files:**
- Modify: `mw/labeler.py` — `AgyLabeler.__init__` default timeout; rewrite `FallbackLabeler`.
- Modify: `meowantd.py:130` — construct `AgyLabeler` with the lower timeout (explicit).
- Test: `tests/test_labeler.py` (append).

**Interfaces:**
- Consumes: `Labeler.predict_visit(frame_paths, refs) -> [{"file","cat","confidence"}]`; sentinel `ERROR`.
- Produces: `FallbackLabeler(primary, fallback, *, fail_threshold=2, cooldown_s=1800, now_fn=time.time)` with a per-frame breaker. `AgyLabeler(timeout=45)` default.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_labeler.py  (append)
from mw.labeler import FallbackLabeler, ERROR


class _StubLabeler:
    """Returns a scripted result per frame; counts how many frames it saw."""
    def __init__(self, result_for):   # result_for: callable(path) -> cat string
        self.result_for = result_for
        self.seen = []
    def predict_visit(self, frame_paths, refs):
        self.seen.extend(frame_paths)
        return [{"file": p, "cat": self.result_for(p), "confidence":
                 0.0 if self.result_for(p) in (ERROR, "none") else 1.0}
                for p in frame_paths]


def test_fallback_uses_fallback_only_on_primary_error():
    primary = _StubLabeler(lambda p: ERROR if "bad" in p else "Ucok")
    fallback = _StubLabeler(lambda p: "Ella")
    fl = FallbackLabeler(primary, fallback, now_fn=lambda: 1000.0)
    out = fl.predict_visit(["good1.jpg", "bad1.jpg"], {})
    assert out[0]["cat"] == "Ucok"          # primary handled the good frame
    assert out[1]["cat"] == "Ella"          # fallback rescued the errored frame
    assert fallback.seen == ["bad1.jpg"]    # fallback only saw the error frame


def test_breaker_opens_after_threshold_and_skips_primary():
    primary = _StubLabeler(lambda p: ERROR)         # primary is fully down
    fallback = _StubLabeler(lambda p: "Ella")
    fl = FallbackLabeler(primary, fallback, fail_threshold=2,
                         cooldown_s=1800, now_fn=lambda: 1000.0)
    fl.predict_visit(["a.jpg", "b.jpg", "c.jpg", "d.jpg"], {})
    # After 2 consecutive primary errors the breaker opens; frames c,d skip primary.
    assert primary.seen == ["a.jpg", "b.jpg"]       # primary tried only twice, then skipped
    assert fallback.seen == ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]  # all rescued


def test_breaker_half_opens_after_cooldown():
    clock = [1000.0]
    primary = _StubLabeler(lambda p: ERROR)
    fallback = _StubLabeler(lambda p: "Ella")
    fl = FallbackLabeler(primary, fallback, fail_threshold=1,
                         cooldown_s=600, now_fn=lambda: clock[0])
    fl.predict_visit(["a.jpg"], {})                 # trips open
    primary.seen.clear()
    clock[0] = 1000.0 + 599                          # still in cooldown
    fl.predict_visit(["b.jpg"], {})
    assert primary.seen == []                        # skipped — breaker open
    clock[0] = 1000.0 + 601                          # cooldown elapsed
    fl.predict_visit(["c.jpg"], {})
    assert primary.seen == ["c.jpg"]                 # half-open: primary retried
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/repos/meowant && python -m pytest tests/test_labeler.py -k "fallback or breaker" -v`
Expected: FAIL — current `FallbackLabeler` has no `fail_threshold`/`now_fn` kwargs and no breaker (TypeError / assertion on `primary.seen`).

- [ ] **Step 3: Lower the agy timeout default**

In `mw/labeler.py`, change `AgyLabeler.__init__`:

```python
    def __init__(self, timeout=45):
        self.timeout = timeout
```

(45s gives Gemini a fair chance when healthy but bounds a failure to 45s, not 240s.)

- [ ] **Step 4: Rewrite `FallbackLabeler` with a per-frame circuit-breaker**

Replace the entire `FallbackLabeler` class in `mw/labeler.py` with:

```python
class FallbackLabeler(Labeler):
    """Primary labeler with a fast-fail circuit-breaker to a fallback.

    Per frame: if the breaker is open, skip the primary and use the fallback
    directly. Otherwise try the primary; on ERROR, count it and use the fallback
    for that frame. After `fail_threshold` consecutive primary errors the breaker
    OPENS for `cooldown_s` (so a primary outage costs ~`fail_threshold` timeouts,
    not one per frame). After the cooldown the next call half-opens — it retries
    the primary once; a success resets, another failure re-trips.
    """

    def __init__(self, primary, fallback, *, fail_threshold=2,
                 cooldown_s=1800, now_fn=time.time):
        self.primary = primary
        self.fallback = fallback
        self.fail_threshold = fail_threshold
        self.cooldown_s = cooldown_s
        self.now = now_fn
        self._fails = 0
        self._open_until = 0.0

    def _fallback_one(self, path, refs):
        fb = self.fallback.predict_visit([path], refs)
        return fb[0] if fb else {"file": path, "cat": ERROR, "confidence": 0.0}

    def predict_visit(self, frame_paths, refs):
        out = []
        for p in frame_paths:
            if self.now() < self._open_until:
                out.append(self._fallback_one(p, refs))   # breaker OPEN -> fallback
                continue
            r = self.primary.predict_visit([p], refs)
            r = r[0] if r else {"file": p, "cat": ERROR, "confidence": 0.0}
            if r.get("cat") == ERROR:
                self._fails += 1
                if self._fails >= self.fail_threshold:
                    self._open_until = self.now() + self.cooldown_s
                    self._fails = 0
                    print("[labeler/breaker] primary tripped open; "
                          f"fallback-only for {self.cooldown_s}s", file=sys.stderr)
                out.append(self._fallback_one(p, refs))
            else:
                self._fails = 0                            # primary healthy -> reset streak
                out.append(r)
        return out
```

Add `import time` at the top of `mw/labeler.py` if not present (it imports `json, subprocess, sys`; add `time`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_labeler.py -q`
Expected: PASS.

- [ ] **Step 6: Wire the lower timeout explicitly in meowantd**

In `meowantd.py` line ~130, change:
`FallbackLabeler(AgyLabeler(), LlamaCppLabeler())`
to:
`FallbackLabeler(AgyLabeler(timeout=45), LlamaCppLabeler())`

- [ ] **Step 7: Full suite + commit**

Run: `cd ~/repos/meowant && python -m pytest -q`
Expected: PASS (≥305).

```bash
git add mw/labeler.py meowantd.py tests/test_labeler.py
git commit -m "fix(labeler): circuit-breaker fails fast to local Gemma; agy timeout 240->45s

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017phvBTbjCeqZo9QYaMRqoG"
```

---

### Task 2: Capture only litterbox cameras (`33i`)

**Root cause:** `CaptureService` is handed the full `cameras` list (all 6). Since the bowl cams (`meowcam5/6`) were added for BowlWatch, every litterbox visit now also grabs bowl frames — irrelevant to ID, and each one burns a labeler call.

**Files:**
- Modify: `meowantd.py` (the `cams`/CaptureService block ~line 86) — pass only litterbox cams.
- Test: `tests/test_meowantd_capture_cams.py` (create) — unit-test the filter helper.

**Interfaces:**
- Produces: a module-level helper `meowantd.litterbox_cameras(cameras, bowls) -> list` returning the cameras NOT referenced by any bowl's `camera` field. meowantd passes its result to `CaptureService`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_meowantd_capture_cams.py
from meowantd import litterbox_cameras


def test_litterbox_cameras_excludes_bowl_cams():
    cams = [{"name": "meowcam1"}, {"name": "meowcam2"}, {"name": "meowcam3"},
            {"name": "meowcam4"}, {"name": "meowcam5"}, {"name": "meowcam6"}]
    bowls = [{"camera": "meowcam6"}, {"camera": "meowcam5"}]
    out = [c["name"] for c in litterbox_cameras(cams, bowls)]
    assert out == ["meowcam1", "meowcam2", "meowcam3", "meowcam4"]


def test_litterbox_cameras_no_bowls_returns_all():
    cams = [{"name": "meowcam1"}, {"name": "meowcam2"}]
    assert litterbox_cameras(cams, []) == cams
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_meowantd_capture_cams.py -v`
Expected: FAIL — `ImportError: cannot import name 'litterbox_cameras'`.

- [ ] **Step 3: Add the helper + use it**

In `meowantd.py`, add a module-level function (above `main`):

```python
def litterbox_cameras(cameras, bowls):
    """Cameras used for litterbox ID/scatter — everything NOT assigned to a bowl.
    Bowl cams (BowlWatch) must not be captured for litterbox visits: their frames
    never show the box and each one costs a labeler call."""
    bowl_cams = {b.get("camera") for b in (bowls or [])}
    return [c for c in cameras if c.get("name") not in bowl_cams]
```

Then in `main`, change the capture wiring. Find:
```python
    cams = config.get(cfg, "cameras", [])
    if cams:
```
Replace with:
```python
    cams = config.get(cfg, "cameras", [])
    litter_cams = litterbox_cameras(cams, config.get(cfg, "bowls", []))
    if litter_cams:
```
and change the `CaptureService(bus, cams, ...)` call to `CaptureService(bus, litter_cams, ...)`, and the two `len(cams)`/`cams` references in that block (CaptureHealth + the print) to `litter_cams`. (CaptureHealth(conn, cams, ...) → CaptureHealth(conn, litter_cams, ...) so the missed-capture guard also only watches litterbox cams.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_meowantd_capture_cams.py -v`
Expected: PASS.

- [ ] **Step 5: Import-smoke + full suite + commit**

Run: `cd ~/repos/meowant && python -c "import meowantd" && python -m pytest -q`
Expected: import OK; suite PASS.

```bash
git add meowantd.py tests/test_meowantd_capture_cams.py
git commit -m "fix(capture): litterbox capture excludes bowl cameras (meowcam5/6)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017phvBTbjCeqZo9QYaMRqoG"
```

---

### CHECKPOINT (controller, after Tasks 1+2 — not a subagent task)

Reload and validate before building 3-5; the two P1 fixes may restore attribution enough that 3-5 shrink.

```bash
launchctl kickstart -k gui/$(id -u)/com.meowant.daemon
```
Then over the next visits, confirm via DB that new visits get `pred`/`label` and a `cat`:
`sqlite3 meowant.db "SELECT v.id, COALESCE(c.name,'?') cat, (SELECT COUNT(*) FROM captures cap WHERE cap.visit_id=v.id AND cap.pred IS NOT NULL) preds FROM visits v LEFT JOIN cats c ON c.id=v.cat_id ORDER BY v.id DESC LIMIT 8;"`
Expected: recent attributed visits gain non-null `cat` and `preds > 0`. If attribution recovers, Tasks 4/5 may downgrade to "nice-to-have."

---

### Task 3: Per-cat no-go alarm hedges on degraded attribution (`j29`)

**Root cause:** when attribution fails, eliminations are logged unattributed; `health_watch._check_no_go` only sees the system-wide-silence guard, which doesn't trip if *any* cat (e.g. Ella) was attributed recently — so Ucok/Garfield still false-alarm.

**Files:**
- Modify: `mw/store.py` — add `unattributed_eliminations_since`.
- Modify: `mw/health_watch.py` — `_check_no_go` suppresses per-cat alarms + emits one honest notice when attribution looks degraded.
- Test: `tests/test_store.py` (append), `tests/test_health_watch.py` (append).

**Interfaces:**
- Produces: `store.unattributed_eliminations_since(conn, after_iso) -> int` (count of eliminated visits with `cat_id IS NULL` at/after `after_iso`).
- Consumes: `store.sessions`, `store._lock`, `store._iso`.

- [ ] **Step 1: Write the failing store test**

```python
# tests/test_store.py  (append)
def test_unattributed_eliminations_since_counts_only_unattributed_elims():
    conn = store.connect(":memory:")
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Ella", "Garfield"])
    def visit(enter, elim, cat=None):
        vid = store.open_visit(conn, enter)
        store.close_visit(conn, vid, enter + 60, 60)
        if elim:
            store.mark_elimination(conn, vid, 90)
        if cat:
            store.set_visit_identity(conn, vid, store.cat_id_by_name(conn, cat), 1.0)
        return vid
    visit(1_000_000.0, True)                 # unattributed elim (counts)
    visit(1_000_100.0, True, cat="Ella")     # attributed (excluded)
    visit(1_000_200.0, False)                # no elim (excluded)
    visit(500_000.0, True)                    # before the window (excluded)
    after = store._iso(999_999.0)
    assert store.unattributed_eliminations_since(conn, after) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_store.py -k unattributed_eliminations -v`
Expected: FAIL — `AttributeError: ... 'unattributed_eliminations_since'`.

- [ ] **Step 3: Implement the store helper**

In `mw/store.py`, near `eliminations_today` / `last_elimination_ts`, add:

```python
def unattributed_eliminations_since(conn, after_iso):
    """Count eliminated visits with no cat attributed, at/after after_iso.
    A nonzero count means the box was used but the labeler couldn't say by whom —
    the per-cat no-go alarm must not confidently claim a cat 'hasn't gone'."""
    with _lock:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM visits "
            "WHERE eliminated=1 AND cat_id IS NULL AND enter_ts >= ?",
            (after_iso,)).fetchone()["n"]
```

- [ ] **Step 4: Write the failing health_watch test**

```python
# tests/test_health_watch.py  (append; mirror existing construction in that file)
def test_no_go_suppressed_when_unattributed_elims_present(tmp_path):
    from mw import store
    from mw.health_watch import HealthWatch
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Ella", "Garfield"])
    now = 2_000_000.0
    # Ella attributed recently (keeps system-wide guard from firing);
    # Ucok last attributed 10h ago (>8h -> would alarm); plus a recent UNATTRIBUTED elim.
    def visit(enter, elim, cat=None):
        vid = store.open_visit(conn, enter); store.close_visit(conn, vid, enter + 60, 60)
        if elim: store.mark_elimination(conn, vid, 90)
        if cat: store.set_visit_identity(conn, vid, store.cat_id_by_name(conn, cat), 1.0)
    visit(now - 1 * 3600, True, cat="Ella")     # recent attributed -> guard stays quiet
    visit(now - 10 * 3600, True, cat="Ucok")    # Ucok 10h ago (>8h)
    visit(now - 2 * 3600, True)                  # recent UNATTRIBUTED elim
    msgs = []
    hw = HealthWatch(conn, notify=msgs.append, now_fn=lambda: now)
    hw._check_no_go()
    # Must NOT fire a confident "Ucok ... No litter box use" alarm; instead one honest notice.
    assert not any("No litter box use" in m for m in msgs)
    assert any("attribution" in m.lower() for m in msgs)
```

- [ ] **Step 5: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_health_watch.py -k unattributed -v`
Expected: FAIL — current code fires the per-cat "No litter box use" alarm.

- [ ] **Step 6: Add the degraded-attribution hedge to `_check_no_go`**

In `mw/health_watch.py`, after the existing system-wide-silence guard
(`if (now - most_recent_any) / 3600.0 >= 8: return`) and before
`THRESHOLDS = {...}`, insert:

```python
        # Degraded-attribution guard: if the box was used but the labeler could
        # not attribute it, a per-cat "X hasn't gone" alarm is unreliable (it could
        # be X). Suppress per-cat no-go and raise ONE honest notice instead.
        from datetime import timezone  # (no-op import guard if already present)
        window_iso = datetime.fromtimestamp(now - 24 * 3600).isoformat()
        unattributed = store.unattributed_eliminations_since(self.conn, window_iso)
        if unattributed >= 2:
            if not self._alarmed.get("_attribution", False):
                self.notify(f"⚠️ Attribution degraded — {unattributed} box uses in 24h "
                            f"couldn't be matched to a cat; per-cat no-go alarms paused. "
                            f"Check the labeler.")
                self._alarmed["_attribution"] = True
            return
        else:
            self._alarmed["_attribution"] = False
```

(Reuse `self._alarmed` for the latch so the notice fires once per episode. The `from datetime import timezone` line is unnecessary if `datetime` is already imported at module top — it is; drop that line and just use the existing `datetime`.)

- [ ] **Step 7: Run tests + full suite + commit**

Run: `cd ~/repos/meowant && python -m pytest tests/test_store.py tests/test_health_watch.py -q && python -m pytest -q`
Expected: PASS.

```bash
git add mw/store.py mw/health_watch.py tests/test_store.py tests/test_health_watch.py
git commit -m "fix(health): pause per-cat no-go alarms when attribution is degraded

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017phvBTbjCeqZo9QYaMRqoG"
```

---

### Task 4: Diagnose + fix 0-frame captures on real visits (`86p`) — controller-led

Not a clean TDD task — it starts as live diagnosis (RTSP grab reliability / presence-timing), then a targeted fix. Do this AFTER the checkpoint; the agy backlog (Task 1) may have been starving the capture path's shared resources.

- [ ] **Step 1:** Reproduce — find recent 0-frame eliminated visits:
  `sqlite3 meowant.db "SELECT v.id, v.enter_ts, v.duration_s, v.eliminated, (SELECT COUNT(*) FROM captures c WHERE c.visit_id=v.id) f FROM visits v WHERE v.eliminated=1 ORDER BY v.id DESC LIMIT 20;"` — quantify how many real visits get 0 frames post-Task-1.
- [ ] **Step 2:** Inspect `mw/capture.py` `run`/`run_once`/`_grab_round`/`_should_capture` for the presence→grab path; check whether `ffmpeg_grab` failures are swallowed and whether a fast visit can close before the first round. Add `print` instrumentation to stderr on grab failure with the camera name + error.
- [ ] **Step 3:** Probe grab latency/failure live against each litterbox cam (the cryze restream may stall): time a single `ffmpeg_grab` per cam, 5×, record failures.
- [ ] **Step 4:** Fix per the finding — likely candidates: (a) grab an immediate first frame on presence-True before the interval sleep (so a 4s visit still yields ≥1 frame); (b) shorten ffmpeg `-timeout` so a stalled cam fails fast instead of consuming the whole visit; (c) retry a failed grab once. Add a unit test for whichever logic changes (e.g. "first round fires immediately on presence").
- [ ] **Step 5:** Commit with a message naming the confirmed cause.

---

### Task 5: Gallery reference re-bootstrap (`y2h`) — controller-led, human-in-loop

The labeler matches against per-cat refs (`gallery/<cat>/`), currently ucok=5 / ella=2 / garfield=2 — too few for the new camera angles. Attribution can't be auto-bootstrapped while it's broken (chicken-and-egg), so this needs human labels.

- [ ] **Step 1:** After Tasks 1-3 land + checkpoint, re-measure attribution. If still weak, gather candidate frames: for each litterbox cam, pull recent cat-present frames (post-Task-1 the labeler's own confident calls, or `store.review_queue`).
- [ ] **Step 2:** Present candidate frames to the owner in batches (SendUserFile) grouped by guessed cat + angle; owner confirms/corrects (tap-to-label).
- [ ] **Step 3:** Write confirmed frames into `gallery/<cat>/` (aim ≥3 per cat per major angle: meowcam1, meowcam2, meowcam4); they feed `discover_refs` on the next autolabel cycle.
- [ ] **Step 4:** Reload; confirm "uncertain" rate drops on subsequent visits. Update `bd` y2h with before/after attribution rate.

---

## Self-Review

**Spec coverage:** mun→T1, 33i→T2, j29→T3, 86p→T4, y2h→T5. All five beads covered. Checkpoint between P1s and the rest prevents over-building (per the agreed sequencing).

**Placeholder scan:** T1-T3 carry full code + tests. T4/T5 are legitimately diagnosis/human-in-loop and are structured as concrete ordered steps rather than fabricated code (a plan must not invent code for an uncharacterized RTSP failure or for human-labeling) — flagged explicitly as controller-led, not subagent TDD.

**Type consistency:** `litterbox_cameras(cameras, bowls)` used identically in test + meowantd. `FallbackLabeler(primary, fallback, *, fail_threshold, cooldown_s, now_fn)` matches across tests + meowantd construction (meowantd uses positional primary/fallback only — defaults apply). `unattributed_eliminations_since(conn, after_iso)` signature identical in store + health_watch + test. `ERROR` sentinel imported from `mw.labeler` in tests.

## Execution Handoff

Tasks 1-3 are subagent-driven TDD (run via superpowers:subagent-driven-development). Task 4 (diagnosis) and Task 5 (human-in-loop) are controller-led after the mid-plan checkpoint.
