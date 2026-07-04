"""Test CaptureService event handling and frame grabbing."""
import os
from mw.bus import EventBus
from mw.events import Event, CAT_ENTER, CAT_LEAVE
from mw.capture import CaptureService


def fake_grabber(url, path, timeout=15):
    with open(path, "w") as f:   # write a stand-in "frame"
        f.write(url)
    return path


def test_cat_enter_grabs_each_camera(tmp_path):
    bus = EventBus()
    cams = [{"name": "front", "url": "rtsp://x/front"},
            {"name": "side", "url": "rtsp://x/side"}]
    recorded = []
    cs = CaptureService(bus, cams, str(tmp_path), grabber=fake_grabber,
                        on_capture=lambda name, path, ts, vid: recorded.append((name, path, ts)))
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    assert len(recorded) == 2
    names = sorted(r[0] for r in recorded)
    assert names == ["front", "side"]
    for _, path, _ in recorded:
        assert os.path.exists(path)


def test_non_enter_event_ignored(tmp_path):
    bus = EventBus()
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber, on_capture=lambda *a: (_ for _ in ()).throw(AssertionError()))
    bus.publish(Event(CAT_LEAVE, 1.0))
    cs.run_once()  # must not call on_capture


def test_spaced_burst_grabs_frames_per_camera(tmp_path):
    bus = EventBus()
    cams = [{"name": "front", "url": "rtsp://x/front"},
            {"name": "side", "url": "rtsp://x/side"}]
    recorded = []
    slept = []
    cs = CaptureService(bus, cams, str(tmp_path), grabber=fake_grabber,
                        on_capture=lambda name, path, ts, vid: recorded.append(path),
                        frames=3, interval_s=3.0, sleep=slept.append)
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    assert len(recorded) == 6                 # 3 frames × 2 cams
    assert len(set(recorded)) == 6            # unique paths (different poses)
    assert slept == [3.0, 3.0]                # spaced between rounds, not after the last


def test_failed_grab_does_not_stop_others(tmp_path):
    bus = EventBus()
    cams = [{"name": "bad", "url": "u1"}, {"name": "good", "url": "u2"}]
    def grabber(url, path, timeout=15):
        if "u1" in url:
            raise RuntimeError("camera offline")
        return fake_grabber(url, path)
    recorded = []
    cs = CaptureService(bus, cams, str(tmp_path), grabber=grabber,
                        on_capture=lambda name, path, ts, vid: recorded.append(name))
    bus.publish(Event(CAT_ENTER, 5.0))
    cs.run_once()
    assert recorded == ["good"]  # bad failed, good still captured


def test_captures_continuously_while_present_then_stops(tmp_path):
    # presence True,True,False -> grab rounds 0,1,2, stop when the cat leaves
    bus = EventBus()
    recorded = []
    present = iter([True, True, False])
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber,
                        on_capture=lambda n, p, t, v: recorded.append(p),
                        presence_fn=lambda: next(present),
                        interval_s=0.0, sleep=lambda s: None)
    bus.publish(Event(CAT_ENTER, 1.0))
    cs.run_once()
    assert len(recorded) == 3                  # captured for the whole presence window


def test_max_frames_caps_a_stuck_presence(tmp_path):
    bus = EventBus()
    recorded = []
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber,
                        on_capture=lambda n, p, t, v: recorded.append(p),
                        presence_fn=lambda: True,   # never "leaves"
                        max_frames=4, interval_s=0.0, sleep=lambda s: None)
    bus.publish(Event(CAT_ENTER, 1.0))
    cs.run_once()
    assert len(recorded) == 4                  # hard safety cap, not infinite


def test_at_least_one_round_even_if_already_gone(tmp_path):
    # presence already False at first check -> still grabbed round 0 once
    bus = EventBus()
    recorded = []
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber,
                        on_capture=lambda n, p, t, v: recorded.append(p),
                        presence_fn=lambda: False, interval_s=0.0, sleep=lambda s: None)
    bus.publish(Event(CAT_ENTER, 1.0))
    cs.run_once()
    assert len(recorded) == 1


