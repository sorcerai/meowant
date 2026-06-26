"""Box-health watchdog: the box's OWN liveness (bin full / auto-clean blocked).

Mirrors HealthWatch's persistent-latching pattern, but for the litter-box hardware:
  - approaching-full heads-up (predictive, from learned per-box capacity)
  - bin-full re-nag while the drawer stays full (auto-clean is paused)
  - hard 'box UNUSABLE' escalation once full long enough that cats can't go
24/7, NO quiet hours — a blocked box is harmful and always alerts. Re-arms on bin_clear.
"""
import sys
import time
from datetime import datetime

from mw import store


class BoxHealthWatch:
    def __init__(self, conn, notify, now_fn=time.time, interval=900,
                 renag_hours=3, unusable_hours=6, approaching_margin=2):
        self.conn = conn
        self.notify = notify
        self.now = now_fn
        self.interval = interval
        self.renag_s = renag_hours * 3600
        self.unusable_s = unusable_hours * 3600
        self.approaching_margin = approaching_margin
        self._last_nag = 0.0          # epoch of the last bin-full / unusable nag
        self._approach_clear = None   # the bin_clear ts the approaching-warn is armed against
        self._approach_warned = False

    def _check(self):
        now = self.now()
        full_since = store.bin_full_since(self.conn)
        if full_since is not None:
            secs = now - datetime.fromisoformat(full_since).timestamp()
            if now - self._last_nag >= self.renag_s:
                h = secs / 3600.0
                if secs >= self.unusable_s:
                    self.notify(f"🚨 Box UNUSABLE {h:.0f}h — auto-clean blocked, "
                                f"cats can't go. Empty the bin NOW.")
                else:
                    self.notify(f"🪣 Litter bin full {h:.0f}h — empty it (auto-clean paused).")
                self._last_nag = now
            return
        # Bin is clear -> reset the full-nag latch so a future fill nags immediately.
        self._last_nag = 0.0
        # Approaching-full heads-up: once per fill cycle, when cleans-since-empty
        # nears the learned capacity.
        last_clear = store.last_bin_clear_ts(self.conn)
        if last_clear != self._approach_clear:
            self._approach_clear = last_clear     # new cycle -> re-arm
            self._approach_warned = False
        if last_clear and not self._approach_warned:
            cap = store.bin_fill_capacity(self.conn)
            if cap:
                cleans = store.cleans_since(self.conn, last_clear)
                if cleans >= cap - self.approaching_margin:
                    left = max(0, cap - cleans)
                    self.notify(f"🪣 Bin getting full — {cleans} auto-cleans since emptied "
                                f"(~{left} till full; your box holds ~{cap}). Empty soon.")
                    self._approach_warned = True

    def run_once(self):
        self._check()

    def run(self):
        while True:
            try:
                self._check()
            except Exception as e:
                print(f"[box-health] error: {e}", file=sys.stderr)
            time.sleep(self.interval)
