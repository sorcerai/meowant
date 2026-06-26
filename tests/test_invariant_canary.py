"""Invariant canary: raw eliminations vs attributed (labeled) ones; fire on a
sustained attribution-rate drop (the labeler silently eating health events)."""
from mw import store
from mw.invariant_canary import InvariantCanary

T = 1_000_000.0


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    return conn


def _elim(conn, enter, attributed, frames=1):
    cid = store.cat_id_by_name(conn, "Ucok")
    vid = store.open_visit(conn, enter)
    store.mark_elimination(conn, vid, 50)
    for i in range(frames):
        store.insert_capture(conn, enter + i, vid, "cam", f"/tmp/{vid}_{i}.jpg")
    if attributed:
        store.set_visit_identity(conn, vid, cid, 0.9)


def test_healthy_attribution_is_silent(tmp_path):
    conn = _db(tmp_path)
    for i in range(6):
        _elim(conn, T - 10000 - i, attributed=True)     # all labeled, past grace
    msgs = []
    c = InvariantCanary(conn, notify=msgs.append, now_fn=lambda: T)
    c.run_once()
    assert msgs == []


def test_low_attribution_fires_once_then_latches(tmp_path):
    conn = _db(tmp_path)
    for i in range(5):
        _elim(conn, T - 10000 - i, attributed=False)    # 5 framed raw, 0 attributed
    msgs = []
    c = InvariantCanary(conn, notify=msgs.append, now_fn=lambda: T,
                        min_sample=4, min_ratio=0.5)
    c.run_once()
    c.run_once()                                          # still bad -> no repeat
    assert len(msgs) == 1
    assert "attribution canary" in msgs[0].lower() and "0/5" in msgs[0]


def test_insufficient_sample_is_silent(tmp_path):
    conn = _db(tmp_path)
    for i in range(2):
        _elim(conn, T - 10000 - i, attributed=False)    # only 2 < min_sample(4)
    msgs = []
    c = InvariantCanary(conn, notify=msgs.append, now_fn=lambda: T, min_sample=4)
    c.run_once()
    assert msgs == []                                     # can't judge -> no false alarm


def test_recent_visits_inside_grace_are_not_counted(tmp_path):
    conn = _db(tmp_path)
    for i in range(5):
        _elim(conn, T - 60 * i, attributed=False)
    msgs = []
    c = InvariantCanary(conn, notify=msgs.append, now_fn=lambda: T,
                        grace_hours=2, min_sample=4)
    c.run_once()
    assert msgs == []


def test_recovery_rearms_the_alarm(tmp_path):
    conn = _db(tmp_path)
    for i in range(5):
        _elim(conn, T - 10000 - i, attributed=False)
    msgs = []
    c = InvariantCanary(conn, notify=msgs.append, now_fn=lambda: T,
                        min_sample=4, min_ratio=0.5)
    c.run_once()
    assert len(msgs) == 1 and c._alarmed is True
    for i in range(10):
        _elim(conn, T - 9000 - i, attributed=True)
    c.run_once()
    assert len(msgs) == 1 and c._alarmed is False


def test_failed_delivery_does_not_latch(tmp_path):
    conn = _db(tmp_path)
    for i in range(5):
        _elim(conn, T - 10000 - i, attributed=False)
    sent = []

    def _notify(m):
        sent.append(m)
        return False

    c = InvariantCanary(conn, notify=_notify, now_fn=lambda: T,
                        min_sample=4, min_ratio=0.5)
    c.run_once()
    c.run_once()
    assert len(sent) == 2


def test_high_frameless_ratio_fires_observability_alarm(tmp_path):
    conn = _db(tmp_path)
    for i in range(5):
        _elim(conn, T - 10000 - i, attributed=False, frames=0)  # 5 frameless
    msgs = []
    c = InvariantCanary(conn, notify=msgs.append, now_fn=lambda: T,
                        min_sample=4, min_ratio=0.5)
    c.run_once()
    assert len(msgs) == 1
    assert "observability canary" in msgs[0].lower() and "5/5" in msgs[0]


def test_min_sample_zero_does_not_zero_divide(tmp_path):
    conn = _db(tmp_path)
    # no eliminations, so raw=0
    c = InvariantCanary(conn, notify=lambda m: None, now_fn=lambda: T, min_sample=0)
    # should not raise ZeroDivisionError because min_sample is clamped to >=1
    # meaning raw (0) >= min_sample (1) will be False
    c.run_once()