def test_both_cameras_grabbed_each_round(tmp_path):
    bus = EventBus()
    import threading as _t
    lock = _t.Lock()
    recorded = []
    def rec(n, p, t, v):
        with lock:
            recorded.append(n)
    present = iter([True, False])              # 2 rounds
    cs = CaptureService(bus, [{"name": "a", "url": "ua"}, {"name": "b", "url": "ub"}],
                        str(tmp_path), grabber=fake_grabber, on_capture=rec,
                        presence_fn=lambda: next(present), interval_s=0.0, sleep=lambda s: None)
    bus.publish(Event(CAT_ENTER, 1.0))
    cs.run_once()
    assert sorted(recorded) == ["a", "a", "b", "b"]   # both cams, both rounds


def test_visit_id_resolved_once_at_trigger_not_per_grab(tmp_path):
    # The visit id must be pinned at cat_enter (when the visit is open), NOT
    # re-resolved after each grab — a quick visit closes before grabs finish,
    # which would otherwise mis-attribute or NULL the frames.
    bus = EventBus()
    cams = [{"name": "front", "url": "u1"}, {"name": "side", "url": "u2"}]
    resolver_calls = []
    seq = iter([42, 99, 7])  # resolver would return different ids if called repeatedly
    def resolver():
        resolver_calls.append(1)
        return next(seq)
    recorded = []
    cs = CaptureService(bus, cams, str(tmp_path), grabber=fake_grabber,
                        visit_resolver=resolver,
                        on_capture=lambda name, path, ts, vid: recorded.append(vid),
                        frames=3, interval_s=0.0, sleep=lambda s: None)
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    assert len(resolver_calls) == 1           # resolved exactly once for the visit
    assert recorded == [42] * 6               # all 6 frames pinned to the same visit


# ---- record the CAT, not the sealed globe: pre-roll ring + exit tail -------
from mw.capture import PrerollRing


def _warm_dir(tmp_path, cams=("front",)):
    wd = tmp_path / "warm"
    wd.mkdir()
    for c in cams:
        (wd / f"{c}.jpg").write_bytes(b"approach-frame-" + c.encode())
    return str(wd)


class _AllCats:
    def has_cat(self, path):
        return True


class _NoCats:
    def has_cat(self, path):
        return False


class _RaisingCats:
    def has_cat(self, path):
        raise RuntimeError("model exploded")


def test_preroll_ring_buffers_and_flushes(tmp_path):
    wd = _warm_dir(tmp_path)
    ring = PrerollRing(["front"], wd, keep_n=3, catfilter=_AllCats())
    for ts in (1.0, 2.0, 3.0, 4.0):        # 4 polls, keep_n=3: oldest dropped
        ring.poll(now=ts)
    entries = ring.flush()
    assert [e[1] for e in entries] == [2.0, 3.0, 4.0]     # (cam, ts, bytes)
    assert all(e[0] == "front" and e[2].startswith(b"approach") for e in entries)
    assert ring.flush() == []               # flush clears: no double-attribution


def test_preroll_ring_flush_ignores_catfilter(tmp_path):
    # Filtering moved out of the ring and into CaptureService._flush_preroll
    # (write-once, fail-open); the ring itself only buffers/drains and must
    # return every entry regardless of any catfilter attached to it.
    wd = _warm_dir(tmp_path)
    ring = PrerollRing(["front"], wd, keep_n=3, catfilter=_NoCats())
    ring.poll(now=1.0)
    entries = ring.flush()
    assert len(entries) == 1
    assert entries[0][0] == "front"


def test_enter_flushes_preroll_into_visit(tmp_path):
    bus = EventBus()
    wd = _warm_dir(tmp_path)
    ring = PrerollRing(["front"], wd, keep_n=2, catfilter=_AllCats())
    ring.poll(now=990.0)
    ring.poll(now=995.0)
    recorded = []
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber,
                        on_capture=lambda n, p, ts, vid: recorded.append((n, ts, vid, p)),
                        visit_resolver=lambda: 7,
                        presence_fn=lambda: False, interval_s=0.0,
                        sleep=lambda s: None, preroll=ring)
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    pre = [r for r in recorded if "_pre" in os.path.basename(r[3])]
    assert len(pre) == 2                    # both buffered approach frames landed
    assert all(r[2] == 7 for r in pre)      # attributed to THIS visit
    assert pre[0][1] == 990.0               # original timestamps preserved
    for r in pre:
        assert os.path.exists(r[3])


