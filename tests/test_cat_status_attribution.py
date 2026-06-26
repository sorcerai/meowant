"""Regression: a per-cat ALERT must NOT fire when the box IS being used but
attribution is uncertain (low-confidence / unattributed recent eliminations).
Garfield reported 'needs attention 32h' while fine — his visits were mis-credited
to Ucok; two recent eliminations carried confidence 0.5. The honest state is
'uncertain' (can't confirm), not a confident alert."""
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

def test_alert_downgraded_to_uncertain_when_recent_lowconf_use(tmp_path):
    conn = _db(tmp_path)
    # Garfield last *attributed* 32h ago -> would be ALERT (threshold 24h)
    _elim(conn, "Garfield", T - 32 * 3600, conf=1.0)
    # two recent low-confidence eliminations (>=2 is the shared gate)
    _elim(conn, "Ucok", T - 9 * 3600, conf=0.5)
    _elim(conn, "Ucok", T - 6 * 3600, conf=0.5)
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    g = rows["Garfield"]
    assert g["status"] != "alert"                 # NOT a confident alarm
    assert g.get("attribution_uncertain") is True # honest "can't confirm"


def test_one_uncertain_elim_does_not_suppress_alert(tmp_path):
    """Boundary: exactly 1 uncertain elimination (<2) must NOT engage the hedge."""
    conn = _db(tmp_path)
    # Garfield last *attributed* 32h ago -> would be ALERT
    _elim(conn, "Garfield", T - 32 * 3600, conf=1.0)
    # only ONE low-confidence elimination in 24h — below the >=2 gate
    _elim(conn, "Ucok", T - 9 * 3600, conf=0.5)
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    g = rows["Garfield"]
    assert g["status"] == "alert"                  # 1 < 2, hedge does not engage
    assert not g.get("attribution_uncertain")      # no false-uncertainty flag

def test_real_alert_still_fires_when_attribution_clean(tmp_path):
    conn = _db(tmp_path)
    # Garfield 32h ago; all recent uses are HIGH-confidence OTHER cats -> genuine absence
    _elim(conn, "Garfield", T - 32 * 3600, conf=1.0)
    _elim(conn, "Ucok", T - 3 * 3600, conf=1.0)
    _elim(conn, "Ella", T - 2 * 3600, conf=1.0)
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    assert rows["Garfield"]["status"] == "alert"   # real no-go alarm preserved
    assert not rows["Garfield"].get("attribution_uncertain")
