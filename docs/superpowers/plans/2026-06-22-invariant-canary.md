# Invariant Canary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect when the vision/labeler pipeline silently drops or fails to attribute real elimination events, by cross-checking raw dp102-derived eliminations against attributed (cat_id-bearing) ones over a rolling window (self-healing Component 5).

**Architecture:** A store function computes `(raw, attributed)` counts of eliminated visits in a `[after, before]` window (where `before = now - grace` excludes visits the labeler hasn't had a chance to process yet). A new `mw/invariant_canary.py` holds an `InvariantCanary` watchdog that evaluates the attribution ratio against a floor — firing only with a minimum sample so a small denominator can't trip it — and latches/re-arms like the existing `HealthWatch`. It runs as its own thread in meowantd, gated to camera installs (no cameras ⇒ no labeler ⇒ nothing to canary).

**Tech Stack:** Python 3, stdlib only (`sqlite3`, `time`), pytest with `tmp_path` SQLite DBs.

## Global Constraints

- This is **Component 5** of the approved self-healing design (`docs/superpowers/specs/2026-06-22-self-healing-design.md`): "A periodic check that raw elimination events ≈ attributed/labeled events over a rolling window. Divergence (the labeler silently dropping or mis-attributing) → fire. A property tests can't fake because it runs against live data."
- **Fail toward LOUD / unknown ≠ silence**: a confirmed low attribution ratio fires; insufficient data is silent (cannot judge), never a false "all clear".
- The canary detects a **rate** drop, not individual misses — some eliminations are legitimately unattributable (frameless IR-flicker visits, ambiguous frames). Guards: a `min_sample` floor and a `grace` window (skip visits too recent for the labeler's ~15-min sweep).
- `store.py` access goes through the module-global `with _lock:`; timestamps via `store._iso(epoch)`. Lexicographic ISO comparison is the codebase convention (fixed-width naive-local ISO).
- Match existing watchdog style: `mw/health_watch.py` (latch + re-arm, `run_once`/`run` loop, `now_fn` injection) and its test `tests/test_health_watch.py`.
- Notify transports return a bool; latch ONLY on confirmed delivery (`if ok is not False:`) — same fail-loud pattern as `mw/deadman.py._fire` (a dead Telegram token must not latch the alarm into permanent silence). `None`-returning test stubs count as delivered.
- Do NOT commit secrets; `config.json` is gitignored. Daemon restarts during manual testing use `launchctl kickstart -k gui/$(id -u)/com.meowant.daemon`, never `stop`/`start`.

---

### Task 1: `elimination_attribution_stats` store function

**Files:**
- Modify: `mw/store.py` (add near `eliminations_today`, ~line 247)
- Test: `tests/test_store.py` (add to the existing file; mirror its `tmp_path` style)

**Interfaces:**
- Consumes: existing `store._lock`, `store._iso`, `store.open_visit`, `store.mark_elimination`, `store.set_visit_identity`.
- Produces: `store.elimination_attribution_stats(conn, after_iso, before_iso) -> tuple[int, int]` — `(raw, attributed)` for eliminated visits with `after_iso <= enter_ts < before_iso`. `raw` = all `eliminated=1`; `attributed` = those with `cat_id IS NOT NULL`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_store.py` (it already imports `store` and builds conns via `tmp_path`; if it uses a helper, reuse it — this snippet is self-contained):

```python
def test_elimination_attribution_stats_counts_raw_and_attributed(tmp_path):
    from mw import store
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    cid = store.cat_id_by_name(conn, "Ucok")
    T = 1_000_000.0

    def _elim(enter, attributed):
        vid = store.open_visit(conn, enter)            # enter_ts = _iso(enter)
        store.mark_elimination(conn, vid, 50)
        if attributed:
            store.set_visit_identity(conn, vid, cid, 0.9)
        return vid

    _elim(T - 100, True)      # in window, attributed
    _elim(T - 90, True)       # in window, attributed
    _elim(T - 80, False)      # in window, NOT attributed
    # a non-eliminated visit with a cat_id must NOT count as a raw elimination
    nv = store.open_visit(conn, T - 70)
    store.set_visit_identity(conn, nv, cid, 0.9)

    after = store._iso(T - 1000)
    before = store._iso(T)
    raw, attributed = store.elimination_attribution_stats(conn, after, before)
    assert raw == 3
    assert attributed == 2


def test_elimination_attribution_stats_respects_window_bounds(tmp_path):
    from mw import store
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    T = 1_000_000.0
    for off in (-5000, -100, -10):    # one too old, one in-window, one too recent
        vid = store.open_visit(conn, T + off)
        store.mark_elimination(conn, vid, 50)
    after = store._iso(T - 1000)      # excludes the -5000 one
    before = store._iso(T - 50)       # excludes the -10 one
    raw, attributed = store.elimination_attribution_stats(conn, after, before)
    assert raw == 1
    assert attributed == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_store.py::test_elimination_attribution_stats_counts_raw_and_attributed -v`
Expected: FAIL — `AttributeError: module 'mw.store' has no attribute 'elimination_attribution_stats'`.

- [ ] **Step 3: Implement the store function**

In `mw/store.py`, after `eliminations_today` (~line 247-ish; place it among the read helpers), add:

```python
def elimination_attribution_stats(conn, after_iso, before_iso):
    """For eliminated visits with after_iso <= enter_ts < before_iso, return
    (raw, attributed): raw = all eliminated=1; attributed = those carrying a
    cat_id. `before_iso` should be earlier than now by the labeler's grace window
    so visits too recent to have been labeled are not counted as 'dropped'."""
    with _lock:
        row = conn.execute(
            "SELECT COUNT(*) AS raw, "
            "  SUM(CASE WHEN cat_id IS NOT NULL THEN 1 ELSE 0 END) AS attributed "
            "FROM visits WHERE eliminated=1 AND enter_ts>=? AND enter_ts<?",
            (after_iso, before_iso)).fetchone()
        return row["raw"], (row["attributed"] or 0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_store.py -k attribution_stats -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/meowant
git add mw/store.py tests/test_store.py
git commit -m "feat(canary): elimination_attribution_stats — raw vs attributed counts (self-heal C5)"
```

---

### Task 2: `InvariantCanary` watchdog

**Files:**
- Create: `mw/invariant_canary.py`
- Test: `tests/test_invariant_canary.py` (create)

**Interfaces:**
- Consumes: `store.elimination_attribution_stats`, `store._iso` (Task 1).
- Produces:
  - `invariant_canary.InvariantCanary(conn, notify, now_fn=time.time, window_hours=48, grace_hours=2, min_sample=4, min_ratio=0.5, interval=3600, realarm=True)`.
  - `InvariantCanary.evaluate() -> tuple[str, str|None]` — returns `("bad", msg)` when ratio < min_ratio with enough sample; `("ok", None)` when ratio >= min_ratio with enough sample; `("insufficient", None)` when raw < min_sample.
  - `InvariantCanary.run_once() -> None` — fires once per bad episode (latched; latch clears on an "ok" evaluation so a recovery re-arms), latching only on confirmed delivery.
  - `InvariantCanary.run()` — `while True: run_once(); sleep(interval)`, never dies on exception.

- [ ] **Step 1: Write the failing test**

Create `tests/test_invariant_canary.py`:

```python
"""Invariant canary: raw eliminations vs attributed (labeled) ones; fire on a
sustained attribution-rate drop (the labeler silently eating health events)."""
from mw import store
from mw.invariant_canary import InvariantCanary

T = 1_000_000.0


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    return conn


def _elim(conn, enter, attributed):
    cid = store.cat_id_by_name(conn, "Ucok")
    vid = store.open_visit(conn, enter)
    store.mark_elimination(conn, vid, 50)
    if attributed:
        store.set_visit_identity(conn, vid, cid, 0.9)


def test_healthy_attribution_is_silent(tmp_path):
    conn = _db(tmp_path)
    for i in range(6):
        _elim(conn, T - 10000 - i, attributed=True)     # all labeled, past grace
    msgs = []
    c = InvariantCanary(conn, notify=msgs.append, now_fn=lambda: T)
    c.run_once()
    assert msgs == []


def test_low_attribution_fires_once_then_latches(tmp_path):
    conn = _db(tmp_path)
    for i in range(5):
        _elim(conn, T - 10000 - i, attributed=False)    # 5 raw, 0 attributed
    msgs = []
    c = InvariantCanary(conn, notify=msgs.append, now_fn=lambda: T,
                        min_sample=4, min_ratio=0.5)
    c.run_once()
    c.run_once()                                          # still bad -> no repeat
    assert len(msgs) == 1
    assert "canary" in msgs[0].lower() and "0/5" in msgs[0]


def test_insufficient_sample_is_silent(tmp_path):
    conn = _db(tmp_path)
    for i in range(2):
        _elim(conn, T - 10000 - i, attributed=False)    # only 2 < min_sample(4)
    msgs = []
    c = InvariantCanary(conn, notify=msgs.append, now_fn=lambda: T, min_sample=4)
    c.run_once()
    assert msgs == []                                     # can't judge -> no false alarm


def test_recent_visits_inside_grace_are_not_counted(tmp_path):
    conn = _db(tmp_path)
    # 5 unattributed but all within the 2h grace window -> labeler hasn't run yet
    for i in range(5):
        _elim(conn, T - 60 * i, attributed=False)
    msgs = []
    c = InvariantCanary(conn, notify=msgs.append, now_fn=lambda: T,
                        grace_hours=2, min_sample=4)
    c.run_once()
    assert msgs == []                                     # too recent to blame the labeler


def test_recovery_rearms_the_alarm(tmp_path):
    conn = _db(tmp_path)
    for i in range(5):
        _elim(conn, T - 10000 - i, attributed=False)    # bad: 0/5 attributed
    msgs = []
    c = InvariantCanary(conn, notify=msgs.append, now_fn=lambda: T,
                        min_sample=4, min_ratio=0.5)
    c.run_once()                                          # fires (1)
    assert len(msgs) == 1 and c._alarmed is True
    for i in range(10):
        _elim(conn, T - 9000 - i, attributed=True)      # flood attributed -> ratio ok
    c.run_once()                                          # recovery -> re-arm, silent
    assert len(msgs) == 1 and c._alarmed is False        # latch cleared = re-armed


def test_failed_delivery_does_not_latch(tmp_path):
    conn = _db(tmp_path)
    for i in range(5):
        _elim(conn, T - 10000 - i, attributed=False)
    sent = []

    def _notify(m):
        sent.append(m)
        return False                                      # transport failed

    c = InvariantCanary(conn, notify=_notify, now_fn=lambda: T,
                        min_sample=4, min_ratio=0.5)
    c.run_once()
    c.run_once()
    assert len(sent) == 2                                 # retried; never latched silent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_invariant_canary.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mw.invariant_canary'`.

- [ ] **Step 3: Implement the watchdog**

Create `mw/invariant_canary.py`:

```python
"""Invariant canary: cross-check raw eliminations (dp102, upstream truth) against
attributed (cat_id-bearing, downstream of the vision/labeler pipeline) ones over a
rolling window. A sustained low attribution ratio means the labeler is silently
dropping or failing to name real elimination events -- a 'fixed-away' bypass that
unit tests cannot catch, because it only shows against live data.

This is a coarse RATE detector, not a per-visit auditor: some eliminations are
legitimately unattributable (frameless IR-flicker visits, ambiguous frames), so it
fires only when the attributed FRACTION drops below a floor over a minimum sample,
and ignores visits still inside the labeler's grace window (too recent to blame).
Fails toward loud on a real drop; stays silent when the sample is too small to judge.
"""
import sys
import time

from mw import store


class InvariantCanary:
    def __init__(self, conn, notify, now_fn=time.time, window_hours=48,
                 grace_hours=2, min_sample=4, min_ratio=0.5, interval=3600,
                 realarm=True):
        self.conn = conn
        self.notify = notify
        self.now = now_fn
        self.window_hours = window_hours
        self.grace_hours = grace_hours
        self.min_sample = min_sample
        self.min_ratio = min_ratio
        self.interval = interval
        self.realarm = realarm
        self._alarmed = False

    def evaluate(self):
        """Returns (status, msg): ('bad', text) | ('ok', None) | ('insufficient', None)."""
        now = self.now()
        after = store._iso(now - self.window_hours * 3600)
        before = store._iso(now - self.grace_hours * 3600)   # skip too-recent visits
        raw, attributed = store.elimination_attribution_stats(self.conn, after, before)
        if raw < self.min_sample:
            return ("insufficient", None)
        ratio = attributed / raw
        if ratio < self.min_ratio:
            return ("bad",
                    f"🔬 Attribution canary: only {attributed}/{raw} recent "
                    f"eliminations got a cat ID ({ratio:.0%}) over the last "
                    f"{self.window_hours}h — the labeler may be silently dropping "
                    f"health events")
        return ("ok", None)

    def run_once(self):
        status, msg = self.evaluate()
        if status == "bad" and not self._alarmed:
            # Latch ONLY on confirmed delivery: a dead Telegram token returning
            # False must not mark this 'sent' and re-suppress it. None (a stub with
            # no signal) is treated as delivered so plain notify callables work.
            if self.notify(msg) is not False:
                self._alarmed = True
        elif status == "ok" and self.realarm:
            self._alarmed = False                # recovered -> re-arm for next drop
        # 'insufficient': leave the latch as-is (cannot judge either way)

    def run(self):
        while True:
            try:
                self.run_once()
            except Exception as e:               # never let the canary thread die
                print(f"[invariant-canary] error: {e}", file=sys.stderr)
            time.sleep(self.interval)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_invariant_canary.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/meowant
git add mw/invariant_canary.py tests/test_invariant_canary.py
git commit -m "feat(canary): InvariantCanary — fire on attribution-rate drop (self-heal C5)"
```

---

### Task 3: wire `InvariantCanary` into meowantd

**Files:**
- Modify: `meowantd.py` (start a canary thread inside the `if cams:` block)
- Test: `tests/test_meowantd_wiring.py` (add a wiring-presence check)

**Interfaces:**
- Consumes: `invariant_canary.InvariantCanary` (Task 2); existing `config.get`, `make_notify`.
- Produces: a daemon thread running the canary when cameras are configured and `canary.enabled` is true.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_meowantd_wiring.py` (match its `inspect.getsource` style):

```python
def test_invariant_canary_is_wired():
    import inspect
    import meowantd
    src = inspect.getsource(meowantd)
    assert "InvariantCanary(" in src           # canary constructed
    assert "canary.enabled" in src             # config-gated
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_meowantd_wiring.py::test_invariant_canary_is_wired -v`
Expected: FAIL — assertion error (`InvariantCanary(` not in source).

- [ ] **Step 3: Wire it in meowantd**

In `meowantd.py`, inside the `if cams:` block (the canary needs the vision pipeline; without cameras there is no labeler to canary). Add this after the `elim-notifier` thread start (after the `print("elim-notifier: ...")` line, ~line 110), before the scatter-detector section:

```python
        # Invariant canary (self-heal C5): cross-check raw eliminations vs
        # attributed ones; fire if the labeler is silently dropping health events.
        if config.get(cfg, "canary.enabled", True):
            from mw.invariant_canary import InvariantCanary
            canary = InvariantCanary(
                conn, make_notify(lambda k: config.get(cfg, k)),
                window_hours=config.get(cfg, "canary.window_hours", 48),
                grace_hours=config.get(cfg, "canary.grace_hours", 2),
                min_sample=config.get(cfg, "canary.min_sample", 4),
                min_ratio=config.get(cfg, "canary.min_ratio", 0.5),
                interval=config.get(cfg, "canary.interval_s", 3600))
            threading.Thread(target=canary.run, daemon=True).start()
            print("invariant-canary: raw-vs-attributed elimination check")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_meowantd_wiring.py -v`
Expected: PASS (the new test plus the existing wiring tests).

- [ ] **Step 5: Run the full suite**

Run: `cd ~/repos/meowant && python -m pytest -q`
Expected: PASS — all prior tests plus the new canary tests, green.

- [ ] **Step 6: Commit**

```bash
cd ~/repos/meowant
git add meowantd.py tests/test_meowantd_wiring.py
git commit -m "feat(canary): wire InvariantCanary into meowantd (camera-gated, self-heal C5)"
```

---

## Self-Review

**1. Spec coverage** (against `2026-06-22-self-healing-design.md`, Component 5):
- "raw elimination events ≈ attributed/labeled events over a rolling window" → `elimination_attribution_stats` (Task 1) + the ratio check in `evaluate` (Task 2) ✓
- "Divergence → fire" → `("bad", msg)` path fires + latches (Task 2) ✓
- "a property tests can't fake because it runs against live data" → runs against the live `visits` table in meowantd (Task 3) ✓
- Fail-loud / unknown ≠ silence → "insufficient" sample is silent (cannot judge, never a false all-clear); confirmed-delivery latch prevents fail-mute ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/placeholder lines. `test_recovery_rearms_the_alarm` asserts the latch clears (`c._alarmed is False`) after a recovery evaluation — a white-box check of the re-arm, acceptable for a watchdog.

**3. Type consistency:** `elimination_attribution_stats(conn, after_iso, before_iso) -> (raw, attributed)` is consumed identically in `InvariantCanary.evaluate`; `evaluate() -> (status, msg)` statuses (`"bad"|"ok"|"insufficient"`) are handled exactly in `run_once`; the `realarm` flag and `_alarmed` latch are consistent across the class. ✓

**Executor note:** confirm `store.seed_cats` and `store.set_visit_identity` exist with the signatures used (they do, per store.py); if `tests/test_store.py` has a shared conn fixture, prefer it over the inline `store.connect` shown here.
