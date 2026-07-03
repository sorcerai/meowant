"""Grab frames per camera while a cat is present; passively build the Phase-3 dataset."""
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

from mw.events import CAT_ENTER


def ffmpeg_grab(rtsp_url, out_path, timeout=15):
    """Grab a single frame from an RTSP stream to out_path via ffmpeg.

    NOTE: every call is a fresh RTSP cold-open (connect -> SPS/PPS -> keyframe).
    The cryze/MediaMTX stack publishes 5 of 6 cams from one shared redroid
    publisher, so many simultaneous cold-opens cause exit-8/timeouts and can
    wedge the stack. Prefer `http_grab` against a warm snapshot sidecar, and
    keep CaptureService's concurrency bounded."""
    subprocess.run(
        ["ffmpeg", "-rtsp_transport", "tcp", "-y", "-i", rtsp_url,
         "-frames:v", "1", "-q:v", "2", out_path],
        timeout=timeout, capture_output=True, check=True)
    return out_path


def http_grab(img_url, out_path, timeout=10):
    """Fetch a single JPEG frame over HTTP (a snapshot sidecar's
    /img/<cam>.jpg) to out_path. Far cheaper than ffmpeg_grab: the sidecar
    holds the stream warm and serves a cached frame, so there is no per-grab
    RTSP handshake and no load on the shared publisher."""
    with urllib.request.urlopen(img_url, timeout=timeout) as r:
        status = getattr(r, "status", 200)
        if status is not None and status != 200:
            raise RuntimeError(f"snapshot sidecar returned HTTP {status}")
        with open(out_path, "wb") as f:
            shutil.copyfileobj(r, f)
    return out_path


class PrerollRing:
    """Rolling buffer of recent warm frames — the cat's APPROACH to the box.

    The globe-tipping discovery (Jul 3): a heavy cat seals the globe behind
    him, so mid-visit frames show a featureless white ball. The identifiable
    moments are walking up (before dp24 fires) and climbing out (after). This
    ring keeps the last `keep_n` warm frames per camera; on CAT_ENTER the
    CaptureService flushes it into the visit, so the visit's dataset contains
    the cat even when the visit itself hides it. Frames without a detectable
    cat are dropped at flush (catfilter) — the trigger records the CAT, not
    another empty-box shot."""

    def __init__(self, cam_names, warm_dir, keep_n=6, catfilter=None):
        self.cam_names = list(cam_names)
        self.warm_dir = warm_dir
        self.keep_n = max(1, keep_n)
        self.catfilter = catfilter
        self._buf = {c: [] for c in self.cam_names}   # cam -> [(ts, bytes)]
        self._lock = threading.Lock()

    def poll(self, now=None):
        """Copy each camera's current warm frame into the ring (cheap: bytes)."""
        ts = time.time() if now is None else now
        for cam in self.cam_names:
            p = os.path.join(self.warm_dir, f"{cam}.jpg")
            try:
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            with self._lock:
                buf = self._buf[cam]
                buf.append((ts, data))
                del buf[:-self.keep_n]

    def flush(self):
        """Drain the ring -> [(cam, ts, bytes)] of frames that show a cat.
        Clears the buffer so one approach never feeds two visits."""
        with self._lock:
            drained = {c: list(b) for c, b in self._buf.items()}
            for b in self._buf.values():
                b.clear()
        out = []
        for cam, frames in drained.items():
            for ts, data in frames:
                if self.catfilter is not None:
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".jpg") as tf:
                        tf.write(data)
                        tf.flush()
                        try:
                            if not self.catfilter.has_cat(tf.name):
                                continue
                        except Exception:
                            continue
                out.append((cam, ts, data))
        out.sort(key=lambda e: e[1])
        return out

    def run(self, interval_s=3.0):
        while True:
            try:
                self.poll()
            except Exception as e:
                print(f"[preroll] poll failed: {e}", file=sys.stderr)
            time.sleep(interval_s)


