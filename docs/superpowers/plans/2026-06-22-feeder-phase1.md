# Cat Feeder Integration — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Monitor and control the PLAF103 auto-feeder from the meowant daemon — reliably log every dispense, alert on missed scheduled drops / empty hopper / unreachable feeder, and allow a manual feed from Telegram.

**Architecture:** A feeder subsystem parallel to the litterbox device path. `FeederDevice` (local tinytuya, by confirmed dp numbers) reads status + dispenses; `FeederMonitor` polls it, detects feeds via the persistent dp-118 feed record (decoded to timestamp + portions), logs them, and runs latched fail-loud watchdogs (missed-drop, hopper-empty, offline). Surfaced through the existing notify, Telegram, and digest channels.

**Tech Stack:** Python 3, stdlib only (`base64`, `time`, `datetime`, `inspect`, `threading`), `tinytuya` (already a dependency), pytest with `tmp_path` SQLite DBs + a fake device.

## Global Constraints

- **All values below are empirically confirmed against the live device** (a real `set_value(3,1)` test feed). Source of truth: `docs/superpowers/specs/2026-06-22-feeder-phase1-design.md`.
- **Confirmed dp map:** dp `3` = manual_feed (write-only command), dp `4` = feed_state (`standby`/`feeding`/`feed_end`), dp `108` = food_level enum (hopper; `full` seen), dp `118` = feed record (Raw base64), dp `1` = meal_plan. dp `11`/`14` are NOT a portion counter (stayed 0 through a real dispense) — **do not use them**.
- **dp-118 feed-record format (10 bytes):** `[year_hi, year_lo, month, day, hour, min, sec, portions, type, flag]`. Example `07 EA 06 16 16 23 28 01 02 00` = `2026-06-22 22:35:40`, portions `1`. It **persists the last feed**, so polling it detects feeds reliably regardless of timing.
- **Local control only** — cloud `manual_feed` returns `1109 param is illegal`. Connect via `tinytuya.Device(dev_id, address, local_key, version)`, `version=3.4`, `address=192.168.2.84`. **Do NOT use `set_socketPersistent(True)`** for the feeder — persistent sockets return partial dp pushes; a fresh non-persistent `status()` returns the full dps (needed for `food_level`/`118`).
- **Fail loud / latch on confirmed delivery:** every alert latches only when `notify(msg) is not False` (a dead transport must not mute a real feeding failure), and re-arms on recovery — matching `mw/health_watch.py`, `mw/deadman.py`, `mw/invariant_canary.py`.
- `store.py` access via `with _lock:`; timestamps via `store._iso(epoch)`. `feed_events` is a NEW table → add to `SCHEMA`, not `_MIGRATIONS`.
- Do NOT commit secrets; `config.json` is gitignored and already holds the `feeder` block (device_id, local_key, address, version). Daemon restarts during manual testing use `launchctl kickstart -k gui/$(id -u)/com.meowant.daemon`.
- The on-device schedule (set in the SmartLife app) is the real feeding guarantee; our monitor failing degrades to "no monitoring," never "cats unfed". `meal_plan` is currently empty, so `mealtimes` config starts `[]` (no missed-drop checks until the owner sets a schedule).

---

### Task 1: `feed_events` table + store functions

**Files:**
- Modify: `mw/store.py` (add table to `SCHEMA`; functions near `eliminations_today`)
- Test: `tests/test_store.py` (add)

**Interfaces:**
- Consumes: `store._lock`, `store._iso`, `store.connect`, `store.init_db`.
- Produces:
  - `store.log_feed_event(conn, portions, source, ts=None) -> None` — `source` = `"scheduled"|"manual"`; `ts` epoch float (None ⇒ now).
  - `store.last_feed_event_ts(conn) -> float|None` — epoch of the most recent feed event (for resume + new-feed detection).
  - `store.feed_in_window(conn, start_epoch, end_epoch) -> bool` — any feed event with `start <= ts <= end`.
  - `store.feed_events_today(conn, day=None) -> tuple[int, int]` — `(meals, total_portions)` for the local day (`day` = ISO date string, default today).
  - `store.recent_feed_events(conn, limit=20) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_store.py`:

