from mw import store


def test_insert_and_fetch_capture(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    vid = store.open_visit(conn, 1000.0)
    cid = store.insert_capture(conn, 1000.5, vid, "litter-front", "/x/a.jpg", is_ir=1)
    assert isinstance(cid, int)
    rows = store.captures_for_visit(conn, vid)
    assert len(rows) == 1
    assert rows[0]["camera"] == "litter-front" and rows[0]["path"] == "/x/a.jpg"
    assert rows[0]["label"] is None and rows[0]["is_ir"] == 1


def test_latest_open_visit_id(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    assert store.latest_open_visit_id(conn) is None
    v1 = store.open_visit(conn, 100.0)
    assert store.latest_open_visit_id(conn) == v1
    store.close_visit(conn, v1, 160.0, 60)
    assert store.latest_open_visit_id(conn) is None
