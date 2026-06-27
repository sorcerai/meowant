"""Attribution-INDEPENDENT deadman catch-all.

With per_cat disabled, the deadman only checked daemon liveness — it never
noticed if eliminations stopped being RECORDED. This catch-all fires if no
eliminated visit (any cat, even unattributed) appears within no_go_hours, so it
survives a labeler outage: a cat using the box counts even when we can't ID it.
Critical for unattended operation.
"""
import datetime as dt

from mw import store
from mw.deadman import DeadManSwitch


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return conn


def _elim(conn, epoch, cat=None, duration=60, use_record=60):
    v = store.open_visit(conn, epoch)
    store.mark_elimination(conn, v, use_record)
    store.close_visit(conn, v, epoch + duration, duration)
    if cat:
        store.set_visit_identity(conn, v, store.cat_id_by_name(conn, cat), 1.0)
    return v


def _sw(conn, now, **kw):
    return DeadManSwitch(conn, notify=lambda m: None, now_fn=lambda: now,
                         state_path=kw.pop("state_path", "/tmp/_dm_nogo_unused.json"), **kw)


# --- store.last_eliminated_ts: attribution-independent ---------------------

def test_last_eliminated_ts_counts_unattributed(tmp_path):
    conn = _db(tmp_path)
    _elim(conn, 1000.0, cat="Ucok")     # attributed, older
    _elim(conn, 5000.0)                  # UNATTRIBUTED, newer
    ts = store.last_eliminated_ts(conn)
    assert abs(dt.datetime.fromisoformat(ts).timestamp() - 5000.0) < 2
    # the attributed-only helper still returns the older attributed one
    ts_attr = store.last_real_elimination_ts_any(conn)
    assert abs(dt.datetime.fromisoformat(ts_attr).timestamp() - 1000.0) < 2


def test_last_eliminated_ts_none_on_empty(tmp_path):
    assert store.last_eliminated_ts(_db(tmp_path)) is None


# --- deadman check_no_go ---------------------------------------------------

def test_no_go_fires_when_silent_past_window(tmp_path):
    conn = _db(tmp_path)
    base = 200_000.0
    _elim(conn, base - 13 * 3600)        # last elimination 13h ago (> 12h limit)
    msg = _sw(conn, base, no_go_hours=12).check_no_go()
    assert msg and "no litter eliminations" in msg.lower()


def test_no_go_silent_when_recent(tmp_path):
    conn = _db(tmp_path)
    base = 200_000.0
    _elim(conn, base - 2 * 3600)         # 2h ago
    assert _sw(conn, base, no_go_hours=12).check_no_go() is None


def test_no_go_counts_unattributed_box_use(tmp_path):
    # The box IS being used recently but the visit is unattributed (labeler
    # outage) — must NOT alarm. This is the whole point of attribution-independence.
    conn = _db(tmp_path)
    base = 200_000.0
    _elim(conn, base - 1 * 3600)         # unattributed, 1h ago
    assert _sw(conn, base, no_go_hours=12).check_no_go() is None


def test_no_go_silent_on_empty_db(tmp_path):
    # Fresh DB / no baseline yet -> don't false-alarm.
    assert _sw(_db(tmp_path), 200_000.0, no_go_hours=12).check_no_go() is None


def test_no_go_wired_into_run_once(tmp_path):
    conn = _db(tmp_path)
    base = 200_000.0
    _elim(conn, base - 20 * 3600)        # silent 20h
    sent = []
    sw = DeadManSwitch(conn, notify=lambda m: sent.append(m) or True,
                       now_fn=lambda: base, no_go_hours=12,
                       state_path=str(tmp_path / "state.json"),
                       state_probe=lambda: {"last_ok_ts": base - 5})  # daemon healthy
    sw.run_once()
    assert any("no litter eliminations" in m.lower() for m in sent)
