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


def _elim(conn, epoch, cat=None, duration=60, use_record=60):
    v = store.open_visit(conn, epoch); store.mark_elimination(conn, v, use_record)
    store.close_visit(conn, v, epoch + duration, duration)
    if cat:
        store.set_visit_identity(conn, v, store.cat_id_by_name(conn, cat), 1.0)
    return v


def _sw(conn, now, **kw):
    return DeadManSwitch(conn, notify=lambda m: None, now_fn=lambda: now,
                         state_path=kw.pop("state_path", "/tmp/_dm_unused.json"), **kw)


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
    msgs = _sw(conn, base, per_cat_enabled=True).check_per_cat()
    assert any(c == "Ella" for c, m in msgs)
    assert not any(c == "Ucok" for c, m in msgs)


def test_per_cat_suppressed_if_system_silent(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 30 * 3600, cat="Ella")          
    _elim(conn, base - 10 * 3600, cat="Ucok")          # Ucok 10h ago (system dead >8h)
    msgs = _sw(conn, base, per_cat_enabled=True).check_per_cat()
    assert len(msgs) == 0


def test_garfield_weight_filter(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    # System alive
    _elim(conn, base - 1 * 3600, cat="Ucok")
    
    # Garfield has a long recent session but no weight -> ignored by last_elim
    _elim(conn, base - 2 * 3600, cat="Garfield", duration=60, use_record=None)
    # So his actual last valid session is 30h ago
    _elim(conn, base - 30 * 3600, cat="Garfield", duration=60, use_record=60)
    
    msgs = _sw(conn, base, per_cat_enabled=True).check_per_cat()
    assert any(c == "Garfield" for c, m in msgs)


def test_garfield_duration_filter(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 1 * 3600, cat="Ucok")
    
    # Garfield has short session -> ignored
    _elim(conn, base - 2 * 3600, cat="Garfield", duration=30, use_record=60)
    _elim(conn, base - 30 * 3600, cat="Garfield", duration=60, use_record=60)
    
    msgs = _sw(conn, base, per_cat_enabled=True).check_per_cat()
    assert any(c == "Garfield" for c, m in msgs)


def test_ucok_daytime_tolerance(tmp_path):
    conn = _db(tmp_path)
    # 14:00 local time -> daytime
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 1 * 3600, cat="Ella") # system alive
    _elim(conn, base - 10 * 3600, cat="Ucok") # 10h ago (>8h threshold)
    
    msgs = _sw(conn, base, per_cat_enabled=True).check_per_cat()
    # Should NOT fire for Ucok because it is daytime
    assert not any(c == "Ucok" for c, m in msgs)
    
    # But if it's nighttime (23:00)
    night = time.mktime(time.strptime("2026-06-22 23:00", "%Y-%m-%d %H:%M"))
    _elim(conn, night - 1 * 3600, cat="Ella") # system alive at night
    msgs_night = _sw(conn, night, per_cat_enabled=True).check_per_cat()
    assert any(c == "Ucok" for c, m in msgs_night)


def test_run_once_survives_corrupt_nondict_state(tmp_path):
    # A valid-JSON-but-non-dict latch file must NOT silence every future run.
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 1 * 3600, cat="Ucok")
    _elim(conn, base - 30 * 3600, cat="Ella")
    st = tmp_path / "st.json"
    st.write_text("[1,2,3]")                          # corrupt: a list, not a dict
    sent = []
    sw = DeadManSwitch(conn, notify=sent.append, now_fn=lambda: base, per_cat_enabled=True,
                       state_path=str(st),
                       state_probe=lambda: {"last_ok_ts": base - 5})  # daemon healthy
    fired = sw.run_once()                             # must not crash
    assert fired >= 1
    assert any("Ella" in m for m in sent)
