"""dp101 (litter+cat load) min/max tracking during visits — the weight dataset
that identifies a cat when the globe tips closed and every camera goes blind."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mw import store
from mw.daemon import Daemon
from mw.device import FakeDevice
from mw.tracker import VisitTracker


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    return conn


def test_update_visit_load_tracks_min_max(tmp_path):
    conn = _db(tmp_path)
    v = store.open_visit(conn, 1.0)
    store.update_visit_load(conn, v, 300)
    store.update_visit_load(conn, v, 350)
    store.update_visit_load(conn, v, 280)
    row = store.get_visit(conn, v)
    assert row["contents_load_min"] == 280 and row["contents_load_max"] == 350


def test_tracker_observes_load_only_while_open(tmp_path):
    conn = _db(tmp_path)
    t = VisitTracker(conn)
    t.observe_load({"101": 500})                    # no open visit: no crash, no row
    from mw.events import Event, CAT_ENTER, CAT_LEAVE
    t.handle(Event(CAT_ENTER, 10.0))
    t.observe_load({"101": 310})
    t.observe_load({"24": "cat_get_in"})            # no dp101 in partial poll: skip
    t.handle(Event(CAT_LEAVE, 20.0))
    t.observe_load({"101": 225})                    # closed: post-visit litter ignored
    row = store.get_visit(conn, 1)
    assert row["contents_load_min"] == 310 and row["contents_load_max"] == 310


class _NullSmartClean:
    def update(self, dps, now):
        return False
    def notify_cleaned(self):
        pass


def test_daemon_ticks_feed_visit_load(tmp_path):
    conn = _db(tmp_path)
    dev = FakeDevice([
        {"24": "standby", "101": 225},              # baseline tick (no events)
        {"24": "cat_get_in", "101": 300},           # enter + cat weight
        {"24": "cat_get_in", "101": 350},           # digging
        {"24": "standby", "101": 225},              # leave
    ])
    d = Daemon(dev, conn, _NullSmartClean(), now_fn=lambda: 1000.0)
    for _ in range(4):
        d.tick()
    row = store.get_visit(conn, 1)
    assert row is not None
    assert row["contents_load_min"] == 300 and row["contents_load_max"] == 350
