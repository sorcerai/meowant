from mw import store
from mw.tracker import VisitTracker
from mw.events import Event, CAT_ENTER, CAT_LEAVE, ELIMINATION

def test_burst_creates_one_visit_per_entry(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    t = VisitTracker(conn)
    # two quick in/out pokes
    t.handle(Event(CAT_ENTER, 100.0)); t.handle(Event(CAT_LEAVE, 109.0))
    t.handle(Event(CAT_ENTER, 120.0)); t.handle(Event(CAT_LEAVE, 282.0))
    rows = store.recent_visits(conn, 10)
    assert len(rows) == 2
    durations = sorted(r["duration_s"] for r in rows)
    assert durations == [9, 162]

def test_elimination_marks_open_visit(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    t = VisitTracker(conn)
    t.handle(Event(CAT_ENTER, 100.0))
    t.handle(Event(ELIMINATION, 150.0, {"use_record": 227}))
    t.handle(Event(CAT_LEAVE, 200.0))
    row = store.recent_visits(conn, 1)[0]
    assert row["eliminated"] == 1 and row["use_record"] == 227

def test_leave_without_open_is_safe(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    t = VisitTracker(conn)
    t.handle(Event(CAT_LEAVE, 5.0))  # no crash, no row
    assert store.recent_visits(conn, 1) == []

def test_elimination_within_grace_attributes_to_last_closed(tmp_path):
    """Elimination 600s after leave (within 1800s grace) marks the closed visit."""
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    t = VisitTracker(conn)
    t.handle(Event(CAT_ENTER, 100.0))
    t.handle(Event(CAT_LEAVE, 160.0))
    # no open visit; elimination arrives 600s later (within 1800s grace)
    t.handle(Event(ELIMINATION, 760.0, {"use_record": 55}))
    row = store.recent_visits(conn, 1)[0]
    assert row["eliminated"] == 1
    assert row["use_record"] == 55

def test_elimination_beyond_grace_is_ignored(tmp_path):
    """Elimination 4000s after leave (beyond 1800s grace) is silently dropped."""
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    t = VisitTracker(conn)
    t.handle(Event(CAT_ENTER, 100.0))
    t.handle(Event(CAT_LEAVE, 160.0))
    # 4000s later — clearly beyond grace window
    t.handle(Event(ELIMINATION, 4160.0, {"use_record": 99}))
    row = store.recent_visits(conn, 1)[0]
    assert row["eliminated"] == 0
    assert row["use_record"] is None
