"""Grab frames per camera while a cat is present; passively build the Phase-3 dataset."""
import os
import queue
import subprocess
import sys
import threading
import time

from mw.events import CAT_ENTER


def ffmpeg_grab(rtsp_url, out_path, timeout=15):
    """Grab a single frame from an RTSP stream to out_path via ffmpeg."""
    subprocess.run(
        ["ffmpeg", "-rtsp_transport", "tcp", "-y", "-i", rtsp_url,
         "-frames:v", "1", "-q:v", "2", out_path],
        timeout=timeout, capture_output=True, check=True)
    return out_path


class CaptureService:
    def __init__(self, bus, cameras, out_dir, grabber=ffmpeg_grab, on_capture=None,
                 frames=1, interval_s=3.0, sleep=time.sleep, visit_resolver=None,
                 presence_fn=None, max_frames=30):
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
        os.makedirs(out_dir, exist_ok=True)
        self._q = bus.subscribe()

    def _grab_one(self, cam, ts, i, visit_id):
        path = os.path.join(self.out_dir, f"{int(ts)}_{cam['name']}_{i}.jpg")
        try:
            self.grabber(cam["url"], path)
        except Exception as e:
            print(f"[capture] {cam['name']} grab failed: {e}", file=sys.stderr)
            return
        if self.on_capture:
            self.on_capture(cam["name"], path, ts, visit_id)

    def _grab_round(self, ts, i, visit_id):
        # Grab all cameras CONCURRENTLY so a round isn't the sum of ffmpeg
        # latencies — critical for catching brief visitors. on_capture writes
        # through the module-locked store, so concurrent calls are safe.
        threads = [threading.Thread(target=self._grab_one, args=(cam, ts, i, visit_id))
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
        i = 0
        while True:
            ts = time.time()                 # real time per round (pose-over-time)
            self._grab_round(ts, i, visit_id)
            i += 1
            if not self._continue(i):
                break
            self._sleep(self.interval_s)     # brief gap, then grab again

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
