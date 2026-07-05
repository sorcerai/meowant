"""Tests for cloud meal_plan -> feeder.mealtimes auto-sync."""
import json

from mw.feed_plan_sync import decode_meal_plan, enabled_mealtimes, FeedPlanSync

DOWNSTAIRS_B64 = "fwoAAQF/DAABAX8RAAEBfxQAAQB/CAABAQ=="
UPSTAIRS_B64 = "fwgAAQF/CwABAX8RAAEBfxMAAQF/FAABAA=="


def test_decode_meal_plan_downstairs_fixture():
    recs = decode_meal_plan(DOWNSTAIRS_B64)
    assert recs == [
        {"time": "10:00", "portions": 1, "enabled": True, "days": 127},
        {"time": "12:00", "portions": 1, "enabled": True, "days": 127},
        {"time": "17:00", "portions": 1, "enabled": True, "days": 127},
        {"time": "20:00", "portions": 1, "enabled": False, "days": 127},
        {"time": "08:00", "portions": 1, "enabled": True, "days": 127},
    ]


def test_decode_meal_plan_upstairs_fixture():
    recs = decode_meal_plan(UPSTAIRS_B64)
    assert recs == [
        {"time": "08:00", "portions": 1, "enabled": True, "days": 127},
        {"time": "11:00", "portions": 1, "enabled": True, "days": 127},
        {"time": "17:00", "portions": 1, "enabled": True, "days": 127},
        {"time": "19:00", "portions": 1, "enabled": True, "days": 127},
        {"time": "20:00", "portions": 1, "enabled": False, "days": 127},
    ]


def test_decode_meal_plan_handles_bad_input():
    assert decode_meal_plan(None) == []
    assert decode_meal_plan("") == []
    assert decode_meal_plan(b"garbage") == []
    assert decode_meal_plan("AA==") == []      # 1 byte, too short for a record
    assert decode_meal_plan("not base64!!") == []


def test_enabled_mealtimes_downstairs():
    assert enabled_mealtimes(DOWNSTAIRS_B64) == ["08:00", "10:00", "12:00", "17:00"]


def test_enabled_mealtimes_upstairs():
    assert enabled_mealtimes(UPSTAIRS_B64) == ["08:00", "11:00", "17:00", "19:00"]


def test_enabled_mealtimes_bad_input_is_empty():
    assert enabled_mealtimes(None) == []
    assert enabled_mealtimes("") == []


class FakeMonitor:
    def __init__(self, label, mealtimes, stale_key=None):
        self.label = label
        self.mealtimes = list(mealtimes)
        self._missed_alerted = {stale_key} if stale_key else set()


def _write_config(tmp_path, feeders):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"feeders": feeders}))
    return str(path)


def test_sync_once_updates_monitor_and_config_and_notifies(tmp_path):
    cfg_path = _write_config(tmp_path, [
        {"label": "downstairs", "device_id": "dev1", "mealtimes": ["10:00", "12:00", "17:00", "08:00"]},
    ])
    mon = FakeMonitor("downstairs", ["10:00", "12:00", "17:00", "08:00"],
                       stale_key=("2026-06-30", "20:00"))
    notified = []
    sync = FeedPlanSync(
        fetch_plan=lambda device_id: DOWNSTAIRS_B64,
        feeders=[{"label": "downstairs", "device_id": "dev1"}],
        monitors={"downstairs": mon},
        notify=notified.append,
        config_path=cfg_path,
    )
    changed = sync.sync_once()
    assert changed == 1
    assert mon.mealtimes == ["08:00", "10:00", "12:00", "17:00"]
    assert mon._missed_alerted == set()          # cleared so stale/new times aren't suppressed
    assert len(notified) == 1
    assert "downstairs" in notified[0]
    assert "08:00" in notified[0] and "10:00" in notified[0]

    on_disk = json.loads(open(cfg_path).read())
    assert on_disk["feeders"][0]["mealtimes"] == ["08:00", "10:00", "12:00", "17:00"]


