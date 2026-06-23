"""Invariant canary: cross-check raw eliminations (dp102, upstream truth) against
attributed (cat_id-bearing, downstream of the vision/labeler pipeline) ones over a
rolling window. A sustained low attribution ratio means the labeler is silently
dropping or failing to name real elimination events -- a 'fixed-away' bypass that
unit tests cannot catch, because it only shows against live data.

This is a coarse RATE detector, not a per-visit auditor: some eliminations are
legitimately unattributable (frameless IR-flicker visits, ambiguous frames), so it
fires only when the attributed FRACTION drops below a floor over a minimum sample,
and ignores visits still inside the labeler's grace window (too recent to blame).
Fails toward loud on a real drop; stays silent when the sample is too small to judge.
"""
import sys
import time

from mw import store


class InvariantCanary:
    def __init__(self, conn, notify, now_fn=time.time, window_hours=48,
                 grace_hours=2, min_sample=4, min_ratio=0.5, interval=3600,
                 realarm=True):
        self.conn = conn
        self.notify = notify
        self.now = now_fn
        self.window_hours = window_hours
        self.grace_hours = grace_hours
        self.min_sample = min_sample
        self.min_ratio = min_ratio
        self.interval = interval
        self.realarm = realarm
        self._alarmed = False

    def evaluate(self):
        """Returns (status, msg): ('bad', text) | ('ok', None) | ('insufficient', None)."""
        now = self.now()
        after = store._iso(now - self.window_hours * 3600)
        before = store._iso(now - self.grace_hours * 3600)   # skip too-recent visits
        raw, attributed = store.elimination_attribution_stats(self.conn, after, before)
        if raw < self.min_sample:
            return ("insufficient", None)
        ratio = attributed / raw
        if ratio < self.min_ratio:
            return ("bad",
                    f"🔬 Attribution canary: only {attributed}/{raw} recent "
                    f"eliminations got a cat ID ({ratio:.0%}) over the last "
                    f"{self.window_hours}h — the labeler may be silently dropping "
                    f"health events")
        return ("ok", None)

    def run_once(self):
        status, msg = self.evaluate()
        if status == "bad" and not self._alarmed:
            # Latch ONLY on confirmed delivery: a dead Telegram token returning
            # False must not mark this 'sent' and re-suppress it. None (a stub with
            # no signal) is treated as delivered so plain notify callables work.
            if self.notify(msg) is not False:
                self._alarmed = True
        elif status == "ok" and self.realarm:
            self._alarmed = False                # recovered -> re-arm for next drop
        # 'insufficient': leave the latch as-is (cannot judge either way)

    def run(self):
        while True:
            try:
                self.run_once()
            except Exception as e:               # never let the canary thread die
                print(f"[invariant-canary] error: {e}", file=sys.stderr)
            time.sleep(self.interval)
