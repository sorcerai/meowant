from mw import store
from mw.events import Event, BIN_FULL, BIN_CLEAR, CLEAN_DONE, FAULT, FAULT_CLEAR
from mw.box_health import BoxHealthWatch

T = 1_000_000.0

def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn); return conn

def _ev(conn, kind, ts):
    store.insert_event(conn, Event(kind, ts))

def _fault(conn, ts, bitmap):
    store.insert_event(conn, Event(FAULT, ts, {"bitmap": bitmap}))

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

# --- Fault watchdog (parity with bin-full: re-nag + UNUSABLE escalation) ---

def test_fault_nags_then_silent_until_renag(tmp_path):
    conn = _db(tmp_path); msgs = []; clock = [T]
    _fault(conn, T, 1)
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0],
                       renag_hours=3, unusable_hours=6)
    w.run_once()                                   # first nag immediate
    assert len(msgs) == 1 and "infrared protection" in msgs[0].lower()
    clock[0] = T + 2 * 3600; w.run_once()          # within renag -> silent
    assert len(msgs) == 1
    clock[0] = T + 3 * 3600; w.run_once()          # 3h -> re-nag
    assert len(msgs) == 2

def test_fault_escalates_to_unusable_after_threshold(tmp_path):
    conn = _db(tmp_path); msgs = []; clock = [T]
    _fault(conn, T, 1)
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0],
                       renag_hours=3, unusable_hours=6)
    clock[0] = T + 6 * 3600; w.run_once()          # 6h stuck -> UNUSABLE escalation
    assert len(msgs) == 1
    m = msgs[0].lower()
    assert "unusable" in m and "infrared protection" in m

def test_fault_escalation_clock_survives_bitmap_change(tmp_path):
    # codes shift at 4h while still stuck: UNUSABLE must still fire at 6h, not 10h.
    conn = _db(tmp_path); msgs = []; clock = [T]
    _fault(conn, T, 1)
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0],
                       renag_hours=3, unusable_hours=6)
    clock[0] = T + 4 * 3600; _fault(conn, clock[0], 3)   # new code appears
    clock[0] = T + 6 * 3600; w.run_once()
    assert msgs and "unusable" in msgs[0].lower()

def test_fault_silent_and_rearms_when_cleared(tmp_path):
    conn = _db(tmp_path); msgs = []; clock = [T]
    _fault(conn, T, 1); _ev(conn, FAULT_CLEAR, T + 10)   # already recovered
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0])
    w.run_once()
    assert msgs == []

# ---- never latch a nag on a failed send (Jul 15 regression) ---------------

def _flaky(fail_times, msgs):
    calls = {"n": 0}
    def notify(m):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            return False
        msgs.append(m)
        return True
    return notify

def test_bin_full_nag_retries_after_failed_send_persisted(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    _ev(conn, BIN_FULL, T)
    w = BoxHealthWatch(conn, _flaky(1, msgs), now_fn=lambda: T, renag_hours=3, unusable_hours=6)
    w.run_once()                                   # send fails: latch must NOT advance
    assert msgs == []
    # fresh instance from the SAME conn: if the latch had persisted despite the
    # failed send, this would stay silent forever instead of retrying
    w2 = BoxHealthWatch(conn, _flaky(0, msgs), now_fn=lambda: T, renag_hours=3, unusable_hours=6)
    w2.run_once()
    assert len(msgs) == 1 and "bin full" in msgs[0].lower()   # exactly one delivery

def test_fault_nag_retries_after_failed_send_persisted(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    _fault(conn, T, 1)
    w = BoxHealthWatch(conn, _flaky(1, msgs), now_fn=lambda: T, renag_hours=3, unusable_hours=6)
    w.run_once()
    assert msgs == []
    w2 = BoxHealthWatch(conn, _flaky(0, msgs), now_fn=lambda: T, renag_hours=3, unusable_hours=6)
    w2.run_once()
    assert len(msgs) == 1 and "infrared protection" in msgs[0].lower()

def test_approaching_full_retries_after_failed_send_persisted(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    _ev(conn, BIN_CLEAR, T - 1000)
    for i in range(3): _ev(conn, CLEAN_DONE, T - 900 + i)
    _ev(conn, BIN_FULL, T - 800)
    _ev(conn, BIN_CLEAR, T)
    _ev(conn, CLEAN_DONE, T + 60); _ev(conn, CLEAN_DONE, T + 120)
    w = BoxHealthWatch(conn, _flaky(1, msgs), now_fn=lambda: T, approaching_margin=1)
    w.run_once()
    assert msgs == []
    w2 = BoxHealthWatch(conn, _flaky(0, msgs), now_fn=lambda: T, approaching_margin=1)
    w2.run_once()
    assert len(msgs) == 1 and "getting full" in msgs[0].lower()
    w2.run_once()                                  # same cycle -> no repeat once it landed
    assert len(msgs) == 1


def test_fault_and_bin_full_both_nag_independently(tmp_path):
    # A box can be both full AND faulted; each must alert (two distinct messages).
    conn = _db(tmp_path); msgs = []; clock = [T]
    _ev(conn, BIN_FULL, T); _fault(conn, T, 1)
    w = BoxHealthWatch(conn, msgs.append, now_fn=lambda: clock[0],
                       renag_hours=3, unusable_hours=6)
    w.run_once()
    assert len(msgs) == 2
    joined = " ".join(msgs).lower()
    assert "bin full" in joined and "infrared protection" in joined
