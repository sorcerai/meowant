"""Named elimination alerts (label-on-leave).

Polls for recently-closed eliminated visits not yet alerted, labels each one NOW
(so the cat name resolves in seconds, not the 15-min sweep), and sends a single
named alert. Poll-based rather than a CAT_LEAVE handler because dp102 can arrive
after CAT_LEAVE via the grace window — a leave-only trigger would miss those."""
import os
import sys
import time

from mw import store
from mw.decode import classify_waste
from mw.imgutil import spread_sample

_WASTE_MARK = {"pee": " — pee 💧", "poop": " — poop 💩", "uncertain": " — uncertain ❓"}


class EliminationNotifier:
    def __init__(self, conn, labeler, notify, now_fn=time.time,
                 settle_s=15, interval=30, sample=5, ask_who=None, pee_threshold=80, poop_threshold=130, enabled=True,
                 matcher=None, catfilter=None, min_views=2, threshold=0.0,
                 live=True):
        self.conn = conn
        self.labeler = labeler            # has .label_visit(vid, sample=...)
        self.notify = notify
        self.now = now_fn
        self.settle_s = settle_s          # wait this long after close (frames settle)
        self.interval = interval
        self.sample = sample              # frames to label for a FAST id (not all ~36)
        self.ask_who = ask_who            # optional (vid, paths, when, waste) -> None
        self.pee_threshold = pee_threshold
        self.poop_threshold = poop_threshold
        self.enabled = enabled
        # Local-first identification: the DINOv2 matcher is fast, on-device and
        # immune to the agy backend saturating — try it BEFORE the labeler.
        self.matcher = matcher            # GalleryMatcher or None (pre-ROI-wrapped by caller)
        self.catfilter = catfilter        # has_cat(path) or None (pre-ROI-wrapped by caller)
        self.min_views = min_views        # frames that must name a cat to commit
        self.threshold = threshold        # min fused confidence to commit
        self.live = live                  # False = shadow-only: never write cat_id

    def _waste_mark(self, visit):
        return _WASTE_MARK.get(classify_waste(visit.get("use_record"), self.pee_threshold, self.poop_threshold), "")

    def _alert_text(self, visit):
        cat = store.cat_name_by_id(self.conn, visit["cat_id"]) if visit["cat_id"] else None
        when = time.strftime("%H:%M", time.localtime(self.now()))
        mark = self._waste_mark(visit)
        if cat:
            return f"🐈 {cat} used the box{mark} [{when}]"
        return f"🐈 A cat used the box{mark} (couldn't ID — likely in-box) [{when}]"

    def _existing_captures(self, vid):
        return [c for c in store.captures_for_visit(self.conn, vid) if os.path.exists(c["path"])]

    def _sampled(self, paths):
        return spread_sample(paths, self.sample)

    def _matcher_fast_id(self, vid):
        """Local matcher pass over sampled frames; commits visits.cat_id on a
        confident multi-view hit. Never runs shadow-only (live=False), never
        runs once the visit already has a cat_id (auto or human — no wasted
        DINOv2 work, no clobbering an existing attribution after a restart
        backlog), and never overrides a human label. Mirrors the live scorer's
        rules but runs in seconds instead of the 600s sweep."""
        if self.matcher is None or not self.live:
            return
        v = store.get_visit(self.conn, vid)
        if v is None or v["cat_id"] is not None:
            return
        caps = self._sampled(self._existing_captures(vid))
        preds = []
        for c in caps:
            try:
                pred = self.matcher.predict(c["path"])
            except Exception as e:
                print(f"[elim-notify] matcher {c['path']} failed: {e}", file=sys.stderr)
                continue
            store.set_capture_prediction(self.conn, c["id"], pred[0], pred[1])
            preds.append(pred)
        from mw.identify import fuse_views
        cat, conf = fuse_views(preds)
        named = sum(1 for cid, _ in preds if cid is not None)
        if (cat is not None and named >= self.min_views and conf >= self.threshold
                and store.visit_established_cat(self.conn, vid) is None):
            store.set_visit_identity(self.conn, vid, cat, conf)

    def _cat_visible(self, vid):
        """Any sampled frame shows a cat? True/False only once at least one
        frame was actually checked; None if there's no information at all (no
        filter, no frames, or every check raised) — a filter crash must read
        as "unknown", not "confirmed no cat", or it wrongly triggers the
        hidden-cat message and suppresses the ask_who human-review prompt."""
        if self.catfilter is None:
            return None
        paths = self._sampled([c["path"] for c in self._existing_captures(vid)])
        if not paths:
            return None
        checked = False
        for p in paths:
            try:
                if self.catfilter.has_cat(p):
                    return True
                checked = True
            except Exception as e:
                print(f"[elim-notify] catfilter {p} failed: {e}", file=sys.stderr)
        return False if checked else None

    def run_once(self):
        before = store._iso(self.now() - self.settle_s)
        for v in store.pending_elimination_notifications(self.conn, before):
            fresh = v
            if not v["cat_id"]:
                self._matcher_fast_id(v["id"])                     # local, fast, free
                fresh = store.get_visit(self.conn, v["id"]) or v
                if not fresh["cat_id"]:
                    try:
                        self.labeler.label_visit(v["id"], sample=self.sample)  # fallback
                    except Exception as e:
                        print(f"[elim-notify] label {v['id']} failed: {e}", file=sys.stderr)
                    fresh = store.get_visit(self.conn, v["id"]) or v
            cat_id = fresh["cat_id"]
            if cat_id:
                if self.enabled:
                    self.notify(self._alert_text(fresh))
            elif self._cat_visible(v["id"]) is False:
                # Frames exist and show NO cat while the box registered a real
                # elimination: the globe tipped closed around the occupant
                # (heavy-cat pattern). Photos of a closed white ball are useless
                # to a human — say what happened instead of asking "who?".
                if self.enabled:
                    when = time.strftime("%H:%M", time.localtime(self.now()))
                    self.notify(
                        f"🐈 A cat used the box{self._waste_mark(fresh)} — hidden "
                        f"inside (globe tipped closed, not visible on camera) [{when}]")
            else:
                # Either a human might be able to ID from photos, or visibility
                # is simply unknown (no filter / crashed / no frames) — either
                # way, don't assert a hidden cat, try to get eyes on it instead.
                paths = None
                if self.ask_who is not None:
                    paths = [c["path"] for c in store.captures_for_visit(self.conn, v["id"])]
                    if not paths:
                        # frameless eliminated fragment (IR-flicker) — recover the sibling
                        # fragments' frames from the surrounding window so the prompt has photos
                        anchor = fresh.get("leave_ts") or fresh.get("enter_ts")
                        if anchor:
                            paths = store.capture_paths_around(self.conn, anchor, window_s=120)
                when = time.strftime("%H:%M", time.localtime(self.now()))
                if paths:
                    self.ask_who(v["id"], paths, when, self._waste_mark(fresh))
                elif self.enabled:
                    self.notify(self._alert_text(fresh))   # nothing to show — plain text
            store.mark_notified(self.conn, v["id"])

    def run(self):
        while True:
            try:
                self.run_once()
            except Exception as e:
                print(f"[elim-notify] error: {e}", file=sys.stderr)
            time.sleep(self.interval)
