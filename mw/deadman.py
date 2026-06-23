"""Independent dead-man's switch: a dumb watchdog that screams to Telegram if the
cats stop being monitored. Run once-and-exit by its own launchd job so it can't
wedge; reads meowant.db directly (no dependency on the meowant daemon) and probes
:8765/state for liveness. Fails LOUD — an exception still fires an alert."""
import json
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

    def check_per_cat(self):
        """Returns (cat, msg) pairs so each cat latches under its own key in run_once."""
        if not self.per_cat_enabled:
            return []
        now = self.now()
        latest = {}   # cat name -> most recent eliminated enter_ts (epoch)
        for s in store.sessions(self.conn):
            if not s["eliminated"] or not s["cat"]:
                continue
            t = datetime.fromisoformat(s["enter_ts"]).timestamp()
            latest[s["cat"]] = max(latest.get(s["cat"], 0), t)
        if not latest:
            return []
        most_recent_any = max(latest.values())
        out = []
        for cat, t in latest.items():
            hours = (now - t) / 3600.0
            # only flag if the SYSTEM is clearly working (someone went recently) but
            # THIS cat is silent — avoids firing during a global quiet/outage period.
            if hours >= self.per_cat_hours and (now - most_recent_any) / 3600.0 < self.per_cat_hours:
                out.append((cat, f"🚨 DEAD-MAN: {cat} hasn't used the box in {hours:.0f}h "
                                 f"(others have) — check on {cat}"))
        return out

    def _load_state(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, state):
        try:
            with open(self.state_path, "w") as f:
                json.dump(state, f)
        except Exception as e:
            print(f"[deadman] state save failed: {e}", file=sys.stderr)

    def _fire(self, key, msg, state):
        last = state.get(key)
        if last is not None:
            age_h = (self.now() - datetime.fromisoformat(last).timestamp()) / 3600.0
            if age_h < self.realarm_hours:
                return 0                              # latched — re-fire later
        self.notify(msg)
        state[key] = datetime.fromtimestamp(self.now()).isoformat(timespec="seconds")
        return 1

    def run_once(self):
        state = self._load_state()
        fired = 0
        # Each check yields (suffix, msg) pairs; suffix=None for whole-system checks
        # (latch key stays the base), or a discriminator (the cat name) so per-cat
        # alerts latch independently — otherwise a 2nd silent cat never alerts.
        for base, fn in (("no_go", lambda: [(None, self.check_no_go())]),
                         ("liveness", lambda: [(None, self.check_liveness())]),
                         ("per_cat", self.check_per_cat)):
            try:
                for suffix, msg in fn():
                    if msg:
                        key = base if suffix is None else f"{base}:{suffix}"
                        fired += self._fire(key, msg, state)
            except Exception as e:
                fired += self._fire(f"{base}_error",
                                    f"🚨 DEAD-MAN: '{base}' check ERRORED ({e}) — "
                                    f"investigate, monitoring integrity unknown", state)
        self._save_state(state)
        return fired
