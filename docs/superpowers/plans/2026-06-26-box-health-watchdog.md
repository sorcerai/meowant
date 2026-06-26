# Box-Health Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stop the litter box from silently becoming unusable — predict approaching-full, re-nag while full, and escalate hard when auto-clean has been blocked long enough that cats can't go.

**Architecture:** A new `BoxHealthWatch` poll-loop watcher that mirrors `mw/health_watch.py`'s persistent-latching pattern, but watches the *box hardware* (bin-full / auto-clean-blocked) instead of cat absence. It reads box-state events (`bin_full` / `bin_clear` / `clean_done`, already persisted by the daemon into the `events` table) via new `store` helpers, so its state survives daemon restarts. Wired as a daemon thread alongside `HealthWatch`. The existing instant edge-alert for `bin_full` in `mw/alerts.py` is removed so `BoxHealthWatch` is the single owner of bin-full messaging (no double-ping).

**Tech Stack:** Python 3, SQLite (`mw/store.py`), pytest (`tmp_path` DBs).

## Background (confirmed root cause, 2026-06-26)

Bin went full 06-25 23:11 → `bin_full` event fired → `Alerts` sent ONE Telegram ping → auto-clean blocked → box sat UNUSABLE 10.5h until the owner manually cleared it 06-26 09:53. Detection works; the gap is **no escalation/re-nag and no detection of the harm state**. `bin_full` recurs ~daily. Learned capacity from the event log: **9–19 auto-cleans per fill (min 9, avg ~14), high variance** — so prediction must be count-based (cleans since empty), not time-based, and must use the MIN observed capacity as the conservative threshold.

Owner decisions: **24/7 re-nag + hard "box UNUSABLE" escalation, no quiet hours.** Plus a predictive "approaching full" heads-up from learned capacity.

## Global Constraints

- `mw/store.py` conventions: every public fn takes `conn` first; wrap DB access in `with _lock:`; timestamps written via `_iso(epoch)` (naive-local ISO); time-window comparisons use `strftime('%s', col) <op> strftime('%s', ?)` (rows may be naive-local OR legacy `+00:00`); event ordering by autoincrement `id` (monotonic, tz-immune) is preferred over ts string comparison for "latest of kind".
- Event kinds already emitted by `mw/events.py`: `bin_full`, `bin_clear`, `clean_done`, `clean_start` (do NOT add new event kinds).
- `BoxHealthWatch` mirrors `HealthWatch`: `__init__(self, conn, notify, now_fn=time.time, interval=...)`, a `run_once()` + `run()` loop wrapping `_check()` in try/except that prints `[box-health] error: ...` to stderr and never dies, and dict/attr latches that re-arm on recovery.
- Tests use `tmp_path` SQLite DBs (`store.connect` + `store.init_db`), insert events via `store.insert_event(conn, Event(kind, ts))` (from `mw.events`), and a mutable list clock `clock=[T]; now_fn=lambda: clock[0]`. See `tests/test_health_watch.py` for the established style.
- Test command: `cd ~/repos/meowant && python -m pytest -q` (pytest.ini scopes to tests/). Suite is currently green at 314.
- 24/7 — NO quiet-hours suppression anywhere in `BoxHealthWatch`.
- config.json is gitignored — never commit it.

---

### Task 1: Store helpers for box-state queries (`mw/store.py`)

**Files:**
- Modify: `mw/store.py` (add four functions, near the other event/visit query helpers)
- Test: `tests/test_box_store.py` (new)

**Interfaces produced (later tasks rely on these exact names/signatures):**
- `bin_full_since(conn) -> str | None` — ISO ts of the most recent `bin_full` not followed by a `bin_clear`; `None` if the bin is currently clear.
- `last_bin_clear_ts(conn) -> str | None` — ISO ts of the most recent `bin_clear`, else `None`.
- `cleans_since(conn, after_iso) -> int` — count of `clean_done` events strictly after `after_iso`.
- `bin_fill_capacity(conn) -> int | None` — MIN `clean_done` count observed between a `bin_clear` and the following `bin_full`, across history; `None` if no complete cycle yet.

- [ ] **Step 1: Write the failing tests** — `tests/test_box_store.py`:

