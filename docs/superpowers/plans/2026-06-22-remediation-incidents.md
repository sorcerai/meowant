# Incidents Table + Deterministic Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the meowant watchdogs a durable incident memory (Component 4) and turn their notify-only responses into a deterministic detect → diagnose/debounce → log → escalate loop (Component 2), without any process-restart auto-fix.

**Architecture:** A new `incidents` SQLite table (in `meowant.db`, same `store.py` lock idiom) records every watchdog episode. A new `mw/remediation.py` holds a small `Remediator` (rate-limit → run playbook → log incident → escalate) plus two pure playbooks: `labeler_stall_playbook` (diagnose `agy`-on-PATH, escalate with root cause — never restarts) and `stream_down_playbook` (wait + re-probe a flaky on-demand stream, escalate only if still down). `CaptureHealth` is extended to route its existing detections through an optional injected `Remediator`, falling back to the current notify-only behavior when none is wired. A `/incidents` Telegram command and `report.incidents_report` surface the history for travel-time visibility.

**Tech Stack:** Python 3, stdlib only (`sqlite3`, `shutil.which`, `subprocess` via existing probe, `json`), pytest with `tmp_path` SQLite DBs.

## Global Constraints

- **DEVIATION FROM SPEC — CONFIRM AT REVIEW GATE:** The approved spec (`docs/superpowers/specs/2026-06-22-self-healing-design.md`, Component 2) gives `labeler stall → launchctl kickstart -k` as the remediation. This plan deliberately does NOT restart meowantd. Reason: the labeler is a thread *inside* meowantd, so a restart is process self-suicide that cannot synchronously verify the fix; restart churn *caused* the 2026-06-22 stall; and process death (launchd KeepAlive) + wedged-but-alive (dead-man's liveness probe) are already covered elsewhere. The labeler playbook instead diagnoses (`agy` on PATH?) and escalates with the root cause. Auto-restart, if ever wanted, belongs only in the dead-man's switch's *separate* process — a future component.
- Deterministic fix belongs in meowantd only if it is **in-process AND synchronously verifiable**. No playbook may restart, mutate config, or edit health-signal logic (thresholds, classification, alert code) — those always escalate.
- Every remediation path **fails LOUD**: unknown/unresolved ⇒ escalate. Never swallow.
- `store.py` access goes through the module-global `with _lock:` (see existing functions). `incidents` is a NEW table → add to `SCHEMA`, not `_MIGRATIONS`.
- Timestamps are naive local-ISO via `store._iso(epoch)` (see `store._iso`). Tests inject a fixed `now`.
- Do NOT commit secrets. `config.json` is gitignored; never add it to a commit.
- Any daemon restart during manual testing uses `launchctl kickstart -k gui/$(id -u)/com.meowant.daemon`, never `stop`/`start`.
- Lean conventions: small focused modules, docstrings explaining *why*, match the existing watchdog/test style (`tests/test_capture_health.py`).

---

### Task 1: `incidents` table + store functions

**Files:**
- Modify: `mw/store.py` (add table to `SCHEMA` ~line 22-27; add functions near `last_elimination_ts` ~line 108)
- Test: `tests/test_incidents_store.py` (create)

**Interfaces:**
- Consumes: existing `store._lock`, `store._iso`, `store.connect`, `store.init_db`.
- Produces:
  - `store.log_incident(conn, kind, signal, action_taken, outcome, notes="", ts=None) -> None` — `signal` is any JSON-serializable dict; `ts` is epoch float (None ⇒ now).
  - `store.recent_incidents(conn, limit=20) -> list[dict]` — newest first; `signal` returned as the parsed dict.
  - `store.incidents_since(conn, kind, after_iso, outcomes=None) -> int` — count of rows of `kind` with `ts >= after_iso`, optionally filtered to `outcomes` (tuple of strings).
  - `store.incident_rollup(conn) -> list[dict]` — `[{"kind","outcome","n"}]` grouped, busiest first.

- [ ] **Step 1: Write the failing test**

Create `tests/test_incidents_store.py`:

```python
"""incidents table: append-only audit/runbook for watchdog episodes."""
from mw import store

T = 1_000_000.0


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


def test_log_and_recent_roundtrip_parses_signal(tmp_path):
    conn = _db(tmp_path)
    store.log_incident(conn, "stream_down", {"camera": "meowcam3"},
                       "re-probed after 5s: still DOWN", "escalated", ts=T)
    rows = store.recent_incidents(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "stream_down"
    assert r["signal"] == {"camera": "meowcam3"}      # parsed back to a dict
    assert r["outcome"] == "escalated"
    assert "still DOWN" in r["action_taken"]


def test_recent_is_newest_first_and_limited(tmp_path):
    conn = _db(tmp_path)
    for i in range(5):
        store.log_incident(conn, "labeler_stall", {"n": i}, "diag", "escalated",
                           ts=T + i)
    rows = store.recent_incidents(conn, limit=3)
    assert [r["signal"]["n"] for r in rows] == [4, 3, 2]


def test_incidents_since_counts_by_kind_and_outcome(tmp_path):
    conn = _db(tmp_path)
    store.log_incident(conn, "stream_down", {}, "a", "escalated", ts=T)
    store.log_incident(conn, "stream_down", {}, "b", "recovered", ts=T + 10)
    store.log_incident(conn, "stream_down", {}, "c", "escalated", ts=T + 20)
    store.log_incident(conn, "labeler_stall", {}, "d", "escalated", ts=T + 30)
    after = store._iso(T - 1)
    assert store.incidents_since(conn, "stream_down", after) == 3
    assert store.incidents_since(conn, "stream_down", after,
                                 outcomes=("escalated",)) == 2
    # window excludes older rows
    assert store.incidents_since(conn, "stream_down", store._iso(T + 15)) == 1


def test_rollup_groups_kind_outcome(tmp_path):
    conn = _db(tmp_path)
    store.log_incident(conn, "stream_down", {}, "a", "escalated", ts=T)
    store.log_incident(conn, "stream_down", {}, "b", "escalated", ts=T + 1)
    store.log_incident(conn, "labeler_stall", {}, "c", "recovered", ts=T + 2)
    roll = {(r["kind"], r["outcome"]): r["n"] for r in store.incident_rollup(conn)}
    assert roll[("stream_down", "escalated")] == 2
    assert roll[("labeler_stall", "recovered")] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_incidents_store.py -v`
Expected: FAIL — `AttributeError: module 'mw.store' has no attribute 'log_incident'`.

- [ ] **Step 3: Add the table to SCHEMA**

In `mw/store.py`, inside the `SCHEMA` string (after the `captures(...)` table, before the closing `"""` at ~line 27), add:

```sql
CREATE TABLE IF NOT EXISTS incidents(
  id INTEGER PRIMARY KEY, ts TEXT, kind TEXT,
  signal TEXT,            -- JSON: detection details
  action_taken TEXT,      -- what the playbook attempted
  outcome TEXT,           -- 'recovered' | 'escalated' | 'suppressed' | 'failed'
  notes TEXT);
```

- [ ] **Step 4: Add the store functions**

In `mw/store.py`, after `last_elimination_ts` (~line 109), add:

```python
def log_incident(conn, kind, signal, action_taken, outcome, notes="", ts=None):
    """Append one watchdog episode to the incidents audit log. `signal` is any
    JSON-serializable dict; `ts` is an epoch float (None -> wall-clock now)."""
    stamp = _iso(ts) if ts is not None else datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn.execute(
            "INSERT INTO incidents(ts, kind, signal, action_taken, outcome, notes) "
            "VALUES(?,?,?,?,?,?)",
            (stamp, kind, json.dumps(signal), action_taken, outcome, notes))
        conn.commit()


def recent_incidents(conn, limit=20):
    """Newest-first incidents, with `signal` parsed back to a dict."""
    with _lock:
        rows = conn.execute(
            "SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["signal"] = json.loads(d["signal"]) if d["signal"] else {}
        out.append(d)
    return out


def incidents_since(conn, kind, after_iso, outcomes=None):
    """Count incidents of `kind` at/after `after_iso`, optionally restricted to
    a tuple of `outcomes` — the rate-limit primitive for the Remediator."""
    q = "SELECT COUNT(*) AS n FROM incidents WHERE kind=? AND ts>=?"
    params = [kind, after_iso]
    if outcomes:
        q += " AND outcome IN (%s)" % ",".join("?" * len(outcomes))
        params.extend(outcomes)
    with _lock:
        return conn.execute(q, params).fetchone()["n"]


def incident_rollup(conn):
    """(kind, outcome) counts, busiest first — the 'how are things going' view."""
    with _lock:
        rows = conn.execute(
            "SELECT kind, outcome, COUNT(*) AS n FROM incidents "
            "GROUP BY kind, outcome ORDER BY n DESC").fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ~/repos/meowant && python -m pytest tests/test_incidents_store.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
cd ~/repos/meowant
git add mw/store.py tests/test_incidents_store.py
git commit -m "feat(incidents): add incidents table + store log/query/rollup (self-heal C4)"
```

---

### Task 2: `Remediator` core (rate-limit → playbook → log → escalate)

**Files:**
- Create: `mw/remediation.py`
- Test: `tests/test_remediation.py` (create)

**Interfaces:**
- Consumes: `store.log_incident`, `store.incidents_since`, `store._iso` (from Task 1).
- Produces:
  - `remediation.Remediator(conn, notify, now_fn=time.time, max_per_window=3, window_s=3600)`.
  - `Remediator.handle(kind, signal, playbook) -> str` — `playbook` is a zero-arg callable returning `{"action": str, "resolved": bool, "escalate": str}`. Returns the outcome: `"recovered"` | `"escalated"` | `"suppressed"`. Rate-limits on prior **escalated** incidents of `kind` in the window; always logs; calls `notify(escalate)` only when not resolved.

- [ ] **Step 1: Write the failing test**

Create `tests/test_remediation.py`:

```python
"""Remediator core: rate-limit -> run playbook -> log incident -> escalate."""
from mw import store
from mw.remediation import Remediator

T = 1_000_000.0


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


def _escalating_playbook():
    return {"action": "diagnosed: broken", "resolved": False,
            "escalate": "🚨 thing is broken"}


def _recovering_playbook():
    return {"action": "re-probed: UP", "resolved": True, "escalate": ""}


def test_unresolved_incident_logs_and_escalates(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    r = Remediator(conn, notify=msgs.append, now_fn=lambda: T)
    outcome = r.handle("stream_down", {"camera": "c"}, _escalating_playbook)
    assert outcome == "escalated"
    assert msgs == ["🚨 thing is broken"]
    rows = store.recent_incidents(conn)
    assert rows[0]["outcome"] == "escalated"
    assert rows[0]["action_taken"] == "diagnosed: broken"


def test_resolved_incident_logs_but_does_not_escalate(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    r = Remediator(conn, notify=msgs.append, now_fn=lambda: T)
    outcome = r.handle("stream_down", {"camera": "c"}, _recovering_playbook)
    assert outcome == "recovered"
    assert msgs == []                                   # good news = no alert
    assert store.recent_incidents(conn)[0]["outcome"] == "recovered"


def test_rate_limit_suppresses_after_threshold(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    r = Remediator(conn, notify=msgs.append, now_fn=lambda: T,
                   max_per_window=2, window_s=3600)
    r.handle("stream_down", {}, _escalating_playbook)   # 1 -> escalate
    r.handle("stream_down", {}, _escalating_playbook)   # 2 -> escalate
    outcome = r.handle("stream_down", {}, _escalating_playbook)  # 3 -> suppressed
    assert outcome == "suppressed"
    assert len(msgs) == 2                               # third did not alert
    assert store.recent_incidents(conn)[0]["outcome"] == "suppressed"


def test_rate_limit_counts_only_escalations_not_recoveries(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    r = Remediator(conn, notify=msgs.append, now_fn=lambda: T,
                   max_per_window=1, window_s=3600)
    r.handle("stream_down", {}, _recovering_playbook)   # recovered, doesn't count
    r.handle("stream_down", {}, _recovering_playbook)   # recovered, doesn't count
    outcome = r.handle("stream_down", {}, _escalating_playbook)  # first escalation
    assert outcome == "escalated"
    assert msgs == ["🚨 thing is broken"]


def test_rate_limit_window_expires(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    r = Remediator(conn, notify=msgs.append, now_fn=lambda: T,
                   max_per_window=1, window_s=3600)
    r.handle("stream_down", {}, _escalating_playbook)   # escalate at T
    r.now = lambda: T + 4000                            # past the 3600s window
    outcome = r.handle("stream_down", {}, _escalating_playbook)
    assert outcome == "escalated"                       # window cleared -> alerts again
    assert len(msgs) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_remediation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mw.remediation'`.

- [ ] **Step 3: Write the Remediator (and the module docstring that locks in the design)**

Create `mw/remediation.py`:

```python
"""Deterministic, in-process remediation for KNOWN watchdog incidents.

Honest scope (per the self-healing council verdict + design review): true
in-process auto-fixes are rare. What this layer actually does is (a) record
every incident to the `incidents` table for audit/runbook/travel-time
visibility, (b) debounce before crying wolf (re-probe a flaky on-demand stream
before escalating), and (c) enrich escalations with a deterministic diagnosis
(e.g. 'agy fell off PATH') so the owner gets an actionable alert, not a bare
symptom.

NOT here: restarting meowantd. The labeler runs as a thread INSIDE meowantd, so
a restart is process self-suicide that can't verify it worked -- and restart
churn CAUSED the 2026-06-22 labeler stall. Process death is already covered by
launchd KeepAlive; wedged-but-alive by the dead-man's switch liveness probe. An
in-process daemon restart sits redundantly between two existing mechanisms and
adds risk, not coverage. If auto-restart is ever wanted, its only safe home is
the dead-man's switch's SEPARATE process.

A playbook is a zero-arg callable returning {action, resolved, escalate}. The
Remediator rate-limits per kind, logs every call, and escalates (notify) only
when the incident was not resolved.
"""
import shutil
import time

from mw import store


class Remediator:
    def __init__(self, conn, notify, now_fn=time.time,
                 max_per_window=3, window_s=3600):
        self.conn = conn
        self.notify = notify
        self.now = now_fn
        self.max_per_window = max_per_window
        self.window_s = window_s

    def handle(self, kind, signal, playbook):
        # Rate-limit on prior ESCALATIONS only: recoveries/suppressions never
        # bothered the owner, so they must not count toward the quiet threshold.
        after = store._iso(self.now() - self.window_s)
        if store.incidents_since(self.conn, kind, after,
                                 outcomes=("escalated",)) >= self.max_per_window:
            store.log_incident(self.conn, kind, signal,
                               "rate-limited (too many escalations recently)",
                               "suppressed", ts=self.now())
            return "suppressed"
        res = playbook()
        outcome = "recovered" if res["resolved"] else "escalated"
        store.log_incident(self.conn, kind, signal, res["action"], outcome,
                           ts=self.now())
        if not res["resolved"]:
            self.notify(res["escalate"])
        return outcome
```

(The two playbook functions arrive in Tasks 3 and 4; `shutil` is imported now because Task 3 adds `labeler_stall_playbook` to this same module.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/repos/meowant && python -m pytest tests/test_remediation.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/meowant
git add mw/remediation.py tests/test_remediation.py
git commit -m "feat(remediation): Remediator core — rate-limit, log, escalate (self-heal C2)"
```

---

### Task 3: labeler-stall playbook (diagnose `agy` on PATH — never restart) + wire into `CaptureHealth`

**Files:**
- Modify: `mw/remediation.py` (add `labeler_stall_playbook`)
- Modify: `mw/capture_health.py` (constructor `+remediator=None`; route `check_labeler`)
- Test: `tests/test_remediation.py` (add playbook tests), `tests/test_capture_health.py` (add routing test)

**Interfaces:**
- Consumes: `Remediator.handle` (Task 2).
- Produces:
  - `remediation.labeler_stall_playbook(stuck_count, which=shutil.which) -> dict` — `which` injectable for tests; always `resolved=False` (no in-process fix); `escalate` text differs by whether `agy` is on PATH.
  - `CaptureHealth(..., remediator=None)` — when set, `check_labeler` routes through `remediator.handle("labeler_stall", ...)`; when None, unchanged notify-only behavior.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_remediation.py`:

```python
from mw.remediation import labeler_stall_playbook


def test_labeler_playbook_agy_missing_says_restart_wont_help():
    res = labeler_stall_playbook(7, which=lambda name: None)
    assert res["resolved"] is False
    assert "not on the daemon PATH" in res["escalate"]
    assert "MISSING" in res["action"]


def test_labeler_playbook_agy_present_warns_against_restart():
    res = labeler_stall_playbook(3, which=lambda name: "/usr/local/bin/agy")
    assert res["resolved"] is False
    assert "7" not in res["escalate"]                  # uses the real count
    assert "3 frame" in res["escalate"]
    assert "restart" in res["escalate"].lower()        # explicitly NOT restarting
    assert "/usr/local/bin/agy" in res["action"]
```

Add to `tests/test_capture_health.py`:

```python
def test_labeler_stall_routes_through_remediator_when_present(tmp_path):
    from mw.remediation import Remediator
    conn = _db(tmp_path)
    vid = store.open_visit(conn, T - 5000)
    store.insert_capture(conn, T - 5000, vid, "c", "/g/x.jpg")   # old, untouched
    msgs = []
    rem = Remediator(conn, notify=msgs.append, now_fn=lambda: T)
    h = CaptureHealth(conn, [{"name": "c", "url": "u"}], notify=lambda m: None,
                      probe=lambda u: True, now_fn=lambda: T,
                      labeler_settle_seconds=1800, remediator=rem)
    h.check_labeler()
    assert len(msgs) == 1 and "stall" in msgs[0].lower()
    # the episode was recorded as an incident (not just an ephemeral notify)
    rows = store.recent_incidents(conn)
    assert rows and rows[0]["kind"] == "labeler_stall"
    assert rows[0]["outcome"] == "escalated"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/repos/meowant && python -m pytest tests/test_remediation.py::test_labeler_playbook_agy_missing_says_restart_wont_help tests/test_capture_health.py::test_labeler_stall_routes_through_remediator_when_present -v`
Expected: FAIL — `ImportError: cannot import name 'labeler_stall_playbook'` and `TypeError: __init__() got an unexpected keyword argument 'remediator'`.

- [ ] **Step 3: Add the playbook**

In `mw/remediation.py`, after the imports and before `class Remediator` (so module-level helpers sit together), add:

```python
def labeler_stall_playbook(stuck_count, which=shutil.which):
    """Diagnose a labeler stall and escalate with the root cause. NEVER restarts:
    the labeler is a thread inside meowantd, restart churn caused the 2026-06-22
    stall, and a restart can't verify itself. `which` is injectable for tests."""
    agy = which("agy")
    if agy is None:
        return {
            "action": "checked `agy` on PATH: MISSING",
            "resolved": False,
            "escalate": (f"🏷️ Auto-labeler stalled — {stuck_count} frame(s) "
                         f"unprocessed AND `agy` is not on the daemon PATH. "
                         f"Labeling is DOWN until the binary is restored "
                         f"(a daemon restart will NOT fix this)."),
        }
    return {
        "action": f"checked `agy` on PATH: present ({agy})",
        "resolved": False,
        "escalate": (f"🏷️ Auto-labeler stalled — {stuck_count} frame(s) unprocessed; "
                     f"`agy` IS on PATH so it's likely a transient wedge. Not "
                     f"auto-restarting (restart churn caused the 2026-06-22 stall) "
                     f"— investigate if it persists."),
    }
```

- [ ] **Step 4: Wire `CaptureHealth`**

In `mw/capture_health.py`, add the import at the top (after `from mw import store`):

```python
from mw import remediation
```

Change the constructor signature (line 37-39) to add `remediator=None`:

```python
    def __init__(self, conn, cameras, notify, probe=ffmpeg_probe,
                 now_fn=time.time, settle_seconds=120, max_age_seconds=3600,
                 labeler_settle_seconds=1800, remediator=None):
```

And store it (after `self._labeler_alerted = False`, line 50):

```python
        self.remediator = remediator     # None -> notify-only (legacy/camera-absent)
```

Replace the `check_labeler` alert block (the `if stuck > 0 and not self._labeler_alerted:` branch, lines 79-83) with:

```python
        if stuck > 0 and not self._labeler_alerted:
            mins = int(self.labeler_settle / 60)
            if self.remediator:
                self.remediator.handle(
                    "labeler_stall", {"stuck": stuck, "grace_min": mins},
                    lambda: remediation.labeler_stall_playbook(stuck))
            else:
                self.notify(f"🏷️ Auto-labeler stalled: {stuck} frame(s) unprocessed "
                            f">{mins}min — labeler may be down")
            self._labeler_alerted = True
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_remediation.py tests/test_capture_health.py -v`
Expected: PASS (all — the new playbook + routing tests, and every existing capture-health test still green because `remediator` defaults to None).

- [ ] **Step 6: Commit**

```bash
cd ~/repos/meowant
git add mw/remediation.py mw/capture_health.py tests/test_remediation.py tests/test_capture_health.py
git commit -m "feat(remediation): labeler-stall diagnose-and-escalate playbook (no restart)"
```

---

### Task 4: stream-down debounce playbook + wire into `CaptureHealth.check_streams`

**Files:**
- Modify: `mw/remediation.py` (add `stream_down_playbook`)
- Modify: `mw/capture_health.py` (route the down-transition in `check_streams`)
- Test: `tests/test_remediation.py` (playbook tests), `tests/test_capture_health.py` (routing test)

**Interfaces:**
- Consumes: `Remediator.handle` (Task 2), `CaptureHealth.remediator` (Task 3).
- Produces:
  - `remediation.stream_down_playbook(cam_name, reprobe, sleep=time.sleep, wait_s=5) -> dict` — `reprobe` is a zero-arg callable returning bool; `sleep`/`wait_s` injectable; `resolved=True` (silent) if the re-probe comes back up, else escalate.
  - `CaptureHealth.check_streams` routes the up→down transition through `remediator.handle("stream_down", ...)` when a remediator is set; recovery (down→up) stays a plain notify.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_remediation.py`:

```python
from mw.remediation import stream_down_playbook


def test_stream_playbook_recovers_silently_when_reprobe_up():
    res = stream_down_playbook("meowcam3", reprobe=lambda: True,
                               sleep=lambda s: None)
    assert res["resolved"] is True
    assert res["escalate"] == ""
    assert "UP" in res["action"]


def test_stream_playbook_escalates_when_still_down():
    res = stream_down_playbook("meowcam3", reprobe=lambda: False,
                               sleep=lambda s: None)
    assert res["resolved"] is False
    assert "meowcam3" in res["escalate"] and "DOWN" in res["escalate"]


def test_stream_playbook_waits_before_reprobing():
    waited = []
    stream_down_playbook("c", reprobe=lambda: True,
                         sleep=lambda s: waited.append(s), wait_s=7)
    assert waited == [7]                                # debounce delay honored
```

Add to `tests/test_capture_health.py`:

```python
def test_stream_down_debounces_transient_drop(tmp_path):
    from mw.remediation import Remediator
    conn = _db(tmp_path)
    # probe sequence: up (seed), down (transition fires playbook), then re-probe up
    states = iter([True, False, True])
    msgs = []
    rem = Remediator(conn, notify=msgs.append, now_fn=lambda: T)
    # patch the debounce sleep to no-op for the test
    import mw.remediation as R
    h = CaptureHealth(conn, [{"name": "c", "url": "u"}], notify=msgs.append,
                      probe=lambda u: next(states), now_fn=lambda: T, remediator=rem)
    orig_sleep = R.time.sleep
    R.time.sleep = lambda s: None
    try:
        h.check_streams()   # seed: up
        h.check_streams()   # up -> down: playbook waits then re-probes -> UP -> silent
    finally:
        R.time.sleep = orig_sleep
    assert msgs == []                                  # transient blip, no alarm
    assert store.recent_incidents(conn)[0]["outcome"] == "recovered"


def test_stream_down_escalates_when_persistent(tmp_path):
    from mw.remediation import Remediator
    conn = _db(tmp_path)
    states = iter([True, False, False])                # down and stays down
    msgs = []
    rem = Remediator(conn, notify=msgs.append, now_fn=lambda: T)
    import mw.remediation as R
    h = CaptureHealth(conn, [{"name": "c", "url": "u"}], notify=msgs.append,
                      probe=lambda u: next(states), now_fn=lambda: T, remediator=rem)
    orig_sleep = R.time.sleep
    R.time.sleep = lambda s: None
    try:
        h.check_streams()   # up
        h.check_streams()   # up -> down, re-probe still down -> escalate
    finally:
        R.time.sleep = orig_sleep
    assert len(msgs) == 1 and "DOWN" in msgs[0]
    assert store.recent_incidents(conn)[0]["outcome"] == "escalated"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/repos/meowant && python -m pytest tests/test_remediation.py::test_stream_playbook_escalates_when_still_down tests/test_capture_health.py::test_stream_down_debounces_transient_drop -v`
Expected: FAIL — `ImportError: cannot import name 'stream_down_playbook'`.

- [ ] **Step 3: Add the playbook**

In `mw/remediation.py`, after `labeler_stall_playbook`, add:

```python
def stream_down_playbook(cam_name, reprobe, sleep=time.sleep, wait_s=5):
    """Debounce a flaky on-demand stream: wait `wait_s`, re-probe, and escalate
    only if it is STILL down. cryze/MediaMTX sources are on-demand and blip
    routinely; a single missed probe should not page the owner. meowantd cannot
    repair an external stream, so a confirmed-down stream always escalates."""
    sleep(wait_s)
    if reprobe():
        return {"action": f"re-probed '{cam_name}' after {wait_s}s: UP (transient)",
                "resolved": True, "escalate": ""}
    return {"action": f"re-probed '{cam_name}' after {wait_s}s: still DOWN",
            "resolved": False,
            "escalate": (f"📷 Camera '{cam_name}' stream DOWN (confirmed after a "
                         f"{wait_s}s re-probe) — captures will be lost")}
```

- [ ] **Step 4: Wire `check_streams`**

In `mw/capture_health.py`, replace the down-transition line in `check_streams` (the `if prev is True and not ok:` branch, line 56-57) with:

```python
            if prev is True and not ok:
                if self.remediator:
                    self.remediator.handle(
                        "stream_down", {"camera": cam["name"]},
                        lambda c=cam: remediation.stream_down_playbook(
                            c["name"], reprobe=lambda: self.probe(c["url"])))
                else:
                    self.notify(f"📷 Camera '{cam['name']}' stream DOWN — captures will be lost")
```

(The `elif prev is False and ok:` recovery branch and `self._up[...] = ok` line are unchanged.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_remediation.py tests/test_capture_health.py -v`
Expected: PASS (all). Existing `test_stream_down_then_recovered_notifies_each_transition` still passes because it constructs `CaptureHealth` with no remediator (notify-only path).

- [ ] **Step 6: Commit**

```bash
cd ~/repos/meowant
git add mw/remediation.py mw/capture_health.py tests/test_remediation.py tests/test_capture_health.py
git commit -m "feat(remediation): stream-down debounce-before-escalate playbook"
```

---

### Task 5: incidents report + `/incidents` Telegram command + meowantd wiring

**Files:**
- Modify: `mw/report.py` (add `incidents_report`)
- Modify: `meowantd.py` (build `Remediator`, pass to `CaptureHealth`, add `/incidents` command + help text)
- Test: `tests/test_report.py` (incidents_report), `tests/test_meowantd_wiring.py` (wiring presence)

**Interfaces:**
- Consumes: `store.recent_incidents`, `store.incident_rollup` (Task 1); `remediation.Remediator` (Task 2); `CaptureHealth(remediator=...)` (Task 3).
- Produces: `report.incidents_report(conn, limit=10) -> str` — human-readable recent list + totals, or a friendly "no incidents" line.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_report.py` (use the file's existing DB-fixture style; if it builds a conn via `store.connect`/`init_db`, mirror that — the snippet below is self-contained):

```python
def test_incidents_report_empty(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    out = report.incidents_report(conn)
    assert "no incident" in out.lower()


def test_incidents_report_lists_recent_and_totals(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.log_incident(conn, "stream_down", {"camera": "meowcam3"},
                       "re-probed after 5s: still DOWN", "escalated", ts=1_000_000.0)
    store.log_incident(conn, "labeler_stall", {"stuck": 4},
                       "checked `agy` on PATH: MISSING", "escalated", ts=1_000_100.0)
    out = report.incidents_report(conn)
    assert "stream_down" in out and "labeler_stall" in out
    assert "still DOWN" in out
    assert "Totals" in out or "totals" in out
```

Add to `tests/test_meowantd_wiring.py` (match its existing import/inspection style; this checks the wiring is present without launching the server):

```python
def test_remediator_is_wired_into_capture_health():
    import inspect
    import meowantd
    src = inspect.getsource(meowantd)
    assert "Remediator(" in src                         # remediator constructed
    assert "remediator=remediator" in src               # passed to CaptureHealth
    assert "/incidents" in src                          # command registered
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/repos/meowant && python -m pytest tests/test_report.py::test_incidents_report_empty tests/test_meowantd_wiring.py::test_remediator_is_wired_into_capture_health -v`
Expected: FAIL — `AttributeError: module 'mw.report' has no attribute 'incidents_report'` and the wiring assertions.

- [ ] **Step 3: Add `incidents_report`**

In `mw/report.py`, add:

```python
def incidents_report(conn, limit=10):
    """Recent watchdog incidents + per-(kind,outcome) totals — the /incidents view."""
    rows = store.recent_incidents(conn, limit)
    if not rows:
        return "🩹 No incidents logged — watchdogs quiet."
    lines = ["🩹 Recent incidents:"]
    for r in rows:
        when = r["ts"][5:16].replace("T", " ")
        lines.append(f"  [{when}] {r['kind']} → {r['outcome']}: {r['action_taken']}")
    lines.append("\nTotals:")
    for r in store.incident_rollup(conn):
        lines.append(f"  {r['kind']}/{r['outcome']}: {r['n']}")
    return "\n".join(lines)
```

(`store` is already imported in `report.py`.)

- [ ] **Step 4: Wire meowantd**

In `meowantd.py`, in the `if cams:` block, build the remediator BEFORE constructing `CaptureHealth` (replace the `health = CaptureHealth(...)` construction at lines 77-79):

```python
        from mw.remediation import Remediator
        remediator = Remediator(
            conn, make_notify(lambda k: config.get(cfg, k)),
            max_per_window=config.get(cfg, "remediation.max_per_window", 3),
            window_s=config.get(cfg, "remediation.window_s", 3600))
        # Make capture failures loud AND remediated: probe streams, guard missed
        # captures, and route detections through the deterministic playbooks
        # (debounce streams, diagnose labeler stalls) -> incidents table + escalate.
        health = CaptureHealth(conn, cams,
                               notify=make_notify(lambda k: config.get(cfg, k)),
                               settle_seconds=config.get(cfg, "capture.settle_seconds", 120),
                               remediator=remediator)
```

In the Telegram command dict (lines 181-186), add the `/incidents` entry:

```python
        bot = TelegramBot(tg_token, tg_chat, {
            "/cats": lambda: report.cat_report(conn),
            "/status": lambda: report.status_report(conn, daemon.state),
            "/health": lambda: report.health_report(conn),
            "/incidents": lambda: report.incidents_report(conn),
            "/start": lambda: "🐈 Meowant SC10 bot. Commands: /cats /status /health /incidents",
        }, label_cb=_label_cb)
```

Update the print line for telegram-bot (line 188) to mention the new command:

```python
        print("telegram-bot: inbound commands (/cats /status /health /incidents), owner-allowlisted")
```

- [ ] **Step 5: Run the full suite**

Run: `cd ~/repos/meowant && python -m pytest -q`
Expected: PASS — all prior tests plus the new report + wiring tests (the suite was 201 before this plan; it should be 201 + the new tests added across Tasks 1-5).

- [ ] **Step 6: Commit**

```bash
cd ~/repos/meowant
git add mw/report.py meowantd.py tests/test_report.py tests/test_meowantd_wiring.py
git commit -m "feat(remediation): /incidents report + wire Remediator into meowantd (self-heal C2/C4)"
```

---

## Self-Review

**1. Spec coverage** (against `2026-06-22-self-healing-design.md`):
- Component 4 (incidents table `id, ts, kind, signal json, action_taken, outcome, notes`; audit + runbook + rollup) → Task 1 ✓
- Component 2 (extend watchdogs: detect → attempt → verify → log → escalate; rate-limited; never edits signal logic) → Tasks 2-4 ✓, with the **documented deviation**: no `kickstart -k` restart (labeler playbook diagnoses + escalates; stream playbook debounces + escalates). This is the one item to confirm at the review gate.
- "Separate channels / fails loud / unknown ≠ silence" → every unresolved path escalates; the rate-limit suppresses only *repeat* escalations and still records them ✓
- Component 3 (claude -p one-shot), Component 5 (invariant canary) → explicitly OUT of this plan (later components, per build order) ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to Task N" — every code step shows complete code. ✓

**3. Type consistency:** `log_incident(conn, kind, signal, action_taken, outcome, notes="", ts=None)` and `incidents_since(conn, kind, after_iso, outcomes=None)` are used identically in Tasks 2-5; playbooks uniformly return `{"action","resolved","escalate"}`; `Remediator.handle(kind, signal, playbook)` signature is stable across Tasks 3-4 wiring and Task 5 construction. ✓

**Note for executor:** Test counts ("201 before") are approximate — assert the suite is green and grows by the new tests, not an exact number.
