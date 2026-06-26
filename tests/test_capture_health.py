"""Capture-health: proactive stream probe + reactive missed-capture guard."""
from mw import store
from mw.capture_health import CaptureHealth

T = 1_000_000.0  # fixed "now" for deterministic settle/age windows


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


def _eliminated_visit(conn, enter_ts, leave_ts):
    vid = store.open_visit(conn, enter_ts)
    store.mark_elimination(conn, vid, 50)
    store.close_visit(conn, vid, leave_ts, int(leave_ts - enter_ts))
    return vid


# ---- proactive stream probe -------------------------------------------------

def test_stream_down_then_recovered_notifies_each_transition(tmp_path):
    conn = _db(tmp_path)
    cams = [{"name": "meowcam1", "url": "rtsp://x/1"}]
    states = iter([True, False, True])          # up, then down, then back up
    msgs = []
    h = CaptureHealth(conn, cams, notify=msgs.append,
                      probe=lambda url: next(states), now_fn=lambda: T)
    h.check_streams()   # first sight: up — no alert (no prior state)
    h.check_streams()   # up -> down: alert
    h.check_streams()   # down -> up: recovery alert
    assert len(msgs) == 2
    assert "DOWN" in msgs[0] and "meowcam1" in msgs[0]
    assert "recover" in msgs[1].lower()


def test_steady_stream_does_not_spam(tmp_path):
    conn = _db(tmp_path)
    cams = [{"name": "c", "url": "u"}]
    msgs = []
    h = CaptureHealth(conn, cams, notify=msgs.append,
                      probe=lambda url: True, now_fn=lambda: T)
    for _ in range(5):
        h.check_streams()
    assert msgs == []   # never transitioned, never alerted


# ---- reactive missed-capture guard -----------------------------------------

def test_eliminated_visit_with_no_frames_alerts_once(tmp_path):
    conn = _db(tmp_path)
    _eliminated_visit(conn, T - 1000, T - 900)   # closed well past settle, no captures
    cams = [{"name": "c", "url": "u"}]
    msgs = []
    h = CaptureHealth(conn, cams, notify=msgs.append,
                      probe=lambda url: True, now_fn=lambda: T, settle_seconds=120)
    h.check_missed()
    h.check_missed()                              # second sweep must NOT re-alert
    assert len(msgs) == 1
    assert "0 frames" in msgs[0] or "captured 0" in msgs[0]


def test_eliminated_visit_with_a_frame_is_silent(tmp_path):
    conn = _db(tmp_path)
    vid = _eliminated_visit(conn, T - 1000, T - 900)
    store.insert_capture(conn, T - 995, vid, "c", "/tmp/f.jpg", None)
    cams = [{"name": "c", "url": "u"}]
    msgs = []
    h = CaptureHealth(conn, cams, notify=msgs.append,
                      probe=lambda url: True, now_fn=lambda: T, settle_seconds=120)
    h.check_missed()
    assert msgs == []   # frames present → healthy


def test_recent_visit_within_settle_is_not_flagged(tmp_path):
    conn = _db(tmp_path)
    _eliminated_visit(conn, T - 40, T - 30)      # closed 30s ago, grabs may still be in flight
    cams = [{"name": "c", "url": "u"}]
    msgs = []
    h = CaptureHealth(conn, cams, notify=msgs.append,
                      probe=lambda url: True, now_fn=lambda: T, settle_seconds=120)
    h.check_missed()
    assert msgs == []   # inside settle window → no false alarm


def test_labeler_stall_alerts_once_then_rearms(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, T - 5000)
    cid = store.insert_capture(conn, T - 5000, vid, "c", "/g/x.jpg")  # old, untouched
    msgs = []
    h = CaptureHealth(conn, [{"name": "c", "url": "u"}], notify=msgs.append,
                      probe=lambda u: True, now_fn=lambda: T, labeler_settle_seconds=1800)
    h.check_labeler()
    h.check_labeler()                                # still stuck -> no repeat
    assert len(msgs) == 1 and "stall" in msgs[0].lower()
    store.mark_capture_examined(conn, cid, "auto-none")   # labeler caught up
    h.check_labeler()                                # backlog clear -> re-arm, silent
    assert len(msgs) == 1
    store.insert_capture(conn, T - 5000, vid, "c", "/g/y.jpg")   # new stall
    h.check_labeler()
    assert len(msgs) == 2                            # alerts again after re-arm


def test_null_visit_frames_never_count_as_stall(tmp_path):
    # orphan frames (visit_id NULL, from a resolver race) are NOT the labeler's
    # work, so they must not pin the stall latch on forever.
    conn = _db(tmp_path)
    store.insert_capture(conn, T - 5000, None, "c", "/g/orphan.jpg")  # old, untouched, NULL visit
    msgs = []
    h = CaptureHealth(conn, [{"name": "c", "url": "u"}], notify=msgs.append,
                      probe=lambda u: True, now_fn=lambda: T, labeler_settle_seconds=1800)
    h.check_labeler()
    assert msgs == []


