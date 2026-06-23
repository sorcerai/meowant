from datetime import datetime
from mw import store, weekly


def _conn():
    conn = store.connect(":memory:")
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Ella", "Garfield"])
    return conn


def _add_void(conn, cat, enter_epoch, dur, weight, *, eliminated=1):
    """Insert one visit row directly (bypasses the live pipeline)."""
    cid = store.cat_id_by_name(conn, cat) if cat else None
    iso = datetime.fromtimestamp(enter_epoch).isoformat(timespec="seconds")
    leave = datetime.fromtimestamp(enter_epoch + dur).isoformat(timespec="seconds")
    with store._lock:
        conn.execute(
            "INSERT INTO visits(enter_ts, leave_ts, duration_s, cat_id, confidence, "
            "eliminated, use_record) VALUES(?,?,?,?,?,?,?)",
            (iso, leave, dur, cid, 1.0 if cid else None, eliminated, weight))
        conn.commit()


def test_collect_facts_counts_and_gaps():
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    h = 3600.0
    # Ucok: 3 voids in the last week, ~4h apart
    _add_void(conn, "Ucok", now - 12 * h, 55, 50)
    _add_void(conn, "Ucok", now - 8 * h, 60, 55)
    _add_void(conn, "Ucok", now - 4 * h, 58, 52)
    facts = weekly.collect_facts(conn, now)
    u = facts["per_cat"]["Ucok"]
    assert u["voids"] == 3
    assert u["gap_h"]["n"] == 2
    assert abs(u["gap_h"]["mean"] - 4.0) < 0.01
    assert facts["period"]["days"] == 7


def test_collect_facts_garfield_pokes_excluded():
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    h = 3600.0
    _add_void(conn, "Garfield", now - 5 * h, 6, 3)     # poke: dur<=40 -> excluded
    _add_void(conn, "Garfield", now - 4 * h, 90, 88)   # real void
    facts = weekly.collect_facts(conn, now)
    assert facts["per_cat"]["Garfield"]["voids"] == 1   # only the real one


def test_collect_facts_attribution_and_flicker():
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    h = 3600.0
    _add_void(conn, "Ucok", now - 4 * h, 55, 50)            # attributed
    _add_void(conn, None, now - 3 * h, 5, None, eliminated=0)  # flicker fragment
    facts = weekly.collect_facts(conn, now)
    s = facts["system"]
    assert s["total_visits"] == 2 and s["attributed"] == 1 and s["unattributed"] == 1
    assert abs(s["attribution_pct"] - 50.0) < 0.01
    assert s["flicker_fragments"] == 1
