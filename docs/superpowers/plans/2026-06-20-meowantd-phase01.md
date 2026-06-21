# meowantd Phase 0–1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `meowantd`, a daemon that owns the SC10's single Tuya socket, turns DP changes into semantic events + visit records in SQLite, runs a smart auto-clean rule that beats the re-entry timer reset, and serves a local status/event API.

**Architecture:** One always-on process (Mac Studio) polls the device every few seconds. A pure `detect_events` diffs successive DP snapshots into semantic events; a `VisitTracker` folds events into `visits` rows; a `SmartClean` state machine triggers a scoop after N seconds of true standby. A Flask app exposes `/state`, `/events` (SSE), and `/command`. The device is touched only by the daemon.

**Tech Stack:** Python 3.10, tinytuya (v3.5 device), SQLite (stdlib), Flask (already installed), pytest.

## Global Constraints

- Python 3.10 (`/opt/homebrew/opt/python@3.10/libexec/bin/python3`); run from repo root `~/repos/meowant`.
- Device is Tuya **v3.5**; credentials/local_key live in `config.json` (gitignored) — never hardcode.
- Only the daemon may open a device socket (single-connection device); other code consumes the API.
- DPS truth (verified from live data): `dp24` ∈ {standby, cat_get_in, waiting, cleaning, clean_done}; `dp7` = elimination counter; `dp21` bit0 = bin full; `dp5` = delay minutes; `dp102` = per-substantive-visit raw uint16. `set_value(24,"cleaning")` triggers a scoop.
- New code lives in the `mw/` package; existing `meowant.py`/`tui.py`/`app.py` keep working.
- TDD: write failing test → see it fail → implement → see it pass → commit.

---

### Task 1: Extract canonical decode module (`mw/decode.py`)

**Files:**
- Create: `mw/__init__.py` (empty)
- Create: `mw/decode.py`
- Modify: `meowant.py` (re-export from `mw.decode` to avoid duplication)
- Test: `tests/test_decode.py`

**Interfaces:**
- Produces: `DOCUMENTED: dict[int,str]`, `VENDOR: dict[int,str]`, `STATUS_VALUES: list[str]`, `NOTIFY_BITS: list[str]`, `PHASE_VALUES: list[str]`, `hhmm(m)->str`, `decode_bits(val, labels)->list[str]`, `decode_dp102(b64)->int|None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_decode.py
from mw import decode

def test_hhmm():
    assert decode.hhmm(1320) == "22:00"
    assert decode.hhmm(480) == "08:00"

def test_decode_bits():
    assert decode.decode_bits(0, decode.NOTIFY_BITS) == ["none"]
    assert decode.decode_bits(1, decode.NOTIFY_BITS) == ["garbage_box_full"]

def test_decode_dp102():
    assert decode.decode_dp102("ADcAAA==") == 55
    assert decode.decode_dp102("AOMAAA==") == 227

def test_status_values():
    assert "cat_get_in" in decode.STATUS_VALUES
    assert "clean_done" in decode.STATUS_VALUES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python3 -m pytest tests/test_decode.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mw'`

- [ ] **Step 3: Create the package and decode module**

```python
# mw/__init__.py
```

