"""The Telegram-tap multiplier: confirming a visit's identity should be able to
label ALL of that visit's (cat-positive) frames at once, not just the first one.
`human_attribute_visit` labels a single frame; `propagate_visit_label` is the
bulk version the labeling accelerator uses."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mw import store


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    conn.execute("INSERT INTO cats(id, name) VALUES(7, 'Ucok')")
    conn.commit()
    return conn


def _visit_with_frames(conn, n, enter_ts=1782583200.0):
    vid = store.open_visit(conn, enter_ts)
    ids = [store.insert_capture(conn, enter_ts, vid, "meowcam1", f"f{i}.jpg") for i in range(n)]
    return vid, ids


def test_propagate_labels_all_visit_frames(tmp_path):
    conn = _db(tmp_path)
    vid, ids = _visit_with_frames(conn, 5)
    n = store.propagate_visit_label(conn, vid, 7)
    assert n == 5
    rows = conn.execute(
        "SELECT label, label_source FROM captures WHERE visit_id=?", (vid,)).fetchall()
    assert all(r["label"] == 7 and r["label_source"] == "human" for r in rows)
    # visit row is synced to the cat
    cat_id = conn.execute("SELECT cat_id FROM visits WHERE id=?", (vid,)).fetchone()["cat_id"]
    assert cat_id == 7


def test_propagate_respects_capture_subset(tmp_path):
    """Caller passes only cat-positive frame ids; the rest stay unlabeled."""
    conn = _db(tmp_path)
    vid, ids = _visit_with_frames(conn, 4)
    keep = ids[:2]
    n = store.propagate_visit_label(conn, vid, 7, capture_ids=keep)
    assert n == 2
    labeled = {r["id"] for r in conn.execute(
        "SELECT id FROM captures WHERE label=7 AND visit_id=?", (vid,)).fetchall()}
    assert labeled == set(keep)
    # the untouched frames have no label
    rest = conn.execute(
        "SELECT label FROM captures WHERE visit_id=? AND id NOT IN (?,?)",
        (vid, keep[0], keep[1])).fetchall()
    assert all(r["label"] is None for r in rest)


def test_propagate_will_not_cross_visit_boundary(tmp_path):
    """A capture id from another visit must not be labeled, even if passed in."""
    conn = _db(tmp_path)
    vid_a, ids_a = _visit_with_frames(conn, 2, 1782583200.0)
    vid_b, ids_b = _visit_with_frames(conn, 2, 1782586800.0)
    n = store.propagate_visit_label(conn, vid_a, 7, capture_ids=ids_a + ids_b)
    assert n == 2  # only vid_a's frames count
    other = conn.execute(
        "SELECT label FROM captures WHERE visit_id=?", (vid_b,)).fetchall()
    assert all(r["label"] is None for r in other)


def test_propagate_empty_is_noop(tmp_path):
    conn = _db(tmp_path)
    vid, ids = _visit_with_frames(conn, 3)
    assert store.propagate_visit_label(conn, vid, 7, capture_ids=[]) == 0
