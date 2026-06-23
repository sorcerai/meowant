"""Independent dead-man's switch: a dumb watchdog that screams to Telegram if the
cats stop being monitored. Run once-and-exit by its own launchd job so it can't
wedge; reads meowant.db directly (no dependency on the meowant daemon) and probes
:8765/state for liveness. Fails LOUD — an exception still fires an alert."""
import json
import os
import sys
import time
from datetime import datetime

from mw import store


def _hhmm_to_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _http_probe(url="http://localhost:8765/state", timeout=5):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


class DeadManSwitch:
    def __init__(self, conn, notify, now_fn=time.time, no_go_hours=12,
                 quiet_start="22:00", quiet_end="08:00", per_cat_enabled=False,
                 per_cat_hours=24, liveness_stale_s=180, realarm_hours=3,
                 state_path="deadman_state.json", state_probe=None):
        self.conn = conn
        self.notify = notify
        self.now = now_fn
        self.no_go_hours = no_go_hours
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self.per_cat_enabled = per_cat_enabled
        self.per_cat_hours = per_cat_hours
        self.liveness_stale_s = liveness_stale_s
        self.realarm_hours = realarm_hours
        self.state_path = state_path
        self.state_probe = state_probe        # () -> dict|None ; None => default HTTP probe

    def _in_quiet(self, now):
        lt = time.localtime(now)
        cur = lt.tm_hour * 60 + lt.tm_min
        s, e = _hhmm_to_min(self.quiet_start), _hhmm_to_min(self.quiet_end)
        return (s <= cur < e) if s <= e else (cur >= s or cur < e)

    def check_no_go(self):
        ts = store.last_elimination_ts(self.conn)
        if ts is None:
            return None
        now = self.now()
        if self._in_quiet(now):
            return None                       # don't alarm overnight; recheck after quiet
        hours = (now - datetime.fromisoformat(ts).timestamp()) / 3600.0
        if hours >= self.no_go_hours:
            since = ts[5:16].replace("T", " ")
            return (f"🚨 DEAD-MAN: no litter box use in {hours:.0f}h (since {since}) "
                    f"— check on the cats")
        return None

    def check_liveness(self):
        probe = self.state_probe or _http_probe
        st = probe()
        if st is None:
            return "🚨 DEAD-MAN: meowant daemon unreachable (:8765 down) — monitoring is OFF"
        last_ok = st.get("last_ok_ts")
        if last_ok is None or (self.now() - last_ok) > self.liveness_stale_s:
            age = "unknown" if last_ok is None else f"{(self.now()-last_ok)/60:.0f}min"
            return (f"🚨 DEAD-MAN: daemon wedged — no device poll in {age} "
                    f"— monitoring may be stalled")
        return None