```python
from mw import store
from mw.events import Event, BIN_FULL, BIN_CLEAR, CLEAN_DONE

T = 1_000_000.0

def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn); return conn

def _ev(conn, kind, ts):
    store.insert_event(conn, Event(kind, ts))

def test_bin_full_since_none_when_clear(tmp_path):
    conn = _db(tmp_path)
    _ev(conn, BIN_FULL, T); _ev(conn, BIN_CLEAR, T + 100)   # cleared after full
    assert store.bin_full_since(conn) is None

def test_bin_full_since_returns_ts_when_full(tmp_path):
    conn = _db(tmp_path)
    _ev(conn, BIN_FULL, T - 100); _ev(conn, BIN_CLEAR, T - 50)  # an old cycle
    _ev(conn, BIN_FULL, T)                                       # full again, not cleared
    assert store.bin_full_since(conn) == store._iso(T)

def test_bin_full_since_none_with_no_events(tmp_path):
    assert store.bin_full_since(_db(tmp_path)) is None

def test_last_bin_clear_ts(tmp_path):
    conn = _db(tmp_path)
    assert store.last_bin_clear_ts(conn) is None
    _ev(conn, BIN_CLEAR, T); _ev(conn, BIN_CLEAR, T + 500)
    assert store.last_bin_clear_ts(conn) == store._iso(T + 500)

def test_cleans_since_counts_after_bound(tmp_path):
    conn = _db(tmp_path)
    _ev(conn, CLEAN_DONE, T - 10)                     # before the bound -> excluded
    _ev(conn, BIN_CLEAR, T)
    for i in range(3):
        _ev(conn, CLEAN_DONE, T + 60 * (i + 1))       # 3 after -> counted
    assert store.cleans_since(conn, store._iso(T)) == 3

def test_bin_fill_capacity_min_over_cycles(tmp_path):
    conn = _db(tmp_path)
    # cycle A: clear, 5 cleans, full
    _ev(conn, BIN_CLEAR, T)
    for i in range(5): _ev(conn, CLEAN_DONE, T + i + 1)
    _ev(conn, BIN_FULL, T + 10)
    # cycle B: clear, 2 cleans, full  -> min should be 2
    _ev(conn, BIN_CLEAR, T + 20)
    for i in range(2): _ev(conn, CLEAN_DONE, T + 21 + i)
    _ev(conn, BIN_FULL, T + 30)
    assert store.bin_fill_capacity(conn) == 2

def test_bin_fill_capacity_none_without_complete_cycle(tmp_path):
    conn = _db(tmp_path)
    _ev(conn, BIN_CLEAR, T)
    _ev(conn, CLEAN_DONE, T + 1)          # no bin_full yet -> no complete cycle
    assert store.bin_fill_capacity(conn) is None
```

- [ ] **Step 2: Run to verify they fail** — `python -m pytest tests/test_box_store.py -q` → FAIL (AttributeError: module has no attribute 'bin_full_since').

- [ ] **Step 3: Implement** — add to `mw/store.py` (place near `last_elimination_ts` / `unattributed_eliminations_since`):

```python
def bin_full_since(conn):
    """ISO ts of the most recent bin_full NOT followed by a bin_clear, else None.
    Ordering by autoincrement id is monotonic and tz-immune. None => bin is clear."""
    with _lock:
        full = conn.execute(
            "SELECT id, ts FROM events WHERE kind='bin_full' ORDER BY id DESC LIMIT 1").fetchone()
        if not full:
            return None
        clear = conn.execute(
            "SELECT id FROM events WHERE kind='bin_clear' ORDER BY id DESC LIMIT 1").fetchone()
        if clear and clear["id"] > full["id"]:
            return None
        return full["ts"]


def last_bin_clear_ts(conn):
    """ISO ts of the most recent bin_clear (the start of the current fill cycle), else None."""
    with _lock:
        row = conn.execute(
            "SELECT ts FROM events WHERE kind='bin_clear' ORDER BY id DESC LIMIT 1").fetchone()
        return row["ts"] if row else None


def cleans_since(conn, after_iso):
    """Count clean_done events strictly after after_iso — cleans into the current fill cycle."""
    with _lock:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='clean_done' "
            "AND strftime('%s', ts) > strftime('%s', ?)",
            (after_iso,)).fetchone()["n"]


def bin_fill_capacity(conn):
    """Learned capacity: MIN clean_done count observed between a bin_clear and the
    following bin_full, across history. Conservative (min) so the approaching-full
    heads-up lands before the earliest-possible fill. None if no complete cycle yet."""
    with _lock:
        rows = conn.execute(
            "SELECT kind FROM events WHERE kind IN ('bin_clear','bin_full','clean_done') "
            "ORDER BY id").fetchall()
    cycles, cleans, armed = [], 0, False
    for r in rows:
        k = r["kind"]
        if k == "bin_clear":
            cleans, armed = 0, True
        elif k == "clean_done":
            if armed:
                cleans += 1
        elif k == "bin_full":
            if armed:
                cycles.append(cleans)
            armed = False
    return min(cycles) if cycles else None
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_box_store.py -q` → PASS (7 tests). Then `python -m pytest -q` → green.

