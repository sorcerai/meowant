"""Shadow scorer + daily report. Verifies the matcher's predictions are logged
against the live attribution WITHOUT touching production, and the owner report
summarizes agreement and flags disagreements."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mw import store, shadow


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    for i, nm in ((1, "Ucok"), (2, "Garfield"), (3, "Ella")):
        conn.execute("INSERT INTO cats(id,name) VALUES(?,?)", (i, nm))
    conn.commit()
    return conn


def _elim_visit(conn, cat_id, n_frames, is_ir=0, ts=1782583200.0):
    vid = store.open_visit(conn, ts)
    conn.execute("UPDATE visits SET eliminated=1, cat_id=? WHERE id=?", (cat_id, vid))
    conn.commit()
    for i in range(n_frames):
        store.insert_capture(conn, ts, vid, "meowcam1", f"v{vid}_f{i}.jpg", is_ir=is_ir)
    return vid


class _StubMatcher:
    """Predicts a fixed cat per visit by reading the frame name prefix v<id>_."""
    def __init__(self, by_visit):
        self.by_visit = by_visit  # {visit_id: (cat_id|None, conf)}

    def predict(self, path):
        vid = int(path.split("_")[0][1:])
        return self.by_visit.get(vid, (None, 0.0))


def test_scores_new_visits_and_logs(tmp_path):
    conn = _db(tmp_path)
    v1 = _elim_visit(conn, 1, 3)            # truly Ucok, committed Ucok
    v2 = _elim_visit(conn, 2, 4)            # committed Garfield...
    m = _StubMatcher({v1: (1, 0.9), v2: (1, 0.8)})   # matcher says Ucok for both
    log = str(tmp_path / "shadow.jsonl"); state = str(tmp_path / "shadow_state.json")
    sc = shadow.ShadowScorer(conn, m, log, state, now_fn=lambda: 1782583200.0)
    assert sc.score_new() == 2
    recs = shadow.read_records(log)
    assert len(recs) == 2
    r1 = next(r for r in recs if r["visit_id"] == v1)
    r2 = next(r for r in recs if r["visit_id"] == v2)
    assert r1["agree"] is True and r1["shadow_cat_id"] == 1
    assert r2["agree"] is False and r2["committed_cat_id"] == 2   # disagreement captured


def test_scorer_is_incremental(tmp_path):
    conn = _db(tmp_path)
    v1 = _elim_visit(conn, 1, 2)
    m = _StubMatcher({v1: (1, 0.9)})
    log = str(tmp_path / "s.jsonl"); state = str(tmp_path / "s.json")
    sc = shadow.ShadowScorer(conn, m, log, state, now_fn=lambda: 1782583200.0)
    assert sc.score_new() == 1
    assert sc.score_new() == 0                      # already scored, no dupes
    v2 = _elim_visit(conn, 3, 2)
    sc.matcher = _StubMatcher({v2: (3, 0.7)})
    assert sc.score_new() == 1                      # only the new one
    assert len(shadow.read_records(log)) == 2


def test_scorer_does_not_touch_visit_rows(tmp_path):
    conn = _db(tmp_path)
    v1 = _elim_visit(conn, 2, 2)
    m = _StubMatcher({v1: (1, 0.9)})               # matcher disagrees
    sc = shadow.ShadowScorer(conn, m, str(tmp_path/"l"), str(tmp_path/"s"),
                             now_fn=lambda: 1782583200.0)
    sc.score_new()
    committed = conn.execute("SELECT cat_id FROM visits WHERE id=?", (v1,)).fetchone()["cat_id"]
    assert committed == 2                           # live attribution UNCHANGED


def test_daily_report_summarizes_and_flags(tmp_path):
    cats = {1: "Ucok", 2: "Garfield", 3: "Ella"}
    now = 1782583200.0
    iso = "2026-06-27T10:00:00"
    recs = [
        {"ts": iso, "visit_id": 1, "shadow_cat_id": 1, "committed_cat_id": 1, "agree": True, "ir_frac": 0.0, "shadow_conf": 0.9},
        {"ts": iso, "visit_id": 2, "shadow_cat_id": 2, "committed_cat_id": 1, "agree": False, "ir_frac": 0.5, "shadow_conf": 0.7},
        {"ts": iso, "visit_id": 3, "shadow_cat_id": None, "committed_cat_id": 3, "agree": False, "ir_frac": 1.0, "shadow_conf": 0.0},
    ]
    txt = shadow.daily_report(recs, now, cats)
    assert "3 visits" in txt
    assert "disagreement" in txt
    assert "matcher=Garfield vs current=Ucok" in txt
    # abstain (visit 3) is not counted as a commit/disagreement
    assert "committed 2" in txt


def test_daily_report_empty():
    assert "no completed visits" in shadow.daily_report([], 1782583200.0, {1: "Ucok"})
