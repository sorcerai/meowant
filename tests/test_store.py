from mw import store
from mw.events import Event, CAT_ENTER

def test_visit_lifecycle(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.insert_event(conn, Event(CAT_ENTER, 1000.0, {"from": "standby"}))
    vid = store.open_visit(conn, 1000.0)
    store.mark_elimination(conn, vid, use_record=55)
    store.close_visit(conn, vid, 1066.0, 66)
    rows = store.recent_visits(conn, 10)
    assert len(rows) == 1
    assert rows[0]["duration_s"] == 66
    assert rows[0]["eliminated"] == 1
    assert rows[0]["use_record"] == 55

def test_reconcile_open_visits(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    vid = store.open_visit(conn, 1000.0)  # left open (NULL leave_ts)
    store.reconcile_open_visits(conn)
    row = conn.execute("SELECT leave_ts, duration_s FROM visits WHERE id=?",
                       (vid,)).fetchone()
    assert row["leave_ts"] is not None
    assert row["duration_s"] == 0


def test_eliminations_today(tmp_path):
    import time
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    now = time.time()
    # one eliminated visit today, one non-eliminated today
    v1 = store.open_visit(conn, now); store.mark_elimination(conn, v1, 174); store.close_visit(conn, v1, now + 60, 60)
    v2 = store.open_visit(conn, now); store.close_visit(conn, v2, now + 9, 9)  # no elimination
    assert store.eliminations_today(conn) == 1
    # a different (past) day doesn't count toward today
    assert store.eliminations_today(conn, day="2020-01-01") == 0


def test_labeling_and_gallery(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    vid = store.open_visit(conn, 1000.0)
    c1 = store.insert_capture(conn, 1000.0, vid, "cam1", "/g/a.jpg", None)
    c2 = store.insert_capture(conn, 1001.0, vid, "cam2", "/g/b.jpg", None)
    # one unlabeled to start
    assert len(store.unlabeled_captures(conn)) == 2
    gid = store.cat_id_by_name(conn, "Garfield")
    store.set_capture_label(conn, c1, gid)
    assert len(store.unlabeled_captures(conn)) == 1     # c1 now labeled
    assert store.gallery_counts(conn)["Garfield"] == 1
    assert store.gallery_counts(conn)["Ella"] == 0
    # unknown cat name -> None (caller decides how to handle)
    assert store.cat_id_by_name(conn, "Nope") is None


def test_set_visit_identity(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    vid = store.open_visit(conn, 1000.0)
    uid = store.cat_id_by_name(conn, "Ucok")
    store.set_visit_identity(conn, vid, uid, 0.91)
    row = conn.execute("SELECT cat_id, confidence FROM visits WHERE id=?", (vid,)).fetchone()
    assert row["cat_id"] == uid and abs(row["confidence"] - 0.91) < 1e-9


# ---- 6v5: visit-level attribution synced from captures.label ----------------

def test_sync_visit_cat_uses_label_majority(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    vid = store.open_visit(conn, 1000.0)
    gid = store.cat_id_by_name(conn, "Garfield")
    uid = store.cat_id_by_name(conn, "Ucok")
    # 3 frames Garfield, 1 stray Ucok -> visit attributes to Garfield @ 0.75
    for i in range(3):
        c = store.insert_capture(conn, 1000.0 + i, vid, "cam", f"/g/g{i}.jpg", None)
        store.apply_auto_label(conn, c, gid, 0.9)
    c = store.insert_capture(conn, 1100.0, vid, "cam", "/g/u.jpg", None)
    store.apply_auto_label(conn, c, uid, 0.9)
    assert store.sync_visit_cat(conn, vid) == (gid, 0.75)
    row = conn.execute("SELECT cat_id, confidence FROM visits WHERE id=?", (vid,)).fetchone()
    assert row["cat_id"] == gid and abs(row["confidence"] - 0.75) < 1e-9


def test_sync_visit_cat_no_labels_is_noop(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    vid = store.open_visit(conn, 1000.0)
    store.insert_capture(conn, 1000.0, vid, "cam", "/g/x.jpg", None)  # unlabeled
    assert store.sync_visit_cat(conn, vid) is None
    row = conn.execute("SELECT cat_id FROM visits WHERE id=?", (vid,)).fetchone()
    assert row["cat_id"] is None


def test_set_capture_label_syncs_visit(tmp_path):
    # a HUMAN label must also update the visit row, not just the capture
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    vid = store.open_visit(conn, 1000.0)
    c1 = store.insert_capture(conn, 1000.0, vid, "cam", "/g/a.jpg", None)
    gid = store.cat_id_by_name(conn, "Garfield")
    store.set_capture_label(conn, c1, gid)
    row = conn.execute("SELECT cat_id FROM visits WHERE id=?", (vid,)).fetchone()
    assert row["cat_id"] == gid


def test_backfill_visit_cats(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    gid = store.cat_id_by_name(conn, "Garfield")
    eid = store.cat_id_by_name(conn, "Ella")
    v1 = store.open_visit(conn, 1000.0)
    store.apply_auto_label(conn, store.insert_capture(conn, 1000.0, v1, "c", "/g/a.jpg", None), gid, 0.9)
    v2 = store.open_visit(conn, 2000.0)
    store.apply_auto_label(conn, store.insert_capture(conn, 2000.0, v2, "c", "/g/b.jpg", None), eid, 0.9)
    v3 = store.open_visit(conn, 3000.0)  # no labeled captures -> untouched
    store.insert_capture(conn, 3000.0, v3, "c", "/g/c.jpg", None)
    assert store.backfill_visit_cats(conn) == 2
    get = lambda v: conn.execute("SELECT cat_id FROM visits WHERE id=?", (v,)).fetchone()["cat_id"]
    assert get(v1) == gid and get(v2) == eid and get(v3) is None


def test_seed_cats(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Orange", "Black", "Tabby"])
    store.seed_cats(conn, ["Orange", "Black", "Tabby"])  # idempotent
    n = conn.execute("SELECT COUNT(*) FROM cats").fetchone()[0]
    assert n == 3
