"""Feeder device: dp-118 feed-record decode + local control wrapper."""
import base64
from datetime import datetime

from mw.feeder import decode_feed_record, FakeFeederDevice
from mw import store
from mw.feeder import FeederMonitor

T = 1_000_000.0


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
