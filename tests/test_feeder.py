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
