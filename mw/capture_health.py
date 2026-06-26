"""Make capture failures loud instead of silent.

Two independent checks, both surfaced as notifications so a broken capture
pipeline can't quietly starve the Phase-3 dataset:

- **proactive stream probe** — periodically test each RTSP source; alert the
  moment it drops (and again when it recovers). cryze/MediaMTX streams are
  on-demand and flaky, so this catches a dead source *before* the next cat is
  lost.
- **reactive missed-capture guard** — flag any eliminated visit that settled
  with zero frames. This is the after-the-fact backstop (it can't tell "dead
  thread" from "dead stream", but either way the frame is gone).
- **labeler liveness** — flag frames left completely unprocessed by the
  auto-labeler past a grace window (the labeler is stuck/dead, e.g. its binary
  fell off the daemon PATH — which once went unnoticed for hours).
"""
import subprocess
import sys
import time

from mw import remediation
from mw import store


def ffmpeg_probe(rtsp_url, timeout=10):
    """True if the RTSP stream is currently publishable (one frame decodes)."""
    try:
        subprocess.run(
            ["ffmpeg", "-rtsp_transport", "tcp", "-i", rtsp_url,
             "-frames:v", "1", "-f", "null", "-"],
            timeout=timeout, capture_output=True, check=True)
        return True
    except Exception:
        return False


class CaptureHealth:
    def __init__(self, conn, cameras, notify, probe=ffmpeg_probe,
                 now_fn=time.time, settle_seconds=120, max_age_seconds=3600,
                 labeler_settle_seconds=1800, remediator=None):
        self.conn = conn
        self.cameras = cameras
        self.notify = notify
        self.probe = probe
        self.now = now_fn
        self.settle = settle_seconds        # ignore visits younger than this (grabs in flight)
        self.max_age = max_age_seconds       # don't re-flag ancient history after a restart
        self.labeler_settle = labeler_settle_seconds  # grace before flagging the labeler stuck
        self._up = {}                        # cam name -> last known up/down
        self._alerted = set()                # visit ids already alerted (per process)
        self._labeler_alerted = False        # latch so we alert once per stall episode
        self.remediator = remediator         # None -> notify-only (legacy/camera-absent)

    def check_streams(self):
        for cam in self.cameras:
            ok = self.probe(cam["url"])
            prev = self._up.get(cam["name"])
            if prev is True and not ok:
                if self.remediator:
                    self.remediator.handle(
                        "stream_down", {"camera": cam["name"]},
                        lambda c=cam: remediation.stream_down_playbook(
                            c["name"], reprobe=lambda: self.probe(c["url"])))
                else:
                    self.notify(f"📷 Camera '{cam['name']}' stream DOWN — captures will be lost")
            elif prev is False and ok:
                self.notify(f"📷 Camera '{cam['name']}' stream recovered")
            self._up[cam["name"]] = ok

    def check_missed(self):
        now = self.now()
        after = store._iso(now - self.max_age)
        before = store._iso(now - self.settle)
        for v in store.eliminated_visits_missing_captures(self.conn, after, before):
            if v["id"] not in self._alerted:
                self.notify(
                    f"🚫 Visit {v['id']} logged an elimination but captured 0 frames "
                    f"— capture pipeline may be down")
                self._alerted.add(v["id"])

    def check_labeler(self):
        """Alert if frames sit completely unprocessed by the auto-labeler past
        the grace window — it's stuck or dead. Latches so it fires once per
        stall and re-arms when the backlog clears."""
        cutoff = store._iso(self.now() - self.labeler_settle)
        stuck = store.stale_unlabeled_count(self.conn, cutoff)
        if stuck > 0 and not self._labeler_alerted:
            mins = int(self.labeler_settle / 60)
            if self.remediator:
                self.remediator.handle(
                    "labeler_stall", {"stuck": stuck, "grace_min": mins},
                    lambda: remediation.labeler_stall_playbook(stuck))
            else:
                self.notify(f"🏷️ Auto-labeler stalled: {stuck} frame(s) unprocessed "
                            f">{mins}min — labeler may be down")
            self._labeler_alerted = True
        elif stuck == 0:
            self._labeler_alerted = False     # backlog cleared — re-arm

    def run_once(self):
        if not self.cameras:
            return  # camera-absent install: nothing to probe or guard
        try:
            self.check_streams()
        except Exception as e:
            print(f"[capture-health] check_streams error: {e}", file=sys.stderr)
        try:
            self.check_missed()
        except Exception as e:
            print(f"[capture-health] check_missed error: {e}", file=sys.stderr)
        try:
            self.check_labeler()
        except Exception as e:
            print(f"[capture-health] check_labeler error: {e}", file=sys.stderr)

    def run(self, interval=300.0):
        while True:
            try:
                self.run_once()
            except Exception as e:  # never let the health thread die
                print(f"[capture-health] error: {e}", file=sys.stderr)
            time.sleep(interval)
