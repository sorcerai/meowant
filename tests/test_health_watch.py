from mw import store
from mw.health_watch import HealthWatch


def _conn(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    return conn


def _elim(conn, ts):
    v = store.open_visit(conn, ts); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, ts + 60, 60)
    return v


def test_no_go_alarm_fires_once_then_latches(tmp_path):
    conn = _conn(tmp_path)
    _elim(conn, 1000.0)                       # last use at t=1000
    sent = []
    now = {"t": 1000.0 + 13 * 3600}           # 13h later (> 12h)
    hw = HealthWatch(conn, sent.append, now_fn=lambda: now["t"],
                     no_go_hours=12, digest_hour=99)   # digest_hour=99 disables digest
    hw.run_once(); hw.run_once()              # two passes
    nogo = [m for m in sent if "No litter box use" in m]
    assert len(nogo) == 1                     # latched: only one alarm


def test_no_go_rearms_after_new_use(tmp_path):
    conn = _conn(tmp_path)
    _elim(conn, 1000.0)
    sent = []
    now = {"t": 1000.0 + 13 * 3600}
    hw = HealthWatch(conn, sent.append, now_fn=lambda: now["t"],
                     no_go_hours=12, digest_hour=99)
    hw.run_once()                             # alarm 1
    _elim(conn, now["t"])                     # a fresh use clears it
    hw.run_once()                             # under threshold -> re-arm, no alarm
    now["t"] += 13 * 3600                     # quiet again > 12h
    hw.run_once()                             # alarm 2
    assert len([m for m in sent if "No litter box use" in m]) == 2


def test_no_alarm_when_recent(tmp_path):
    conn = _conn(tmp_path)
    _elim(conn, 1000.0)
    sent = []
    hw = HealthWatch(conn, sent.append, now_fn=lambda: 1000.0 + 3600,  # 1h
                     no_go_hours=12, digest_hour=99)
    hw.run_once()
    assert [m for m in sent if "No litter box use" in m] == []


def test_no_alarm_on_empty_db(tmp_path):
    conn = _conn(tmp_path)
    sent = []
    hw = HealthWatch(conn, sent.append, now_fn=lambda: 1_000_000.0,
                     no_go_hours=12, digest_hour=99)
    hw.run_once()
    assert sent == []                         # no data -> no alarm


def test_daily_digest_fires_once_per_day(tmp_path):
    import time as _t
    conn = _conn(tmp_path)
    # pick a now at 10:00 local on some day, with a use today
    base = _t.mktime(_t.strptime("2026-06-22 10:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 3600)
    sent = []
    hw = HealthWatch(conn, sent.append, now_fn=lambda: base,
                     no_go_hours=999, digest_hour=9)    # no_go disabled
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
