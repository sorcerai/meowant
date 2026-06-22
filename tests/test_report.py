"""report.py text builders over a real in-memory DB."""
from mw import store, report


def _db():
    conn = store.connect(":memory:")
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield"])
    return conn


def _visit(conn, enter, dur, *, cat=None, elim=False):
    vid = store.open_visit(conn, enter)
    store.close_visit(conn, vid, enter + dur, dur)
    if elim:
        store.mark_elimination(conn, vid, 55)
    if cat:
        store.set_visit_identity(conn, vid, store.cat_id_by_name(conn, cat), 1.0)
    return vid


def test_cat_report_names_cats_and_counts():
    conn = _db()
    _visit(conn, 1000.0, 60, cat="Ucok", elim=True)
    _visit(conn, 5000.0, 60, cat="Garfield", elim=True)
    txt = report.cat_report(conn)
    assert "Ucok" in txt and "Garfield" in txt
    assert "PER-CAT REPORT" in txt


def test_health_report_reports_hours_since():
    conn = _db()
    # last elimination 2h before "now"
    now = 10_000.0
    _visit(conn, now - 7200, 60, cat="Ucok", elim=True)
    txt = report.health_report(conn, now=now)
    assert "HEALTH" in txt and "Ucok" in txt
    assert "2.0h ago" in txt


def test_health_report_empty():
    conn = _db()
    assert "no eliminations" in report.health_report(conn).lower()


def test_status_report_reads_state():
    conn = _db()
    _visit(conn, 1000.0, 60, cat="Ucok", elim=True)
    # dp21 bit0 set => bin full; dp22=2 => fault E2
    txt = report.status_report(conn, {"24": "standby", "21": 1, "22": 2})
    assert "standby" in txt
    assert "FULL" in txt and "E2" in txt


def test_digest_summarizes_today(tmp_path):
    import time
    from datetime import date
    conn = _db()
    now = time.time()
    v = store.open_visit(conn, now); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, now + 60, 60)
    store.set_visit_identity(conn, v, store.cat_id_by_name(conn, "Ucok"), 1.0)
    txt = report.digest(conn, now=now + 120)
    assert "Ucok" in txt and ("1" in txt)
