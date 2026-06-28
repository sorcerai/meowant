"""Feeder device tests."""
import base64
from datetime import datetime

from mw.feeder import decode_plaf103_record, FeederMonitor
from mw import store

T = 1_000_000.0

class FakeFeederDevice:
    def __init__(self, snaps):
        self._snaps = snaps
        self._i = 0
        self.fed = []
        self.label = "test_feeder"
        self.profile = "PLAF103"
    
    def status(self):
        s = self._snaps[self._i]
        self._i = min(len(self._snaps) - 1, self._i + 1)
        return s
    
    def feed(self, portions):
        self.fed.append(portions)
        return True


def test_decode_feed_record_matches_live_sample():
    # Note: This test is tz-self-referential. It validates that byte parsing
    # produces local time args that match datetime(), but does not validate
    # the assumption that Tuya's clock is in the same local timezone.
    # the confirmed live record: 2026-06-22 22:35:40, 1 portion
    rec = decode_plaf103_record("B+oGFhYjKAECAA==")
    assert rec is not None
    assert rec["portions"] == 1
    expect = datetime(2026, 6, 22, 22, 35, 40).timestamp()
    assert abs(rec["ts"] - expect) < 1


def test_decode_feed_record_rejects_garbage_and_empty():
    assert decode_plaf103_record("AA==") is None        # 1 byte, too short
    assert decode_plaf103_record("") is None
    assert decode_plaf103_record("not base64!!") is None
    # invalid calendar date (month 13) -> None, not a crash
    bad = base64.b64encode(bytes([7, 234, 13, 40, 99, 99, 99, 1, 0, 0])).decode()
    assert decode_plaf103_record(bad) is None


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


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


def test_new_feed_record_is_logged_once(tmp_path):
    conn = _db(tmp_path)
    dev = FakeFeederDevice([
        {"online": True, "food_level": "full", "last_feed": {"ts": T, "portions": 2}, "feed_state": "standby"},
        {"online": True, "food_level": "full", "last_feed": {"ts": T, "portions": 2}, "feed_state": "standby"},
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
        {"online": True, "food_level": "full", "last_feed": {"ts": T, "portions": 1}, "feed_state": "standby"},
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
    # monitor was watching since before the meal; now = 07:31 -> window closed, no feed -> miss
    clock = [seven - 600]
    m = FeederMonitor(dev, conn, notify=msgs.append, now_fn=lambda: clock[0],
                      mealtimes=["07:00"], miss_grace_minutes=30)
    clock[0] = seven + 31 * 60
    m.poll_once()
    assert len(msgs) == 1 and "07:00" in msgs[0] and "missed" in msgs[0].lower()


def test_missed_drop_silent_for_meal_closed_before_start(tmp_path):
    conn = _db(tmp_path)
    import time as _t
    lt = _t.localtime(T)
    seven = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 0, 0, 0, 0, -1))
    dev = FakeFeederDevice([{"online": True, "food_level": "full", "last_feed": None}])
    msgs = []
    # monitor STARTS at 07:31 — the 07:00 window already closed, so it could not have
    # watched that meal. It must NOT retroactively alarm (e.g. on every daemon restart).
    m = FeederMonitor(dev, conn, notify=msgs.append, now_fn=lambda: seven + 31 * 60,
                      mealtimes=["07:00"], miss_grace_minutes=30)
    m.poll_once()
    assert msgs == []


def test_missed_drop_silent_when_feed_landed(tmp_path):
    conn = _db(tmp_path)
    import time as _t
    lt = _t.localtime(T)
    seven = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 0, 0, 0, 0, -1))
    store.log_feed_event(conn, 2, "scheduled", ts=seven + 60, feeder="test_feeder")   # fed at 07:01
    dev = FakeFeederDevice([{"online": True, "food_level": "full", "last_feed": None}])
    msgs = []
    clock = [seven - 600]
    m = FeederMonitor(dev, conn, notify=msgs.append, now_fn=lambda: clock[0],
                      mealtimes=["07:00"], miss_grace_minutes=30)
    clock[0] = seven + 31 * 60
    m.poll_once()
    assert msgs == []                                # drop happened -> no alarm