def test_recent_unlabeled_not_flagged_as_stall(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, T - 60)
    store.insert_capture(conn, T - 60, vid, "c", "/g/x.jpg")      # only 60s old
    msgs = []
    h = CaptureHealth(conn, [{"name": "c", "url": "u"}], notify=msgs.append,
                      probe=lambda u: True, now_fn=lambda: T, labeler_settle_seconds=1800)
    h.check_labeler()
    assert msgs == []                               # within grace; labeler just busy


def test_no_cameras_no_probing(tmp_path):
    conn = _db(tmp_path)
    probed = []
    h = CaptureHealth(conn, [], notify=lambda m: None,
                      probe=lambda url: probed.append(url) or True, now_fn=lambda: T)
    h.run_once()
    assert probed == []   # camera-absent install: capture-health is a no-op


def test_labeler_stall_routes_through_remediator_when_present(tmp_path):
    from mw.remediation import Remediator
    conn = _db(tmp_path)
    vid = store.open_visit(conn, T - 5000)
    store.insert_capture(conn, T - 5000, vid, "c", "/g/x.jpg")   # old, untouched
    msgs = []
    rem = Remediator(conn, notify=msgs.append, now_fn=lambda: T)
    h = CaptureHealth(conn, [{"name": "c", "url": "u"}], notify=lambda m: None,
                      probe=lambda u: True, now_fn=lambda: T,
                      labeler_settle_seconds=1800, remediator=rem)
    h.check_labeler()
    assert len(msgs) == 1 and "stall" in msgs[0].lower()
    # the episode was recorded as an incident (not just an ephemeral notify)
    rows = store.recent_incidents(conn)
    assert rows and rows[0]["kind"] == "labeler_stall"
    assert rows[0]["outcome"] == "escalated"


def test_stream_down_debounces_transient_drop(tmp_path):
    from mw.remediation import Remediator
    conn = _db(tmp_path)
    # probe sequence: up (seed), down (transition fires playbook), then re-probe up
    states = iter([True, False, True])
    msgs = []
    rem = Remediator(conn, notify=msgs.append, now_fn=lambda: T)
    # patch the debounce sleep to no-op for the test
    import mw.remediation as R
    h = CaptureHealth(conn, [{"name": "c", "url": "u"}], notify=msgs.append,
                      probe=lambda u: next(states), now_fn=lambda: T, remediator=rem)
    orig_sleep = R.time.sleep
    R.time.sleep = lambda s: None
    try:
        h.check_streams()   # seed: up
        h.check_streams()   # up -> down: playbook waits then re-probes -> UP -> silent
    finally:
        R.time.sleep = orig_sleep
    assert msgs == []                                  # transient blip, no alarm
    assert store.recent_incidents(conn)[0]["outcome"] == "recovered"


def test_stream_down_escalates_when_persistent(tmp_path):
    from mw.remediation import Remediator
    conn = _db(tmp_path)
    states = iter([True, False, False])                # down and stays down
    msgs = []
    rem = Remediator(conn, notify=msgs.append, now_fn=lambda: T)
    import mw.remediation as R
    h = CaptureHealth(conn, [{"name": "c", "url": "u"}], notify=msgs.append,
                      probe=lambda u: next(states), now_fn=lambda: T, remediator=rem)
    orig_sleep = R.time.sleep
    R.time.sleep = lambda s: None
    try:
        h.check_streams()   # up
        h.check_streams()   # up -> down, re-probe still down -> escalate
    finally:
        R.time.sleep = orig_sleep
    assert len(msgs) == 1 and "DOWN" in msgs[0]
    assert store.recent_incidents(conn)[0]["outcome"] == "escalated"


def test_run_once_isolates_exceptions(tmp_path):
    conn = _db(tmp_path)
    h = CaptureHealth(conn, [{"name": "c", "url": "u"}], notify=lambda m: None,
                      probe=lambda u: True, now_fn=lambda: T)
    
    ran = set()
    def fail_streams():
        ran.add("streams")
        raise ValueError("streams crashed")
    def fail_missed():
        ran.add("missed")
        raise RuntimeError("missed crashed")
    def fail_labeler():
        ran.add("labeler")
        raise Exception("labeler crashed")
        
    h.check_streams = fail_streams
    h.check_missed = fail_missed
    h.check_labeler = fail_labeler
    
    # none of the raises should bubble out, and all three should run
    h.run_once()
    assert ran == {"streams", "missed", "labeler"}
