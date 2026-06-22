# tests/test_sessions.py
from mw import store


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield"])
    return conn


def _visit(conn, enter, dur, *, cat=None, elim=False, use_record=None,
           conn_cats=None):
    """Open+close a visit; optionally attribute a cat and mark elimination."""
    vid = store.open_visit(conn, enter)
    store.close_visit(conn, vid, enter + dur, dur)
    if elim:
        store.mark_elimination(conn, vid, use_record)
    if cat is not None:
        store.set_visit_identity(conn, vid, store.cat_id_by_name(conn, cat), 1.0)
    return vid


def test_flicker_tail_collapses_into_pee(tmp_path):
    # Ucok: a 70s real pee (dp102) + a 2s flicker tail 4s later -> ONE session
    conn = _db(tmp_path)
    v1 = _visit(conn, 1000.0, 70, cat="Ucok", elim=True, use_record=55)
    v2 = _visit(conn, 1074.0, 2)              # 1070 leave -> 1074 enter = 4s gap, no elim
    sess = store.sessions(conn, gap_s=30)
    assert len(sess) == 1
    s = sess[0]
    assert s["visit_ids"] == [v1, v2]
    assert s["eliminated"] == 1
    assert s["cat"] == "Ucok"
    assert s["n_fragments"] == 2
    assert s["duration_s"] == 76            # 1076 - 1000


def test_gaming_blips_stay_separate(tmp_path):
    # Garfield: two 2s blips, no elimination on either -> stay TWO sessions
    conn = _db(tmp_path)
    _visit(conn, 1000.0, 2, cat="Garfield")
    _visit(conn, 1007.0, 2, cat="Garfield")   # 5s gap, both elim=0
    sess = store.sessions(conn, gap_s=30)
    assert len(sess) == 2


def test_two_real_pees_stay_separate(tmp_path):
    # Two eliminating visits close in time are two trips, never merged
    conn = _db(tmp_path)
    _visit(conn, 1000.0, 60, cat="Ucok", elim=True)
    _visit(conn, 1065.0, 60, cat="Ucok", elim=True)   # 5s gap, both elim=1
    sess = store.sessions(conn, gap_s=30)
    assert len(sess) == 2


def test_gap_beyond_window_not_merged(tmp_path):
    # A non-elim fragment far after the pee (> gap_s) is its own session
    conn = _db(tmp_path)
    _visit(conn, 1000.0, 60, cat="Ucok", elim=True)
    _visit(conn, 1100.0, 5)                    # 40s gap > 30 -> separate
    sess = store.sessions(conn, gap_s=30)
    assert len(sess) == 2


def test_resolved_cat_conflict_not_merged(tmp_path):
    # Pee is Ucok, the adjacent fragment resolved to Garfield -> do not merge
    conn = _db(tmp_path)
    _visit(conn, 1000.0, 60, cat="Ucok", elim=True)
    _visit(conn, 1065.0, 3, cat="Garfield")    # 5s gap, but a different resolved cat
    sess = store.sessions(conn, gap_s=30)
    assert len(sess) == 2


def test_unresolved_tail_merges(tmp_path):
    # The flicker tail has no cat_id yet (vision pending) -> still merges into the pee
    conn = _db(tmp_path)
    v1 = _visit(conn, 1000.0, 60, cat="Ucok", elim=True)
    v2 = _visit(conn, 1065.0, 2)               # cat_id NULL
    sess = store.sessions(conn, gap_s=30)
    assert len(sess) == 1 and sess[0]["visit_ids"] == [v1, v2]


def test_newest_first_order(tmp_path):
    conn = _db(tmp_path)
    _visit(conn, 1000.0, 60, cat="Ucok", elim=True)
    _visit(conn, 5000.0, 60, cat="Garfield", elim=True)
    sess = store.sessions(conn, gap_s=30)
    assert sess[0]["enter_ts"] > sess[1]["enter_ts"]   # newest first
