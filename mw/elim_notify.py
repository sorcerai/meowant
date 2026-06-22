"""Named elimination alerts (label-on-leave).

Polls for recently-closed eliminated visits not yet alerted, labels each one NOW
(so the cat name resolves in seconds, not the 15-min sweep), and sends a single
named alert. Poll-based rather than a CAT_LEAVE handler because dp102 can arrive
after CAT_LEAVE via the grace window — a leave-only trigger would miss those."""
import sys
import time

from mw import store


class EliminationNotifier:
    def __init__(self, conn, labeler, notify, now_fn=time.time,
                 settle_s=15, interval=30, sample=5, ask_who=None):
        self.conn = conn
        self.labeler = labeler            # has .label_visit(vid, sample=...)
        self.notify = notify
        self.now = now_fn
        self.settle_s = settle_s          # wait this long after close (frames settle)
        self.interval = interval
        self.sample = sample              # frames to label for a FAST id (not all ~36)
        self.ask_who = ask_who            # optional (vid, paths, when) -> None

    def _alert_text(self, visit):
        cat = store.cat_name_by_id(self.conn, visit["cat_id"]) if visit["cat_id"] else None
        when = time.strftime("%H:%M", time.localtime(self.now()))
        if cat:
            return f"🐈 {cat} used the box [{when}]"
        return f"🐈 A cat used the box (couldn't ID — likely in-box) [{when}]"

    def run_once(self):
        before = store._iso(self.now() - self.settle_s)
        for v in store.pending_elimination_notifications(self.conn, before):
            try:
                self.labeler.label_visit(v["id"], sample=self.sample)  # fast id
            except Exception as e:
                print(f"[elim-notify] label {v['id']} failed: {e}", file=sys.stderr)
            fresh = store.get_visit(self.conn, v["id"]) or v   # re-read post-label cat_id
            cat_id = fresh["cat_id"]
            if cat_id:
                self.notify(self._alert_text(fresh))
            elif self.ask_who is not None:
                paths = [c["path"] for c in store.captures_for_visit(self.conn, v["id"])]
                if not paths:
                    # frameless eliminated fragment (IR-flicker) — recover the sibling
                    # fragments' frames from the surrounding window so the prompt has photos
                    anchor = fresh.get("leave_ts") or fresh.get("enter_ts")
                    if anchor:
                        paths = store.capture_paths_around(self.conn, anchor, window_s=120)
                when = time.strftime("%H:%M", time.localtime(self.now()))
                if paths:
                    self.ask_who(v["id"], paths, when)
                else:
                    self.notify(self._alert_text(fresh))   # nothing to show — plain text
            else:
                self.notify(self._alert_text(fresh))     # fallback: dead-end text
            store.mark_notified(self.conn, v["id"])

    def run(self):
        while True:
            try:
                self.run_once()
            except Exception as e:
                print(f"[elim-notify] error: {e}", file=sys.stderr)
            time.sleep(self.interval)
