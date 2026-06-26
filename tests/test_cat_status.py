import time
from datetime import datetime
from mw import store, cat_status

T = datetime(2026, 6, 26, 12, 0, 0).timestamp()

def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return conn

def _elim(conn, cat_name, ts):
    # an attributed, eliminated visit at ts
    cid = store.cat_id_by_name(conn, cat_name)
    with store._lock:
        cur = conn.execute("INSERT INTO visits(enter_ts, eliminated, cat_id, use_record, duration_s) "
                           "VALUES(?,1,?,60,60)", (store._iso(ts), cid))
        conn.commit()

def test_ok_when_recent(tmp_path):
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 3600)             # 1h ago, threshold 8h -> ok
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    assert rows["Ucok"]["status"] == "ok"
    assert rows["Ucok"]["threshold_h"] == 8
    assert rows["Ucok"]["litter_count_today"] == 1

def test_watch_band(tmp_path):
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 7 * 3600)         # 7h, threshold 8 -> >=6 and <8 -> watch
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    assert rows["Ucok"]["status"] == "watch"

def test_alert_at_threshold(tmp_path):
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 9 * 3600)         # 9h >= 8 -> alert
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    assert rows["Ucok"]["status"] == "alert"

def test_no_data_is_ok_not_alarm(tmp_path):
    conn = _db(tmp_path)
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    assert rows["Ella"]["status"] == "ok"
    assert rows["Ella"]["last_litter_ts"] is None
    assert rows["Ella"]["hours_since"] is None

def test_band_boundaries_are_inclusive(tmp_path):
    # Pins the >= edges: exactly 0.75*threshold -> watch, exactly threshold -> alert.
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 6.0 * 3600)       # exactly 0.75*8 = 6.0h -> watch
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    assert rows["Ucok"]["status"] == "watch"

    sub = tmp_path / "b"; sub.mkdir()
    conn2 = _db(sub)
    _elim(conn2, "Ucok", T - 8.0 * 3600)      # exactly threshold 8.0h -> alert
    rows2 = {r["name"]: r for r in cat_status.cat_status(conn2, now_fn=lambda: T)}
    assert rows2["Ucok"]["status"] == "alert"

def test_today_count_is_tz_correct(tmp_path):
    # Build timestamps from now's LOCAL midnight so the test is tz-agnostic.
    conn = _db(tmp_path)
    lt = time.localtime(T)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    _elim(conn, "Ucok", midnight + 3600)      # ~1h after midnight today -> counts
    _elim(conn, "Ucok", midnight - 3600)      # ~1h before midnight (yesterday) -> not today
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    assert rows["Ucok"]["litter_count_today"] == 1
