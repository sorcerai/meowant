"""Deterministic, in-process remediation for KNOWN watchdog incidents.

Honest scope (per the self-healing council verdict + design review): true
in-process auto-fixes are rare. What this layer actually does is (a) record
every incident to the `incidents` table for audit/runbook/travel-time
visibility, (b) debounce before crying wolf (re-probe a flaky on-demand stream
before escalating), and (c) enrich escalations with a deterministic diagnosis
(e.g. 'agy fell off PATH') so the owner gets an actionable alert, not a bare
symptom.

NOT here: restarting meowantd. The labeler runs as a thread INSIDE meowantd, so
a restart is process self-suicide that can't verify it worked -- and restart
churn CAUSED the 2026-06-22 labeler stall. Process death is already covered by
launchd KeepAlive; wedged-but-alive by the dead-man's switch liveness probe. An
in-process daemon restart sits redundantly between two existing mechanisms and
adds risk, not coverage. If auto-restart is ever wanted, its only safe home is
the dead-man's switch's SEPARATE process.

A playbook is a zero-arg callable returning {action, resolved, escalate}. The
Remediator rate-limits per kind, logs every call, and escalates (notify) only
when the incident was not resolved.
"""
import shutil
import time

from mw import store


class Remediator:
    def __init__(self, conn, notify, now_fn=time.time,
                 max_per_window=3, window_s=3600):
        self.conn = conn
        self.notify = notify
        self.now = now_fn
        self.max_per_window = max_per_window
        self.window_s = window_s

    def handle(self, kind, signal, playbook):
        # Rate-limit on prior ESCALATIONS only: recoveries/suppressions never
        # bothered the owner, so they must not count toward the quiet threshold.
        after = store._iso(self.now() - self.window_s)
        if store.incidents_since(self.conn, kind, after,
                                 outcomes=("escalated",)) >= self.max_per_window:
            store.log_incident(self.conn, kind, signal,
                               "rate-limited (too many escalations recently)",
                               "suppressed", ts=self.now())
            return "suppressed"
        res = playbook()
        outcome = "recovered" if res["resolved"] else "escalated"
        store.log_incident(self.conn, kind, signal, res["action"], outcome,
                           ts=self.now())
        if not res["resolved"]:
            self.notify(res["escalate"])
        return outcome
