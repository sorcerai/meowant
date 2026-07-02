"""LitterWatch: dp101 (contents_load) is a litter-mass load cell. Alert the
sitters when litter runs low — sampled only in standby (a cat on the load cell
or a mid-clean drum pollutes the reading), sustained across M samples (one
weird poll must not ping anyone), with hysteresis so refill re-arms cleanly."""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mw import store
from mw.litter_watch import LitterWatch


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


class _Notify:
    def __init__(self):
        self.msgs = []

    def __call__(self, m):
        self.msgs.append(m)


def _watch(tmp_path, conn, notify, dps_seq, **kw):
    """LitterWatch fed from a scripted sequence of DPS snapshots."""
    seq = list(dps_seq)

    def state_fn():
        return seq.pop(0) if seq else {}

    kw.setdefault("low_threshold", 110)
    kw.setdefault("consecutive", 3)
    return LitterWatch(conn, state_fn, notify,
                       log_path=str(tmp_path / "litter.jsonl"),
                       now_fn=lambda: 1782583200.0, **kw)


def _standby(load):
    return {"24": "standby", "101": load}


def test_sustained_low_alerts_once(tmp_path):
    conn = _db(tmp_path)
    n = _Notify()
    w = _watch(tmp_path, conn, n, [_standby(96)] * 5)
    for _ in range(5):
        w.sample_once()
    assert len(n.msgs) == 1
    assert "litter" in n.msgs[0].lower() and "low" in n.msgs[0].lower()


def test_single_low_sample_is_not_enough(tmp_path):
    conn = _db(tmp_path)
    n = _Notify()
    w = _watch(tmp_path, conn, n, [_standby(300), _standby(96), _standby(300)])
    for _ in range(3):
        w.sample_once()
    assert n.msgs == []


def test_non_standby_samples_ignored(tmp_path):
    conn = _db(tmp_path)
    n = _Notify()
    seq = [{"24": "cleaning", "101": 20},        # drum mid-flip: garbage reading
           {"24": "cat_get_in", "101": 500},     # cat on the load cell
           _standby(300)]
    w = _watch(tmp_path, conn, n, seq)
    for _ in range(3):
        w.sample_once()
    assert n.msgs == []
    recs = [json.loads(l) for l in open(str(tmp_path / "litter.jsonl"))]
    assert len(recs) == 1 and recs[0]["load"] == 300   # only standby logged


def test_refill_rearms_with_hysteresis(tmp_path):
    conn = _db(tmp_path)
    n = _Notify()
    seq = ([_standby(96)] * 3            # low -> alert
           + [_standby(115)] * 3         # above threshold but inside hysteresis band
           + [_standby(200)] * 1         # true refill -> re-arm
           + [_standby(96)] * 3)         # low again -> second alert
    w = _watch(tmp_path, conn, n, seq, rearm_margin=40)
    for _ in range(10):
        w.sample_once()
    assert len(n.msgs) == 2


def test_latch_survives_restart(tmp_path):
    conn = _db(tmp_path)
    n = _Notify()
    w = _watch(tmp_path, conn, n, [_standby(96)] * 3)
    for _ in range(3):
        w.sample_once()
    assert len(n.msgs) == 1
    n2 = _Notify()
    w2 = _watch(tmp_path, conn, n2, [_standby(96)] * 3)
    for _ in range(3):
        w2.sample_once()
    assert n2.msgs == []                 # still low, already alerted: silent


def test_missing_dp101_is_skipped(tmp_path):
    conn = _db(tmp_path)
    n = _Notify()
    w = _watch(tmp_path, conn, n, [{"24": "standby"}, {}, _standby(96)])
    for _ in range(3):
        w.sample_once()
    assert n.msgs == []                  # only one valid low sample so far