```python
def test_feed_events_log_and_query(tmp_path):
    from mw import store
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    T = 1_000_000.0
    store.log_feed_event(conn, 2, "scheduled", ts=T)
    store.log_feed_event(conn, 1, "manual", ts=T + 3600)
    assert abs(store.last_feed_event_ts(conn) - (T + 3600)) < 1
    assert store.feed_in_window(conn, T - 10, T + 10) is True
    assert store.feed_in_window(conn, T + 100, T + 200) is False
    rows = store.recent_feed_events(conn)
    assert len(rows) == 2 and rows[0]["source"] == "manual"


def test_feed_events_today_aggregates(tmp_path):
    from mw import store
    from datetime import date, datetime
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    # two feeds "today" (use now so the local-day filter matches), one portions 2 + one 1
    now = datetime.now().timestamp()
    store.log_feed_event(conn, 2, "scheduled", ts=now - 100)
    store.log_feed_event(conn, 1, "manual", ts=now - 50)
    meals, portions = store.feed_events_today(conn)
    assert meals == 2 and portions == 3


def test_last_feed_event_ts_empty_is_none(tmp_path):
    from mw import store
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    assert store.last_feed_event_ts(conn) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_store.py -k feed_event -v`
Expected: FAIL — `AttributeError: module 'mw.store' has no attribute 'log_feed_event'`.

- [ ] **Step 3: Add the table to SCHEMA**

In `mw/store.py`, inside the `SCHEMA` string (after the `incidents(...)` table), add:

```sql
CREATE TABLE IF NOT EXISTS feed_events(
  id INTEGER PRIMARY KEY, ts TEXT, portions INTEGER,
  source TEXT);          -- 'scheduled' | 'manual'
```

- [ ] **Step 4: Add the store functions**

In `mw/store.py`, after `eliminations_today` (or near the other read helpers), add:

```python
def log_feed_event(conn, portions, source, ts=None):
    """Record one dispense (from the dp-118 feed record or a manual /feed)."""
    stamp = _iso(ts) if ts is not None else datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn.execute("INSERT INTO feed_events(ts, portions, source) VALUES(?,?,?)",
                     (stamp, int(portions), source))
        conn.commit()


def last_feed_event_ts(conn):
    """Epoch of the most recent feed event, or None — drives new-feed detection."""
    with _lock:
        row = conn.execute("SELECT MAX(ts) AS m FROM feed_events").fetchone()
    if not row or row["m"] is None:
        return None
    return datetime.fromisoformat(row["m"]).timestamp()


def feed_in_window(conn, start_epoch, end_epoch):
    """True if any feed event landed in [start, end] (inclusive)."""
    with _lock:
        row = conn.execute(
            "SELECT 1 FROM feed_events WHERE ts>=? AND ts<=? LIMIT 1",
            (_iso(start_epoch), _iso(end_epoch))).fetchone()
        return row is not None


def feed_events_today(conn, day=None):
    """(meals, total_portions) for the given local day (default today)."""
    day = day or date.today().isoformat()
    with _lock:
        row = conn.execute(
            "SELECT COUNT(*) AS meals, COALESCE(SUM(portions),0) AS portions "
            "FROM feed_events WHERE ts LIKE ?", (day + "%",)).fetchone()
        return row["meals"], row["portions"]


def recent_feed_events(conn, limit=20):
    with _lock:
        rows = conn.execute("SELECT * FROM feed_events ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
        return [dict(r) for r in rows]
```

