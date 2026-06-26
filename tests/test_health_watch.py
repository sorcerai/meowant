from mw import store
from mw.health_watch import HealthWatch


def _conn(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return conn


def _elim(conn, ts, cat="Ucok", duration=60, use_record=60):
    v = store.open_visit(conn, ts); store.mark_elimination(conn, v, use_record)
    store.close_visit(conn, v, ts + duration, duration)
    if cat:
        store.set_visit_identity(conn, v, store.cat_id_by_name(conn, cat), 1.0)
    return v


def test_no_go_alarm_fires_once_then_latches(tmp_path):
    conn = _conn(tmp_path)
    _elim(conn, 1000.0, cat="Ella")           # last use at t=1000
    sent = []
    # simulate nighttime (03:00) so Ucok doesn't suppress, or just test Ella (24h limit)
    now = {"t": 1000.0 + 25 * 3600}           # 25h later (> 24h)
    
    # We must ensure there is a recent visit by SOMEONE, otherwise the 8h system check suppresses it!
    # Wait, the threshold for system check is 8h!
    # If nobody goes for 8h, it's suppressed.
    # So we need someone else to go recently!
    _elim(conn, now["t"] - 3600, cat="Garfield")  # Garfield went 1h ago
    
    hw = HealthWatch(conn, sent.append, now_fn=lambda: now["t"], digest_hour=99)
    hw.run_once(); hw.run_once()              # two passes
    nogo = [m for m in sent if "No litter box use" in m]
    assert len(nogo) == 1                     # latched: only one alarm
    assert "Ella" in nogo[0]


def test_no_go_rearms_after_new_use(tmp_path):
    conn = _conn(tmp_path)
    _elim(conn, 1000.0, cat="Ella")
    sent = []
    now = {"t": 1000.0 + 25 * 3600}
    _elim(conn, now["t"] - 3600, cat="Garfield")
    
    hw = HealthWatch(conn, sent.append, now_fn=lambda: now["t"], digest_hour=99)
    hw.run_once()                             # alarm 1
    assert len([m for m in sent if "No litter box use" in m]) == 1

    _elim(conn, now["t"], cat="Ella")         # a fresh use clears it
    hw.run_once()                             # under threshold -> re-arm, no alarm
    
    now["t"] += 25 * 3600                     # quiet again > 24h
    _elim(conn, now["t"] - 3600, cat="Garfield")
    hw.run_once()                             # alarm 2
    assert len([m for m in sent if "No litter box use" in m]) == 2


def test_no_alarm_when_recent(tmp_path):
    conn = _conn(tmp_path)
    _elim(conn, 1000.0, cat="Ella")
    sent = []
    hw = HealthWatch(conn, sent.append, now_fn=lambda: 1000.0 + 3600, digest_hour=99)
    hw.run_once()
    assert [m for m in sent if "No litter box use" in m] == []


def test_no_alarm_on_empty_db(tmp_path):
    conn = _conn(tmp_path)
    sent = []
    hw = HealthWatch(conn, sent.append, now_fn=lambda: 1_000_000.0, digest_hour=99)
    hw.run_once()
    assert sent == []                         # no data -> no alarm


def test_daily_digest_fires_once_per_day(tmp_path):
    import time as _t
    conn = _conn(tmp_path)
    # pick a now at 10:00 local on some day, with a use today
    base = _t.mktime(_t.strptime("2026-06-22 10:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 3600, cat="Ella")
    sent = []
    hw = HealthWatch(conn, sent.append, now_fn=lambda: base, digest_hour=9)
    hw.run_once(); hw.run_once()
    digests = [m for m in sent if "alive" in m.lower()]
    assert len(digests) == 1                  # once per day even on repeated passes


def test_heartbeat_pings_url():
    from mw.health_watch import Heartbeat
    hits = []
    hb = Heartbeat("https://hc-ping.com/abc", getter=lambda url: hits.append(url))
    hb.run_once()
    assert hits == ["https://hc-ping.com/abc"]


def test_heartbeat_swallows_errors():
    from mw.health_watch import Heartbeat
    def boom(url): raise OSError("network down")
    hb = Heartbeat("https://hc-ping.com/abc", getter=boom)
    hb.run_once()   # must not raise


def test_no_go_suppressed_when_unattributed_elims_present(tmp_path):
    from mw import store
    from mw.health_watch import HealthWatch
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Ella", "Garfield"])
    now = 2_000_000.0
    # Ella attributed recently (keeps system-wide guard from firing);
    # Ucok last attributed 10h ago (>8h -> would alarm); plus TWO recent UNATTRIBUTED
    # elims (>=2 -> genuine backlog, hedge engages).
    def visit(enter, elim, cat=None):
        vid = store.open_visit(conn, enter); store.close_visit(conn, vid, enter + 60, 60)
        if elim: store.mark_elimination(conn, vid, 90)
        if cat: store.set_visit_identity(conn, vid, store.cat_id_by_name(conn, cat), 1.0)
    visit(now - 1 * 3600, True, cat="Ella")     # recent attributed -> guard stays quiet
    visit(now - 10 * 3600, True, cat="Ucok")    # Ucok 10h ago (>8h)
    visit(now - 2 * 3600, True)                  # recent UNATTRIBUTED elim #1
    visit(now - 3 * 3600, True)                  # recent UNATTRIBUTED elim #2
    msgs = []
    hw = HealthWatch(conn, notify=msgs.append, now_fn=lambda: now)
    hw._check_no_go()
    # Must NOT fire a confident "Ucok ... No litter box use" alarm; instead one honest notice.
    assert not any("No litter box use" in m for m in msgs)
    assert any("attribution" in m.lower() for m in msgs)


def test_no_go_hedge_not_engaged_at_single_unattributed(tmp_path):
    """Boundary: a SINGLE unattributed elim (often one false-IR empty-box visit) must
    NOT mute real per-cat sick-cat alarms. Hedge requires genuine backlog (>=2)."""
    from mw import store
    from mw.health_watch import HealthWatch
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Ella", "Garfield"])
    now = 2_000_000.0
    def visit(enter, elim, cat=None):
        vid = store.open_visit(conn, enter); store.close_visit(conn, vid, enter + 60, 60)
        if elim: store.mark_elimination(conn, vid, 90)
        if cat: store.set_visit_identity(conn, vid, store.cat_id_by_name(conn, cat), 1.0)
    # Ella went 25h ago (>24h -> would alarm); Garfield recent (keeps system guard quiet);
    # exactly ONE unattributed elim -> hedge must stay disengaged.
    visit(now - 25 * 3600, True, cat="Ella")    # Ella 25h ago (>24h limit)
    visit(now - 1 * 3600, True, cat="Garfield") # recent attributed -> system guard quiet
    visit(now - 2 * 3600, True)                  # single UNATTRIBUTED elim (count==1)
    msgs = []
    hw = HealthWatch(conn, notify=msgs.append, now_fn=lambda: now)
    hw._check_no_go()
    # Hedge NOT engaged: real per-cat alarm still fires, no attribution notice.
    assert any("No litter box use" in m and "Ella" in m for m in msgs)
    assert not any("attribution" in m.lower() for m in msgs)
