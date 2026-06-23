"""Remediator core: rate-limit -> run playbook -> log incident -> escalate."""
from mw import store
from mw.remediation import Remediator

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
