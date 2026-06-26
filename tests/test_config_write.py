"""Phase 3b: config_write is the safety core for the Settings panel. It must
(1) never expose secrets to the frontend, (2) only accept the editable allowlist,
(3) validate every field, (4) preserve secrets + unknown keys on write, and
(5) write atomically so a bad save can't brick the daemon's config."""
import json
import pytest

from mw import config_write


def _cfg(tmp_path):
    cfg = {
        "device_id": "SECRET_DEV", "local_key": "SECRET_KEY", "address": "192.168.2.75",
        "cloud": {"api_id": "S", "api_secret": "S"},
        "quiet_start": "22:00", "quiet_end": "08:00",
        "smartclean": {"enabled": False, "idle_seconds": 60, "max_wait_seconds": 240},
        "feeders": [
            {"label": "downstairs", "device_id": "FD_SECRET", "local_key": "FK_SECRET",
             "mealtimes": ["08:00", "14:00", "19:00"], "poll_interval_s": 120},
        ],
        "thresholds": {"Ucok": 8},
    }
    p = tmp_path / "config.json"; p.write_text(json.dumps(cfg))
    return str(p), cfg


def test_read_safe_excludes_secrets(tmp_path):
    _, cfg = _cfg(tmp_path)
    safe = config_write.read_safe(cfg)
    blob = json.dumps(safe)
    for secret in ("SECRET_DEV", "SECRET_KEY", "FD_SECRET", "FK_SECRET", "192.168.2.75"):
        assert secret not in blob, f"{secret} leaked into safe config"


def test_read_safe_shape(tmp_path):
    _, cfg = _cfg(tmp_path)
    safe = config_write.read_safe(cfg)
    assert safe["quiet_start"] == "22:00" and safe["quiet_end"] == "08:00"
    assert safe["smartclean"] == {"enabled": False, "idle_seconds": 60}
    assert safe["feeders"] == [{"label": "downstairs", "mealtimes": ["08:00", "14:00", "19:00"]}]
    # thresholds merge code defaults so the panel always shows every cat
    assert safe["thresholds"] == {"Ucok": 8, "Ella": 24, "Garfield": 24}


def test_apply_edits_preserves_secrets_and_writes_change(tmp_path):
    path, _ = _cfg(tmp_path)
    config_write.apply_edits(path, {"quiet_start": "23:00", "quiet_end": "06:00"})
    on_disk = json.loads(open(path).read())
    assert on_disk["quiet_start"] == "23:00" and on_disk["quiet_end"] == "06:00"
    assert on_disk["device_id"] == "SECRET_DEV"           # secrets untouched
    assert on_disk["local_key"] == "SECRET_KEY"
    assert on_disk["feeders"][0]["local_key"] == "FK_SECRET"


def test_apply_edits_feeder_mealtimes_by_label_keeps_device(tmp_path):
    path, _ = _cfg(tmp_path)
    config_write.apply_edits(path, {"feeders": [{"label": "downstairs", "mealtimes": ["09:00", "18:00"]}]})
    fd = json.loads(open(path).read())["feeders"][0]
    assert fd["mealtimes"] == ["09:00", "18:00"]
    assert fd["device_id"] == "FD_SECRET" and fd["poll_interval_s"] == 120   # preserved


def test_apply_edits_thresholds_and_smartclean(tmp_path):
    path, _ = _cfg(tmp_path)
    config_write.apply_edits(path, {"thresholds": {"Ucok": 6}, "smartclean": {"idle_seconds": 90}})
    d = json.loads(open(path).read())
    assert d["thresholds"]["Ucok"] == 6
    assert d["smartclean"]["idle_seconds"] == 90
    assert d["smartclean"]["max_wait_seconds"] == 240    # untouched sub-key preserved


@pytest.mark.parametrize("bad", [
    {"quiet_start": "25:00"},
    {"quiet_end": "8am"},
    {"smartclean": {"idle_seconds": -5}},
    {"smartclean": {"enabled": "yes"}},
    {"thresholds": {"Ucok": 0}},
    {"thresholds": {"Ucok": -3}},
    {"feeders": [{"label": "downstairs", "mealtimes": ["08:00", "99:99"]}]},
    {"feeders": [{"label": "nonexistent", "mealtimes": ["08:00"]}]},
    {"feeders": [{"label": "downstairs", "mealtimes": []}]},  # empty -> would zero all feedings
    {"smartclean": {"max_wait_seconds": 1}},                  # unknown sub-key (clobber guard)
    {"smartclean": {"idle_seconds": 300.5}},                  # float, not int
    {},                                                       # empty edit -> needless restart
    {"device_id": "hacked"},          # secret / non-allowlisted key
    {"cameras": []},                  # non-allowlisted key
])
def test_apply_edits_rejects_invalid(tmp_path, bad):
    path, original = _cfg(tmp_path)
    before = open(path).read()
    with pytest.raises(ValueError):
        config_write.apply_edits(path, bad)
    assert open(path).read() == before, "config changed despite invalid edit (not atomic)"