class CaptureService:
    def __init__(self, bus, cameras, out_dir, grabber=ffmpeg_grab, on_capture=None,
                 frames=1, interval_s=3.0, sleep=time.sleep, visit_resolver=None,
                 presence_fn=None, max_frames=30, max_concurrent=2,
                 grab_retries=1, retry_backoff_s=0.5, preroll=None, tail_rounds=0):
        self.bus = bus
        self.cameras = cameras
        self.out_dir = out_dir
        self.grabber = grabber
        self.on_capture = on_capture
        self.frames = max(1, frames)        # legacy: fixed rounds when no presence_fn
        self.interval_s = interval_s        # spacing between rounds
        self._sleep = sleep                 # injectable for tests
        self.visit_resolver = visit_resolver  # () -> visit_id, called once per visit
        self.presence_fn = presence_fn      # () -> bool: keep grabbing while a cat is present
        self.max_frames = max(1, max_frames)  # hard safety cap on rounds per visit
        # Cap simultaneous grabs: 6 cold RTSP opens at once overwhelm the shared
        # publisher. A semaphore keeps at most `max_concurrent` ffmpeg in flight
        # while still grabbing every camera (the rest queue, no added sleeps).
        self.max_concurrent = max(1, max_concurrent)
        self.grab_retries = max(0, grab_retries)   # extra attempts on transient failure
        self.retry_backoff_s = retry_backoff_s
        self.preroll = preroll              # PrerollRing: approach frames -> visit
        self.tail_rounds = max(0, tail_rounds)  # exit shots after presence ends
        os.makedirs(out_dir, exist_ok=True)
        self._q = bus.subscribe()

    def _flush_preroll(self, visit_id):
        """Write the ring's cat-bearing approach frames into out_dir and
        register them on this visit with their ORIGINAL timestamps."""
        if self.preroll is None:
            return
        try:
            entries = self.preroll.flush()
        except Exception as e:
            print(f"[capture] preroll flush failed: {e}", file=sys.stderr)
            return
        for j, (cam, ts, data) in enumerate(entries):
            path = os.path.join(self.out_dir, f"{int(ts)}_{cam}_pre{j}.jpg")
            try:
                with open(path, "wb") as f:
                    f.write(data)
            except OSError as e:
                print(f"[capture] preroll write failed: {e}", file=sys.stderr)
                continue
            if self.on_capture:
                self.on_capture(cam, path, ts, visit_id)

    def _grab_one(self, cam, ts, i, visit_id):
        path = os.path.join(self.out_dir, f"{int(ts)}_{cam['name']}_{i}.jpg")
        # Retry a transient grab failure rather than losing the frame outright —
        # but with bounded attempts + backoff so we don't amplify load on a
        # publisher that's already struggling. Backoff grows per attempt.
        for attempt in range(self.grab_retries + 1):
            try:
                self.grabber(cam["url"], path)
                break
            except Exception as e:
                if attempt < self.grab_retries:
                    self._sleep(self.retry_backoff_s * (attempt + 1))
                    continue
                print(f"[capture] {cam['name']} grab failed after "
                      f"{attempt + 1} attempt(s): {e}", file=sys.stderr)
                return
        if self.on_capture:
            self.on_capture(cam["name"], path, ts, visit_id)

    def _grab_round(self, ts, i, visit_id):
        # Grab cameras concurrently (a round isn't the sum of ffmpeg latencies —
        # critical for brief visitors) but BOUNDED by a semaphore so we never
        # hit the shared publisher with more than `max_concurrent` cold opens at
        # once. on_capture writes through the module-locked store, so concurrent
        # calls are safe.
        sem = threading.Semaphore(self.max_concurrent)

        def worker(cam):
            with sem:
                self._grab_one(cam, ts, i, visit_id)

        threads = [threading.Thread(target=worker, args=(cam,))
                   for cam in self.cameras]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _continue(self, rounds_done):
        if rounds_done >= self.max_frames:
            return False                     # safety cap always wins
        if self.presence_fn is not None:
            return bool(self.presence_fn())  # keep grabbing while the cat is present
        return rounds_done < self.frames     # legacy: fixed number of rounds

    def _handle(self, ev):
        if ev.kind != CAT_ENTER:
            return
        # Pin the visit id NOW, while the visit is open. Grabs may outlast the
        # visit, so resolving per-grab would NULL or mis-attribute the frames.
        visit_id = self.visit_resolver() if self.visit_resolver else None
        self._flush_preroll(visit_id)        # approach frames: cat BEFORE entry
        i = 0
        while True:
            ts = time.time()                 # real time per round (pose-over-time)
            self._grab_round(ts, i, visit_id)
            i += 1
            if not self._continue(i):
                break
            self._sleep(self.interval_s)     # brief gap, then grab again
        # Exit tail: the cat is most identifiable climbing OUT (the sealed
        # globe hides everything in between). Same visit id.
        for t in range(self.tail_rounds):
            self._sleep(self.interval_s)
            self._grab_round(time.time(), f"t{t}", visit_id)

    def run_once(self):
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                return
            self._handle(ev)

    def run(self):
        while True:
            try:
                self._handle(self._q.get())
            except Exception as e:  # a store/resolver error must not kill the thread
                print(f"[capture] unhandled error: {e}", file=sys.stderr)