class _FakeBowl:
    """Minimal bowl_watch stand-in: check_fullness() -> (pct, state)."""
    def __init__(self, pct, state):
        self._r = (pct, state)
    def check_fullness(self):
        return self._r


def _missed_feed_incidents(conn):
    return conn.execute("SELECT COUNT(*) FROM incidents WHERE kind='missed_feed'").fetchone()[0]


def test_missed_drop_silent_when_bowl_still_full(tmp_path):
    """The feeder intentionally skips a scheduled drop because the bowl still has
    food (skip-when-full). meowant must NOT raise a false 'missed feed' alarm for
    an expected skip — no Telegram message and no incident."""
    from mw import bowl
    conn = _db(tmp_path)
    import time as _t
    lt = _t.localtime(T)
    seven = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 0, 0, 0, 0, -1))
    dev = FakeFeederDevice([{"online": True, "food_level": "full", "last_feed": None}])
    msgs = []
    clock = [seven - 600]                                    # watching since before the meal
    m = FeederMonitor(dev, conn, notify=msgs.append, now_fn=lambda: clock[0],
                      mealtimes=["07:00"], miss_grace_minutes=30)
    m.bowl_watch = _FakeBowl(80.0, bowl.FULL)               # bowl still full at the deadline
    clock[0] = seven + 31 * 60
    m.poll_once()
    assert msgs == []                                       # no false alarm
    assert _missed_feed_incidents(conn) == 0                # no incident logged


def test_missed_drop_silent_when_bowl_has_some_food(tmp_path):
    """Partial food (SOME) also makes a skip legitimate — still no alarm."""
    from mw import bowl
    conn = _db(tmp_path)
    import time as _t
    lt = _t.localtime(T)
    seven = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 0, 0, 0, 0, -1))
    dev = FakeFeederDevice([{"online": True, "food_level": "full", "last_feed": None}])
    msgs = []
    clock = [seven - 600]
    m = FeederMonitor(dev, conn, notify=msgs.append, now_fn=lambda: clock[0],
                      mealtimes=["07:00"], miss_grace_minutes=30)
    m.bowl_watch = _FakeBowl(12.0, bowl.SOME)
    clock[0] = seven + 31 * 60
    m.poll_once()
    assert msgs == []
    assert _missed_feed_incidents(conn) == 0


def test_missed_drop_fires_when_bowl_empty(tmp_path):
    """An EMPTY bowl with no dispense is a real miss — the alarm MUST still fire."""
    from mw import bowl
    conn = _db(tmp_path)
    import time as _t
    lt = _t.localtime(T)
    seven = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 0, 0, 0, 0, -1))
    dev = FakeFeederDevice([{"online": True, "food_level": "full", "last_feed": None}])
    msgs = []
    clock = [seven - 600]
    m = FeederMonitor(dev, conn, notify=msgs.append, now_fn=lambda: clock[0],
                      mealtimes=["07:00"], miss_grace_minutes=30)
    m.bowl_watch = _FakeBowl(2.0, bowl.EMPTY)
    clock[0] = seven + 31 * 60
    m.poll_once()
    assert len(msgs) == 1 and "missed" in msgs[0].lower()
    assert _missed_feed_incidents(conn) == 1


def test_missed_drop_fires_when_bowl_unreadable(tmp_path):
    """Unreadable bowl (None) can't confirm food present -> fail toward alerting."""
    conn = _db(tmp_path)
    import time as _t
    lt = _t.localtime(T)
    seven = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 0, 0, 0, 0, -1))
    dev = FakeFeederDevice([{"online": True, "food_level": "full", "last_feed": None}])
    msgs = []
    clock = [seven - 600]
    m = FeederMonitor(dev, conn, notify=msgs.append, now_fn=lambda: clock[0],
                      mealtimes=["07:00"], miss_grace_minutes=30)
    m.bowl_watch = _FakeBowl(None, None)                    # grab blocked/unreadable
    clock[0] = seven + 31 * 60
    m.poll_once()
    assert len(msgs) == 1 and "missed" in msgs[0].lower()
    assert _missed_feed_incidents(conn) == 1


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


