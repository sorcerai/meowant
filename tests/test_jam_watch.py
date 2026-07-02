"""JamWatch: cross-sensor jam detection. The box logs eliminated visits while
no camera frame contains a cat -> after K consecutive such visits the drum is
likely stuck (fault-free firmware, dp22=0), the deadman is being pacified by
phantom eliminations, and the OWNER must be told. A visible cat re-arms it."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mw import store
from mw.jam_watch import JamWatch


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


def _elim_visit(tmp_path, conn, with_cat, n_frames=4, ts=1782583200.0):
    """Eliminated visit whose frames exist on disk; filenames carry the
    cat/nocat marker the fake filter keys on."""
    vid = store.open_visit(conn, ts)
    conn.execute("UPDATE visits SET eliminated=1 WHERE id=?", (vid,))
    conn.commit()
    tag = "cat" if with_cat else "nocat"
    for i in range(n_frames):
        p = str(tmp_path / f"v{vid}_{tag}_{i}.jpg")
        with open(p, "wb") as f:
            f.write(b"jpg")
        store.insert_capture(conn, ts, vid, "meowcam1", p)
    return vid


class _FakeFilter:
    def __init__(self):
        self.calls = 0

    def has_cat(self, path):
        self.calls += 1
        return "_cat_" in os.path.basename(path)


class _Notify:
    def __init__(self):
        self.msgs = []

    def __call__(self, msg):
        self.msgs.append(msg)


def _watch(conn, notify, k=3, **kw):
    return JamWatch(conn, _FakeFilter(), notify, k=k,
                    now_fn=lambda: 1782583200.0, **kw)


def test_k_consecutive_no_cat_visits_alert_once(tmp_path):
    conn = _db(tmp_path)
    for _ in range(3):
        _elim_visit(tmp_path, conn, with_cat=False)
    n = _Notify()
    w = _watch(conn, n, k=3)
    w.check_once()
    assert len(n.msgs) == 1 and "JAM" in n.msgs[0].upper()
    w.check_once()                                   # no new visits ->
    assert len(n.msgs) == 1                          # no re-alert spam


def test_visible_cat_resets_streak_no_alert(tmp_path):
    conn = _db(tmp_path)
    _elim_visit(tmp_path, conn, with_cat=False)
    _elim_visit(tmp_path, conn, with_cat=False)
    _elim_visit(tmp_path, conn, with_cat=True)       # cat seen -> streak dies
    _elim_visit(tmp_path, conn, with_cat=False)
    _elim_visit(tmp_path, conn, with_cat=False)
    n = _Notify()
    _watch(conn, n, k=3).check_once()
    assert n.msgs == []


def test_below_k_no_alert(tmp_path):
    conn = _db(tmp_path)
    for _ in range(2):
        _elim_visit(tmp_path, conn, with_cat=False)
    n = _Notify()
    _watch(conn, n, k=3).check_once()
    assert n.msgs == []


def test_cat_after_alert_sends_cleared_and_rearms(tmp_path):
    conn = _db(tmp_path)
    for _ in range(3):
        _elim_visit(tmp_path, conn, with_cat=False)
    n = _Notify()
    w = _watch(conn, n, k=3)
    w.check_once()
    assert len(n.msgs) == 1
    _elim_visit(tmp_path, conn, with_cat=True)
    w.check_once()
    assert len(n.msgs) == 2 and "clear" in n.msgs[1].lower()
    for _ in range(3):                               # re-armed: jams again
        _elim_visit(tmp_path, conn, with_cat=False)
    w.check_once()
    assert len(n.msgs) == 3 and "JAM" in n.msgs[2].upper()


def test_missing_frame_files_count_as_no_cat(tmp_path):
    conn = _db(tmp_path)
    for _ in range(3):
        vid = _elim_visit(tmp_path, conn, with_cat=True)
        for c in store.captures_for_visit(conn, vid):
            os.remove(c["path"])                     # pruned/blind visit
    n = _Notify()
    _watch(conn, n, k=3).check_once()
    assert len(n.msgs) == 1                          # no evidence != healthy


def test_state_survives_restart(tmp_path):
    conn = _db(tmp_path)
    for _ in range(3):
        _elim_visit(tmp_path, conn, with_cat=False)
    n = _Notify()
    _watch(conn, n, k=3).check_once()
    assert len(n.msgs) == 1
    n2 = _Notify()
    _watch(conn, n2, k=3).check_once()               # fresh instance, same db
    assert n2.msgs == []                             # latch persisted: no re-alert


def test_first_run_seeds_cursor_to_recent_window(tmp_path):
    conn = _db(tmp_path)
    for _ in range(30):                              # ancient pruned history
        vid = _elim_visit(tmp_path, conn, with_cat=True)
        for c in store.captures_for_visit(conn, vid):
            os.remove(c["path"])
    n = _Notify()
    w = _watch(conn, n, k=3)
    w.check_once()
    f = w.catfilter
    assert f.calls == 0                              # deleted files never probed
    # only the last 2k eliminated visits were considered at all
    assert w._streak() <= 2 * 3


def test_filter_crash_is_not_fatal_counts_as_no_evidence(tmp_path):
    conn = _db(tmp_path)

    class _Boom:
        def has_cat(self, path):
            raise RuntimeError("mps fell over")

    for _ in range(3):
        _elim_visit(tmp_path, conn, with_cat=True)
    n = _Notify()
    w = JamWatch(conn, _Boom(), n, k=3, now_fn=lambda: 1782583200.0)
    w.check_once()                                   # must not raise
    assert len(n.msgs) == 1                          # no evidence -> streak
