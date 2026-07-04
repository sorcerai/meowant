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
    kw.setdefault("lag_s", 0)          # legacy tests: evaluate immediately
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
    w = JamWatch(conn, _Boom(), n, k=3, lag_s=0, now_fn=lambda: 1782583200.0)
    w.check_once()                                   # must not raise
    assert len(n.msgs) == 1                          # no evidence -> streak


# ---- globe-tipping era fixes: evidence-first, attribution lag, all frames --

def _elim_visit_at(tmp_path, conn, with_cat, ts, n_frames=4, cat_id=None):
    vid = store.open_visit(conn, ts)
    conn.execute("UPDATE visits SET eliminated=1, leave_ts=?, cat_id=? WHERE id=?",
                 (store._iso(ts + 60), cat_id, vid))
    conn.commit()
    tag = "cat" if with_cat else "nocat"
    for i in range(n_frames):
        p = str(tmp_path / f"v{vid}_{tag}_{i}.jpg")
        open(p, "wb").write(b"jpg")
        store.insert_capture(conn, ts, vid, "meowcam1", p)
    return vid


NOW = 1782583200.0
OLD = NOW - 3600          # closed long ago: attribution has definitely run


def test_attributed_visit_is_cat_evidence_without_cameras(tmp_path):
    """Sealed-globe visit: frames show nothing, but the matcher/agy already
    named the cat in the DB. Jam-watch must trust that, not its own eyes."""
    conn = _db(tmp_path)
    for _ in range(3):
        _elim_visit_at(tmp_path, conn, with_cat=False, ts=OLD, cat_id=1)
    n = _Notify()
    w = _watch(conn, n, k=3)
    w.check_once()
    assert n.msgs == []                      # attributed => not phantom
    assert w.catfilter.calls == 0            # DB evidence: no camera pass needed


def test_capture_label_counts_as_evidence(tmp_path):
    conn = _db(tmp_path)
    for _ in range(3):
        vid = _elim_visit_at(tmp_path, conn, with_cat=False, ts=OLD)
        cid = store.captures_for_visit(conn, vid)[0]["id"]
        conn.execute("UPDATE captures SET label=2, label_source='auto' WHERE id=?", (cid,))
        conn.commit()
    n = _Notify()
    _watch(conn, n, k=3).check_once()
    assert n.msgs == []                      # agy named frames: cat was there


def test_young_visits_wait_for_attribution(tmp_path):
    """A visit closed seconds ago hasn't been scored yet — evaluating it now
    would count a soon-to-be-named visit as phantom. Defer, keep cursor."""
    conn = _db(tmp_path)
    for _ in range(3):
        _elim_visit_at(tmp_path, conn, with_cat=False, ts=NOW - 120)  # 1 min old
    n = _Notify()
    w = _watch(conn, n, k=3, lag_s=1200)
    w.check_once()
    assert n.msgs == [] and w._streak() == 0    # nothing evaluated yet
    w2 = JamWatch(conn, w.catfilter, n, k=3, lag_s=1200,
                  now_fn=lambda: NOW + 3600)    # later: visits now old enough
    w2.check_once()
    assert len(n.msgs) == 1                     # evaluated and correctly flagged


def test_frames_per_visit_uses_spread_sample_finds_last_frame(tmp_path):
    """With a bounded per-visit sample, the old int(i*len/n) index formula could
    never land on the final frame — exactly where the cat shows up here. The
    shared spread_sample helper must include it, or this false-alarms a JAM."""
    conn = _db(tmp_path)
    for _ in range(3):
        vid = store.open_visit(conn, OLD)
        conn.execute("UPDATE visits SET eliminated=1, leave_ts=? WHERE id=?",
                     (store._iso(OLD + 60), vid))
        conn.commit()
        for i in range(36):
            tag = "cat" if i == 35 else "nocat"
            p = str(tmp_path / f"v{vid}_{tag}_{i}.jpg")
            open(p, "wb").write(b"jpg")
            store.insert_capture(conn, OLD, vid, "meowcam1", p)
    n = _Notify()
    _watch(conn, n, k=3, frames_per_visit=8).check_once()
    assert n.msgs == []                      # last (cat) frame was sampled -> no alert


def test_too_young_uses_store_parse_ts_naive_local_convention(tmp_path):
    """_too_young must parse timestamps through store._parse_ts (naive-local
    convention: a tz suffix is dropped, not converted) rather than hand-rolling
    fromisoformat().timestamp(), which would treat a tz-aware string as an
    absolute UTC-relative instant and disagree with the rest of the codebase."""
    conn = _db(tmp_path)
    leave_ts = "2026-07-04T00:00:00+00:00"   # legacy tz-suffixed row
    vid = store.open_visit(conn, NOW)
    conn.execute("UPDATE visits SET eliminated=1, leave_ts=? WHERE id=?", (leave_ts, vid))
    conn.commit()
    expected_closed = store._parse_ts(leave_ts).timestamp()
    w_inside = JamWatch(conn, _FakeFilter(), _Notify(), k=3, lag_s=1200,
                        now_fn=lambda: expected_closed + 1199)
    assert w_inside._too_young(vid) is True          # still inside the lag window
    w_outside = JamWatch(conn, _FakeFilter(), _Notify(), k=3, lag_s=1200,
                         now_fn=lambda: expected_closed + 1201)
    assert w_outside._too_young(vid) is False        # past the lag window


def test_cat_in_last_frame_is_found(tmp_path):
    """Entry/exit visibility: cat appears in ONE late frame of many. The old
    8-frame sample missed it; the sweep must check every existing frame."""
    conn = _db(tmp_path)
    for _ in range(3):
        vid = store.open_visit(conn, OLD)
        conn.execute("UPDATE visits SET eliminated=1, leave_ts=? WHERE id=?",
                     (store._iso(OLD + 60), vid))
        conn.commit()
        for i in range(36):
            tag = "cat" if i == 35 else "nocat"
            p = str(tmp_path / f"v{vid}_{tag}_{i}.jpg")
            open(p, "wb").write(b"jpg")
            store.insert_capture(conn, OLD, vid, "meowcam1", p)
    n = _Notify()
    _watch(conn, n, k=3).check_once()
    assert n.msgs == []                      # late-frame cat found => no alert
