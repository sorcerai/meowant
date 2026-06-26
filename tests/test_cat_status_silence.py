"""Regression (meowant-hp4): cat_status must port health_watch's system-wide
silence guard. If NO cat has an attributed box use in >=8h, the camera/vision
pipeline is likely down (not the cats) — suppress per-cat ALERTs so the
dashboard doesn't cry wolf on every cat. A vision outage does NOT set
daemon.stale, so this is the only thing that catches it on the UI side."""
import time
from mw import store, cat_status

T = time.mktime((2026, 6, 26, 14, 0, 0, 0, 0, -1))


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return conn


def _elim(conn, cat, ts, conf=1.0):
    cid = store.cat_id_by_name(conn, cat) if cat else None
    with store._lock:
        conn.execute("INSERT INTO visits(enter_ts,eliminated,cat_id,confidence,use_record,duration_s) "
                     "VALUES(?,1,?,?,60,60)", (store._iso(ts), cid, conf))
        conn.commit()


def test_system_silence_suppresses_per_cat_alert(tmp_path):
    """All cats last used >8h ago (camera/vision down) -> no confident ALERT."""
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 10 * 3600, conf=1.0)      # 10h > 8h threshold -> would ALERT
    _elim(conn, "Ella", T - 12 * 3600, conf=1.0)      # within 24h threshold -> ok
    _elim(conn, "Garfield", T - 12 * 3600, conf=1.0)  # within 24h threshold -> ok
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    u = rows["Ucok"]
    assert u["status"] != "alert"                  # silence guard suppresses
    assert u.get("attribution_uncertain") is True  # honest "can't confirm"


def test_per_cat_alert_fires_when_box_demonstrably_working(tmp_path):
    """One cat used the box 1h ago -> box works -> a different cat over its
    threshold is a GENUINE per-cat alert, not silence. Must still fire."""
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 10 * 3600, conf=1.0)   # 10h > 8h -> ALERT
    _elim(conn, "Ella", T - 1 * 3600, conf=1.0)    # box demonstrably working 1h ago
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    assert rows["Ucok"]["status"] == "alert"       # genuine no-go preserved
    assert not rows["Ucok"].get("attribution_uncertain")
