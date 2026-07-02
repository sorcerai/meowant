"""Litter-level watch on dp101 (contents_load).

The Jul 1 jam recovery exposed that dp101 is a live load-cell reading of the
drum contents: 225 while the drum sat inverted on the cell (garbage), 96 with
a visually bare bed (ground truth for LOW). There is no factory low-litter
alert, so this fills the gap: sample dp101, alert the sitters when it stays
low, re-arm after a real refill.

Reading discipline — the load cell measures whatever is on it, so a sample
only counts when dp24 == standby (a cat in the box or a mid-clean drum flip
swamps the litter signal). Alerting needs `consecutive` low standby samples;
re-arm needs the level back above threshold + rearm_margin (hysteresis, so a
clump shifting across the cell can't re-arm and re-fire forever).

Every accepted sample is appended to a JSONL log — dp101 was never recorded
before, so this doubles as the calibration dataset: once the sitter fills to
the MAX line we learn what "full" reads and can tune low_threshold from data
instead of the single 96-is-low observation.
"""
import json
import sys
import time

from mw import store

_STATE_KEY = "litter_watch.state"


class LitterWatch:
    def __init__(self, conn, state_fn, notify, low_threshold=110, consecutive=3,
                 rearm_margin=40, interval=300, log_path="litter_load.jsonl",
                 now_fn=time.time):
        self.conn = conn
        self.state_fn = state_fn          # -> raw DPS dict (daemon.state)
        self.notify = notify
        self.low_threshold = low_threshold
        self.consecutive = consecutive
        self.rearm_margin = rearm_margin
        self.interval = interval
        self.log_path = log_path
        self.now = now_fn

    def _latch(self):
        return store.get_daemon_state(self.conn, _STATE_KEY,
                                      {"low_run": 0, "alerted": False})

    def _save(self, st):
        store.set_daemon_state(self.conn, _STATE_KEY, st)

    def sample_once(self):
        dps = self.state_fn() or {}
        if dps.get("24") != "standby":
            return                        # cat/clean on the load cell: not litter
        load = dps.get("101")
        if load is None:
            return
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps({"ts": self.now(), "load": load}) + "\n")
        except Exception as e:
            print(f"[litter_watch] log failed: {e}", file=sys.stderr)
        st = self._latch()
        if load < self.low_threshold:
            st["low_run"] += 1
        else:
            st["low_run"] = 0
            if st["alerted"] and load >= self.low_threshold + self.rearm_margin:
                st["alerted"] = False     # genuine refill: re-armed silently
        if st["low_run"] >= self.consecutive and not st["alerted"]:
            self.notify(
                f"🪣 Litter running LOW in the litter box (level {load}, "
                f"threshold {self.low_threshold}). Please top it up to the MAX "
                f"line on your next visit — low litter makes waste stick and "
                f"can jam the cleaning cycle (the E1 error again).")
            st["alerted"] = True
        self._save(st)

    def run(self):
        while True:
            try:
                self.sample_once()
            except Exception as e:
                print(f"[litter_watch] sample failed: {e}", file=sys.stderr)
            time.sleep(self.interval)
