"""incidents table: append-only audit/runbook for watchdog episodes."""
from mw import store

T = 1_000_000.0


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


def test_log_and_recent_roundtrip_parses_signal(tmp_path):
    conn = _db(tmp_path)
    store.log_incident(conn, "stream_down", {"camera": "meowcam3"},
                       "re-probed after 5s: still DOWN", "escalated", ts=T)
    rows = store.recent_incidents(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "stream_down"
    assert r["signal"] == {"camera": "meowcam3"}      # parsed back to a dict
    assert r["outcome"] == "escalated"
    assert "still DOWN" in r["action_taken"]


def test_recent_is_newest_first_and_limited(tmp_path):
    conn = _db(tmp_path)
    for i in range(5):
        store.log_incident(conn, "labeler_stall", {"n": i}, "diag", "escalated",
                           ts=T + i)
    rows = store.recent_incidents(conn, limit=3)
    assert [r["signal"]["n"] for r in rows] == [4, 3, 2]


def test_incidents_since_counts_by_kind_and_outcome(tmp_path):
    conn = _db(tmp_path)
    store.log_incident(conn, "stream_down", {}, "a", "escalated", ts=T)
    store.log_incident(conn, "stream_down", {}, "b", "recovered", ts=T + 10)
    store.log_incident(conn, "stream_down", {}, "c", "escalated", ts=T + 20)
    store.log_incident(conn, "labeler_stall", {}, "d", "escalated", ts=T + 30)
    after = store._iso(T - 1)
    assert store.incidents_since(conn, "stream_down", after) == 3
    assert store.incidents_since(conn, "stream_down", after,
                                 outcomes=("escalated",)) == 2
    # window excludes older rows
    assert store.incidents_since(conn, "stream_down", store._iso(T + 15)) == 1


def test_rollup_groups_kind_outcome(tmp_path):
    conn = _db(tmp_path)
    store.log_incident(conn, "stream_down", {}, "a", "escalated", ts=T)
    store.log_incident(conn, "stream_down", {}, "b", "escalated", ts=T + 1)
    store.log_incident(conn, "labeler_stall", {}, "c", "recovered", ts=T + 2)
    roll = {(r["kind"], r["outcome"]): r["n"] for r in store.incident_rollup(conn)}
    assert roll[("stream_down", "escalated")] == 2
    assert roll[("labeler_stall", "recovered")] == 1
