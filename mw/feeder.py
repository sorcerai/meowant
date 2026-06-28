"""Feeder module: local Tuya control for multiple feeders.

Supports device-specific DP profiles (PLAF103 vs Andoll).
"""
import base64
import sys
import threading
import time
from datetime import datetime
from mw import bowl, store

def decode_plaf103_record(b64):
    """Decode a dp-118 feed record -> {"ts": epoch, "portions": int}, or None."""
    if not b64:
        return None
    try:
        b = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if len(b) < 8:
        return None
    year = (b[0] << 8) | b[1]
    try:
        dt = datetime(year, b[2], b[3], b[4], b[5], b[6])
    except ValueError:
        return None
    return {"ts": dt.timestamp(), "portions": b[7]}

PROFILES = {
    "PLAF103": {
        "manual_feed": 3,
        "feed_state": "4",
        "food_level": "108",
        "feed_record": "118",
    },
    "Andoll": {
        "manual_feed": 3,
        "feed_state": "4",
        "food_level": None,  # dp10 validated NOT food level (stays 0 through a real feed)
        "feed_record": "14", # dp14 = last-feed portion count (best-effort; not a reliable trigger)
    }
}

# Transient feed-active states. A dispense is detected as a transition INTO one of
# these from a resting state. PLAF103 rests in "feed_end", Andoll in "standby" — so
# neither resting state is here. "done" is included because the Andoll "feeding"
# window is ~3s and a poll can land on the post-feed "done" instead.
ACTIVE_FEED_STATES = ("feeding", "done")

class FeederDevice:
    def __init__(self, cfg):
        import tinytuya
        self._lock = threading.Lock()
        self._cfg = cfg
        self._dev = None
        self._tinytuya = tinytuya
        self.label = cfg.get("label", "default")
        self.profile_name = cfg.get("dp_profile", "PLAF103")
        self.profile = PROFILES.get(self.profile_name, PROFILES["PLAF103"])
        self._last_raw_record = None

    def _device(self):
        if self._dev is None:
            self._dev = self._tinytuya.Device(
                dev_id=self._cfg["device_id"], address=self._cfg["address"],
                local_key=self._cfg["local_key"], version=float(self._cfg["version"]))
            self._dev.set_socketTimeout(5)     # NOT persistent (avoids partial pushes)
        return self._dev

    def status(self):
        with self._lock:
            for _ in (1, 2):
                try:
                    data = self._device().status()
                    dps = data.get("dps", {}) if isinstance(data, dict) else {}
                    if dps:
                        rec = dps.get(self.profile["feed_record"])
                        last_feed = None
                        if rec is not None:
                            if self.profile_name == "PLAF103":
                                last_feed = decode_plaf103_record(rec)
                            else:
                                # For Andoll, rec is an integer (portions). We emit a new feed
                                # only if the record value changes. (Imperfect for consecutive identical feeds).
                                if rec != self._last_raw_record and self._last_raw_record is not None:
                                    last_feed = {"ts": time.time(), "portions": rec}
                                self._last_raw_record = rec
                                
                        return {
                            "feed_state": dps.get(self.profile["feed_state"]),
                            "food_level": dps.get(self.profile["food_level"]),
                            "last_feed": last_feed,
                            "online": True,
                        }
                except Exception:
                    self._dev = None
            return {"online": False}

    def feed(self, portions):
        with self._lock:
            try:
                dp = self.profile["manual_feed"]
                self._device().set_value(dp, int(portions))
                return True
            except Exception as e:
                print(f"[feeder {self.label}] feed({portions}) failed: {e}", file=sys.stderr)
                self._dev = None
                return False


