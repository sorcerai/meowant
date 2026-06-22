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


def test_last_elimination_ts(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    assert store.last_elimination_ts(conn) is None        # empty DB
    v1 = store.open_visit(conn, 1000.0); store.mark_elimination(conn, v1, 55)
    store.close_visit(conn, v1, 1060.0, 60)
    v2 = store.open_visit(conn, 5000.0)                    # later but NOT eliminated
    store.close_visit(conn, v2, 5005.0, 5)
    ts = store.last_elimination_ts(conn)
    assert ts == store._iso(1000.0)                        # the eliminated one


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


def test_seed_cats(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Orange", "Black", "Tabby"])
    store.seed_cats(conn, ["Orange", "Black", "Tabby"])  # idempotent
    n = conn.execute("SELECT COUNT(*) FROM cats").fetchone()[0]
    assert n == 3


def test_pop_empty_captures(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    vid = store.open_visit(conn, 1000.0)
    uid = store.cat_id_by_name(conn, "Ucok")

    # one labeled capture (keep), two auto-none (prune), one untouched (keep)
    f_keep  = tmp_path / "labeled.jpg"; f_keep.write_text("x")
    f_none1 = tmp_path / "none1.jpg";  f_none1.write_text("x")
    f_none2 = tmp_path / "none2.jpg";  f_none2.write_text("x")
    f_raw   = tmp_path / "raw.jpg";    f_raw.write_text("x")

    c_keep = store.insert_capture(conn, 1000.0, vid, "cam", str(f_keep))
    store.apply_auto_label(conn, c_keep, uid, 0.9)

    c1 = store.insert_capture(conn, 1001.0, vid, "cam", str(f_none1))
    store.mark_capture_examined(conn, c1, "auto-none")
    c2 = store.insert_capture(conn, 1002.0, vid, "cam", str(f_none2))
    store.mark_capture_examined(conn, c2, "auto-none")

    store.insert_capture(conn, 1003.0, vid, "cam", str(f_raw))  # untouched

    paths = store.pop_empty_captures(conn)
    assert set(paths) == {str(f_none1), str(f_none2)}

    remaining = conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
    assert remaining == 2   # labeled + untouched, auto-none rows gone


def test_pending_and_mark_notified(tmp_path):
    import time
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    now = time.time()
    # eliminated + closed + old enough -> pending
    v1 = store.open_visit(conn, now - 100); store.mark_elimination(conn, v1, 55)
    store.close_visit(conn, v1, now - 90, 10)
    # eliminated but too recent (inside settle) -> not pending under a tight `before`
    v2 = store.open_visit(conn, now - 5); store.mark_elimination(conn, v2, 60)
    store.close_visit(conn, v2, now - 4, 1)
    # not eliminated -> never pending
    v3 = store.open_visit(conn, now - 100); store.close_visit(conn, v3, now - 95, 5)

    before = store._iso(now - 30)
    pend = store.pending_elimination_notifications(conn, before)
    assert [p["id"] for p in pend] == [v1]      # only v1: elim, closed, settled

    store.mark_notified(conn, v1)
    assert store.pending_elimination_notifications(conn, before) == []   # v1 cleared


def test_human_attribute_visit(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Ella"])
    vid = store.open_visit(conn, 1000.0); store.mark_elimination(conn, vid, 55)
    store.insert_capture(conn, 1000.0, vid, "cam", "/g/a.jpg")
    store.insert_capture(conn, 1001.0, vid, "cam", "/g/b.jpg")
    eid = store.cat_id_by_name(conn, "Ella")
    assert store.human_attribute_visit(conn, vid, eid) is True
    # visit attributed to Ella, and it's human-established (auto-labeler won't override)
    assert store.get_visit(conn, vid)["cat_id"] == eid
    assert store.visit_established_cat(conn, vid) == "Ella"

def test_human_attribute_visit_no_captures(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ella"])
    vid = store.open_visit(conn, 1000.0)
    assert store.human_attribute_visit(conn, vid, store.cat_id_by_name(conn, "Ella")) is False


def test_capture_paths_around(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    # captures at t=1000 and t=1100; window ending 1110 with 120s back includes both
    store.insert_capture(conn, 1000.0, 1, "cam", "/g/a.jpg")
    store.insert_capture(conn, 1100.0, 1, "cam", "/g/b.jpg")
    store.insert_capture(conn, 500.0, 1, "cam", "/g/old.jpg")   # too old
    paths = store.capture_paths_around(conn, store._iso(1110.0), window_s=120)
    assert set(paths) == {"/g/a.jpg", "/g/b.jpg"}               # old.jpg excluded


def test_human_mark_no_cat_clears_elimination(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    v = store.open_visit(conn, 1000.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 1060.0, 60)
    assert store.get_visit(conn, v)["eliminated"] == 1
    store.human_mark_no_cat(conn, v)
    row = store.get_visit(conn, v)
    assert row["eliminated"] == 0 and row["use_record"] is None   # false trigger un-counted


def test_set_visit_scatter_keeps_worst(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    v = store.open_visit(conn, 1000.0)
    store.set_visit_scatter(conn, v, 1, 0.9, 40)        # apron: light
    store.set_visit_scatter(conn, v, 3, 11.0, 500)      # fling zone: heavy -> wins
    assert store.get_visit(conn, v)["scatter_severity"] == 3
    store.set_visit_scatter(conn, v, 1, 0.5, 20)        # a later lighter score must NOT clobber
    assert store.get_visit(conn, v)["scatter_severity"] == 3
