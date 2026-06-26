"""Phase 3b: GET/POST /config. GET never leaks secrets; POST validates+writes
then triggers the (injected) daemon reload; invalid edits 400 and change nothing."""
import json

from mw import store
from mw.device import FakeDevice
from mw.daemon import Daemon
from mw.smartclean import SmartClean
from mw.api import create_app

CFG = {
    "device_id": "SECRET_DEV", "local_key": "SECRET_KEY",
    "quiet_start": "22:00", "quiet_end": "08:00",
    "smartclean": {"enabled": False, "idle_seconds": 60, "max_wait_seconds": 240},
    "feeders": [{"label": "downstairs", "local_key": "FK", "mealtimes": ["08:00", "19:00"]}],
    "thresholds": {"Ucok": 8},
}


def _app(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    p = tmp_path / "config.json"; p.write_text(json.dumps(CFG))
    reloads = []
    dev = FakeDevice([{"24": "standby"}])
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0); d.tick()
    app = create_app(d, conn, config_path=str(p), reload_fn=lambda: reloads.append(1))
    return app.test_client(), str(p), reloads


def test_get_config_excludes_secrets(tmp_path):
    c, _, _ = _app(tmp_path)
    body = c.get("/config")
    assert body.status_code == 200
    blob = body.get_data(as_text=True)
    assert "SECRET_DEV" not in blob and "SECRET_KEY" not in blob and "FK" not in blob
    d = body.get_json()
    assert d["quiet_start"] == "22:00"
    assert d["thresholds"] == {"Ucok": 8, "Ella": 24, "Garfield": 24}


def test_post_config_valid_writes_and_reloads(tmp_path):
    c, path, reloads = _app(tmp_path)
    r = c.post("/config", json={"quiet_start": "23:00", "thresholds": {"Ucok": 6}})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    on_disk = json.loads(open(path).read())
    assert on_disk["quiet_start"] == "23:00" and on_disk["thresholds"]["Ucok"] == 6
    assert on_disk["device_id"] == "SECRET_DEV"        # secrets preserved
    assert reloads == [1]                              # reload triggered exactly once


def test_post_config_invalid_400_no_reload_no_change(tmp_path):
    c, path, reloads = _app(tmp_path)
    before = open(path).read()
    r = c.post("/config", json={"quiet_start": "25:99", "device_id": "hack"})
    assert r.status_code == 400
    assert "error" in r.get_json()
    assert open(path).read() == before                # unchanged
    assert reloads == []                              # NOT reloaded on invalid edit