(`date` and `datetime` are already imported at the top of `store.py`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_store.py -k feed -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
cd ~/repos/meowant
git add mw/store.py tests/test_store.py
git commit -m "feat(feeder): feed_events table + store log/query (Phase 1)"
```

---

### Task 2: `mw/feeder.py` — feed-record decode + `FeederDevice` + fake

**Files:**
- Create: `mw/feeder.py`
- Test: `tests/test_feeder.py` (create)

**Interfaces:**
- Consumes: `tinytuya` (lazy import inside `FeederDevice`).
- Produces:
  - `feeder.decode_feed_record(b64) -> {"ts": float, "portions": int} | None` — pure; decodes the dp-118 10-byte record; None on empty/garbage/invalid date.
  - `feeder.FeederDevice(cfg)` — `.status() -> {"feed_state","food_level","last_feed","online"}` (full non-persistent read; `{"online": False}` on I/O error); `.feed(portions) -> bool` (`set_value(3, portions)`).
  - `feeder.FakeFeederDevice(snapshots)` — replays a list of `status()` dicts; records `feed()` calls in `.fed`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_feeder.py`:

```python
"""Feeder device: dp-118 feed-record decode + local control wrapper."""
import base64
from datetime import datetime

from mw.feeder import decode_feed_record, FakeFeederDevice


def test_decode_feed_record_matches_live_sample():
    # the confirmed live record: 2026-06-22 22:35:40, 1 portion
    rec = decode_feed_record("B+oGFhYjKAECAA==")
    assert rec is not None
    assert rec["portions"] == 1
    expect = datetime(2026, 6, 22, 22, 35, 40).timestamp()
    assert abs(rec["ts"] - expect) < 1


def test_decode_feed_record_rejects_garbage_and_empty():
    assert decode_feed_record("AA==") is None        # 1 byte, too short
    assert decode_feed_record("") is None
    assert decode_feed_record("not base64!!") is None
    # invalid calendar date (month 13) -> None, not a crash
    bad = base64.b64encode(bytes([7, 234, 13, 40, 99, 99, 99, 1, 0, 0])).decode()
    assert decode_feed_record(bad) is None


def test_fake_feeder_device_replays_and_records():
    fake = FakeFeederDevice([
        {"feed_state": "standby", "food_level": "full", "last_feed": None, "online": True},
        {"feed_state": "feeding", "food_level": "full",
         "last_feed": {"ts": 100.0, "portions": 2}, "online": True},
    ])
    assert fake.status()["feed_state"] == "standby"
    assert fake.status()["last_feed"]["portions"] == 2
    assert fake.feed(3) is True
    assert fake.fed == [3]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_feeder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mw.feeder'`.

- [ ] **Step 3: Implement `mw/feeder.py` (decode + device + fake)**

Create `mw/feeder.py`:

```python
"""PLAF103 feeder: local Tuya control + the dp-118 feed-record decode.

Confirmed against the live device (a real test feed). Local control only — the
cloud `manual_feed` returns 1109. NON-persistent socket: a persistent socket
returns partial dp pushes; a fresh status() returns the full dp set we need
(food_level + the dp-118 feed record).
"""
import base64
import sys
import threading
from datetime import datetime

DP_MANUAL_FEED = 3        # write-only: set_value(3, portions) dispenses
DP_FEED_STATE = "4"       # standby | feeding | feed_end
DP_FOOD_LEVEL = "108"     # hopper enum (full | ...)
DP_FEED_RECORD = "118"    # base64 last-feed record (persists)


def decode_feed_record(b64):
    """Decode a dp-118 feed record -> {"ts": epoch, "portions": int}, or None.
    Format (>=8 bytes): [year_hi, year_lo, month, day, hour, min, sec, portions, ...]."""
    if not b64:
        return None
    try:
        b = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if len(b) < 8:
        return None
    year = (b[0] << 8) | b[1]
    try:
        dt = datetime(year, b[2], b[3], b[4], b[5], b[6])
    except ValueError:
        return None
    return {"ts": dt.timestamp(), "portions": b[7]}


class FeederDevice:
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
            self._dev.set_socketTimeout(5)     # NOT persistent (avoids partial pushes)
        return self._dev

    def status(self):
        with self._lock:
            for _ in (1, 2):
                try:
                    data = self._device().status()
                    dps = data.get("dps", {}) if isinstance(data, dict) else {}
                    if dps:
                        rec = dps.get(DP_FEED_RECORD)
                        return {
                            "feed_state": dps.get(DP_FEED_STATE),
                            "food_level": dps.get(DP_FOOD_LEVEL),
                            "last_feed": decode_feed_record(rec) if rec else None,
                            "online": True,
                        }
                except Exception:
                    self._dev = None
            return {"online": False}

    def feed(self, portions):
        with self._lock:
            try:
                self._device().set_value(DP_MANUAL_FEED, int(portions))
                return True
            except Exception as e:
                print(f"[feeder] feed({portions}) failed: {e}", file=sys.stderr)
                self._dev = None
                return False


class FakeFeederDevice:
    """Replays status() snapshots; records feed() portions in .fed."""
    def __init__(self, snapshots):
        self._snaps = list(snapshots)
        self._i = 0
        self.fed = []

    def status(self):
        if self._i < len(self._snaps):
            s = self._snaps[self._i]
            self._i += 1
            return dict(s)
        return dict(self._snaps[-1]) if self._snaps else {"online": False}

    def feed(self, portions):
        self.fed.append(portions)
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_feeder.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/meowant
git add mw/feeder.py tests/test_feeder.py
git commit -m "feat(feeder): FeederDevice local control + dp-118 feed-record decode"
```

---

### Task 3: `FeederMonitor` watchdog

**Files:**
- Modify: `mw/feeder.py` (add `FeederMonitor`)
- Test: `tests/test_feeder.py` (add)

**Interfaces:**
- Consumes: `FeederDevice`/`FakeFeederDevice` (Task 2); `store.log_feed_event`, `store.last_feed_event_ts`, `store.feed_in_window` (Task 1).
- Produces: `feeder.FeederMonitor(device, conn, notify, mealtimes=(), now_fn=time.time, poll_interval_s=120, miss_grace_minutes=30, offline_minutes=30, low_food_levels=("empty","low"), manual_window_s=600)` with `.poll_once()`, `.note_manual_feed()`, `.run()`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_feeder.py`:

```python
from mw import store
from mw.feeder import FeederMonitor

T = 1_000_000.0


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


def test_new_feed_record_is_logged_once(tmp_path):
    conn = _db(tmp_path)
    dev = FakeFeederDevice([
        {"online": True, "food_level": "full", "last_feed": {"ts": T, "portions": 2}},
        {"online": True, "food_level": "full", "last_feed": {"ts": T, "portions": 2}},
    ])
    m = FeederMonitor(dev, conn, notify=lambda x: None, now_fn=lambda: T + 5)
    m.poll_once()
    m.poll_once()                                    # same record -> not re-logged
    rows = store.recent_feed_events(conn)
    assert len(rows) == 1 and rows[0]["portions"] == 2
    assert rows[0]["source"] == "scheduled"          # no manual expectation set


def test_manual_feed_is_labelled_when_expected(tmp_path):
    conn = _db(tmp_path)
    dev = FakeFeederDevice([
        {"online": True, "food_level": "full", "last_feed": {"ts": T, "portions": 1}},
    ])
    m = FeederMonitor(dev, conn, notify=lambda x: None, now_fn=lambda: T + 5)
    m.note_manual_feed()                             # we just commanded a /feed
    m.poll_once()
    assert store.recent_feed_events(conn)[0]["source"] == "manual"


def test_hopper_empty_alerts_once_then_rearms(tmp_path):
    conn = _db(tmp_path)
    dev = FakeFeederDevice([
        {"online": True, "food_level": "empty", "last_feed": None},
        {"online": True, "food_level": "empty", "last_feed": None},
        {"online": True, "food_level": "full", "last_feed": None},
        {"online": True, "food_level": "empty", "last_feed": None},
    ])
    msgs = []
    m = FeederMonitor(dev, conn, notify=msgs.append, now_fn=lambda: T)
    m.poll_once(); m.poll_once()                     # empty, empty -> one alert
    assert len(msgs) == 1 and "hopper" in msgs[0].lower()
    m.poll_once()                                    # full -> re-arm, silent
    m.poll_once()                                    # empty again -> alert
    assert len(msgs) == 2


def test_offline_alerts_after_threshold_and_recovers(tmp_path):
    conn = _db(tmp_path)
    dev = FakeFeederDevice([{"online": False}])
    msgs = []
    clock = [T]
    m = FeederMonitor(dev, conn, notify=msgs.append, now_fn=lambda: clock[0],
                      offline_minutes=30)
    m.poll_once()                                    # first seen offline -> no alert yet
    assert msgs == []
    clock[0] = T + 31 * 60
    m.poll_once()                                    # 31min offline -> alert
    assert len(msgs) == 1 and "unreachable" in msgs[0].lower()
    dev._snaps = [{"online": True, "food_level": "full", "last_feed": None}]
    dev._i = 0
    m.poll_once()                                    # recovered -> re-arm
    clock[0] = T + 100 * 60
    dev._snaps = [{"online": False}]; dev._i = 0
    m.poll_once(); clock[0] = T + 200 * 60; m.poll_once()
    assert len(msgs) == 2                            # alerts again after re-arm


def test_missed_drop_fires_after_grace(tmp_path):
    conn = _db(tmp_path)
    # build "today 07:00" in local epoch
    import time as _t
    lt = _t.localtime(T)
    seven = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 0, 0, 0, 0, -1))
    dev = FakeFeederDevice([{"online": True, "food_level": "full", "last_feed": None}])
    msgs = []
    # now = 07:31, grace 30min -> window closed, no feed logged -> miss
    m = FeederMonitor(dev, conn, notify=msgs.append, now_fn=lambda: seven + 31 * 60,
                      mealtimes=["07:00"], miss_grace_minutes=30)
    m.poll_once()
    assert len(msgs) == 1 and "07:00" in msgs[0] and "missed" in msgs[0].lower()


