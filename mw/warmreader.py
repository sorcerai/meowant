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


def _build_cmd(rtsp_url, out_path, fps, hwaccel=None):
    cmd = ["ffmpeg", "-nostdin"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    # -timeout (RTSP demuxer socket I/O timeout, µs; this ffmpeg build rejects
    # -rw_timeout for RTSP): fail the read after 15s of socket silence.
    # Without it a MediaMTX restart on the bridge blackholes the established
    # TCP session (its netns is destroyed, no RST reaches us) and ffmpeg
    # blocks in read() FOREVER — alive process, frozen output, invisible to
    # poll()-based supervision. That mode blinded capture for 24h on 2026-07-16.
    cmd += ["-timeout", "15000000",
            "-rtsp_transport", "tcp", "-i", rtsp_url,
            "-vf", f"fps={fps}", "-update", "1", "-y", out_path]
    return cmd


def _default_launch(rtsp_url, out_path, fps, hwaccel=None):
    """Launch a persistent ffmpeg that overwrites out_path with the latest frame
    at `fps`. `-update 1` keeps writing the same file; `-nostdin` so it never
    steals the daemon's stdin. `hwaccel` (e.g. 'videotoolbox' on macOS) offloads
    decode to the media engine so steady readers cost ~no CPU."""
    return subprocess.Popen(_build_cmd(rtsp_url, out_path, fps, hwaccel),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class WarmReaderPool:
    """One persistent ffmpeg reader per camera, supervised: restart on death
    AND on frozen output (alive process, stale frame file)."""

    def __init__(self, cameras, out_dir, fps=1.0, launch=_default_launch,
                 sleep=time.sleep, hwaccel=None, stale_after_s=30.0,
                 now_fn=time.time):
        self.cameras = cameras
        self.out_dir = out_dir
        self.fps = fps
        self._launch = launch
        self._sleep = sleep
        self.hwaccel = hwaccel        # e.g. 'videotoolbox' to offload decode
        self.stale_after_s = stale_after_s  # alive reader + older output = hung
        self.now = now_fn
        self._procs = {}        # name -> process handle
        self._started = {}      # name -> launch ts (startup grace for staleness)
        self._stop = False
        os.makedirs(out_dir, exist_ok=True)

    def frame_path(self, name):
        return os.path.join(self.out_dir, f"{name}.jpg")

    def _start_one(self, cam):
        self._procs[cam["name"]] = self._launch(
            cam["url"], self.frame_path(cam["name"]), self.fps, self.hwaccel)
        self._started[cam["name"]] = self.now()

    def start(self):
        for cam in self.cameras:
            self._start_one(cam)

    def supervise_once(self):
        """Relaunch any reader whose process has exited — or whose process is
        alive but whose output file has frozen past stale_after_s (a reader
        hung on a blackholed TCP session writes nothing yet never exits; the
        -rw_timeout in the launch cmd is the first line of defense, this is
        the backstop). Freshness is measured from max(file mtime, launch ts)
        so a just-launched reader gets startup grace instead of a kill loop."""
        now = self.now()
        for cam in self.cameras:
            name = cam["name"]
            p = self._procs.get(name)
            if p is None or p.poll() is not None:
                print(f"[warmreader] {name} reader down; restarting",
                      file=sys.stderr)
                self._start_one(cam)
                continue
            try:
                mtime = os.path.getmtime(self.frame_path(name))
            except OSError:
                mtime = 0.0
            fresh_ref = max(mtime, self._started.get(name, 0.0))
            if now - fresh_ref > self.stale_after_s:
                print(f"[warmreader] {name} output stale "
                      f"{int(now - fresh_ref)}s with live process; killing hung "
                      f"reader", file=sys.stderr)
                try:
                    # SIGKILL, not terminate(): an ffmpeg blocked in a dead
                    # read ignores SIGTERM (observed 2026-07-17).
                    p.kill()
                    p.wait(timeout=5)
                except Exception:
                    pass
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
