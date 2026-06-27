"""Keep RTSP streams WARM so frame capture never pays a cold-open.

Background: the cryze/MediaMTX stack publishes 5 of 6 cams from one shared
redroid publisher. Opening a fresh ffmpeg RTSP session per grab (6 cold-opens
every ~1.5s) caused exit-8/timeouts and could wedge the stack. MediaMTX pulls
each camera's upstream ONCE and fans it out to readers, so the fix is to hold a
persistent reader per camera: one long-lived ffmpeg writing the latest frame to
<dir>/<cam>.jpg (-update 1). Capture then copies that always-fresh file
(file_grab) instead of cold-opening RTSP. Six steady connections is what the
publisher can sustain; the connect/teardown thrash is what it couldn't.
"""
import os
import shutil
import subprocess
import sys
import time


def _default_launch(rtsp_url, out_path, fps, hwaccel=None):
    """Launch a persistent ffmpeg that overwrites out_path with the latest frame
    at `fps`. `-update 1` keeps writing the same file; `-nostdin` so it never
    steals the daemon's stdin. `hwaccel` (e.g. 'videotoolbox' on macOS) offloads
    decode to the media engine so steady readers cost ~no CPU."""
    cmd = ["ffmpeg", "-nostdin"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    cmd += ["-rtsp_transport", "tcp", "-i", rtsp_url,
            "-vf", f"fps={fps}", "-update", "1", "-y", out_path]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class WarmReaderPool:
    """One persistent ffmpeg reader per camera, supervised (restart on death)."""

    def __init__(self, cameras, out_dir, fps=1.0, launch=_default_launch,
                 sleep=time.sleep, hwaccel=None):
        self.cameras = cameras
        self.out_dir = out_dir
        self.fps = fps
        self._launch = launch
        self._sleep = sleep
        self.hwaccel = hwaccel        # e.g. 'videotoolbox' to offload decode
        self._procs = {}        # name -> process handle
        self._stop = False
        os.makedirs(out_dir, exist_ok=True)

    def frame_path(self, name):
        return os.path.join(self.out_dir, f"{name}.jpg")

    def _start_one(self, cam):
        self._procs[cam["name"]] = self._launch(
            cam["url"], self.frame_path(cam["name"]), self.fps, self.hwaccel)

    def start(self):
        for cam in self.cameras:
            self._start_one(cam)

    def supervise_once(self):
        """Relaunch any reader whose process has exited."""
        for cam in self.cameras:
            p = self._procs.get(cam["name"])
            if p is None or p.poll() is not None:
                print(f"[warmreader] {cam['name']} reader down; restarting",
                      file=sys.stderr)
                self._start_one(cam)

    def run(self, interval=10.0):
        self.start()
        while not self._stop:
            self._sleep(interval)
            try:
                self.supervise_once()
            except Exception as e:  # supervision must never kill the thread
                print(f"[warmreader] supervise error: {e}", file=sys.stderr)

    def stop(self):
        self._stop = True
        for p in self._procs.values():
            try:
                p.terminate()
            except Exception:
                pass


def file_grab(src_path, out_path, timeout=None, max_age_s=10.0, now_fn=time.time):
    """'Grab' a frame by copying the warm reader's latest file.

    A MISSING or STALE frame (reader died / stream down) raises — never silently
    copy an ancient frame — so CaptureService's retry and the capture-health /
    remediation paths still fire. `timeout` is accepted for grabber-signature
    compatibility and ignored (the copy is local)."""
    if not os.path.exists(src_path):
        raise RuntimeError(f"no warm frame at {src_path}")
    age = now_fn() - os.path.getmtime(src_path)
    if age > max_age_s:
        raise RuntimeError(f"warm frame stale ({age:.0f}s > {max_age_s}s) at {src_path}")
    shutil.copyfile(src_path, out_path)
    return out_path