def test_missed_drop_silent_when_feed_landed(tmp_path):
    conn = _db(tmp_path)
    import time as _t
    lt = _t.localtime(T)
    seven = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 0, 0, 0, 0, -1))
    store.log_feed_event(conn, 2, "scheduled", ts=seven + 60)   # fed at 07:01
    dev = FakeFeederDevice([{"online": True, "food_level": "full", "last_feed": None}])
    msgs = []
    m = FeederMonitor(dev, conn, notify=msgs.append, now_fn=lambda: seven + 31 * 60,
                      mealtimes=["07:00"], miss_grace_minutes=30)
    m.poll_once()
    assert msgs == []                                # drop happened -> no alarm


def test_failed_delivery_does_not_latch_hopper(tmp_path):
    conn = _db(tmp_path)
    dev = FakeFeederDevice([{"online": True, "food_level": "empty", "last_feed": None}])
    sent = []

    def _notify(m):
        sent.append(m)
        return False                                 # transport down

    m = FeederMonitor(dev, conn, notify=_notify, now_fn=lambda: T)
    m.poll_once(); m.poll_once()
    assert len(sent) == 2                            # retried, never latched silent
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/repos/meowant && python -m pytest tests/test_feeder.py -k "Monitor or feed or hopper or offline or missed" -v`
Expected: FAIL — `ImportError: cannot import name 'FeederMonitor'`.

- [ ] **Step 3: Implement `FeederMonitor`**

In `mw/feeder.py`, add at the top alongside the others: `import time` and `from datetime import date` (extend the existing `from datetime import datetime` line to `from datetime import datetime, date`), `from mw import store`. Then append:

```python
class FeederMonitor:
    """Polls the feeder: logs each new dp-118 feed, and runs latched fail-loud
    watchdogs (missed scheduled drop, empty hopper, unreachable). Source of a feed
    is 'manual' if a /feed was issued within manual_window_s, else 'scheduled'."""
    def __init__(self, device, conn, notify, mealtimes=(), now_fn=time.time,
                 poll_interval_s=120, miss_grace_minutes=30, offline_minutes=30,
                 low_food_levels=("empty", "low"), manual_window_s=600):
        self.device = device
        self.conn = conn
        self.notify = notify
        self.mealtimes = list(mealtimes)
        self.now = now_fn
        self.poll_interval_s = poll_interval_s
        self.miss_grace_minutes = miss_grace_minutes
        self.offline_minutes = offline_minutes
        self.low_food_levels = set(low_food_levels)
        self.manual_window_s = manual_window_s
        self._last_logged_feed_ts = store.last_feed_event_ts(conn)  # resume across restarts
        self._offline_since = None
        self._offline_alerted = False
        self._hopper_alerted = False
        self._missed_alerted = set()        # {(date_iso, "HH:MM")}
        self._expect_manual_until = 0

    def note_manual_feed(self):
        """Call right after a successful /feed so the next detected feed is 'manual'."""
        self._expect_manual_until = self.now() + self.manual_window_s

    def _fire(self, msg, latch_attr):
        if getattr(self, latch_attr):
            return
        if self.notify(msg) is not False:
            setattr(self, latch_attr, True)

    def _check_online(self, online):
        now = self.now()
        if online:
            self._offline_since = None
            self._offline_alerted = False
            return
        if self._offline_since is None:
            self._offline_since = now
        elif (not self._offline_alerted
              and (now - self._offline_since) >= self.offline_minutes * 60):
            if self.notify(f"🍽️ Feeder unreachable for {self.offline_minutes}min+ "
                           f"— can't confirm feeding") is not False:
                self._offline_alerted = True

    def _detect_dispense(self, last_feed):
        if not last_feed or last_feed.get("ts") is None:
            return
        ts = last_feed["ts"]
        # +1s guard so an equal stored ts isn't re-logged (float round-trip slack)
        if self._last_logged_feed_ts is not None and ts <= self._last_logged_feed_ts + 1:
            return
        source = "manual" if self.now() <= self._expect_manual_until else "scheduled"
        store.log_feed_event(self.conn, last_feed.get("portions", 0), source, ts=ts)
        self._last_logged_feed_ts = ts
        if source == "manual":
            self._expect_manual_until = 0

    def _check_hopper(self, food_level):
        if food_level is None:
            return
        if food_level in self.low_food_levels:
            self._fire(f"🍽️ Feeder hopper {food_level} — refill soon "
                       f"(cats will run out)", "_hopper_alerted")
        else:
            self._hopper_alerted = False     # back to full -> re-arm

    def _check_missed_drops(self):
        if not self.mealtimes:
            return
        now = self.now()
        lt = time.localtime(now)
        today = "%04d-%02d-%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday)
        for hhmm in self.mealtimes:
            key = (today, hhmm)
            if key in self._missed_alerted:
                continue
            h, m = (int(x) for x in hhmm.split(":"))
            meal = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
            deadline = meal + self.miss_grace_minutes * 60
            if now < deadline:
                continue                     # window still open
            if store.feed_in_window(self.conn, meal, deadline):
                self._missed_alerted.add(key)            # satisfied
            elif self.notify(f"🚨 Feeder MISSED the {hhmm} drop (no dispense by "
                             f"+{self.miss_grace_minutes}min) — check the feeder") is not False:
                self._missed_alerted.add(key)

    def poll_once(self):
        st = self.device.status()
        self._check_online(bool(st.get("online")))
        if not st.get("online"):
            return                           # can't trust other signals when offline
        self._detect_dispense(st.get("last_feed"))
        self._check_hopper(st.get("food_level"))
        self._check_missed_drops()

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:           # never let the feeder thread die
                print(f"[feeder-monitor] error: {e}", file=sys.stderr)
            time.sleep(self.poll_interval_s)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_feeder.py -v`
Expected: PASS (all — Task 2 + Task 3 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/meowant
git add mw/feeder.py tests/test_feeder.py
git commit -m "feat(feeder): FeederMonitor — dispense logging + missed-drop/hopper/offline watchdogs"
```