```python
# mw/decode.py
"""Canonical DPS maps + decoders for the Meowant SC10 (Tuya category msp)."""
import base64

DOCUMENTED = {
    4: "auto_clean", 5: "delay_clean_time", 7: "excretion_times_day",
    10: "sleep", 11: "sleep_start_time", 12: "sleep_end_time",
    21: "notification", 22: "fault", 23: "factory_reset", 24: "status",
}
STATUS_VALUES = ["standby", "cat_get_in", "waiting", "cleaning", "clean_done"]
VENDOR = {
    101: "contents_load", 102: "use_record", 103: "flag_103?", 104: "substate_a",
    105: "flag_105?", 106: "substate_b", 107: "phase", 108: "flag_108?",
    109: "flag_109?", 111: "flag_111?",
}
PHASE_VALUES = ["enter", "finish_clean"]
NOTIFY_BITS = ["garbage_box_full", "E1", "E2", "E3", "E4", "E5"]


def hhmm(m):
    try:
        m = int(m)
        return f"{m // 60:02d}:{m % 60:02d}"
    except (TypeError, ValueError):
        return str(m)


def decode_bits(val, labels):
    val = int(val or 0)
    on = [labels[i] for i in range(len(labels)) if val & (1 << i)]
    return on or ["none"]


def decode_dp102(b64):
    try:
        return int.from_bytes(base64.b64decode(b64)[:2], "big")
    except Exception:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/repos/meowant && python3 -m pytest tests/test_decode.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Point `meowant.py` at the canonical module (DRY)**

Replace the inline `DOCUMENTED`/`VENDOR`/`STATUS_VALUES`/`NOTIFY_BITS`/`PHASE_VALUES` definitions and the `hhmm`/`decode_bits`/`decode_dp102` functions in `meowant.py` with a re-export at the top of the file (just after the existing module docstring/imports):

```python
from mw.decode import (
    DOCUMENTED, VENDOR, STATUS_VALUES, NOTIFY_BITS, PHASE_VALUES,
    hhmm, decode_bits, decode_dp102,
)
```

Delete the now-duplicated definitions from `meowant.py`. Keep everything else.

- [ ] **Step 6: Verify existing CLI still works**

Run: `cd ~/repos/meowant && python3 -c "import ast; ast.parse(open('meowant.py').read())" && python3 meowant.py report`
Expected: report prints (uses the re-exported helpers); no ImportError.

- [ ] **Step 7: Commit**

```bash
git add mw/ tests/test_decode.py meowant.py
git commit -m "feat: extract canonical mw.decode module, dedupe meowant.py"
```

---

### Task 2: Config loader (`mw/config.py`)

**Files:**
- Create: `mw/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `load(path="config.json") -> dict` (raises `SystemExit` with a clear message if missing); `get(cfg, dotted, default)` for nested lookups like `get(cfg, "smartclean.idle_seconds", 90)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import json, mw.config as C

def test_get_nested(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"device_id": "x", "smartclean": {"idle_seconds": 45}}))
    cfg = C.load(str(p))
    assert cfg["device_id"] == "x"
    assert C.get(cfg, "smartclean.idle_seconds", 90) == 45
    assert C.get(cfg, "smartclean.missing", 90) == 90
    assert C.get(cfg, "nope.deep", "d") == "d"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mw.config'`

- [ ] **Step 3: Implement**

```python
# mw/config.py
import json, os, sys

def load(path="config.json"):
    if not os.path.exists(path):
        sys.exit(f"Missing config at {path} — copy config.example.json and fill it in.")
    with open(path) as f:
        return json.load(f)

def get(cfg, dotted, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mw/config.py tests/test_config.py
git commit -m "feat: mw.config loader with nested get"
```

---

### Task 3: Semantic event detection (`mw/events.py`)

**Files:**
- Create: `mw/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: `mw.decode.decode_dp102`.
- Produces: `Event` dataclass `(kind: str, ts: float, detail: dict)`; kind constants `CAT_ENTER, CAT_LEAVE, CLEAN_START, CLEAN_DONE, BIN_FULL, BIN_CLEAR, FAULT, ELIMINATION`; `detect_events(prev: dict, new: dict, ts: float) -> list[Event]`. DPS dicts are keyed by **string** dp numbers (as tinytuya returns).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_events.py
from mw.events import (detect_events, CAT_ENTER, CAT_LEAVE, CLEAN_START,
                       CLEAN_DONE, BIN_FULL, ELIMINATION)

def kinds(evs): return [e.kind for e in evs]

def test_cat_enter_and_leave():
    assert kinds(detect_events({"24": "standby"}, {"24": "cat_get_in"}, 1.0)) == [CAT_ENTER]
    assert kinds(detect_events({"24": "cat_get_in"}, {"24": "standby"}, 2.0)) == [CAT_LEAVE]

def test_clean_cycle():
    assert CLEAN_START in kinds(detect_events({"24": "waiting"}, {"24": "cleaning"}, 3.0))
    assert CLEAN_DONE in kinds(detect_events({"24": "cleaning"}, {"24": "clean_done"}, 4.0))

def test_bin_full_edge_only():
    assert kinds(detect_events({"21": 0}, {"21": 1}, 5.0)) == [BIN_FULL]
    assert detect_events({"21": 1}, {"21": 1}, 6.0) == []  # no repeat

def test_elimination_from_dp7_increment():
    assert ELIMINATION in kinds(detect_events({"7": 1}, {"7": 2}, 7.0))

def test_elimination_from_dp102_record():
    evs = detect_events({"102": None}, {"102": "ADcAAA=="}, 8.0)
    assert ELIMINATION in kinds(evs)
    assert evs[0].detail["use_record"] == 55

def test_no_change_no_events():
    assert detect_events({"24": "standby"}, {"24": "standby"}, 9.0) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mw.events'`

