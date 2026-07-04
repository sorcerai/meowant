"""Cross-sensor jam detection for the SC10.

The Jun 30 incident: the drum stuck mid-rotation with the ramp knocked off,
firmware kept reporting standby/dp22=0 (no E1), and the misaligned IR sensor
manufactured ~20 plausible-looking eliminated "visits" over 34h. Every phantom
elimination reset the no_go deadman, so the one alarm built for "cats can't
use the box" could never fire — and nothing else had a signal to alert on.

The tell was cross-sensor disagreement: the box swears cats are eliminating,
the cameras never see one. This watch encodes that: after K CONSECUTIVE
eliminated visits in which no sampled frame contains a cat (SSDLite catfilter
— identity-independent, so an unknown/occluded-but-visible cat still counts as
seen), alert the OWNER that the box is likely jammed and the deadman is
unreliable. A visit with a visible cat resets the streak; if we alerted, it
also sends the all-clear and re-arms.

Missing/unreadable frames count toward the streak — "no evidence of a cat" is
the suspicious condition, and a healthy period's worst occlusion streak was 3
visits (K defaults to 6). On first run the cursor seeds to the last 2K
eliminated visits so pruned ancient history is never scanned.
"""
import os
import sys
import time

from mw import store
from mw.imgutil import spread_sample

_STATE_KEY = "jam_watch.state"


class JamWatch:
    def __init__(self, conn, catfilter, notify, k=6, frames_per_visit=None,
                 interval=600, lag_s=1200, now_fn=time.time):
        self.conn = conn
        self.catfilter = catfilter
        self.notify = notify
        self.k = k
        # None = sweep ALL existing frames. The globe-tipping incident (Jul 3)
        # showed cats visible in only 1-8 entry/exit frames of 36 — an 8-frame
        # sample missed them and fired a false JAM to the sitters.
        self.frames_per_visit = frames_per_visit
        self.interval = interval
        # Evaluate a visit only after the scorers (matcher 600s, agy 900s) have
        # had a chance to attribute it — their verdict is primary evidence.
        self.lag_s = lag_s
        self.now = now_fn

    # ---- persisted state: survives daemon restarts (no re-alert spam) ------
    def _state(self):
        st = store.get_daemon_state(self.conn, _STATE_KEY, None)
        if st is None:
            st = {"cursor": self._seed_cursor(), "streak": 0,
                  "streak_start": None, "alerted": False}
        return st

    def _save(self, st):
        store.set_daemon_state(self.conn, _STATE_KEY, st)

    def _seed_cursor(self):
        """Start watching from the last 2K eliminated visits, not all history —
        old visits' frames are pruned and would read as a bogus jam streak."""
        with store._lock:
            rows = self.conn.execute(
                "SELECT id FROM visits WHERE eliminated=1 ORDER BY id DESC LIMIT ?",
                (2 * self.k,)).fetchall()
        return (min(r["id"] for r in rows) - 1) if rows else 0

    def _streak(self):
        return self._state()["streak"]

    # ---- detection ---------------------------------------------------------
    def _cat_seen(self, visit_id, attributed_cat):
        """Cat-presence evidence, cheapest first: (1) the visit was attributed
        by matcher/agy/human — the DB already knows a cat was there (sealed-
        globe visits get named from sparse entry/exit frames the camera sweep
        can miss); (2) any capture carries a cat label or prediction; (3) the
        SSDLite sweep over every existing frame. Filter errors and missing
        files yield no evidence, not a crash."""
        if attributed_cat is not None:
            return True
        caps = store.captures_for_visit(self.conn, visit_id)
        if any(c.get("label") is not None or c.get("pred") is not None
               for c in caps):
            return True
        paths = [c["path"] for c in caps if os.path.exists(c["path"])]
        if self.frames_per_visit:
            paths = spread_sample(paths, self.frames_per_visit)
        for p in paths:
            try:
                if self.catfilter.has_cat(p):
                    return True
            except Exception as e:
                print(f"[jam_watch] has_cat({p}) failed: {e}", file=sys.stderr)
        return False

    def _too_young(self, visit_id):
        """Attribution hasn't had time to run yet: defer this visit."""
        with store._lock:
            row = self.conn.execute(
                "SELECT leave_ts, enter_ts FROM visits WHERE id=?",
                (visit_id,)).fetchone()
        ts = (row["leave_ts"] or row["enter_ts"]) if row else None
        if ts is None:
            return False
        try:
            closed = store._parse_ts(str(ts)).timestamp()
        except (TypeError, ValueError):
            return False
        return (self.now() - closed) < self.lag_s

    def check_once(self):
        st = self._state()
        for vid, _cat in store.eliminated_visits_after(self.conn, st["cursor"]):
            if self._too_young(vid):
                break                  # ordered by id: everything after is younger
            if self._cat_seen(vid, _cat):
                if st["alerted"]:
                    self.notify("✅ SC10 jam cleared: a cat is visible at the "
                                "box again — visit logging looks real.")
                st.update(streak=0, streak_start=None, alerted=False)
            else:
                st["streak"] += 1
                if st["streak_start"] is None:
                    with store._lock:
                        row = self.conn.execute(
                            "SELECT enter_ts FROM visits WHERE id=?", (vid,)).fetchone()
                    st["streak_start"] = row["enter_ts"] if row else None
            st["cursor"] = vid
        if st["streak"] >= self.k and not st["alerted"]:
            since = st["streak_start"] or "?"
            self.notify(
                f"🚨 SC10 may be JAMMED: {st['streak']} consecutive "
                f"\"eliminated\" visits since {since} with NO cat on any "
                f"camera. The box is likely stuck mid-cycle while its firmware "
                f"reports no fault, and the no-go deadman is being reset by "
                f"phantom visits — it CANNOT alarm until this clears. "
                f"Check the box / power-cycle it at the plug.")
            st["alerted"] = True
        self._save(st)

    def run(self):
        while True:
            try:
                self.check_once()
            except Exception as e:
                print(f"[jam_watch] check failed: {e}", file=sys.stderr)
            time.sleep(self.interval)