---

### Task 4: digest feed line + `/feedstatus` text helper

**Files:**
- Modify: `mw/report.py` (extend `digest`; add `feed_status_text`)
- Test: `tests/test_report.py` (add)

**Interfaces:**
- Consumes: `store.feed_events_today`, `store.recent_feed_events` (Task 1).
- Produces: `report.feed_status_text(conn, status) -> str` — a `/feedstatus` reply from a `FeederDevice.status()` dict + the last logged feed.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_report.py`:

```python
def test_digest_includes_feeds_line(tmp_path):
    from mw import store, report
    from datetime import datetime
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    now = datetime.now().timestamp()
    store.log_feed_event(conn, 2, "scheduled", ts=now - 100)
    out = report.digest(conn)
    assert "feed" in out.lower()                       # mentions feeding
    assert "2 portion" in out or "2 meal" in out or "1 feed" in out


def test_feed_status_text(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.log_feed_event(conn, 1, "manual", ts=1_000_000.0)
    status = {"online": True, "feed_state": "standby", "food_level": "full",
              "last_feed": {"ts": 1_000_000.0, "portions": 1}}
    txt = report.feed_status_text(conn, status)
    assert "full" in txt.lower() and ("online" in txt.lower() or "ok" in txt.lower())


def test_feed_status_text_offline(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    txt = report.feed_status_text(conn, {"online": False})
    assert "offline" in txt.lower() or "unreachable" in txt.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_report.py -k "feed" -v`
Expected: FAIL — `AttributeError: ... 'feed_status_text'` and the digest assertion.

- [ ] **Step 3: Extend `digest` and add `feed_status_text`**

In `mw/report.py`, at the END of the `digest` function, change the final `return` so the feeds line is appended. Replace the function's two `return` statements as follows — the no-elim branch:

```python
    if not today_elim:
        base = f"✅ Meowant alive [{today}] — no box uses yet today."
        return base + _feeds_suffix(conn, today)
```

and the main branch:

```python
    return (f"✅ Meowant alive [{today}] — {len(today_elim)} box uses today "
            f"(last {last}). {parts}" + _feeds_suffix(conn, today))
```

Then add these helpers after `digest`:

```python
def _feeds_suffix(conn, today):
    meals, portions = store.feed_events_today(conn, today)
    if not meals:
        return ""
    return f" 🍽️ {meals} feed(s)/{portions} portions."


def feed_status_text(conn, status):
    """Reply for /feedstatus from a FeederDevice.status() dict + last logged feed."""
    if not status.get("online"):
        return "🍽️ Feeder OFFLINE — unreachable on the LAN."
    rows = store.recent_feed_events(conn, limit=1)
    last = rows[0] if rows else None
    last_txt = (f"last feed {last['ts'][5:16].replace('T', ' ')} "
                f"({last['portions']}p, {last['source']})") if last else "no feeds logged"
    return (f"🍽️ Feeder online — state {status.get('feed_state')}, "
            f"hopper {status.get('food_level')}; {last_txt}")
```

(`store` is already imported in `report.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_report.py -k feed -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/meowant
git add mw/report.py tests/test_report.py
git commit -m "feat(feeder): digest feeds line + /feedstatus text helper"
```

---

### Task 5: arg-aware Telegram dispatch + meowantd wiring

**Files:**
- Modify: `mw/telegram_bot.py` (`_dispatch` passes an arg to handlers that accept one)
- Modify: `meowantd.py` (construct `FeederDevice` + `FeederMonitor` thread; `/feed`, `/feedstatus` commands; existing handlers unchanged)
- Test: `tests/test_telegram_bot.py` (add arg-dispatch test), `tests/test_meowantd_wiring.py` (add wiring test)

**Interfaces:**
- Consumes: `feeder.FeederDevice`, `feeder.FeederMonitor` (Tasks 2-3); `report.feed_status_text` (Task 4).
- Produces: arg-aware command handlers; a running feeder thread when `feeder.enabled` + creds present.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_telegram_bot.py`:

```python
def test_dispatch_passes_arg_to_handler_that_accepts_one():
    from mw.telegram_bot import TelegramBot
    seen = []
    bot = TelegramBot("tok", "123", {
        "/feed": lambda arg="": seen.append(arg) or f"fed {arg}",
        "/cats": lambda: "cats",                  # zero-arg still works
    })
    assert bot._dispatch("/feed 3") == "fed 3"
    assert seen == ["3"]
    assert bot._dispatch("/cats") == "cats"        # unchanged contract
```

Add to `tests/test_meowantd_wiring.py`:

```python
def test_feeder_is_wired():
    import inspect, meowantd
    src = inspect.getsource(meowantd)
    assert "FeederMonitor(" in src
    assert "feeder.enabled" in src
    assert "/feed" in src and "/feedstatus" in src
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/repos/meowant && python -m pytest tests/test_telegram_bot.py::test_dispatch_passes_arg_to_handler_that_accepts_one tests/test_meowantd_wiring.py::test_feeder_is_wired -v`
Expected: FAIL — dispatch passes no arg; wiring strings absent.

- [ ] **Step 3: Make `_dispatch` arg-aware**

In `mw/telegram_bot.py`, add `import inspect` at the top (with the other imports). Replace the body of `_dispatch` (lines ~121-129) with:

```python
    def _dispatch(self, text):
        parts = text.split(maxsplit=1)
        cmd = (parts[0].lower() if parts else "").split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        fn = self.handlers.get(cmd)
        try:
            if not fn:
                return self._help()
            takes_arg = len(inspect.signature(fn).parameters) >= 1
            return fn(arg) if takes_arg else fn()
        except Exception as e:               # a broken handler must not kill the bot
            print(f"[telegram] handler {cmd} error: {e}", file=sys.stderr)
            return f"⚠️ {cmd} failed: {e}"
```

- [ ] **Step 4: Wire the feeder into meowantd**

In `meowantd.py`, add the feeder block. Place it after the litterbox `device`/`daemon` are built and after `conn` exists — a clean spot is right before the `app = create_app(...)` line near the end (it doesn't depend on cameras). Insert:

```python
    # Feeder (Phase 1): local Tuya control + dispense logging + watchdogs.
    feeder_monitor = None
    if config.get(cfg, "feeder.enabled", False) and config.get(cfg, "feeder.device_id"):
        from mw.feeder import FeederDevice, FeederMonitor
        feeder_dev = FeederDevice(config.get(cfg, "feeder"))
        feeder_monitor = FeederMonitor(
            feeder_dev, conn, make_notify(lambda k: config.get(cfg, k)),
            mealtimes=config.get(cfg, "feeder.mealtimes", []),
            poll_interval_s=config.get(cfg, "feeder.poll_interval_s", 120),
            miss_grace_minutes=config.get(cfg, "feeder.miss_grace_minutes", 30),
            offline_minutes=config.get(cfg, "feeder.offline_minutes", 30),
            low_food_levels=config.get(cfg, "feeder.low_food_levels", ["empty", "low"]))
        threading.Thread(target=feeder_monitor.run, daemon=True).start()
        print("feeder: local control + dispense logging + watchdogs")
```

Then, in the Telegram command dict (the `bot = TelegramBot(tg_token, tg_chat, { ... })` block), add the two feeder commands **only when the feeder is wired** — insert before the `"/start"` entry:

```python
            **({"/feed": (lambda arg="": _do_feed(feeder_dev, feeder_monitor, arg)),
                "/feedstatus": (lambda: report.feed_status_text(conn, feeder_dev.status()))}
               if feeder_monitor else {}),
```

and add the `_do_feed` helper near the other local helpers in `main()` (e.g. just before `bot = TelegramBot(...)`):

```python
        def _do_feed(dev, monitor, arg):
            try:
                n = int(arg) if arg.strip() else 1
            except ValueError:
                return "Usage: /feed <portions 1-50>"
            n = max(1, min(50, n))
            if dev.feed(n):
                monitor.note_manual_feed()       # so the poll labels it 'manual'
                return f"🍽️ Dispensed {n} portion(s)."
            return "⚠️ Feed command failed (feeder unreachable?)."
```

Update the `/start` help text to mention the new commands (append ` /feed /feedstatus`).

(Note: `feeder_dev`/`feeder_monitor` are defined in `main()`'s scope before the Telegram block, so the lambdas capture them. The feeder block above must run BEFORE the `if tg_token and tg_chat:` Telegram block — place it accordingly.)

- [ ] **Step 5: Run tests + full suite**

Run: `cd ~/repos/meowant && python -m pytest -q`
Expected: PASS — all prior tests plus the new dispatch + wiring tests.

- [ ] **Step 6: Commit**

```bash
cd ~/repos/meowant
git add mw/telegram_bot.py meowantd.py tests/test_telegram_bot.py tests/test_meowantd_wiring.py
git commit -m "feat(feeder): /feed + /feedstatus commands + meowantd wiring (Phase 1 complete)"
```

---

## Self-Review

**1. Spec coverage** (against `2026-06-22-feeder-phase1-design.md`):
- Local control (dp 3, v3.4, non-persistent) → Task 2 `FeederDevice` ✓
- Reliable dispense logging via dp-118 decode → Tasks 2 (decode) + 3 (`_detect_dispense`) ✓
- Hopper-empty (dp 108), unreachable, missed-drop alerts (latched, fail-loud) → Task 3 ✓
- `feed_events` table → Task 1 ✓
- `/feed`, `/feedstatus`, digest feeds line → Tasks 4-5 ✓
- Config `feeder` block (already written) consumed → Task 5 wiring ✓
- Out of scope (bowl camera, system-managed schedule, battery) → not present ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step is complete.

**3. Type consistency:** `status()` dict keys (`feed_state`/`food_level`/`last_feed`/`online`) are produced in Task 2 and consumed identically in Tasks 3-5; `decode_feed_record -> {"ts","portions"}` matches `_detect_dispense` and `feed_status_text` usage; `log_feed_event(conn, portions, source, ts=)` and `feed_in_window(conn, start_epoch, end_epoch)` are used consistently; the `notify(msg) is not False` latch idiom is uniform.

**Executor notes:**
- `_missed_alerted` accumulates one entry per (date, mealtime) for the daemon's lifetime — negligible; no pruning needed.
- The dp-118 timestamp is decoded as the daemon host's local time (`datetime(...).timestamp()`); device and host share the America/Chicago tz, so this is consistent. If a real feed ever logs with a skewed time, revisit the tz assumption.
- Task 5 ordering matters: build the feeder block (defining `feeder_dev`/`feeder_monitor`) BEFORE the Telegram `bot = ...` block so the command lambdas can capture them.