- [ ] **Step 3: Implement**

```python
# mw/events.py
"""Diff successive DPS snapshots into semantic events."""
from dataclasses import dataclass, field
from mw.decode import decode_dp102

CAT_ENTER = "cat_enter"
CAT_LEAVE = "cat_leave"
CLEAN_START = "clean_start"
CLEAN_DONE = "clean_done"
BIN_FULL = "bin_full"
BIN_CLEAR = "bin_clear"
FAULT = "fault"
ELIMINATION = "elimination"


@dataclass
class Event:
    kind: str
    ts: float
    detail: dict = field(default_factory=dict)


def _g(d, k):
    return d.get(str(k))


def detect_events(prev, new, ts):
    evs = []
    o24, n24 = _g(prev, 24), _g(new, 24)
    if n24 is not None and n24 != o24:
        if n24 == "cat_get_in":
            evs.append(Event(CAT_ENTER, ts, {"from": o24}))
        elif o24 == "cat_get_in":
            evs.append(Event(CAT_LEAVE, ts, {"to": n24}))
        if n24 == "cleaning":
            evs.append(Event(CLEAN_START, ts))
        if n24 == "clean_done":
            evs.append(Event(CLEAN_DONE, ts))

    o21, n21 = int(_g(prev, 21) or 0), int(_g(new, 21) or 0)
    if (n21 & 1) and not (o21 & 1):
        evs.append(Event(BIN_FULL, ts))
    if (o21 & 1) and not (n21 & 1):
        evs.append(Event(BIN_CLEAR, ts))

    o22, n22 = int(_g(prev, 22) or 0), int(_g(new, 22) or 0)
    if n22 and n22 != o22:
        evs.append(Event(FAULT, ts, {"bitmap": n22}))

    o7, n7 = _g(prev, 7), _g(new, 7)
    if o7 is not None and n7 is not None and int(n7) > int(o7):
        evs.append(Event(ELIMINATION, ts, {"count": int(n7)}))

    o102, n102 = _g(prev, 102), _g(new, 102)
    if n102 and n102 != o102:
        evs.append(Event(ELIMINATION, ts, {"use_record": decode_dp102(n102)}))

    return evs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_events.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add mw/events.py tests/test_events.py
git commit -m "feat: semantic event detection from DPS diffs"
```

---

### Task 4: SQLite store (`mw/store.py`)

**Files:**
- Create: `mw/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `connect(path)->sqlite3.Connection`; `init_db(conn)`; `insert_event(conn, ev)`; `open_visit(conn, enter_ts)->int`; `close_visit(conn, visit_id, leave_ts, duration_s)`; `mark_elimination(conn, visit_id, use_record=None)`; `recent_visits(conn, limit=20)->list[dict]`; `seed_cats(conn, names)`. Timestamps stored as ISO strings via internal `_iso(ts_float)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
from mw import store
from mw.events import Event, CAT_ENTER

