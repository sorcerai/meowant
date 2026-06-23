"""Remediator core: rate-limit -> run playbook -> log incident -> escalate."""
from mw import store
from mw.remediation import Remediator
from mw.remediation import labeler_stall_playbook
from mw.remediation import stream_down_playbook

T = 1_000_000.0


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


def _escalating_playbook():
    return {"action": "diagnosed: broken", "resolved": False,
            "escalate": "🚨 thing is broken"}


def _recovering_playbook():
    return {"action": "re-probed: UP", "resolved": True, "escalate": ""}


def test_unresolved_incident_logs_and_escalates(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    r = Remediator(conn, notify=msgs.append, now_fn=lambda: T)
    outcome = r.handle("stream_down", {"camera": "c"}, _escalating_playbook)
    assert outcome == "escalated"
    assert msgs == ["🚨 thing is broken"]
    rows = store.recent_incidents(conn)
    assert rows[0]["outcome"] == "escalated"
    assert rows[0]["action_taken"] == "diagnosed: broken"


def test_resolved_incident_logs_but_does_not_escalate(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    r = Remediator(conn, notify=msgs.append, now_fn=lambda: T)
    outcome = r.handle("stream_down", {"camera": "c"}, _recovering_playbook)
    assert outcome == "recovered"
    assert msgs == []                                   # good news = no alert
    assert store.recent_incidents(conn)[0]["outcome"] == "recovered"


def test_rate_limit_suppresses_after_threshold(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    r = Remediator(conn, notify=msgs.append, now_fn=lambda: T,
                   max_per_window=2, window_s=3600)
    r.handle("stream_down", {}, _escalating_playbook)   # 1 -> escalate
    r.handle("stream_down", {}, _escalating_playbook)   # 2 -> escalate
    outcome = r.handle("stream_down", {}, _escalating_playbook)  # 3 -> suppressed
    assert outcome == "suppressed"
    assert len(msgs) == 2                               # third did not alert
    assert store.recent_incidents(conn)[0]["outcome"] == "suppressed"


def test_rate_limit_counts_only_escalations_not_recoveries(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    r = Remediator(conn, notify=msgs.append, now_fn=lambda: T,
                   max_per_window=1, window_s=3600)
    r.handle("stream_down", {}, _recovering_playbook)   # recovered, doesn't count
    r.handle("stream_down", {}, _recovering_playbook)   # recovered, doesn't count
    outcome = r.handle("stream_down", {}, _escalating_playbook)  # first escalation
    assert outcome == "escalated"
    assert msgs == ["🚨 thing is broken"]


def test_rate_limit_window_expires(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    r = Remediator(conn, notify=msgs.append, now_fn=lambda: T,
                   max_per_window=1, window_s=3600)
    r.handle("stream_down", {}, _escalating_playbook)   # escalate at T
    r.now = lambda: T + 4000                            # past the 3600s window
    outcome = r.handle("stream_down", {}, _escalating_playbook)
    assert outcome == "escalated"                       # window cleared -> alerts again
    assert len(msgs) == 2


def test_labeler_playbook_agy_missing_says_restart_wont_help():
    res = labeler_stall_playbook(7, which=lambda name: None)
    assert res["resolved"] is False
    assert "not on the daemon PATH" in res["escalate"]
    assert "MISSING" in res["action"]


def test_labeler_playbook_agy_present_warns_against_restart():
    res = labeler_stall_playbook(3, which=lambda name: "/usr/local/bin/agy")
    assert res["resolved"] is False
    assert "7" not in res["escalate"]                  # uses the real count
    assert "3 frame" in res["escalate"]
    assert "restart" in res["escalate"].lower()        # explicitly NOT restarting
    assert "/usr/local/bin/agy" in res["action"]


def test_stream_playbook_recovers_silently_when_reprobe_up():
    res = stream_down_playbook("meowcam3", reprobe=lambda: True,
                               sleep=lambda s: None)
    assert res["resolved"] is True
    assert res["escalate"] == ""
    assert "UP" in res["action"]


def test_stream_playbook_escalates_when_still_down():
    res = stream_down_playbook("meowcam3", reprobe=lambda: False,
                               sleep=lambda s: None)
    assert res["resolved"] is False
    assert "meowcam3" in res["escalate"] and "DOWN" in res["escalate"]


def test_stream_playbook_waits_before_reprobing():
    waited = []
    stream_down_playbook("c", reprobe=lambda: True,
                         sleep=lambda s: waited.append(s), wait_s=7)
    assert waited == [7]                                # debounce delay honored


def test_raising_playbook_fails_loud_not_silent(tmp_path):
    conn = _db(tmp_path)
    msgs = []
    r = Remediator(conn, notify=msgs.append, now_fn=lambda: T)

    def _boom():
        raise ValueError("playbook blew up")

    outcome = r.handle("labeler_stall", {"x": 1}, _boom)   # must NOT propagate
    assert outcome == "failed"
    assert len(msgs) == 1 and "ERRORED" in msgs[0]          # owner was paged
    row = store.recent_incidents(conn)[0]                   # failure was recorded
    assert row["outcome"] == "failed"
    assert "playbook raised" in row["action_taken"]


def test_handle_survives_a_broken_audit_log(tmp_path):
    # If even the incident write is what's broken, the owner must STILL be paged.
    conn = _db(tmp_path)
    msgs = []
    r = Remediator(conn, notify=msgs.append, now_fn=lambda: T)
    conn.close()                                            # force every store call to raise
    outcome = r.handle("stream_down", {}, lambda: {"action": "a", "resolved": False,
                                                   "escalate": "x"})
    assert outcome == "failed"
    assert msgs and "ERRORED" in msgs[0]                    # paged despite dead DB
