"""BowlWatch: poll the bowl camera, judge full/empty, refill / observe.

Pipeline per poll: grab a frame -> skip if a cat is at the bowl (catfilter) ->
cv2 diff-vs-empty (mw.bowl) -> if it reads empty, CONFIRM with agy -> require 2
consecutive confirmed-empty (debounce) -> alert and/or auto-feed (rate-limited).
On a full->empty transition, log time-since-last-feed (consumption trend).

Every failure degrades toward no-action/alert, never a silent over-feed or
missed empty. Latches per empty episode, re-arms on refill, and latches only on
confirmed delivery (matching the other watchdogs).
"""
import subprocess
import sys
import time

from mw import bowl, store


def agy_bowl_empty(frame_path, timeout=240):
    """True if agy says the bowl is empty, False if it has food, None on error.
    One `agy --print` call (same backend as the labeler)."""
    prompt = (f"Look at the cat food bowl in this image: {frame_path}. "
              f"Is the bowl EMPTY (no food) or does it have FOOD in it? "
              f"Answer with one word: EMPTY or FOOD.")
    try:
        out = subprocess.run(["agy", "--print", prompt], capture_output=True,
                             text=True, timeout=timeout, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError) as e:
        print(f"[bowl/agy] {frame_path} failed ({e})", file=sys.stderr)
        return None
    t = out.stdout.lower()
    ie, ifd = t.find("empty"), t.find("food")
    if ie == -1 and ifd == -1:
        return None
    if ie == -1:
        return False
    if ifd == -1:
        return True
    return ie < ifd            # whichever word agy says first wins


class BowlWatch:
    def __init__(self, grab, catfilter, conn, notify, feeder=None,
                 confirm_empty=agy_bowl_empty, now_fn=time.time, *,
                 empty_ref, roi=bowl.DEFAULT_ROI, empty_max=5.0, full_min=20.0,
                 delta=22, poll_interval_s=1200, auto_feed=False,
                 auto_feed_portions=1, auto_feed_max_per_day=4, location="downstairs"):
        self.grab = grab
        self.catfilter = catfilter
        self.conn = conn
        self.notify = notify
        self.feeder = feeder
        self.confirm_empty = confirm_empty
        self.now = now_fn
        self.empty_ref = empty_ref
        self.roi = roi
        self.empty_max = empty_max
        self.full_min = full_min
        self.delta = delta
        self.poll_interval_s = poll_interval_s
        self.auto_feed = auto_feed
        self.auto_feed_portions = auto_feed_portions
        self.auto_feed_max_per_day = auto_feed_max_per_day
        self.location = location
        self._prev_state = store.last_bowl_state(conn, location=self.location)   # resume across restarts
        self._empty_streak = 0
        self._empty_alerted = False

    def _record_has_food(self, state):
        """Re-arm and persist a vision row on a state CHANGE so /bowl + the digest
        reflect the current bowl state. Logs only on change (not every poll)."""
        self._empty_streak = 0
        self._empty_alerted = False
        if state != self._prev_state:
            store.log_bowl_event(self.conn, state, "vision", location=self.location, ts=self.now())
            self._prev_state = state

    def check_fullness(self):
        """Take an immediate grab and return (changed_pct, state). Returns (None, None) if blocked or unreadable."""
        path = self.grab()
        if not path or not self.catfilter.is_clear(path):
            return None, None
        pct = bowl.changed_pct(path, self.empty_ref, self.roi, self.delta)
        if pct is None:
            return None, None
        if pct <= self.empty_max:
            return pct, bowl.EMPTY
        if pct >= self.full_min:
            return pct, bowl.FULL
        return pct, bowl.SOME

    def poll_once(self):
        path = self.grab()
        if not path:
            return                       # grab failed -> skip (no false empty)
        if not self.catfilter.is_clear(path):
            return                       # a cat is at the bowl -> don't judge
        state = bowl.fullness(path, self.empty_ref, self.roi, self.delta,
                              self.empty_max, self.full_min)
        if state is None:
            return                       # unreadable -> skip
        if state != bowl.EMPTY:
            self._record_has_food(state)
            return
        conf = self.confirm_empty(path)
        if conf is None:
            return                       # agy inconclusive -> skip
        if not conf:                     # agy: actually has food -> not empty
            self._record_has_food(bowl.SOME)
            return
        self._empty_streak += 1
        if self._empty_streak >= 2:      # debounce: 2 consecutive confirmed-empty
            self._on_empty()

    def _on_empty(self):
        now = self.now()
        if self._prev_state != bowl.EMPTY:          # transition: log consumption once
            f_lbl = self.feeder.label if self.feeder else None
            lf = store.last_feed_event_ts(self.conn, feeder=f_lbl)
            secs = int(now - lf) if lf is not None else None
            store.log_bowl_event(self.conn, bowl.EMPTY, "vision",
                                 secs_since_feed=secs, location=self.location, ts=now)
            self._prev_state = bowl.EMPTY
        if self._empty_alerted:
            return                                  # one action per empty episode
        if self.auto_feed and self.feeder is not None:
            if store.auto_feeds_today(self.conn, location=self.location) >= self.auto_feed_max_per_day:
                if self.notify(f"\U0001f514 Bowl '{self.location}' empty — auto-feed daily limit "
                               f"({self.auto_feed_max_per_day}) reached; refill "
                               f"manually.") is not False:
                    self._empty_alerted = True
            elif self.feeder.feed(self.auto_feed_portions):
                store.log_bowl_event(self.conn, bowl.EMPTY, "auto_feed", location=self.location)
                if self.notify(f"\U0001f37d️ Bowl '{self.location}' was empty — auto-fed "
                               f"{self.auto_feed_portions} portion(s).") is not False:
                    self._empty_alerted = True
            else:
                self.notify(f"⚠️ Bowl '{self.location}' empty + auto-feed FAILED (feeder unreachable?).")
        else:
            if self.notify(f"\U0001f514 Bowl '{self.location}' empty — /feed {self.location} to refill?") is not False:
                self._empty_alerted = True

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:                  # never let the bowl thread die
                print(f"[bowl-watch] error: {e}", file=sys.stderr)
            time.sleep(self.poll_interval_s)
