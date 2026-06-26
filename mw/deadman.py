"""Independent dead-man's switch: a dumb watchdog that screams to Telegram if the
cats stop being monitored. Run once-and-exit by its own launchd job so it can't
wedge; reads meowant.db directly (no dependency on the meowant daemon) and probes
:8765/state for liveness. Fails LOUD — an exception still fires an alert."""
import json
import sys
import time
from datetime import datetime

from mw import store, schedule
from mw.cat_status import THRESHOLDS


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
        return schedule.is_quiet(now, self.quiet_start, self.quiet_end)

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
            if s["cat"] == "Garfield":
                if s["use_record"] is None or s["duration_s"] <= 40:
                    continue
            t = datetime.fromisoformat(s["enter_ts"]).timestamp()
            latest[s["cat"]] = max(latest.get(s["cat"], 0), t)
        if not latest:
            return []
            
        most_recent_any = max(latest.values())
        if (now - most_recent_any) / 3600.0 >= 8:
            return []

        in_quiet = self._in_quiet(now)
        out = []

        for cat, limit in THRESHOLDS.items():
            t = latest.get(cat)
            if not t:
                continue
            hours = (now - t) / 3600.0
            
            if hours >= limit:
                if cat == "Ucok":
                    if not in_quiet: # daytime = tolerant
                        continue
                else:
                    if in_quiet:     # don't alarm overnight for others
                        continue
                out.append((cat, f"🚨 DEAD-MAN: {cat} hasn't used the box in {hours:.0f}h "
                                 f"— check on {cat}"))
        return out

    def _load_state(self):
        # A valid-JSON-but-non-dict latch file (e.g. "[1,2,3]") would make every
        # state.get() in _fire raise — which run_once's fail-loud except would then
        # re-trigger, crashing uncaught and silencing ALL alerts forever. Coerce.
        try:
            with open(self.state_path) as f:
                s = json.load(f)
            return s if isinstance(s, dict) else {}
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
        # Latch ONLY on confirmed delivery, else a dead Telegram token would mark the
        # alert "sent" and re-suppress it every realarm window — failing MUTE forever.
        # An explicit False means the transport failed; None (a stub with no signal)
        # is treated as delivered so older notify callables keep working.
        ok = self.notify(msg)
        if ok is not False:
            state[key] = datetime.fromtimestamp(self.now()).isoformat(timespec="seconds")
            return 1
        return 0

    def run_once(self):
        state = self._load_state()
        fired = 0
        # Each check yields (suffix, msg) pairs; suffix=None for whole-system checks
        # (latch key stays the base), or a discriminator (the cat name) so per-cat
        # alerts latch independently — otherwise a 2nd silent cat never alerts.
        for base, fn in (("liveness", lambda: [(None, self.check_liveness())]),
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
