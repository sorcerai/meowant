"""Capture reliability: the cryze/MediaMTX stack publishes 5 of 6 cams from ONE
shared redroid publisher with a tiny AV-session budget. Six simultaneous ffmpeg
RTSP cold-opens every few seconds caused exit-8/timeouts AND could wedge the
stack (watchdog then restarts all cams). So capture must (1) bound how many
grabs run at once, (2) retry a transient grab failure instead of losing the
frame outright, and (3) be able to pull a frame over plain HTTP from a warm
snapshot sidecar (cheap cached frame, no RTSP handshake)."""
import io
import threading
import time

from mw.bus import EventBus
from mw.events import Event, CAT_ENTER
from mw.capture import CaptureService, http_grab


def fake_grabber(url, path, timeout=15):
    with open(path, "w") as f:
        f.write(url)
    return path


def _concurrency_tracking_grabber(hold_s=0.02):
    """Grabber that records the PEAK number of concurrent in-flight grabs."""
    state = {"cur": 0, "peak": 0}
    lock = threading.Lock()

    def grab(url, path, timeout=15):
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        time.sleep(hold_s)              # hold the slot so overlap is observable
        with lock:
            state["cur"] -= 1
        return fake_grabber(url, path)

    return grab, state


def test_grab_round_bounds_concurrency(tmp_path):
    bus = EventBus()
    cams = [{"name": f"c{i}", "url": f"u{i}"} for i in range(6)]
    grab, state = _concurrency_tracking_grabber()
    recorded = []
    cs = CaptureService(bus, cams, str(tmp_path), grabber=grab,
                        on_capture=lambda n, p, t, v: recorded.append(n),
                        max_concurrent=2)
    bus.publish(Event(CAT_ENTER, 1.0))
    cs.run_once()
    assert len(recorded) == 6          # every camera still captured
    assert state["peak"] <= 2          # but never more than 2 ffmpeg at once


def test_transient_failure_is_retried(tmp_path):
    bus = EventBus()
    calls = {"n": 0}

    def grabber(url, path, timeout=15):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient stream hiccup")
        return fake_grabber(url, path)

    recorded = []
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=grabber,
                        on_capture=lambda n, p, t, v: recorded.append(n),
                        grab_retries=1, retry_backoff_s=0.0, sleep=lambda s: None)
    bus.publish(Event(CAT_ENTER, 1.0))
    cs.run_once()
    assert recorded == ["front"]       # recovered on the retry
    assert calls["n"] == 2             # tried exactly twice


def test_retry_exhausted_gives_up_cleanly(tmp_path):
    bus = EventBus()

    def grabber(url, path, timeout=15):
        raise RuntimeError("camera down")

    recorded = []
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=grabber,
                        on_capture=lambda n, p, t, v: recorded.append(n),
                        grab_retries=2, retry_backoff_s=0.0, sleep=lambda s: None)
    bus.publish(Event(CAT_ENTER, 1.0))
    cs.run_once()
    assert recorded == []              # no frame, but no crash either


def test_retry_backoff_uses_injected_sleep(tmp_path):
    bus = EventBus()
    calls = {"n": 0}

    def grabber(url, path, timeout=15):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("blip")
        return fake_grabber(url, path)

    slept = []
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=grabber,
                        on_capture=lambda n, p, t, v: None,
                        grab_retries=2, retry_backoff_s=0.5, sleep=slept.append)
    bus.publish(Event(CAT_ENTER, 1.0))
    cs.run_once()
    # two failures -> two backoff sleeps, increasing (0.5, 1.0)
    assert slept == [0.5, 1.0]


def test_http_grab_writes_frame(tmp_path, monkeypatch):
    """http_grab pulls a JPEG from a snapshot-sidecar URL into out_path."""
    class FakeResp(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): self.close()

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda url, timeout=10: FakeResp(b"\xff\xd8\xffJPEG"))
    out = str(tmp_path / "front.jpg")
    http_grab("http://192.168.2.79:9999/img/front.jpg", out)
    with open(out, "rb") as f:
        assert f.read() == b"\xff\xd8\xffJPEG"


def test_http_grab_raises_on_bad_status(tmp_path, monkeypatch):
    class FakeResp(io.BytesIO):
        status = 503
        def __enter__(self): return self
        def __exit__(self, *a): self.close()

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda url, timeout=10: FakeResp(b""))
    out = str(tmp_path / "front.jpg")
    try:
        http_grab("http://x/img/front.jpg", out)
        assert False, "expected failure on 503"
    except Exception:
        pass