- [ ] **Step 5: Commit** — `feat(store): box-state query helpers (bin_full_since, last_bin_clear_ts, cleans_since, bin_fill_capacity)`

---

### Task 2: `BoxHealthWatch` watcher (`mw/box_health.py`)

**Files:**
- Create: `mw/box_health.py`
- Test: `tests/test_box_health.py` (new)

**Interfaces consumed (from Task 1):** `store.bin_full_since`, `store.last_bin_clear_ts`, `store.cleans_since`, `store.bin_fill_capacity`.

**Interfaces produced:** `BoxHealthWatch(conn, notify, now_fn=time.time, interval=900, renag_hours=3, unusable_hours=6, approaching_margin=2)` with `run_once()` and `run()`.

**Behavior (the `_check()` contract):**
1. If `bin_full_since` is not None: the bin is full. Compute hours-full. If `>= renag_hours` since the last nag (and `_last_nag` starts at 0 so the first nag is immediate): send a re-nag. Message escalates: `secs >= unusable_hours*3600` → `🚨 Box UNUSABLE {h}h — auto-clean blocked, cats can't go. Empty the bin NOW.`; else → `🪣 Litter bin full {h}h — empty it (auto-clean paused).` Then `return`.
2. Bin is clear: reset `_last_nag = 0.0` (so the next fill nags immediately).
3. Approaching-full heads-up (once per fill cycle): if `last_bin_clear_ts` changed since last armed, re-arm (`_approach_warned = False`). If not yet warned this cycle and `bin_fill_capacity` is known and `cleans_since(last_clear) >= capacity - approaching_margin`: send `🪣 Bin getting full — {cleans} auto-cleans since emptied (~{left} till full; your box holds ~{cap}). Empty soon.` and latch `_approach_warned = True`.

- [ ] **Step 1: Write the failing tests** — `tests/test_box_health.py`:

```python
from mw import store
from mw.events import Event, BIN_FULL, BIN_CLEAR, CLEAN_DONE
from mw.box_health import BoxHealthWatch

T = 1_000_000.0

def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn); return conn

def _ev(conn, kind, ts):
    store.insert_event(conn, Event(kind, ts))

def test_bin_full_nags_then_silent_until_renag(tmp_path):
    conn = _db(tmp_path); msgs = []; clock = [T]
    _ev(conn, BIN_FULL, T)
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0],
                       renag_hours=3, unusable_hours=6)
    w.run_once()                                   # first nag immediate
    assert len(msgs) == 1 and "bin full" in msgs[0].lower()
    clock[0] = T + 2 * 3600; w.run_once()          # 2h later -> still within renag, silent
    assert len(msgs) == 1
    clock[0] = T + 3 * 3600; w.run_once()          # 3h -> re-nag
    assert len(msgs) == 2

def test_escalates_to_unusable_after_threshold(tmp_path):
    conn = _db(tmp_path); msgs = []; clock = [T]
    _ev(conn, BIN_FULL, T)
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0],
                       renag_hours=3, unusable_hours=6)
    clock[0] = T + 6 * 3600; w.run_once()          # 6h full -> UNUSABLE escalation
    assert len(msgs) == 1 and "unusable" in msgs[0].lower()

def test_silent_and_rearms_when_clear(tmp_path):
    conn = _db(tmp_path); msgs = []; clock = [T]
    _ev(conn, BIN_FULL, T); _ev(conn, BIN_CLEAR, T + 10)   # already cleared
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0])
    w.run_once()
    assert msgs == []                              # clear -> no bin-full nag

def test_approaching_full_heads_up_once_per_cycle(tmp_path):
    conn = _db(tmp_path); msgs = []; clock = [T]
    # history: one complete cycle of capacity 3 (clear,3 cleans,full)
    _ev(conn, BIN_CLEAR, T - 1000)
    for i in range(3): _ev(conn, CLEAN_DONE, T - 900 + i)
    _ev(conn, BIN_FULL, T - 800)
    # current cycle: cleared, now 2 cleans (>= cap(3) - margin(1))
    _ev(conn, BIN_CLEAR, T)
    _ev(conn, CLEAN_DONE, T + 60); _ev(conn, CLEAN_DONE, T + 120)
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0], approaching_margin=1)
    w.run_once()
    assert len(msgs) == 1 and "getting full" in msgs[0].lower()
    w.run_once()                                   # same cycle -> no repeat
    assert len(msgs) == 1

def test_no_approaching_warn_without_learned_capacity(tmp_path):
    conn = _db(tmp_path); msgs = []
    _ev(conn, BIN_CLEAR, T)
    _ev(conn, CLEAN_DONE, T + 60)                  # no complete prior cycle -> capacity None
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: T, approaching_margin=1)
    w.run_once()
    assert msgs == []
```

