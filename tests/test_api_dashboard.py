"""Tests for GET /cats dashboard endpoint (Task 2)."""
from mw import store, api
from mw.events import Event, BIN_CLEAR, BIN_FULL, CLEAN_DONE


def _app(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])

    class _Dev:  # minimal daemon stub
        state = {"dps": {}}
        last_ok_ts = None
        device = None
        smartclean = None

    app = api.create_app(_Dev(), conn)
    return app.test_client(), conn


def test_cats_endpoint_shape(tmp_path):
    client, conn = _app(tmp_path)
    r = client.get("/cats")
    assert r.status_code == 200
    data = r.get_json()
    names = {c["name"] for c in data}
    assert names == {"Ucok", "Garfield", "Ella"}
    for c in data:
        assert c["status"] in ("ok", "watch", "alert")
        assert "litter_count_today" in c and "last_ate" in c


def test_cats_last_ate_null_when_no_bowl_sessions(tmp_path):
    client, conn = _app(tmp_path)
    r = client.get("/cats")
    data = r.get_json()
    for c in data:
        assert c["last_ate"] is None


def test_cats_last_ate_populated_for_matching_cat(tmp_path):
    client, conn = _app(tmp_path)
    store.log_bowl_session(conn, location="kitchen", cat="Ucok", duration_s=42)
    r = client.get("/cats")
    data = r.get_json()
    ucok = next(c for c in data if c["name"] == "Ucok")
    assert ucok["last_ate"] is not None
    assert ucok["last_ate"]["location"] == "kitchen"
    assert ucok["last_ate"]["duration_s"] == 42
    assert "ts" in ucok["last_ate"]

    # Cats with no bowl session stay null
    garfield = next(c for c in data if c["name"] == "Garfield")
    assert garfield["last_ate"] is None


def test_cats_last_ate_picks_most_recent(tmp_path):
    client, conn = _app(tmp_path)
    store.log_bowl_session(conn, location="living_room", cat="Garfield", duration_s=10)
    store.log_bowl_session(conn, location="kitchen", cat="Garfield", duration_s=99)
    r = client.get("/cats")
    data = r.get_json()
    garfield = next(c for c in data if c["name"] == "Garfield")
    assert garfield["last_ate"]["duration_s"] == 99
    assert garfield["last_ate"]["location"] == "kitchen"


def test_boxhealth_endpoint(tmp_path):
    client, conn = _app(tmp_path)
    r = client.get("/boxhealth")
    assert r.status_code == 200
    d = r.get_json()
    for k in ("bin_full_since", "capacity", "cleans_since_empty",
              "est_cleans_left", "auto_clean", "faults"):
        assert k in d


def test_bowls_and_feeders_endpoints(tmp_path):
    client, conn = _app(tmp_path)
    assert client.get("/bowls").status_code == 200
    assert isinstance(client.get("/bowls").get_json(), list)
    assert client.get("/feeders").status_code == 200
    assert isinstance(client.get("/feeders").get_json(), list)


def test_boxhealth_est_cleans_left_none_when_capacity_unknown(tmp_path):
    # Fresh DB: no complete fill cycle, no bin_clear yet.
    client, conn = _app(tmp_path)
    d = client.get("/boxhealth").get_json()
    assert d["capacity"] is None           # nothing learned yet
    assert d["cleans_since_empty"] is None  # no bin_clear to count from
    assert d["est_cleans_left"] is None     # cannot estimate without both


def test_boxhealth_est_cleans_left_clamped_to_zero(tmp_path):
    client, conn = _app(tmp_path)
    t0 = 1_700_000_000  # a real post-2020 epoch (strftime('%s') stays positive)
    # One complete cycle so capacity learns 2: clear, 2 cleans, full.
    store.insert_event(conn, Event(BIN_CLEAR, t0))
    store.insert_event(conn, Event(CLEAN_DONE, t0 + 10))
    store.insert_event(conn, Event(CLEAN_DONE, t0 + 20))
    store.insert_event(conn, Event(BIN_FULL, t0 + 30))
    # New cycle: a fresh clear, then MORE cleans than capacity (4 > 2).
    store.insert_event(conn, Event(BIN_CLEAR, t0 + 1000))
    store.insert_event(conn, Event(CLEAN_DONE, t0 + 1010))
    store.insert_event(conn, Event(CLEAN_DONE, t0 + 1020))
    store.insert_event(conn, Event(CLEAN_DONE, t0 + 1030))
    store.insert_event(conn, Event(CLEAN_DONE, t0 + 1040))

    d = client.get("/boxhealth").get_json()
    assert d["capacity"] == 2
    assert d["cleans_since_empty"] == 4
    assert d["est_cleans_left"] == 0   # max(0, 2-4) clamps, never negative
