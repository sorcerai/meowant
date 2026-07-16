"""Daily alive heartbeat: proof the alert PIPE itself is still working.

Every other watcher in this daemon only speaks up when something is wrong —
so if the notify channel dies (the exact Jul 15 failure mode: a transient
DNS error swallowing a Telegram send), the owner has no way to distinguish
"a quiet day" from "the alert pipe is dead". This sends one heartbeat per
calendar day; if it stops arriving, the pipe is the thing that broke, not
the cats. Silence becomes the signal.
"""
import sys
import time

_STATE_KEY = "alive.last_sent_date"


class AliveHeartbeat:
    def __init__(self, notify, hour_local=9, now_fn=time.time,
                 state_get=None, state_set=None):
        self.notify = notify
        self.hour_local = hour_local
        self.now = now_fn
        self.state_get = state_get      # (key, default) -> value, e.g. store.get_daemon_state pre-bound to conn
        self.state_set = state_set      # (key, value) -> None

    def _today(self):
        lt = time.localtime(self.now())
        return "%04d-%02d-%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday)

    def _last_sent(self):
        return self.state_get(_STATE_KEY, None) if self.state_get else None

    def tick(self):
        lt = time.localtime(self.now())
        if lt.tm_hour < self.hour_local:
            return                                  # too early today
        today = self._today()
        if self._last_sent() == today:
            return                                  # already sent today
        msg = f"✅ meowant alive — all watchers running [{today}]"
        if self.notify(msg) is not False:
            if self.state_set:
                self.state_set(_STATE_KEY, today)
            # else: no persistence wired -> best-effort, may resend after a restart

    def run(self, interval_s=600):
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"[alive] tick failed: {e}", file=sys.stderr)
            time.sleep(interval_s)