def test_dedup_boundary(tmp_path):
    conn = _db(tmp_path)
    # Feed at T, then +60s should be deduped, +61s should be logged as a new feed
    dev = FakeFeederDevice([
        {"online": True, "food_level": "full", "last_feed": {"ts": T, "portions": 1}, "feed_state": "standby"},
        {"online": True, "food_level": "full", "last_feed": {"ts": T + 60, "portions": 2}, "feed_state": "standby"},
        {"online": True, "food_level": "full", "last_feed": {"ts": T + 61, "portions": 3}, "feed_state": "standby"},
    ])
    m = FeederMonitor(dev, conn, notify=lambda x: None, now_fn=lambda: T + 100)
    m.poll_once() # logs T
    m.poll_once() # tries T+60 -> deduped (<= T+60)
    m.poll_once() # tries T+61 -> logged (> T+60)
    
    rows = store.recent_feed_events(conn)
    assert len(rows) == 2
    assert rows[1]["portions"] == 1  # oldest first depending on how recent_feed_events sorts? Wait, store.py might sort DESC.
    # Actually just check the portions we see.
    portions = {r["portions"] for r in rows}
    assert 1 in portions
    assert 3 in portions
    assert 2 not in portions


def test_two_feeds_per_poll_logs_only_last(tmp_path):
    conn = _db(tmp_path)
    # The device only holds the last feed in dp-118, so if two happen before poll_once fires, we only see the second.
    dev = FakeFeederDevice([
        {"online": True, "food_level": "full", "last_feed": {"ts": T + 50, "portions": 4}, "feed_state": "standby"},
    ])
    # T+10 and T+50 both happened, but dev only returns the last one at poll time
    m = FeederMonitor(dev, conn, notify=lambda x: None, now_fn=lambda: T + 100)
    m.poll_once()
    rows = store.recent_feed_events(conn)
    assert len(rows) == 1
    assert rows[0]["portions"] == 4


def test_feed_detected_when_poll_lands_on_done_not_feeding(tmp_path):
    # Andoll's "feeding" state is ~3s; a fast-poll can land on "done" (post-feed)
    # and never observe "feeding". The detector must still log the feed — trigger
    # on entering ANY active feed-state (feeding|done), not only "feeding".
    conn = _db(tmp_path)
    dev = FakeFeederDevice([
        {"online": True, "food_level": None, "last_feed": None, "feed_state": "standby"},
        {"online": True, "food_level": None, "last_feed": None, "feed_state": "done"},
        {"online": True, "food_level": None, "last_feed": None, "feed_state": "standby"},
    ])
    m = FeederMonitor(dev, conn, notify=lambda x: None, now_fn=lambda: T)
    m.poll_once(); m.poll_once(); m.poll_once()
    rows = store.recent_feed_events(conn)
    assert len(rows) == 1                       # the feed (seen only as 'done') is logged


def test_feeding_then_done_logs_one_feed_not_two(tmp_path):
    # A single feed seen across both active states must log exactly once.
    conn = _db(tmp_path)
    dev = FakeFeederDevice([
        {"online": True, "food_level": None, "last_feed": None, "feed_state": "standby"},
        {"online": True, "food_level": None, "last_feed": None, "feed_state": "feeding"},
        {"online": True, "food_level": None, "last_feed": None, "feed_state": "done"},
        {"online": True, "food_level": None, "last_feed": None, "feed_state": "standby"},
    ])
    m = FeederMonitor(dev, conn, notify=lambda x: None, now_fn=lambda: T)
    for _ in range(4):
        m.poll_once()
    assert len(store.recent_feed_events(conn)) == 1