def test_tail_rounds_after_presence_ends(tmp_path):
    bus = EventBus()
    recorded = []
    present = iter([True, False])           # in box for 1 round, then leaves
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber,
                        on_capture=lambda n, p, ts, vid: recorded.append((vid, p)),
                        visit_resolver=lambda: 9,
                        presence_fn=lambda: next(present, False),
                        interval_s=0.0, sleep=lambda s: None, tail_rounds=3)
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    tails = [p for _, p in recorded if "_t" in os.path.basename(p)]
    assert len(tails) == 3                  # exit shots keep coming after leave
    assert all(vid == 9 for vid, _ in recorded)   # same visit, incl. tail


# ---- reordering: live round 0 must beat preroll flush -----------------------

def test_live_round_happens_before_preroll_flush(tmp_path):
    # A brief visitor needs round 0 on disk immediately; pre-roll (past-
    # timestamped approach frames) must be flushed AFTER the live round, not
    # before it, even though the ring already has frames buffered and ready.
    bus = EventBus()
    wd = _warm_dir(tmp_path)
    ring = PrerollRing(["front"], wd, keep_n=2)   # no catfilter: nothing dropped
    ring.poll(now=990.0)
    ring.poll(now=995.0)
    order = []
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber,
                        on_capture=lambda n, p, ts, vid: order.append(
                            "pre" if "_pre" in os.path.basename(p) else "live"),
                        visit_resolver=lambda: 7,
                        interval_s=0.0, sleep=lambda s: None, preroll=ring)
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    assert order[0] == "live"               # round 0 lands before any preroll write
    assert order[1:] == ["pre", "pre"]       # preroll frames still show up, just after


# ---- write-once + fail-open preroll gating -----------------------------------

def test_preroll_frame_kept_when_filter_raises(tmp_path):
    bus = EventBus()
    wd = _warm_dir(tmp_path)
    ring = PrerollRing(["front"], wd, keep_n=1)   # ring carries no catfilter
    ring.poll(now=990.0)
    recorded = []
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber,
                        on_capture=lambda n, p, ts, vid: recorded.append(p),
                        visit_resolver=lambda: 1,
                        presence_fn=lambda: False, interval_s=0.0,
                        sleep=lambda s: None, preroll=ring,
                        preroll_catfilter=_RaisingCats())
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    pre = [p for p in recorded if "_pre" in os.path.basename(p)]
    assert len(pre) == 1                    # fail OPEN: kept despite the crash
    assert os.path.exists(pre[0])


def test_preroll_frame_dropped_only_on_clean_false(tmp_path):
    bus = EventBus()
    wd = _warm_dir(tmp_path)
    ring = PrerollRing(["front"], wd, keep_n=1)
    ring.poll(now=990.0)
    recorded = []
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber,
                        on_capture=lambda n, p, ts, vid: recorded.append(p),
                        visit_resolver=lambda: 1,
                        presence_fn=lambda: False, interval_s=0.0,
                        sleep=lambda s: None, preroll=ring,
                        preroll_catfilter=_NoCats())
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    pre = [p for p in recorded if "_pre" in os.path.basename(p)]
    assert pre == []                        # clean False: dropped, no on_capture
    leftover = [f for f in os.listdir(tmp_path) if "_pre" in f]
    assert leftover == []                   # the written file was cleaned up too


def test_preroll_frame_kept_on_clean_true(tmp_path):
    bus = EventBus()
    wd = _warm_dir(tmp_path)
    ring = PrerollRing(["front"], wd, keep_n=1)
    ring.poll(now=990.0)
    recorded = []
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber,
                        on_capture=lambda n, p, ts, vid: recorded.append(p),
                        visit_resolver=lambda: 1,
                        presence_fn=lambda: False, interval_s=0.0,
                        sleep=lambda s: None, preroll=ring,
                        preroll_catfilter=_AllCats())
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    pre = [p for p in recorded if "_pre" in os.path.basename(p)]
    assert len(pre) == 1
    assert os.path.exists(pre[0])