def test_visit_lifecycle(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.insert_event(conn, Event(CAT_ENTER, 1000.0, {"from": "standby"}))
    vid = store.open_visit(conn, 1000.0)
    store.mark_elimination(conn, vid, use_record=55)
    store.close_visit(conn, vid, 1066.0, 66)
    rows = store.recent_visits(conn, 10)
    assert len(rows) == 1
    assert rows[0]["duration_s"] == 66
    assert rows[0]["eliminated"] == 1
    assert rows[0]["use_record"] == 55

def test_seed_cats(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Orange", "Black", "Tabby"])
    store.seed_cats(conn, ["Orange", "Black", "Tabby"])  # idempotent
    n = conn.execute("SELECT COUNT(*) FROM cats").fetchone()[0]
    assert n == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mw.store'`

- [ ] **Step 3: Implement**

```python
# mw/store.py
"""SQLite persistence for events and visits."""
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS cats(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, notes TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS visits(
  id INTEGER PRIMARY KEY,
  enter_ts TEXT, leave_ts TEXT, duration_s INTEGER,
  cat_id INTEGER REFERENCES cats(id), confidence REAL,
  eliminated INTEGER DEFAULT 0, use_record INTEGER,
  contents_load_min INTEGER, contents_load_max INTEGER,
  frame_path TEXT);
"""


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def insert_event(conn, ev):
    conn.execute("INSERT INTO events(ts, kind, detail) VALUES(?,?,?)",
                 (_iso(ev.ts), ev.kind, json.dumps(ev.detail)))
    conn.commit()


def open_visit(conn, enter_ts):
    cur = conn.execute("INSERT INTO visits(enter_ts) VALUES(?)", (_iso(enter_ts),))
    conn.commit()
    return cur.lastrowid


def close_visit(conn, visit_id, leave_ts, duration_s):
    conn.execute("UPDATE visits SET leave_ts=?, duration_s=? WHERE id=?",
                 (_iso(leave_ts), duration_s, visit_id))
    conn.commit()


def mark_elimination(conn, visit_id, use_record=None):
    conn.execute(
        "UPDATE visits SET eliminated=1, use_record=COALESCE(?, use_record) WHERE id=?",
        (use_record, visit_id))
    conn.commit()


def recent_visits(conn, limit=20):
    cur = conn.execute("SELECT * FROM visits ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in cur.fetchall()]


def seed_cats(conn, names):
    for n in names:
        conn.execute("INSERT OR IGNORE INTO cats(name) VALUES(?)", (n,))
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_store.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add mw/store.py tests/test_store.py
git commit -m "feat: SQLite store for events and visits"
```

---

### Task 5: Visit tracker (`mw/tracker.py`)

**Files:**
- Create: `mw/tracker.py`
- Test: `tests/test_tracker.py`

**Interfaces:**
- Consumes: `mw.store` (open_visit/close_visit/mark_elimination), `mw.events` kinds.
- Produces: `VisitTracker(conn)` with `handle(ev: Event) -> None`. Maintains one open visit at a time: `CAT_ENTER` opens (if none open), `CAT_LEAVE` closes the open one with computed duration, `ELIMINATION` marks the open one. Events outside an open visit are ignored safely.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tracker.py
from mw import store
from mw.tracker import VisitTracker
from mw.events import Event, CAT_ENTER, CAT_LEAVE, ELIMINATION

def test_burst_creates_one_visit_per_entry(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    t = VisitTracker(conn)
    # two quick in/out pokes
    t.handle(Event(CAT_ENTER, 100.0)); t.handle(Event(CAT_LEAVE, 109.0))
    t.handle(Event(CAT_ENTER, 120.0)); t.handle(Event(CAT_LEAVE, 282.0))
    rows = store.recent_visits(conn, 10)
    assert len(rows) == 2
    durations = sorted(r["duration_s"] for r in rows)
    assert durations == [9, 162]

def test_elimination_marks_open_visit(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    t = VisitTracker(conn)
    t.handle(Event(CAT_ENTER, 100.0))
    t.handle(Event(ELIMINATION, 150.0, {"use_record": 227}))
    t.handle(Event(CAT_LEAVE, 200.0))
    row = store.recent_visits(conn, 1)[0]
    assert row["eliminated"] == 1 and row["use_record"] == 227

def test_leave_without_open_is_safe(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    t = VisitTracker(conn)
    t.handle(Event(CAT_LEAVE, 5.0))  # no crash, no row
    assert store.recent_visits(conn, 1) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tracker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mw.tracker'`

- [ ] **Step 3: Implement**

```python
# mw/tracker.py
"""Fold semantic events into visit rows (one open visit at a time)."""
from mw import store
from mw.events import CAT_ENTER, CAT_LEAVE, ELIMINATION


class VisitTracker:
    def __init__(self, conn):
        self.conn = conn
        self._open_id = None
        self._enter_ts = None

    def handle(self, ev):
        if ev.kind == CAT_ENTER:
            if self._open_id is None:
                self._open_id = store.open_visit(self.conn, ev.ts)
                self._enter_ts = ev.ts
        elif ev.kind == CAT_LEAVE:
            if self._open_id is not None:
                dur = int(ev.ts - self._enter_ts)
                store.close_visit(self.conn, self._open_id, ev.ts, dur)
                self._open_id = None
                self._enter_ts = None
        elif ev.kind == ELIMINATION:
            if self._open_id is not None:
                store.mark_elimination(self.conn, self._open_id,
                                       ev.detail.get("use_record"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tracker.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add mw/tracker.py tests/test_tracker.py
git commit -m "feat: visit tracker folds events into visit rows"
```

---

### Task 6: Smart auto-clean rule (`mw/smartclean.py`)

**Files:**
- Create: `mw/smartclean.py`
- Test: `tests/test_smartclean.py`

**Interfaces:**
- Produces: `SmartClean(idle_seconds=90, enabled=True)` with `update(dps: dict, now: float) -> bool`. Returns `True` exactly once when `dp24` has been `standby` continuously for `idle_seconds` with no `cat_get_in` since; re-arms only after the next presence. Non-standby/non-presence states (waiting/cleaning/clean_done) pause the idle timer without re-arming.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_smartclean.py
from mw.smartclean import SmartClean

def test_fires_once_after_idle():
    sc = SmartClean(idle_seconds=90)
    assert sc.update({"24": "cat_get_in"}, 0) is False
    assert sc.update({"24": "standby"}, 10) is False     # timer starts at 10
    assert sc.update({"24": "standby"}, 99) is False      # 89s < 90
    assert sc.update({"24": "standby"}, 100) is True      # 90s -> fire
    assert sc.update({"24": "standby"}, 200) is False     # one-shot, no repeat

def test_reentry_resets_then_fires_later():
    sc = SmartClean(idle_seconds=90)
    sc.update({"24": "standby"}, 10)
    sc.update({"24": "cat_get_in"}, 50)                   # re-entry resets+re-arms
    assert sc.update({"24": "standby"}, 60) is False      # new timer from 60
    assert sc.update({"24": "standby"}, 149) is False
    assert sc.update({"24": "standby"}, 150) is True

def test_disabled_never_fires():
    sc = SmartClean(idle_seconds=1, enabled=False)
    sc.update({"24": "standby"}, 0)
    assert sc.update({"24": "standby"}, 100) is False

def test_cleaning_state_pauses_timer():
    sc = SmartClean(idle_seconds=90)
    sc.update({"24": "standby"}, 10)
    sc.update({"24": "cleaning"}, 20)                     # not standby -> timer cleared
    assert sc.update({"24": "standby"}, 30) is False      # restart from 30
    assert sc.update({"24": "standby"}, 119) is False
    assert sc.update({"24": "standby"}, 120) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_smartclean.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mw.smartclean'`

- [ ] **Step 3: Implement**

```python
# mw/smartclean.py
"""Trigger a scoop after N seconds of true standby — beats the re-entry reset."""


class SmartClean:
    def __init__(self, idle_seconds=90, enabled=True):
        self.idle = idle_seconds
        self.enabled = enabled
        self._standby_since = None
        self._armed = True

    def update(self, dps, now):
        status = dps.get("24")
        if status == "cat_get_in":
            self._standby_since = None
            self._armed = True
            return False
        if status == "standby":
            if self._standby_since is None:
                self._standby_since = now
            if (self.enabled and self._armed
                    and now - self._standby_since >= self.idle):
                self._armed = False  # one-shot until next presence
                return True
            return False
        # waiting / cleaning / clean_done: pause the idle timer
        self._standby_since = None
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_smartclean.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add mw/smartclean.py tests/test_smartclean.py
git commit -m "feat: smart auto-clean idle rule"
```

---

### Task 7: Device wrapper + daemon core (`mw/device.py`, `mw/daemon.py`)

**Files:**
- Create: `mw/device.py`
- Create: `mw/daemon.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- `mw/device.py` produces: `TuyaDevice(cfg)` with `status_dps()->dict` (thread-safe, one reconnect retry, returns `{}` on failure) and `clean()->None` (`set_value(24,"cleaning")`). Also a `FakeDevice(snapshots: list[dict])` test double exposing the same `status_dps()`/`clean()` (records `clean_calls`).
- `mw/daemon.py` produces: `Daemon(device, conn, smartclean, on_event=None, now_fn=time.time)` with `tick()->dict` (polls once, detects+persists+tracks events, runs smart-clean, returns current dps) and `run(interval, ticks=None)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daemon.py
from mw import store
from mw.device import FakeDevice
from mw.daemon import Daemon
from mw.smartclean import SmartClean

def make(tmp_path, snapshots, idle=90):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    dev = FakeDevice(snapshots)
    clock = {"t": 0.0}
    def now(): return clock["t"]
    d = Daemon(dev, conn, SmartClean(idle_seconds=idle), now_fn=now)
    return conn, dev, d, clock

def test_full_visit_with_elimination_records_one_visit(tmp_path):
    snaps = [
        {"24": "standby", "7": 1, "21": 0},
        {"24": "cat_get_in", "7": 1, "21": 0},
        {"24": "cat_get_in", "7": 1, "21": 0, "102": "AOMAAA=="},  # use record (227)
        {"24": "standby", "7": 1, "21": 0},
    ]
    conn, dev, d, clock = make(tmp_path, snaps)
    for i in range(len(snaps)):
        clock["t"] = 100.0 + i * 10
        d.tick()
    rows = store.recent_visits(conn, 10)
    assert len(rows) == 1
    assert rows[0]["eliminated"] == 1 and rows[0]["use_record"] == 227

def test_smartclean_triggers_clean_after_idle(tmp_path):
    snaps = [{"24": "cat_get_in"}] + [{"24": "standby"}] * 5
    conn, dev, d, clock = make(tmp_path, snaps, idle=20)
    for i in range(len(snaps)):
        clock["t"] = i * 10  # 0,10,20,30,40,50
        d.tick()
    assert dev.clean_calls >= 1

def test_no_clean_while_cat_present(tmp_path):
    snaps = [{"24": "cat_get_in"}] * 6
    conn, dev, d, clock = make(tmp_path, snaps, idle=1)
    for i in range(len(snaps)):
        clock["t"] = i * 10
        d.tick()
    assert dev.clean_calls == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_daemon.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mw.device'`

- [ ] **Step 3: Implement the device wrapper**

```python
# mw/device.py
"""Single owner of the SC10 socket (real) + a test double."""
import threading


class TuyaDevice:
    def __init__(self, cfg):
        import tinytuya
        self._lock = threading.Lock()
        self._cfg = cfg
        self._dev = None
        self._tinytuya = tinytuya

    def _device(self):
        if self._dev is None:
            self._dev = self._tinytuya.Device(
                dev_id=self._cfg["device_id"], address=self._cfg["address"],
                local_key=self._cfg["local_key"], version=float(self._cfg["version"]))
            self._dev.set_socketPersistent(True)
        return self._dev

    def status_dps(self):
        with self._lock:
            for _ in (1, 2):
                try:
                    data = self._device().status()
                    dps = data.get("dps", {}) if isinstance(data, dict) else {}
                    if dps:
                        return dps
                except Exception:
                    self._dev = None
            return {}

    def clean(self):
        with self._lock:
            try:
                self._device().set_value(24, "cleaning")
            except Exception:
                self._dev = None


class FakeDevice:
    """Replays a list of dps snapshots; records clean() calls."""
    def __init__(self, snapshots):
        self._snaps = list(snapshots)
        self._i = 0
        self.clean_calls = 0

    def status_dps(self):
        if self._i < len(self._snaps):
            dps = self._snaps[self._i]
            self._i += 1
            return dict(dps)
        return dict(self._snaps[-1]) if self._snaps else {}

    def clean(self):
        self.clean_calls += 1
```

- [ ] **Step 4: Implement the daemon core**

```python
# mw/daemon.py
"""Poll the device, detect+persist+track events, run smart-clean."""
import time

from mw import store
from mw.events import detect_events
from mw.tracker import VisitTracker


class Daemon:
    def __init__(self, device, conn, smartclean, on_event=None, now_fn=time.time):
        self.device = device
        self.conn = conn
        self.smartclean = smartclean
        self.on_event = on_event
        self.now = now_fn
        self.tracker = VisitTracker(conn)
        self.prev = {}
        self.state = {}

    def tick(self):
        now = self.now()
        dps = self.device.status_dps()
        if dps:
            for ev in detect_events(self.prev, dps, now):
                store.insert_event(self.conn, ev)
                self.tracker.handle(ev)
                if self.on_event:
                    self.on_event(ev)
            if self.smartclean.update(dps, now):
                self.device.clean()
            self.prev = {**self.prev, **dps}  # merge: tolerate partial updates
            self.state = self.prev
        return self.state

    def run(self, interval=3.0, ticks=None):
        n = 0
        while ticks is None or n < ticks:
            self.tick()
            n += 1
            time.sleep(interval)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_daemon.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add mw/device.py mw/daemon.py tests/test_daemon.py
git commit -m "feat: device wrapper + daemon core (poll/detect/track/smartclean)"
```

---

### Task 8: HTTP API + entry point (`mw/api.py`, `meowantd.py`)

**Files:**
- Create: `mw/api.py`
- Create: `meowantd.py`
- Modify: `.gitignore` (add `*.db`)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `mw.store.recent_visits`, `mw.decode`, a live `Daemon` instance.
- Produces: `create_app(daemon, conn) -> flask.Flask` with routes `GET /state` (JSON: decoded current state), `GET /visits?limit=N` (JSON list), `POST /command` (`{"action": "clean"|"autoclean"|...}`). `meowantd.py` wires real config → store → device → daemon (background thread) → app on port 8765.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api.py
from mw import store
from mw.device import FakeDevice
from mw.daemon import Daemon
from mw.smartclean import SmartClean
from mw.api import create_app

def test_state_and_visits_and_command(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    dev = FakeDevice([{"24": "standby", "4": True, "7": 1, "21": 0}])
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0)
    d.tick()
    app = create_app(d, conn).test_client()

    st = app.get("/state").get_json()
    assert st["status"] == "standby"
    assert st["auto_clean"] is True

    assert app.get("/visits").get_json() == []

    r = app.post("/command", json={"action": "clean"})
    assert r.get_json()["ok"] is True
    assert dev.clean_calls == 1

    bad = app.post("/command", json={"action": "nope"})
    assert bad.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mw.api'`

- [ ] **Step 3: Implement the API**

```python
# mw/api.py
"""Read/command HTTP API over the daemon's current state + store."""
from flask import Flask, jsonify, request

from mw import decode, store


def _decode_state(dps):
    g = lambda k: dps.get(str(k))
    return {
        "status": g(24),
        "auto_clean": bool(g(4)),
        "delay_clean_time": g(5),
        "uses_today": g(7),
        "sleep_active": bool(g(10)),
        "quiet_start": decode.hhmm(g(11)),
        "quiet_end": decode.hhmm(g(12)),
        "bin_full": bool((g(21) or 0) & 1),
        "faults": decode.decode_bits(g(22), ["E1", "E2", "E3", "E4", "E5"]),
        "phase": g(107),
        "raw": dps,
    }


def create_app(daemon, conn):
    app = Flask(__name__)

    @app.get("/state")
    def state():
        return jsonify(_decode_state(daemon.state))

    @app.get("/visits")
    def visits():
        limit = int(request.args.get("limit", 20))
        return jsonify(store.recent_visits(conn, limit))

    @app.post("/command")
    def command():
        body = request.get_json(force=True) or {}
        action = body.get("action")
        if action == "clean":
            daemon.device.clean()
        elif action == "autoclean":
            return _set_value(daemon, 4, bool(body.get("value")))
        elif action == "delay":
            return _set_value(daemon, 5, max(1, min(60, int(body.get("value")))))
        else:
            return jsonify({"ok": False, "error": f"unknown action {action}"}), 400
        return jsonify({"ok": True})

    return app


def _set_value(daemon, dp, value):
    with daemon.device._lock:
        try:
            daemon.device._device().set_value(dp, value)
        except Exception as e:
            daemon.device._dev = None
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_api.py -v`
Expected: PASS

Note: the `FakeDevice` has no `_lock`/`_device`; the test only exercises `clean` and the two read routes, so `_set_value` is not hit in the test. The real device (Task 7) provides `_lock`/`_device`.

- [ ] **Step 5: Implement the entry point**

```python
# meowantd.py
#!/usr/bin/env python3
"""Run the Meowant SC10 daemon: owns the device, serves the API on :8765."""
import threading

from mw import config, store
from mw.daemon import Daemon
from mw.device import TuyaDevice
from mw.smartclean import SmartClean
from mw.api import create_app


def main():
    cfg = config.load("config.json")
    conn = store.connect("meowant.db")
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])

    device = TuyaDevice(cfg)
    sc = SmartClean(
        idle_seconds=config.get(cfg, "smartclean.idle_seconds", 90),
        enabled=config.get(cfg, "smartclean.enabled", True))
    daemon = Daemon(device, conn, sc)

    t = threading.Thread(target=daemon.run, kwargs={"interval": 3.0}, daemon=True)
    t.start()

    app = create_app(daemon, conn)
    print("meowantd → http://0.0.0.0:8765  (smart-clean idle="
          f"{sc.idle}s, enabled={sc.enabled})")
    app.run(host="0.0.0.0", port=8765, threaded=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Add SQLite db to gitignore and verify whole suite**

Add to `.gitignore`:

```
*.db
```

Run: `cd ~/repos/meowant && python3 -m pytest tests/ -v`
Expected: PASS (all tests across the 8 modules)

- [ ] **Step 7: Commit**

```bash
git add mw/api.py meowantd.py tests/test_api.py .gitignore
git commit -m "feat: HTTP API + meowantd entry point"
```

---

## Self-Review

**Spec coverage (Phase 0–1 scope):**
- Single-socket ownership → `TuyaDevice` + daemon-only access (Task 7). ✅
- Semantic events → `mw/events.py` (Task 3). ✅
- Visit tracking → `mw/tracker.py` + `visits` table (Tasks 4–5). ✅
- Smart auto-clean rule (idle N, re-entry-proof, config) → `mw/smartclean.py` (Task 6). ✅
- Store (SQLite, events/visits/cats) → `mw/store.py` (Task 4). ✅
- Status/command API → `mw/api.py` (Task 8). ✅
- Replay-testable core → daemon `now_fn` injection + `FakeDevice` (Task 7). ✅
- **Deferred to follow-on plan (Phase 1 remainder + 2–4):** SSE `/events` stream, alerts-service, chute-full flag wiring (needs the drawer-pull experiment), TUI/web client refactor, camera capture, inference. Noted, not built here.

**Placeholder scan:** Clean — the earlier stray no-op line in the `autoclean` branch was removed; the branch is just `return _set_value(daemon, 4, bool(body.get("value")))`. No other placeholders.

**Type consistency:** `status_dps()`/`clean()` match between `TuyaDevice` and `FakeDevice` and are called identically in `Daemon`. `Event(kind, ts, detail)` is consistent across events/tracker/daemon. `recent_visits` returns `list[dict]` consumed by the API. `now_fn` injection consistent (daemon + tests). DPS dicts are string-keyed everywhere.
