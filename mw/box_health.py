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
        # Latch state, PERSISTED across restarts: reset to defaults on every
        # restart, an active bin-full would re-nag immediately and the
        # approaching-full heads-up would re-fire — duplicate alerts.
        st = store.get_daemon_state(conn, "box_health.latch", {}) or {}
        self._last_nag = st.get("last_nag", 0.0)        # epoch of last bin-full/unusable nag
        self._approach_clear = st.get("approach_clear") # bin_clear ts the warn is armed against
        self._approach_warned = st.get("approach_warned", False)

    def _save_latch(self):
        store.set_daemon_state(self.conn, "box_health.latch", {
            "last_nag": self._last_nag,
            "approach_clear": self._approach_clear,
            "approach_warned": self._approach_warned,
        })

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
        self._save_latch()   # persist so a restart doesn't re-nag an active alarm

    def run(self):
        while True:
            try:
                self.run_once()   # _check + persist latch
            except Exception as e:
                print(f"[box-health] error: {e}", file=sys.stderr)
            time.sleep(self.interval)
