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


def test_preroll_ring_buffers_and_flushes(tmp_path):
    wd = _warm_dir(tmp_path)
    ring = PrerollRing(["front"], wd, keep_n=3, catfilter=_AllCats())
    for ts in (1.0, 2.0, 3.0, 4.0):        # 4 polls, keep_n=3: oldest dropped
        ring.poll(now=ts)
    entries = ring.flush()
    assert [e[1] for e in entries] == [2.0, 3.0, 4.0]     # (cam, ts, bytes)
    assert all(e[0] == "front" and e[2].startswith(b"approach") for e in entries)
    assert ring.flush() == []               # flush clears: no double-attribution


def test_preroll_flush_drops_frames_without_cat(tmp_path):
    wd = _warm_dir(tmp_path)
    ring = PrerollRing(["front"], wd, keep_n=3, catfilter=_NoCats())
    ring.poll(now=1.0)
    assert ring.flush() == []               # catless approach frames not recorded


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