- [ ] **Step 2: Run to verify they fail** — `python -m pytest tests/test_box_health.py -q` → FAIL (ModuleNotFoundError: mw.box_health).

- [ ] **Step 3: Implement** — `mw/box_health.py`:

```python
"""Box-health watchdog: the box's OWN liveness (bin full / auto-clean blocked).

Mirrors HealthWatch's persistent-latching pattern, but for the litter-box hardware:
  - approaching-full heads-up (predictive, from learned per-box capacity)
  - bin-full re-nag while the drawer stays full (auto-clean is paused)
  - hard 'box UNUSABLE' escalation once full long enough that cats can't go
24/7, NO quiet hours — a blocked box is harmful and always alerts. Re-arms on bin_clear.
"""
import sys
import time
from datetime import datetime

from mw import store


class BoxHealthWatch:
    def __init__(self, conn, notify, now_fn=time.time, interval=900,
                 renag_hours=3, unusable_hours=6, approaching_margin=2):
        self.conn = conn
        self.notify = notify
        self.now = now_fn
        self.interval = interval
        self.renag_s = renag_hours * 3600
        self.unusable_s = unusable_hours * 3600
        self.approaching_margin = approaching_margin
        self._last_nag = 0.0          # epoch of the last bin-full / unusable nag
        self._approach_clear = None   # the bin_clear ts the approaching-warn is armed against
        self._approach_warned = False

    def _check(self):
        now = self.now()
        full_since = store.bin_full_since(self.conn)
        if full_since is not None:
            secs = now - datetime.fromisoformat(full_since).timestamp()
            if now - self._last_nag >= self.renag_s:
                h = secs / 3600.0
                if secs >= self.unusable_s:
                    self.notify(f"🚨 Box UNUSABLE {h:.0f}h — auto-clean blocked, "
                                f"cats can't go. Empty the bin NOW.")
                else:
                    self.notify(f"🪣 Litter bin full {h:.0f}h — empty it (auto-clean paused).")
                self._last_nag = now
            return
        # Bin is clear -> reset the full-nag latch so a future fill nags immediately.
        self._last_nag = 0.0
        # Approaching-full heads-up: once per fill cycle, when cleans-since-empty
        # nears the learned capacity.
        last_clear = store.last_bin_clear_ts(self.conn)
        if last_clear != self._approach_clear:
            self._approach_clear = last_clear     # new cycle -> re-arm
            self._approach_warned = False
        if last_clear and not self._approach_warned:
            cap = store.bin_fill_capacity(self.conn)
            if cap:
                cleans = store.cleans_since(self.conn, last_clear)
                if cleans >= cap - self.approaching_margin:
                    left = max(0, cap - cleans)
                    self.notify(f"🪣 Bin getting full — {cleans} auto-cleans since emptied "
                                f"(~{left} till full; your box holds ~{cap}). Empty soon.")
                    self._approach_warned = True

    def run_once(self):
        self._check()

    def run(self):
        while True:
            try:
                self._check()
            except Exception as e:
                print(f"[box-health] error: {e}", file=sys.stderr)
            time.sleep(self.interval)
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_box_health.py -q` → PASS (5 tests). Then `python -m pytest -q` → green.

