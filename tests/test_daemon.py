# tests/test_daemon.py
from mw import store
from mw.device import FakeDevice
from mw.daemon import Daemon
from mw.smartclean import SmartClean

def make(tmp_path, snapshots, idle=90):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    dev = FakeDevice(snapshots)
    clock = {"t": 0.0}
    def now(): return clock["t"]
    d = Daemon(dev, conn, SmartClean(idle_seconds=idle), now_fn=now)
    return conn, dev, d, clock

def test_full_visit_with_elimination_records_one_visit(tmp_path):
    snaps = [
        {"24": "standby", "7": 1, "21": 0},
        {"24": "cat_get_in", "7": 1, "21": 0},
        {"24": "cat_get_in", "7": 1, "21": 0, "102": "AOMAAA=="},  # use record (227)
        {"24": "standby", "7": 1, "21": 0},
    ]
    conn, dev, d, clock = make(tmp_path, snaps)
    for i in range(len(snaps)):
        clock["t"] = 100.0 + i * 10
        d.tick()
    rows = store.recent_visits(conn, 10)
    assert len(rows) == 1
    assert rows[0]["eliminated"] == 1 and rows[0]["use_record"] == 227

def test_smartclean_triggers_clean_after_idle(tmp_path):
    snaps = [{"24": "cat_get_in"}] + [{"24": "standby"}] * 5
    conn, dev, d, clock = make(tmp_path, snaps, idle=20)
    for i in range(len(snaps)):
        clock["t"] = i * 10  # 0,10,20,30,40,50
        d.tick()
    assert dev.clean_calls >= 1

def test_no_clean_while_cat_present(tmp_path):
    snaps = [{"24": "cat_get_in"}] * 6
    conn, dev, d, clock = make(tmp_path, snaps, idle=1)
    for i in range(len(snaps)):
        clock["t"] = i * 10
        d.tick()
    assert dev.clean_calls == 0

def test_partial_poll_missing_dp24_does_not_reset_idle_timer(tmp_path):
    """C2: A partial poll missing key '24' must not reset the smart-clean idle timer.

    Sequence (first tick is baseline; cat_get_in arms smartclean):
      t=0   baseline: cat present (arms)
      t=5   full poll: cat leaves -> standby (idle clock starts at 5)
      t=10  partial poll: only dp102 present (no '24') — must NOT reset timer
      t=25  full poll: still standby — idle (20s) reached from t=5, clean fires
    If the partial poll HAD reset the timer (sb=10), 25-10=15 < 20 would not fire.
    """
    snaps = [
        {"24": "cat_get_in"},       # t=0: baseline, arms smartclean
        {"24": "standby"},          # t=5: standby starts (sb=5)
        {"102": "AOMAAA=="},        # t=10: partial, no "24"
        {"24": "standby"},          # t=25: still standby; idle >= 20 -> fires
    ]
    conn, dev, d, clock = make(tmp_path, snaps, idle=20)

    clock["t"] = 0.0;  d.tick()
    clock["t"] = 5.0;  d.tick()
    clock["t"] = 10.0; d.tick()
    clock["t"] = 25.0; d.tick()

    assert dev.clean_calls >= 1, (
        "smart-clean should have fired; partial poll must not reset the idle timer"
    )


def test_first_poll_is_baseline_no_events(tmp_path):
    """The first successful poll establishes baseline and emits nothing."""
    snaps = [
        {"24": "cat_get_in", "7": 1, "102": "AOMAAA=="},  # baseline snapshot
        {"24": "standby", "7": 1},                          # second tick
    ]
    conn, dev, d, clock = make(tmp_path, snaps)

    clock["t"] = 100.0
    d.tick()  # baseline: no events, no visit rows
    assert store.recent_visits(conn, 10) == []
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_tick_survives_exception(tmp_path):
    """A device that raises on first poll must not propagate; recovers next tick."""
    class FlakyDevice(FakeDevice):
        def __init__(self, snapshots):
            super().__init__(snapshots)
            self._raised = False

        def status_dps(self):
            if not self._raised:
                self._raised = True
                raise RuntimeError("hung socket")
            return super().status_dps()

    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    dev = FlakyDevice([{"24": "standby"}])
    clock = {"t": 0.0}
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: clock["t"])

    clock["t"] = 1.0
    d.tick()  # raises internally, swallowed
    assert d.last_ok_ts is None  # never had a good poll yet

    clock["t"] = 2.0
    d.tick()  # recovers
    assert d.last_ok_ts == 2.0


def test_startup_reconcile(tmp_path):
    """A pre-existing open visit is closed when the Daemon is constructed."""
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    vid = store.open_visit(conn, 500.0)  # left open before daemon starts
    dev = FakeDevice([{"24": "standby"}])
    Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0)
    row = conn.execute("SELECT leave_ts, duration_s FROM visits WHERE id=?",
                       (vid,)).fetchone()
    assert row["leave_ts"] is not None
    assert row["duration_s"] == 0
