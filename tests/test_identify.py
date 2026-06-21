"""Phase-3 identification scaffold: fusion + backfill plumbing (model-independent)."""
from mw import store
from mw.identify import fuse_views, identify_visit, NullMatcher


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return conn


# ---- fuse_views (pure) ------------------------------------------------------

def test_fuse_empty_is_unknown():
    assert fuse_views([]) == (None, 0.0)


def test_fuse_all_unknown_is_unknown():
    assert fuse_views([(None, 0.0), (None, 0.0)]) == (None, 0.0)


def test_fuse_single_view_passes_through():
    assert fuse_views([(2, 0.8)]) == (2, 0.8)


def test_fuse_confidence_weighted_vote():
    # cat 2 named by two weak views (0.4+0.4=0.8) beats cat 1's single 0.7
    cat, conf = fuse_views([(1, 0.7), (2, 0.4), (2, 0.4)])
    assert cat == 2
    assert conf == 0.4  # winner's BEST single-view confidence, not the sum


# ---- identify_visit (plumbing) ---------------------------------------------

class FakeMatcher:
    """Maps a frame path substring -> (cat_id, confidence)."""
    def __init__(self, table):
        self.table = table
    def predict(self, image_path):
        for key, val in self.table.items():
            if key in image_path:
                return val
        return (None, 0.0)


def test_identify_visit_writes_per_view_and_fused_identity(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    store.insert_capture(conn, 1000.0, vid, "cam1", "/g/1000_cam1_0.jpg", None)
    store.insert_capture(conn, 1001.0, vid, "cam2", "/g/1001_cam2_0.jpg", None)
    m = FakeMatcher({"cam1": (2, 0.9), "cam2": (2, 0.5)})  # both say Garfield(id=2)
    cat, conf = identify_visit(conn, vid, m)
    assert cat == 2 and conf == 0.9
    # per-view predictions persisted
    caps = store.captures_for_visit(conn, vid)
    assert sorted(c["pred"] for c in caps) == [2, 2]
    # fused identity written onto the visit
    row = conn.execute("SELECT cat_id, confidence FROM visits WHERE id=?", (vid,)).fetchone()
    assert row["cat_id"] == 2 and abs(row["confidence"] - 0.9) < 1e-9


def test_null_matcher_yields_unknown_and_writes_no_identity(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    store.insert_capture(conn, 1000.0, vid, "cam1", "/g/x.jpg", None)
    cat, conf = identify_visit(conn, vid, NullMatcher())
    assert cat is None and conf == 0.0
    row = conn.execute("SELECT cat_id FROM visits WHERE id=?", (vid,)).fetchone()
    assert row["cat_id"] is None  # never write a confident wrong guess


def test_threshold_suppresses_low_confidence_identity(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    store.insert_capture(conn, 1000.0, vid, "cam1", "/g/x_cam1.jpg", None)
    m = FakeMatcher({"cam1": (1, 0.3)})
    cat, conf = identify_visit(conn, vid, m, threshold=0.5)
    assert cat == 1 and conf == 0.3          # returned for the caller's awareness
    row = conn.execute("SELECT cat_id FROM visits WHERE id=?", (vid,)).fetchone()
    assert row["cat_id"] is None             # but NOT committed below threshold
