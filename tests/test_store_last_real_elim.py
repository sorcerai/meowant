"""meowant-hp4: the system-silence guard input must match health_watch's
'latest' set — most recent REAL elimination across all cats, EXCLUDING
Garfield's deliberate short re-entries (use_record IS NULL OR duration_s <= 40).
Otherwise a 30s Garfield re-entry resets the dashboard's silence clock while
Telegram's stays put, re-introducing the exact divergence hp4 kills."""
import time
from mw import store

T = time.mktime((2026, 6, 26, 14, 0, 0, 0, 0, -1))


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return conn


def _elim(conn, cat, ts, use_record=60, duration_s=60):
    cid = store.cat_id_by_name(conn, cat)
    with store._lock:
        conn.execute("INSERT INTO visits(enter_ts,eliminated,cat_id,use_record,duration_s) "
                     "VALUES(?,1,?,?,?)", (store._iso(ts), cid, use_record, duration_s))
        conn.commit()


def test_returns_most_recent_real_across_cats(tmp_path):
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 10 * 3600)
    _elim(conn, "Ella", T - 2 * 3600)   # most recent real
    ts = store.last_real_elimination_ts_any(conn)
    assert abs(datetime_ts(ts) - (T - 2 * 3600)) < 1.5


def test_excludes_garfield_short_reentry(tmp_path):
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 10 * 3600)                         # last REAL use 10h ago
    _elim(conn, "Garfield", T - 120, duration_s=30)           # 30s re-entry -> excluded
    ts = store.last_real_elimination_ts_any(conn)
    assert abs(datetime_ts(ts) - (T - 10 * 3600)) < 1.5       # Garfield re-entry ignored


def test_excludes_garfield_null_use_record(tmp_path):
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 10 * 3600)
    _elim(conn, "Garfield", T - 120, use_record=None, duration_s=999)  # no use_record -> excluded
    ts = store.last_real_elimination_ts_any(conn)
    assert abs(datetime_ts(ts) - (T - 10 * 3600)) < 1.5


def test_garfield_real_long_visit_counts(tmp_path):
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 10 * 3600)
    _elim(conn, "Garfield", T - 120, duration_s=60)   # real >40s visit -> counts
    ts = store.last_real_elimination_ts_any(conn)
    assert abs(datetime_ts(ts) - (T - 120)) < 1.5


def test_none_when_no_eliminations(tmp_path):
    conn = _db(tmp_path)
    assert store.last_real_elimination_ts_any(conn) is None


def datetime_ts(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()
