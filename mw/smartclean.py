"""Trigger a scoop after idle standby OR a hard max-wait cap since first departure.

idle_seconds: scoop after this many seconds of continuous standby (resets when a
cat re-enters — don't scoop mid-use).
max_wait_seconds: scoop this many seconds after the FIRST departure even if the
cat keeps re-entering (this is the part firmware dp5 cannot do — it beats the
re-entry starvation). Only ever fires while the cat is NOT present (standby).
"""


class SmartClean:
    def __init__(self, idle_seconds=90, max_wait_seconds=480, enabled=True):
        self.idle = idle_seconds
        self.max_wait = max_wait_seconds
        self.enabled = enabled
        self._standby_since = None
        self._first_departure_ts = None
        self._armed = False  # cold start: do NOT scoop until a cat has been seen

    def _reset(self):
        self._standby_since = None
        self._first_departure_ts = None
        self._armed = False

    def notify_cleaned(self):
        """Call after ANY clean (smartclean-triggered or manual API) so a manual
        clean can't be followed by a redundant auto-scoop."""
        self._reset()

    def update(self, dps, now):
        status = dps.get("24")
        if status is None:
            return False  # partial poll missing dp24 → no-op, do NOT reset timers
        if status == "cat_get_in":
            self._standby_since = None
            self._armed = True
            return False
        if status in ("cleaning", "clean_done"):
            self._reset()  # a clean is happening/finished → clear, avoid re-fire
            return False
        if status == "standby":
            if not self._armed:
                return False
            if self._first_departure_ts is None:
                self._first_departure_ts = now
            if self._standby_since is None:
                self._standby_since = now
            if not self.enabled:
                return False
            idle_ok = now - self._standby_since >= self.idle
            cap_ok = now - self._first_departure_ts >= self.max_wait
            if idle_ok or cap_ok:
                self._reset()  # one-shot until next presence
                return True
            return False
        # "waiting" (firmware countdown) or any other non-present state:
        self._standby_since = None  # pause idle clock; keep max-wait running
        return False