class FeederMonitor:
    """Polls the feeder: logs each new feed, and runs watchdogs."""
    def __init__(self, device, conn, notify, mealtimes=(), now_fn=time.time,
                 poll_interval_s=120, miss_grace_minutes=30, miss_lead_minutes=5,
                 offline_minutes=30, low_food_levels=("empty", "low"), manual_window_s=600):
        self.device = device
        self.conn = conn
        self.notify = notify
        self.mealtimes = list(mealtimes)
        self.now = now_fn
        self.poll_interval_s = poll_interval_s
        self.miss_grace_minutes = miss_grace_minutes
        self.miss_lead_minutes = miss_lead_minutes
        self.offline_minutes = offline_minutes
        self.low_food_levels = set(low_food_levels)
        self.manual_window_s = manual_window_s
        
        self.label = self.device.label
        self._last_logged_feed_ts = store.last_feed_event_ts(conn, feeder=self.label)
        self._offline_since = None
        self._offline_alerted = False
        self._hopper_alerted = False
        self._missed_alerted = set()        # {(date_iso, "HH:MM")}
        self._expect_manual_until = 0
        self._started = self.now()          # don't alarm meals that closed pre-start

        self.bowl_watch = None
        self._preshots = {}                 # hhmm -> (ts, changed_pct)
        self._last_feed_state = None

    def note_manual_feed(self):
        self._expect_manual_until = self.now() + self.manual_window_s

    def _fire(self, msg, latch_attr):
        if getattr(self, latch_attr):
            return
        if self.notify(msg) is not False:
            setattr(self, latch_attr, True)

    def _check_online(self, online):
        now = self.now()
        if online:
            self._offline_since = None
            self._offline_alerted = False
            return
        if self._offline_since is None:
            self._offline_since = now
        elif (not self._offline_alerted
              and (now - self._offline_since) >= self.offline_minutes * 60):
            if self.notify(f"🍽️ Feeder '{self.label}' unreachable for {self.offline_minutes}min+ "
                           f"— can't confirm feeding") is not False:
                self._offline_alerted = True

    def _detect_dispense(self, st):
        last_feed = st.get("last_feed")
        feed_state = st.get("feed_state")
        now = self.now()
        source = "manual" if now <= self._expect_manual_until else "scheduled"

        active = feed_state in ACTIVE_FEED_STATES
        was_active = self._last_feed_state in ACTIVE_FEED_STATES
        if active and not was_active:
            # Entered an active feed-state -> a dispense. Trigger on feeding|done so a
            # fast-poll that misses the brief "feeding" still catches the feed via "done".
            store.log_feed_event(self.conn, 0, source, feeder=self.label, ts=now)
            self._last_logged_feed_ts = now
            if source == "manual":
                self._expect_manual_until = 0
        self._last_feed_state = feed_state

        if not last_feed or last_feed.get("ts") is None:
            return
        
        # Note: dp-118 holds only the LAST feed event.
        # Two dispenses within one 120s poll will log as one (the first is overwritten).
        # This is practically implausible given normal meal spacing, so we accept the undercount.
        ts = last_feed["ts"]
        if self._last_logged_feed_ts is not None and ts <= self._last_logged_feed_ts + 60:
            return
        store.log_feed_event(self.conn, last_feed.get("portions", 0), source, feeder=self.label, ts=ts)
        self._last_logged_feed_ts = ts
        if source == "manual":
            self._expect_manual_until = 0

    def _check_hopper(self, food_level):
        if food_level is None:
            return
        # If food_level is an integer (e.g. some percentage), skip the string comparison
        if isinstance(food_level, int):
            return
            
        if food_level in self.low_food_levels:
            self._fire(f"🍽️ Feeder '{self.label}' hopper {food_level} — refill soon", "_hopper_alerted")
        else:
            self._hopper_alerted = False     # back to full -> re-arm

    def _check_missed_drops(self):
        if not self.mealtimes:
            return
        now = self.now()
        lt = time.localtime(now)
        today = "%04d-%02d-%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday)
        for hhmm in self.mealtimes:
            key = (today, hhmm)
            if key in self._missed_alerted:
                continue
            h, m = (int(x) for x in hhmm.split(":"))
            meal = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
            start = meal - (self.miss_lead_minutes * 60)
            deadline = meal + self.miss_grace_minutes * 60
            if deadline <= self._started:
                continue                     # meal closed before we started watching
            if now < deadline:
                continue                     # window still open
            
            has_feed = store.feed_in_window(self.conn, start, deadline, feeder=self.label)
            expected_skip = False
            if not has_feed and self.bowl_watch:
                post_pct, post_state = self.bowl_watch.check_fullness()
                # (a) a dispense the device poll missed shows up as a bowl rise vs the pre-shot
                if hhmm in self._preshots and post_pct is not None:
                    pre_pct = self._preshots[hhmm][1]
                    if post_pct > pre_pct + 3.0:
                        store.log_feed_event(self.conn, 0, "scheduled", feeder=self.label, ts=meal)
                        has_feed = True
                # (b) bowl still has food -> the feeder's skip-when-full is intentional, not a
                # miss. Suppress the false alarm. An EMPTY or unreadable (None) bowl still
                # alarms — fail toward alerting, never toward a silent starve.
                if not has_feed and post_state is not None and post_state != bowl.EMPTY:
                    expected_skip = True

            if has_feed or expected_skip:
                self._missed_alerted.add(key)            # fed, or an intentional skip-when-full
            elif self.notify(f"🚨 Feeder '{self.label}' MISSED the {hhmm} drop (no dispense by "
                             f"+{self.miss_grace_minutes}min) — check the feeder") is not False:
                store.log_incident(self.conn, "missed_feed", {"mealtime": hhmm, "feeder": self.label}, "escalated", "failed", "No feed or bowl-rise detected in window")
                self._missed_alerted.add(key)

    def poll_once(self):
        st = self.device.status()
        self._check_online(bool(st.get("online")))
        if not st.get("online"):
            return
        self._detect_dispense(st)
        self._check_hopper(st.get("food_level"))
        self._check_missed_drops()

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:
                print(f"[feeder-monitor {self.label}] error: {e}", file=sys.stderr)
            
            sleep_s = self.poll_interval_s
            now = self.now()
            lt = time.localtime(now)
            if self.mealtimes:
                for hhmm in self.mealtimes:
                    h, m = (int(x) for x in hhmm.split(":"))
                    meal = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
                    start = meal - (self.miss_lead_minutes * 60)
                    deadline = meal + (self.miss_grace_minutes * 60)
                    if start <= now <= deadline:
                        # Fast polling window
                        sleep_s = 3
                        # Take pre-shot if we haven't
                        if self.bowl_watch and hhmm not in self._preshots:
                            pct, _ = self.bowl_watch.check_fullness()
                            if pct is not None:
                                self._preshots[hhmm] = (now, pct)
                        break
                    elif now > deadline and hhmm in self._preshots:
                        del self._preshots[hhmm]

            time.sleep(sleep_s)