def test_preroll_no_double_write(tmp_path):
    # Old path: wrote once to a NamedTemporaryFile just to run the filter,
    # then wrote the surviving bytes AGAIN into out_dir. New path writes to
    # out_dir once and filters the real file in place.
    bus = EventBus()
    wd = _warm_dir(tmp_path)
    ring = PrerollRing(["front"], wd, keep_n=2)
    ring.poll(now=990.0)
    ring.poll(now=995.0)
    recorded = []
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber,
                        on_capture=lambda n, p, ts, vid: recorded.append(p),
                        visit_resolver=lambda: 1,
                        presence_fn=lambda: False, interval_s=0.0,
                        sleep=lambda s: None, preroll=ring,
                        preroll_catfilter=_AllCats())
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    pre_files = [f for f in os.listdir(tmp_path) if "_pre" in f]
    assert len(pre_files) == 2               # exactly one file per surviving frame
    for f in pre_files:
        with open(os.path.join(tmp_path, f), "rb") as fh:
            assert fh.read().startswith(b"approach-frame-front")


# ---- tail gating: skip on a blip, run on a real visit ------------------------

def test_blip_visit_produces_no_tail_frames(tmp_path):
    bus = EventBus()
    recorded = []
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber,
                        on_capture=lambda n, p, ts, vid: recorded.append(p),
                        visit_resolver=lambda: 3,
                        presence_fn=lambda: False,   # gone immediately: 1 round only
                        interval_s=0.0, sleep=lambda s: None, tail_rounds=8)
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    tails = [p for p in recorded if "_t" in os.path.basename(p)]
    assert tails == []                       # a blip earns zero exit-tail rounds


def test_three_round_visit_produces_tail_rounds_frames(tmp_path):
    bus = EventBus()
    recorded = []
    present = iter([True, True, False])      # 3 live rounds, then leaves
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber,
                        on_capture=lambda n, p, ts, vid: recorded.append(p),
                        visit_resolver=lambda: 4,
                        presence_fn=lambda: next(present, False),
                        interval_s=0.0, sleep=lambda s: None, tail_rounds=2)
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    tails = [p for p in recorded if "_t" in os.path.basename(p)]
    assert len(tails) == 2                   # a real visit gets its full exit tail


# ---- TorchvisionCatFilter: one model, many concurrent callers ---------------

def test_catfilter_has_cat_calls_are_serialized(tmp_path):
    import threading as _threading
    import time as _time
    import torch
    from PIL import Image
    from mw.catfilter import TorchvisionCatFilter

    img_path = tmp_path / "frame.jpg"
    Image.new("RGB", (4, 4)).save(img_path)

    state = {"busy": False, "overlap": False}
    state_lock = _threading.Lock()

    class _RecordingModel:
        def __call__(self, inputs):
            with state_lock:
                if state["busy"]:
                    state["overlap"] = True
                state["busy"] = True
            _time.sleep(0.05)          # wide enough window for a race to show up
            with state_lock:
                state["busy"] = False
            return [{"labels": torch.tensor([17]), "scores": torch.tensor([0.9])}]

    filt = TorchvisionCatFilter()

    def fake_ensure_model():
        filt._model = _RecordingModel()
        filt._device = "cpu"
        filt._preprocess = lambda img: torch.zeros(3, 4, 4)

    filt._ensure_model = fake_ensure_model

    threads = [_threading.Thread(target=filt.has_cat, args=(str(img_path),))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["overlap"] is False         # the lock kept the two calls from overlapping


def test_preroll_flushes_after_round_zero_not_after_whole_loop(tmp_path):
    """The ring polls continuously in its own thread; waiting until the live
    loop ends lets a long visit cycle the ring and evict the approach frames.
    Flush must happen right after round 0: first grab lands with no delay,
    ring drains before eviction."""
    bus = EventBus()
    wd = _warm_dir(tmp_path)
    ring = PrerollRing(["front"], wd, keep_n=2)
    ring.poll(now=990.0)
    order = []
    present = iter([True, True, False])              # 3 live rounds

    def on_cap(n, p, ts, vid):
        base = os.path.basename(p)
        order.append("pre" if "_pre" in base else base.split("_")[-1].split(".")[0])

    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber, on_capture=on_cap,
                        visit_resolver=lambda: 5,
                        presence_fn=lambda: next(present, False),
                        interval_s=0.0, sleep=lambda s: None,
                        preroll=ring, preroll_catfilter=_AllCats())
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    assert "pre" in order
    # preroll must drain after round 0 but before round 1
    assert order.index("pre") == order.index("0") + 1
    assert order.index("pre") < order.index("1")