- [ ] **Step 5: Commit** — `feat(box-health): BoxHealthWatch — predictive approaching-full, 24/7 re-nag, UNUSABLE escalation`

---

### Task 3: Wire into daemon + stop the duplicate instant bin-full alert

**Files:**
- Modify: `meowantd.py` (start a `BoxHealthWatch` thread near the `HealthWatch` wiring, ~lines 215-227)
- Modify: `mw/alerts.py` (remove `BIN_FULL` from `_MESSAGES` so `BoxHealthWatch` is the single owner of bin-full messaging; keep `CHUTE_FULL` and `FAULT` instant)
- Test: `tests/test_alerts.py` (new or existing) — assert `bin_full` no longer produces an instant alert message, but `fault` still does.

**Interfaces consumed:** `BoxHealthWatch` (Task 2), `make_notify` / `config.get` (existing in meowantd).

- [ ] **Step 1: Write the failing test** — `tests/test_alerts.py`:

```python
from mw.alerts import alert_message
from mw.events import Event, BIN_FULL, FAULT

def test_bin_full_no_longer_instant_alert():
    # BoxHealthWatch owns bin-full messaging now; Alerts must not double-ping it.
    assert alert_message(Event(BIN_FULL, 0.0)) is None

def test_fault_still_instant_alert():
    msg = alert_message(Event(FAULT, 0.0, {"bitmap": 1}))
    assert msg is not None and "fault" in msg.lower()
```

- [ ] **Step 2: Run to verify it fails** — `python -m pytest tests/test_alerts.py -q` → FAIL (bin_full still returns a message).

- [ ] **Step 3: Implement**

In `mw/alerts.py`, remove the `BIN_FULL` line from `_MESSAGES` (and the now-unused `BIN_FULL` import if it is no longer referenced; keep `CHUTE_FULL`, `FAULT`):

```python
from mw.events import CHUTE_FULL, FAULT

_MESSAGES = {
    CHUTE_FULL: lambda e: "⚠️ Waste chute full or blocked",
    FAULT: lambda e: f"❌ SC10 fault: {e.detail.get('bitmap')}",
}
```

In `meowantd.py`, near the `HealthWatch` / `Heartbeat` wiring (~line 215), add:

```python
    from mw.box_health import BoxHealthWatch
    bhw = BoxHealthWatch(
        conn, make_notify(lambda k: config.get(cfg, k)),
        interval=config.get(cfg, "box_health.check_interval_s", 900),
        renag_hours=config.get(cfg, "box_health.renag_hours", 3),
        unusable_hours=config.get(cfg, "box_health.unusable_hours", 6),
        approaching_margin=config.get(cfg, "box_health.approaching_margin", 2))
    threading.Thread(target=bhw.run, daemon=True).start()
    print("box-health: bin-full re-nag + UNUSABLE escalation + approaching-full heads-up")
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_alerts.py -q` → PASS. `python -c "import meowantd"` → exit 0. `python -m pytest -q` → green.

- [ ] **Step 5: Commit** — `feat(daemon): wire BoxHealthWatch; alerts.py drops duplicate instant bin-full ping`

---

## Self-Review

- **Spec coverage:** predictive heads-up (Task 2 _check step 3 + capacity helper Task 1) ✅; 24/7 re-nag (Task 2 step 1, no quiet-hours code) ✅; UNUSABLE escalation (Task 2 step 1) ✅; re-arm on clear (Task 2 _last_nag reset + _approach re-arm) ✅; no double-ping (Task 3 alerts.py) ✅; survives restart (state derived from event log, not memory) ✅.
- **Type consistency:** `bin_full_since`/`last_bin_clear_ts` return `str|None` (ISO), consumed via `datetime.fromisoformat` / passed to `cleans_since`; `bin_fill_capacity` returns `int|None`, guarded with `if cap:` before arithmetic. Consistent across tasks.
- **Placeholder scan:** none — all steps carry full code.
- **Convention check:** helpers take `conn` first, wrap `with _lock:`, use `strftime` for the one ts comparison (`cleans_since`) and id-ordering elsewhere; watcher mirrors HealthWatch's loop/latch shape.
