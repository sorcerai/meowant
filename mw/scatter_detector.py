"""Per-visit litter-scatter detection on the floor camera (meowcam3).

Owns its own meowcam3 grabs because the ID CaptureService is presence-gated (it
only shoots while a cat is in the box), but scatter scoring needs the opposite:
a cat-FREE floor before the visit and after it.

- A rolling clean reference: while the box is idle (standby, no open visit) we
  periodically grab one meowcam3 frame. That is the cat-free, lighting-current
  floor state. Pinned at CAT_ENTER as this visit's baseline.
- At CAT_LEAVE: wait briefly for the cat to clear the apron, grab a few frames,
  and score the delta vs the pinned reference (mw.scatter). Persist the score and
  fire a 'time to sweep' alert when severity clears the threshold.

The reference is whatever the floor looked like just before THIS visit (which may
already carry earlier, unswept scatter), so the score is the NEW scatter this
visit added — which is exactly the per-cat attribution number once the visit is
labeled. See store.per_cat_scatter.
"""
import os
import queue
import sys
import time

from mw import scatter, store
from mw.capture import ffmpeg_grab
from mw.events import CAT_ENTER, CAT_LEAVE

_SEVERITY_WORD = {1: "light", 2: "moderate", 3: "heavy"}


class ScatterDetector:
    def __init__(self, bus, conn, cam_url, out_dir, notify,
                 grabber=ffmpeg_grab, sleep=time.sleep,
                 presence_fn=None, visit_resolver=None,
                 threshold=1, min_duration_s=20,
                 post_leave_delay_s=8, post_frames=3, post_interval_s=2.0,
                 ref_interval_s=45, roi=None,
                 delta=22, min_blob=40, consensus=2):
        self.bus = bus
        self.conn = conn
        self.cam_url = cam_url
        self.out_dir = out_dir
        self.notify = notify
        self.grabber = grabber
        self._sleep = sleep
        self.presence_fn = presence_fn          # () -> bool: a cat is in the box
        self.visit_resolver = visit_resolver    # () -> open visit id (or None)
        self.threshold = threshold              # min severity that alerts
        self.min_duration_s = min_duration_s    # skip double-entry / blip visits
        self.post_leave_delay_s = post_leave_delay_s
        self.post_frames = max(1, post_frames)
        self.post_interval_s = post_interval_s
        self.ref_interval_s = ref_interval_s
        self.roi = roi if roi is not None else scatter.DEFAULT_ROI
        self.delta = delta
        self.min_blob = min_blob
        self.consensus = consensus
        os.makedirs(out_dir, exist_ok=True)
        self._q = bus.subscribe()
        self._rolling_ref = None     # latest cat-free meowcam3 frame
        self._open_vid = None        # visit id pinned at the current CAT_ENTER
        self._visit_ref = {}         # visit id -> pinned reference path

    # --- frame I/O -------------------------------------------------------
    def _grab(self, name):
        path = os.path.join(self.out_dir, f"{name}.jpg")
        self.grabber(self.cam_url, path)
        return path

    def _idle(self):
        """True when no cat is in the box and no visit is open — safe to refresh
        the clean reference."""
        if self.presence_fn is not None and self.presence_fn():
            return False
        if self.visit_resolver is not None and self.visit_resolver() is not None:
            return False
        return True

    def _refresh_rolling_ref(self):
        if not self._idle():
            return
        try:
            self._rolling_ref = self._grab("rolling_clean")
        except Exception as e:
            print(f"[scatter] rolling-ref grab failed: {e}", file=sys.stderr)

    # --- event handlers --------------------------------------------------
    def _on_enter(self):
        # Only one visit is ever open at a time; drop any orphan reference from a
        # prior visit whose LEAVE was dropped by a full bus, so _visit_ref can't
        # grow unbounded over long uptimes.
        self._visit_ref.clear()
        vid = self.visit_resolver() if self.visit_resolver else None
        self._open_vid = vid
        if vid is not None and self._rolling_ref:
            self._visit_ref[vid] = self._rolling_ref

    def _on_leave(self):
        vid, self._open_vid = self._open_vid, None
        if vid is None:
            return
        ref = self._visit_ref.pop(vid, None)
        visit = store.get_visit(self.conn, vid)
        dur = (visit or {}).get("duration_s") or 0
        if ref is None or dur < self.min_duration_s:
            return   # no baseline, or a blip/double-entry — nothing to score
        self._sleep(self.post_leave_delay_s)   # let the cat clear the apron
        post = []
        for i in range(self.post_frames):
            if self.presence_fn is not None and self.presence_fn():
                post = []          # a cat is back on the apron — frames would be
                break              # contaminated; abandon scoring for this visit
            try:
                post.append(self._grab(f"post_{vid}_{i}"))
            except Exception as e:
                print(f"[scatter] post-leave grab failed: {e}", file=sys.stderr)
            if i < self.post_frames - 1:
                self._sleep(self.post_interval_s)
        if post:
            self.score_and_record(vid, ref, post)

    # --- testable core ---------------------------------------------------
    def _format_alert(self, result):
        word = _SEVERITY_WORD.get(result["severity"], "")
        return (f"🧹 Time to sweep — {word} litter scatter on the floor "
                f"({result['changed_pct']}% of the apron)")

    def score_and_record(self, visit_id, ref_path, post_paths):
        """Score the post-leave frames against the pinned reference, persist the
        result on the visit, and alert if severity >= threshold. Returns
        (result, alert_msg_or_None)."""
        result = scatter.score(post_paths, ref_path, roi=self.roi, delta=self.delta,
                               min_blob=self.min_blob, consensus=self.consensus)
        store.set_visit_scatter(self.conn, visit_id, result["severity"],
                                result["changed_pct"], result["area"])
        msg = None
        if result["severity"] >= self.threshold:
            msg = self._format_alert(result)
            self.notify(msg)
        return result, msg

    # --- loop ------------------------------------------------------------
    def _handle(self, ev):
        if ev.kind == CAT_ENTER:
            self._on_enter()
        elif ev.kind == CAT_LEAVE:
            self._on_leave()

    def run(self):
        while True:
            try:
                ev = self._q.get(timeout=self.ref_interval_s)
            except queue.Empty:
                self._refresh_rolling_ref()   # idle tick: keep the reference fresh
                continue
            try:
                self._handle(ev)
            except Exception as e:  # a grab/store error must not kill the thread
                print(f"[scatter] unhandled error: {e}", file=sys.stderr)
