"""Poll the device, detect+persist+track events, run smart-clean."""
import sys
import time

from mw import store
from mw.events import detect_events
from mw.tracker import VisitTracker


class Daemon:
    def __init__(self, device, conn, smartclean, on_event=None, now_fn=time.time):
        self.device = device
        self.conn = conn
        self.smartclean = smartclean
        self.on_event = on_event
        self.now = now_fn
        self.tracker = VisitTracker(conn)
        self.prev = {}
        self.state = {}
        self.last_ok_ts = None
        self._baseline_done = False
        # Close any visit left open by a prior crash/restart.
        store.reconcile_open_visits(self.conn)

    def tick(self):
        try:
            now = self.now()
            dps = self.device.status_dps()
            if dps:
                self.last_ok_ts = now
                # First successful poll establishes baseline WITHOUT emitting
                # events (a restart must not synthesize edges off an empty prev).
                if not self._baseline_done:
                    self.prev = dict(dps)
                    self.state = self.prev
                    self._baseline_done = True
                    self.smartclean.update(self.prev, now)  # may arm
                    return self.state
                for ev in detect_events(self.prev, dps, now):
                    store.insert_event(self.conn, ev)
                    self.tracker.handle(ev)
                    if self.on_event:
                        self.on_event(ev)
                self.tracker.observe_load(dps)    # dp101 -> open visit min/max
                self.prev = {**self.prev, **dps}  # merge: tolerate partial updates
                self.state = self.prev
                if self.smartclean.update(self.prev, now):
                    self.device.clean()
                    self.smartclean.notify_cleaned()
        except Exception as e:  # never let the loop thread die
            print(f"[meowantd] tick error: {e}", file=sys.stderr)
        return self.state

    def run(self, interval=3.0, ticks=None):
        n = 0
        while ticks is None or n < ticks:
            self.tick()
            n += 1
            time.sleep(interval)
