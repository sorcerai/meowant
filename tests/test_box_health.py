from mw import store
from mw.events import Event, BIN_FULL, BIN_CLEAR, CLEAN_DONE
from mw.box_health import BoxHealthWatch

T = 1_000_000.0

def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn); return conn

def _ev(conn, kind, ts):
    store.insert_event(conn, Event(kind, ts))

def test_bin_full_nags_then_silent_until_renag(tmp_path):
    conn = _db(tmp_path); msgs = []; clock = [T]
    _ev(conn, BIN_FULL, T)
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0],
                       renag_hours=3, unusable_hours=6)
    w.run_once()                                   # first nag immediate
    assert len(msgs) == 1 and "bin full" in msgs[0].lower()
    clock[0] = T + 2 * 3600; w.run_once()          # 2h later -> still within renag, silent
    assert len(msgs) == 1
    clock[0] = T + 3 * 3600; w.run_once()          # 3h -> re-nag
    assert len(msgs) == 2

def test_escalates_to_unusable_after_threshold(tmp_path):
    conn = _db(tmp_path); msgs = []; clock = [T]
    _ev(conn, BIN_FULL, T)
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0],
                       renag_hours=3, unusable_hours=6)
    clock[0] = T + 6 * 3600; w.run_once()          # 6h full -> UNUSABLE escalation
    assert len(msgs) == 1 and "unusable" in msgs[0].lower()

def test_silent_and_rearms_when_clear(tmp_path):
    conn = _db(tmp_path); msgs = []; clock = [T]
    _ev(conn, BIN_FULL, T); _ev(conn, BIN_CLEAR, T + 10)   # already cleared
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0])
    w.run_once()
    assert msgs == []                              # clear -> no bin-full nag

def test_approaching_full_heads_up_once_per_cycle(tmp_path):
    conn = _db(tmp_path); msgs = []; clock = [T]
    # history: one complete cycle of capacity 3 (clear,3 cleans,full)
    _ev(conn, BIN_CLEAR, T - 1000)
    for i in range(3): _ev(conn, CLEAN_DONE, T - 900 + i)
    _ev(conn, BIN_FULL, T - 800)
    # current cycle: cleared, now 2 cleans (>= cap(3) - margin(1))
    _ev(conn, BIN_CLEAR, T)
    _ev(conn, CLEAN_DONE, T + 60); _ev(conn, CLEAN_DONE, T + 120)
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0], approaching_margin=1)
    w.run_once()
    assert len(msgs) == 1 and "getting full" in msgs[0].lower()
    w.run_once()                                   # same cycle -> no repeat
    assert len(msgs) == 1

def test_no_approaching_warn_without_learned_capacity(tmp_path):
    conn = _db(tmp_path); msgs = []
    _ev(conn, BIN_CLEAR, T)
    _ev(conn, CLEAN_DONE, T + 60)                  # no complete prior cycle -> capacity None
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: T, approaching_margin=1)
    w.run_once()
    assert msgs == []
