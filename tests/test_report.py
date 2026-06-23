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


def test_incidents_report_empty():
    conn = _db()
    out = report.incidents_report(conn)
    assert "no incident" in out.lower()


def test_incidents_report_lists_recent_and_totals():
    conn = _db()
    store.log_incident(conn, "stream_down", {"camera": "meowcam3"},
                       "re-probed after 5s: still DOWN", "escalated", ts=1_000_000.0)
    store.log_incident(conn, "labeler_stall", {"stuck": 4},
                       "checked `agy` on PATH: MISSING", "escalated", ts=1_000_100.0)
    out = report.incidents_report(conn)
    assert "stream_down" in out and "labeler_stall" in out
    assert "still DOWN" in out
    assert "Totals" in out or "totals" in out


def test_digest_includes_feeds_line(tmp_path):
    from mw import store, report
    from datetime import date, datetime, time
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    # anchor to noon today so the offset can't cross midnight (today-filter is date-based)
    noon = datetime.combine(date.today(), time(12, 0)).timestamp()
    store.log_feed_event(conn, 2, "scheduled", ts=noon)
    out = report.digest(conn)
    assert "feed" in out.lower()                       # mentions feeding
    assert "2 portion" in out or "2 meal" in out or "1 feed" in out


def test_feed_status_text(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.log_feed_event(conn, 1, "manual", ts=1_000_000.0)
    status = {"online": True, "feed_state": "standby", "food_level": "full",
              "last_feed": {"ts": 1_000_000.0, "portions": 1}}
    txt = report.feed_status_text(conn, status)
    assert "full" in txt.lower() and ("online" in txt.lower() or "ok" in txt.lower())


def test_feed_status_text_offline(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    txt = report.feed_status_text(conn, {"online": False})
    assert "offline" in txt.lower() or "unreachable" in txt.lower()


def test_digest_includes_bowl_when_data_present(tmp_path):
    from mw import store, report
    from datetime import date, datetime, time
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    noon = datetime.combine(date.today(), time(12, 0)).timestamp()
    store.log_bowl_event(conn, "empty", "vision", secs_since_feed=7200, ts=noon - 10)
    out = report.digest(conn)
    assert "bowl" in out.lower()


def test_digest_silent_on_bowl_when_no_data(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    out = report.digest(conn)
    assert "bowl" not in out.lower()        # no bowl data -> no bowl line


def test_bowl_status_text(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.log_bowl_event(conn, "empty", "vision", secs_since_feed=3600, ts=1_000_000.0)
    txt = report.bowl_status_text(conn)
    assert "empty" in txt.lower()


def test_bowl_status_text_no_data(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    assert "no bowl" in report.bowl_status_text(conn).lower()


def test_weekly_status_text_no_report(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    assert "no weekly" in report.weekly_status_text(conn).lower()


def test_weekly_status_text_returns_latest(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.log_weekly_report(conn, "2026-06-16T00:00:00", "2026-06-23T00:00:00",
                            "{}", "[]", None, ts=1_000_000.0)
    # Phase 1 stores no narrative; the rendered table is rebuilt on demand from
    # facts — but for the no-narrative case we surface a pointer to the period.
    txt = report.weekly_status_text(conn)
    assert "2026-06-23" in txt
