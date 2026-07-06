"""Capture-health: proactive stream probe + reactive missed-capture guard."""
import os

from mw import store
from mw.capture_health import CaptureHealth

T = 1_000_000.0  # fixed "now" for deterministic settle/age windows


def _warm(tmp_path, ages):
    """Build a warm-frame dir; ages = {cam_name: seconds_old} (omit a cam to leave
    its frame missing). Returns the dir path."""
    d = tmp_path / "warm"
    d.mkdir(exist_ok=True)
    for name, age in ages.items():
        f = d / f"{name}.jpg"
        f.write_bytes(b"x")
        os.utime(f, (T - age, T - age))
    return str(d)


def _cams(*names):
    return [{"name": n, "url": f"rtsp://x/{n}"} for n in names]


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


# ---- proactive warm-frame blackout guard -----------------------------------

def test_warm_blackout_all_stale_alerts_once(tmp_path):
    conn = _db(tmp_path)
    wd = _warm(tmp_path, {"meowcam1": 600, "meowcam2": 600})   # both stale > 180s
    msgs = []
    h = CaptureHealth(conn, _cams("meowcam1", "meowcam2"), notify=msgs.append,
                      now_fn=lambda: T, warm_dir=wd, warm_stale_seconds=180)
    h.check_warm_frames()
    h.check_warm_frames()                                        # latched -> no repeat
    assert len(msgs) == 1 and "blind" in msgs[0].lower()

def test_warm_one_fresh_camera_suppresses_alert(tmp_path):
    # cam4 chronically dead but cam1 fresh -> NOT a blackout, no alert.
    conn = _db(tmp_path)
    wd = _warm(tmp_path, {"meowcam1": 5, "meowcam4": 9000})
    msgs = []
    h = CaptureHealth(conn, _cams("meowcam1", "meowcam4"), notify=msgs.append,
                      now_fn=lambda: T, warm_dir=wd, warm_stale_seconds=180)
    h.check_warm_frames()
    assert msgs == []

def test_warm_missing_frame_counts_as_blind(tmp_path):
    conn = _db(tmp_path)
    wd = _warm(tmp_path, {})                                     # no files at all
    msgs = []
    h = CaptureHealth(conn, _cams("meowcam1", "meowcam2"), notify=msgs.append,
                      now_fn=lambda: T, warm_dir=wd, warm_stale_seconds=180)
    h.check_warm_frames()
    assert len(msgs) == 1 and "blind" in msgs[0].lower()

def test_warm_recovers_and_rearms(tmp_path):
    conn = _db(tmp_path)
    wd = _warm(tmp_path, {"meowcam1": 600, "meowcam2": 600})
    msgs = []
    h = CaptureHealth(conn, _cams("meowcam1", "meowcam2"), notify=msgs.append,
                      now_fn=lambda: T, warm_dir=wd, warm_stale_seconds=180)
    h.check_warm_frames()
    assert len(msgs) == 1
    os.utime(os.path.join(wd, "meowcam1.jpg"), (T - 5, T - 5))   # one cam recovers
    h.check_warm_frames()                                        # re-arm, silent
    assert len(msgs) == 1
    os.utime(os.path.join(wd, "meowcam1.jpg"), (T - 600, T - 600))  # blind again
    h.check_warm_frames()
    assert len(msgs) == 2                                        # alerts again after re-arm

def test_warm_check_noop_without_warm_dir(tmp_path):
    # http-sidecar / rtsp installs don't run warm readers -> signal n/a, never alert.
    conn = _db(tmp_path)
    msgs = []
    h = CaptureHealth(conn, _cams("meowcam1"), notify=msgs.append,
                      now_fn=lambda: T, warm_dir=None)
    h.check_warm_frames()
    assert msgs == []


# ---- blackout ignore-list (a fresh cam on a separate bridge must not mask a
# blackout on the shared bridge) --------------------------------------------

def test_warm_ignored_fresh_cam_does_not_mask_real_blackout(tmp_path):
    # regression: meowcam4 lives on its own host and stays fresh while the
    # shared-bridge cams (1,2,3,5,6) all go stale during a real outage. Without
    # the ignore-list, meowcam4's freshness silently suppressed the alert for
    # ~21h. With meowcam4 ignored, the alert must fire.
    conn = _db(tmp_path)
    wd = _warm(tmp_path, {
        "meowcam1": 600, "meowcam2": 600, "meowcam3": 600,
        "meowcam5": 600, "meowcam6": 600, "meowcam4": 5,
    })
    msgs = []
    h = CaptureHealth(conn, _cams("meowcam1", "meowcam2", "meowcam3",
                                   "meowcam5", "meowcam6", "meowcam4"),
                      notify=msgs.append, now_fn=lambda: T, warm_dir=wd,
                      warm_stale_seconds=180, blackout_ignore_cams=["meowcam4"])
    h.check_warm_frames()
    assert len(msgs) == 1 and "blind" in msgs[0].lower()


def test_warm_ignore_one_non_ignored_fresh_suppresses_and_rearms(tmp_path):
    conn = _db(tmp_path)
    wd = _warm(tmp_path, {"meowcam1": 5, "meowcam2": 600, "meowcam4": 9000})
    msgs = []
    h = CaptureHealth(conn, _cams("meowcam1", "meowcam2", "meowcam4"),
                      notify=msgs.append, now_fn=lambda: T, warm_dir=wd,
                      warm_stale_seconds=180, blackout_ignore_cams=["meowcam4"])
    h.check_warm_frames()
    assert msgs == []


def test_warm_ignored_cam_stale_non_ignored_fresh_no_alert(tmp_path):
    conn = _db(tmp_path)
    wd = _warm(tmp_path, {"meowcam1": 5, "meowcam4": 9000})
    msgs = []
    h = CaptureHealth(conn, _cams("meowcam1", "meowcam4"), notify=msgs.append,
                      now_fn=lambda: T, warm_dir=wd, warm_stale_seconds=180,
                      blackout_ignore_cams=["meowcam4"])
    h.check_warm_frames()
    assert msgs == []


def test_warm_empty_ignore_list_behaves_as_before(tmp_path):
    # explicit empty list == the old all-stale-required behavior: one fresh
    # cam (even meowcam4) suppresses the alert.
    conn = _db(tmp_path)
    wd = _warm(tmp_path, {"meowcam1": 600, "meowcam4": 5})
    msgs = []
    h = CaptureHealth(conn, _cams("meowcam1", "meowcam4"), notify=msgs.append,
                      now_fn=lambda: T, warm_dir=wd, warm_stale_seconds=180,
                      blackout_ignore_cams=[])
    h.check_warm_frames()
    assert msgs == []


def test_warm_ignore_list_covers_every_camera_falls_back_to_all_cams(tmp_path):
    # if every camera is on the ignore-list, honoring it literally would make
    # the alert impossible to ever fire -> fall back to considering all cams.
    conn = _db(tmp_path)
    wd = _warm(tmp_path, {"meowcam1": 600, "meowcam2": 600})
    msgs = []
    h = CaptureHealth(conn, _cams("meowcam1", "meowcam2"), notify=msgs.append,
                      now_fn=lambda: T, warm_dir=wd, warm_stale_seconds=180,
                      blackout_ignore_cams=["meowcam1", "meowcam2"])
    h.check_warm_frames()
    assert len(msgs) == 1 and "blind" in msgs[0].lower()


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
