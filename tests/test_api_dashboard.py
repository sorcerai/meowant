"""Tests for GET /cats dashboard endpoint (Task 2)."""
from mw import store, api


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