def test_sync_once_no_change_does_not_notify_or_write(tmp_path):
    current = ["08:00", "10:00", "12:00", "17:00"]
    cfg_path = _write_config(tmp_path, [
        {"label": "downstairs", "device_id": "dev1", "mealtimes": current},
    ])
    mon = FakeMonitor("downstairs", current)
    notified = []
    writes = []
    sync = FeedPlanSync(
        fetch_plan=lambda device_id: DOWNSTAIRS_B64,
        feeders=[{"label": "downstairs", "device_id": "dev1"}],
        monitors={"downstairs": mon},
        notify=notified.append,
        config_path=cfg_path,
    )
    sync._persist = lambda label, mealtimes: writes.append((label, mealtimes))
    changed = sync.sync_once()
    assert changed == 0
    assert notified == []
    assert writes == []
    assert mon.mealtimes == current


def test_sync_once_none_or_empty_read_does_not_wipe_schedule(tmp_path):
    current = ["08:00", "10:00", "12:00", "17:00"]
    cfg_path = _write_config(tmp_path, [
        {"label": "downstairs", "device_id": "dev1", "mealtimes": current},
    ])

    for bad_value in (None, ""):
        mon = FakeMonitor("downstairs", current)
        notified = []
        sync = FeedPlanSync(
            fetch_plan=lambda device_id: bad_value,
            feeders=[{"label": "downstairs", "device_id": "dev1"}],
            monitors={"downstairs": mon},
            notify=notified.append,
            config_path=cfg_path,
        )
        changed = sync.sync_once()
        assert changed == 0
        assert mon.mealtimes == current
        assert notified == []


def test_sync_once_isolates_per_feeder_errors(tmp_path):
    cfg_path = _write_config(tmp_path, [
        {"label": "downstairs", "device_id": "dev1", "mealtimes": ["07:00"]},
        {"label": "upstairs", "device_id": "dev2", "mealtimes": ["08:00", "11:00", "17:00", "19:00"]},
    ])
    mon_down = FakeMonitor("downstairs", ["07:00"])
    mon_up = FakeMonitor("upstairs", ["07:00"])     # stale -> must still update despite dev1's error

    def fetch_plan(device_id):
        if device_id == "dev1":
            raise RuntimeError("cloud timeout")
        return UPSTAIRS_B64

    notified = []
    sync = FeedPlanSync(
        fetch_plan=fetch_plan,
        feeders=[
            {"label": "downstairs", "device_id": "dev1"},
            {"label": "upstairs", "device_id": "dev2"},
        ],
        monitors={"downstairs": mon_down, "upstairs": mon_up},
        notify=notified.append,
        config_path=cfg_path,
    )
    changed = sync.sync_once()          # must not raise
    assert changed == 1                 # upstairs still syncs despite downstairs's fetch error
    assert mon_up.mealtimes == ["08:00", "11:00", "17:00", "19:00"]
    assert mon_down.mealtimes == ["07:00"]     # untouched after the fetch error
    assert len(notified) == 1 and "upstairs" in notified[0]


def test_sync_once_skips_feeders_without_device_id_or_monitor(tmp_path):
    cfg_path = _write_config(tmp_path, [
        {"label": "downstairs", "device_id": None, "mealtimes": ["07:00"]},
        {"label": "unmonitored", "device_id": "dev9", "mealtimes": ["07:00"]},
    ])
    calls = []

    def fetch_plan(device_id):
        calls.append(device_id)
        return DOWNSTAIRS_B64

    sync = FeedPlanSync(
        fetch_plan=fetch_plan,
        feeders=[
            {"label": "downstairs", "device_id": None},
            {"label": "unmonitored", "device_id": "dev9"},
        ],
        monitors={"downstairs": FakeMonitor("downstairs", ["07:00"])},
        notify=lambda m: None,
        config_path=cfg_path,
    )
    changed = sync.sync_once()
    assert changed == 0
    assert calls == []                  # neither feeder was even fetched


def test_run_loop_survives_sync_once_exception_and_sleeps(monkeypatch):
    sync = FeedPlanSync(
        fetch_plan=lambda device_id: None,
        feeders=[],
        monitors={},
        notify=lambda m: None,
        config_path="/nonexistent/config.json",
        interval_s=0,
    )

    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise SystemExit
        raise RuntimeError("boom")

    sync.sync_once = boom
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    try:
        sync.run()
    except SystemExit:
        pass
    assert calls["n"] == 2
    assert slept == [0]
