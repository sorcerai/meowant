"""Fold semantic events into visit rows (one open visit at a time)."""
from mw import store
from mw.events import CAT_ENTER, CAT_LEAVE, ELIMINATION


class VisitTracker:
    def __init__(self, conn, grace_seconds=1800):
        self.conn = conn
        self.grace_seconds = grace_seconds
        self._open_id = None
        self._enter_ts = None
        self._last_closed_id = None
        self._last_closed_ts = None

    def handle(self, ev):
        if ev.kind == CAT_ENTER:
            if self._open_id is None:
                self._open_id = store.open_visit(self.conn, ev.ts)
                self._enter_ts = ev.ts
        elif ev.kind == CAT_LEAVE:
            if self._open_id is not None:
                dur = int(ev.ts - self._enter_ts)
                store.close_visit(self.conn, self._open_id, ev.ts, dur)
                self._last_closed_id = self._open_id
                self._last_closed_ts = ev.ts
                self._open_id = None
                self._enter_ts = None
        elif ev.kind == ELIMINATION:
            if self._open_id is not None:
                store.mark_elimination(self.conn, self._open_id,
                                       ev.detail.get("use_record"))
            elif (self._last_closed_id is not None
                  and self._last_closed_ts is not None
                  and ev.ts - self._last_closed_ts <= self.grace_seconds):
                store.mark_elimination(self.conn, self._last_closed_id,
                                       ev.detail.get("use_record"))

    def observe_load(self, dps):
        """Feed the current poll's dp101 into the open visit (if any). Called
        every tick; while a cat is inside, the load cell reads litter+cat."""
        if self._open_id is not None and dps.get("101") is not None:
            store.update_visit_load(self.conn, self._open_id, dps["101"])
