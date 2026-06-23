# tests/test_deadman.py
import time
from datetime import datetime
from mw import store
from mw.deadman import DeadManSwitch


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return conn


def _elim(conn, epoch, cat=None):
    v = store.open_visit(conn, epoch); store.mark_elimination(conn, v, 60)
    store.close_visit(conn, v, epoch + 60, 60)
    if cat:
        store.set_visit_identity(conn, v, store.cat_id_by_name(conn, cat), 1.0)
    return v


def _sw(conn, now, **kw):
    return DeadManSwitch(conn, notify=lambda m: None, now_fn=lambda: now,
                         state_path=kw.pop("state_path", "/tmp/_dm_unused.json"), **kw)


def test_no_go_fires_past_threshold(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 13 * 3600)                      # last use 13h ago
    sw = _sw(conn, base, no_go_hours=12)
    msg = sw.check_no_go()
    assert msg is not None and "13" in msg


def test_no_go_quiet_for_recent(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 2 * 3600)                       # 2h ago — fine
    assert _sw(conn, base, no_go_hours=12).check_no_go() is None


def test_no_go_suppressed_during_quiet_hours(tmp_path):
    conn = _db(tmp_path)
    # 03:00 local, inside 22:00–08:00 quiet window; last use 13h ago
    base = time.mktime(time.strptime("2026-06-22 03:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 13 * 3600)
    assert _sw(conn, base, no_go_hours=12).check_no_go() is None   # deferred until quiet ends


def test_no_go_none_on_empty_db(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    assert _sw(conn, base).check_no_go() is None       # no data -> no alarm


def test_liveness_fires_when_unreachable(tmp_path):
    conn = _db(tmp_path)
    sw = _sw(conn, 10_000.0, state_probe=lambda: None)     # daemon down
    assert "daemon" in sw.check_liveness().lower()


def test_liveness_fires_when_wedged(tmp_path):
    conn = _db(tmp_path)
    now = 10_000.0
    sw = _sw(conn, now, liveness_stale_s=180,
             state_probe=lambda: {"last_ok_ts": now - 600})  # last poll 10min ago
    assert sw.check_liveness() is not None


def test_liveness_ok_when_fresh(tmp_path):
    conn = _db(tmp_path)
    now = 10_000.0
    sw = _sw(conn, now, liveness_stale_s=180,
             state_probe=lambda: {"last_ok_ts": now - 5})    # polled 5s ago
    assert sw.check_liveness() is None


def test_per_cat_off_by_default(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 30 * 3600, cat="Ella")          # Ella silent 30h
    _elim(conn, base - 1 * 3600, cat="Ucok")           # Ucok recent
    assert _sw(conn, base).check_per_cat() == []        # disabled -> nothing


def test_per_cat_fires_for_silent_cat(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 30 * 3600, cat="Ella")          # Ella 30h ago
    _elim(conn, base - 1 * 3600, cat="Ucok")           # Ucok 1h ago (system clearly working)
    msgs = _sw(conn, base, per_cat_enabled=True, per_cat_hours=24).check_per_cat()
    assert any("Ella" in m for m in msgs)
    assert not any("Ucok" in m for m in msgs)


def test_run_once_fires_and_latches(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 13 * 3600)
    sent = []
    sw = DeadManSwitch(conn, notify=sent.append, now_fn=lambda: base, no_go_hours=12,
                       state_path=str(tmp_path / "st.json"),
                       state_probe=lambda: {"last_ok_ts": base - 5})  # daemon healthy
    assert sw.run_once() == 1                       # no-go fires
    assert sw.run_once() == 0                       # latched within realarm window
    assert any("no litter box use" in m.lower() for m in sent)


def test_run_once_realarms_after_window(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 13 * 3600)
    sent = []
    st = str(tmp_path / "st.json")
    DeadManSwitch(conn, sent.append, now_fn=lambda: base, no_go_hours=12, realarm_hours=3,
                  state_path=st, state_probe=lambda: {"last_ok_ts": base-5}).run_once()
    later = base + 4 * 3600                          # 4h later, still bad
    n = DeadManSwitch(conn, sent.append, now_fn=lambda: later, no_go_hours=12,
                      realarm_hours=3, state_path=st,
                      state_probe=lambda: {"last_ok_ts": later-5}).run_once()
    assert n == 1                                    # re-alarmed after the window


def test_run_once_fails_loud_on_exception(tmp_path):
    conn = _db(tmp_path)
    sent = []
    sw = DeadManSwitch(conn, notify=sent.append, now_fn=lambda: 10_000.0,
                       state_path=str(tmp_path / "st.json"),
                       state_probe=lambda: {"last_ok_ts": 10_000.0 - 5})
    sw.check_no_go = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # force failure
    sw.run_once()
    assert any("dead-man" in m.lower() and ("error" in m.lower() or "boom" in m.lower())
               for m in sent)                        # screamed instead of dying silently
