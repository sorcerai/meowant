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

def test_uses_today_from_our_tracking_not_dp7(tmp_path):
    import time
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    # box claims dp7=1, but we logged 2 real eliminations today
    dev = FakeDevice([{"24": "standby", "7": 1}])
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0); d.tick()
    now = time.time()
    for _ in range(2):
        v = store.open_visit(conn, now); store.mark_elimination(conn, v, 100); store.close_visit(conn, v, now + 30, 30)
    st = create_app(d, conn).test_client().get("/state").get_json()
    assert st["uses_today"] == 2          # our count
    assert st["uses_today_dp7"] == 1      # box's (wrong) claim kept for reference


def test_sleep_and_quiet_and_named(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    dev = FakeDevice([{"24": "standby", "11": 1320, "12": 480}])
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0)
    d.tick()
    app = create_app(d, conn).test_client()

    st = app.get("/state").get_json()
    assert st["named"]["status"] == "standby"

    assert app.post("/command", json={"action": "sleep", "value": True}).get_json()["ok"] is True
    assert (10, True) in dev.set_values

    r = app.post("/command", json={"action": "quiet", "value": {"start": "22:00", "end": "08:00"}})
    assert r.get_json()["ok"] is True
    assert (11, 1320) in dev.set_values and (12, 480) in dev.set_values

    assert app.post("/command", json={"action": "quiet", "value": "nope"}).status_code == 400


def test_autoclean_command(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    dev = FakeDevice([{"24": "standby"}])
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0)
    d.tick()
    app = create_app(d, conn).test_client()

    r = app.post("/command", json={"action": "autoclean", "value": True})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert dev.set_values == [(4, True)]

def test_delay_command(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    dev = FakeDevice([{"24": "standby"}])
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0)
    d.tick()
    app = create_app(d, conn).test_client()

    r = app.post("/command", json={"action": "delay", "value": 30})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert dev.set_values == [(5, 30)]

def test_command_bad_input_returns_400(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    dev = FakeDevice([{"24": "standby"}])
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0)
    d.tick()
    app = create_app(d, conn).test_client()

    # delay without a value -> 400 not 500
    r = app.post("/command", json={"action": "delay"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False

    # delay with non-integer value -> 400 not 500
    r = app.post("/command", json={"action": "delay", "value": "oops"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False

def test_manual_clean_disarms_smartclean(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    dev = FakeDevice([{"24": "cat_get_in"}])
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0)
    d.tick()  # baseline arms smartclean
    assert d.smartclean._armed is True
    app = create_app(d, conn).test_client()

    r = app.post("/command", json={"action": "clean"})
    assert r.get_json()["ok"] is True
    assert d.smartclean._armed is False  # notify_cleaned ran

def test_command_delay_bad_value_is_400(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    dev = FakeDevice([{"24": "standby"}])
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0)
    d.tick()
    app = create_app(d, conn).test_client()

    r = app.post("/command", json={"action": "delay", "value": "abc"})
    assert r.status_code == 400
