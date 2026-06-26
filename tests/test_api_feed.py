from mw import store, api

class _Feeder:
    def __init__(self): self.fed = []
    def feed(self, n): self.fed.append(n); return True

class _FailFeeder:
    def feed(self, n): return False

class _Monitor:
    def __init__(self): self.manual_feeds = 0
    def note_manual_feed(self): self.manual_feeds += 1

class _Dev:
    state = {"dps": {}}; last_ok_ts = None; device = None; smartclean = None

def _client(tmp_path, feeders, monitors=None):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    return api.create_app(_Dev(), conn, feeders=feeders, monitors=monitors).test_client()

def test_feed_dispatches_to_named_feeder(tmp_path):
    f = _Feeder()
    c = _client(tmp_path, {"downstairs": f})
    r = c.post("/command", json={"action": "feed", "feeder": "downstairs", "portions": 2})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert f.fed == [2]

def test_feed_unknown_feeder_400(tmp_path):
    c = _client(tmp_path, {"downstairs": _Feeder()})
    r = c.post("/command", json={"action": "feed", "feeder": "nope", "portions": 1})
    assert r.status_code == 400 and r.get_json()["ok"] is False

def test_feed_clamps_and_validates_portions(tmp_path):
    f = _Feeder(); c = _client(tmp_path, {"downstairs": f})
    r = c.post("/command", json={"action": "feed", "feeder": "downstairs", "portions": 99})
    assert r.status_code == 200 and f.fed == [10]   # clamped to max 10

def test_feed_calls_note_manual_feed_on_success(tmp_path):
    f = _Feeder()
    m = _Monitor()
    c = _client(tmp_path, {"downstairs": f}, monitors={"downstairs": m})
    r = c.post("/command", json={"action": "feed", "feeder": "downstairs", "portions": 1})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert m.manual_feeds == 1, "note_manual_feed() must be called exactly once on success"

def test_feed_no_monitors_no_crash(tmp_path):
    f = _Feeder()
    c = _client(tmp_path, {"downstairs": f}, monitors=None)
    r = c.post("/command", json={"action": "feed", "feeder": "downstairs", "portions": 1})
    assert r.status_code == 200 and r.get_json()["ok"] is True

def test_feed_note_manual_feed_not_called_on_failure(tmp_path):
    m = _Monitor()
    c = _client(tmp_path, {"downstairs": _FailFeeder()}, monitors={"downstairs": m})
    r = c.post("/command", json={"action": "feed", "feeder": "downstairs", "portions": 1})
    assert r.status_code == 500 and r.get_json()["ok"] is False
    assert m.manual_feeds == 0, "note_manual_feed() must NOT be called when feed() returns falsy"
