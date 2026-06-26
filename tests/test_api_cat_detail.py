from mw import store, api


class _Dev:
    state = {"dps": {}}; last_ok_ts = None; device = None; smartclean = None


def _client(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return api.create_app(_Dev(), conn).test_client(), conn


def test_cat_detail_known_cat(tmp_path):
    c, conn = _client(tmp_path)
    r = c.get("/cat/Ucok")
    assert r.status_code == 200
    d = r.get_json()
    assert d["name"] == "Ucok"
    assert "timeline" in d and "weekly" in d and "photos" in d
    assert isinstance(d["timeline"], list)


def test_cat_detail_unknown_404(tmp_path):
    c, conn = _client(tmp_path)
    assert c.get("/cat/Nobody").status_code == 404
