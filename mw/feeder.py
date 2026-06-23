"""PLAF103 feeder: local Tuya control + the dp-118 feed-record decode.

Confirmed against the live device (a real test feed). Local control only — the
cloud `manual_feed` returns 1109. NON-persistent socket: a persistent socket
returns partial dp pushes; a fresh status() returns the full dp set we need
(food_level + the dp-118 feed record).
"""
import base64
import sys
import threading
import time
from datetime import datetime, date
from mw import store

DP_MANUAL_FEED = 3        # write-only: set_value(3, portions) dispenses
DP_FEED_STATE = "4"       # standby | feeding | feed_end
DP_FOOD_LEVEL = "108"     # hopper enum (full | ...)
DP_FEED_RECORD = "118"    # base64 last-feed record (persists)


def decode_feed_record(b64):
    """Decode a dp-118 feed record -> {"ts": epoch, "portions": int}, or None.
    Format (>=8 bytes): [year_hi, year_lo, month, day, hour, min, sec, portions, ...]."""
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


class FeederDevice:
    def __init__(self, cfg):
        import tinytuya
        self._lock = threading.Lock()
        self._cfg = cfg
        self._dev = None
        self._tinytuya = tinytuya

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
                        rec = dps.get(DP_FEED_RECORD)
                        return {
                            "feed_state": dps.get(DP_FEED_STATE),
                            "food_level": dps.get(DP_FOOD_LEVEL),
                            "last_feed": decode_feed_record(rec) if rec else None,
                            "online": True,
                        }
                except Exception:
                    self._dev = None
            return {"online": False}

    def feed(self, portions):
        with self._lock:
            try:
                self._device().set_value(DP_MANUAL_FEED, int(portions))
                return True
            except Exception as e:
                print(f"[feeder] feed({portions}) failed: {e}", file=sys.stderr)
                self._dev = None
                return False


class FakeFeederDevice:
    """Replays status() snapshots; records feed() portions in .fed."""
    def __init__(self, snapshots):
        self._snaps = list(snapshots)
        self._i = 0
        self.fed = []

    def status(self):
        if self._i < len(self._snaps):
            s = self._snaps[self._i]
            self._i += 1
            return dict(s)
        return dict(self._snaps[-1]) if self._snaps else {"online": False}

    def feed(self, portions):
        self.fed.append(portions)
        return True


class FeederMonitor:
    """Polls the feeder: logs each new dp-118 feed, and runs latched fail-loud
    watchdogs (missed scheduled drop, empty hopper, unreachable). Source of a feed
    is 'manual' if a /feed was issued within manual_window_s, else 'scheduled'."""
    def __init__(self, device, conn, notify, mealtimes=(), now_fn=time.time,
                 poll_interval_s=120, miss_grace_minutes=30, offline_minutes=30,
                 low_food_levels=("empty", "low"), manual_window_s=600):
        self.device = device
        self.conn = conn
        self.notify = notify
        self.mealtimes = list(mealtimes)
        self.now = now_fn
        self.poll_interval_s = poll_interval_s
        self.miss_grace_minutes = miss_grace_minutes
        self.offline_minutes = offline_minutes
        self.low_food_levels = set(low_food_levels)
        self.manual_window_s = manual_window_s
        self._last_logged_feed_ts = store.last_feed_event_ts(conn)  # resume across restarts
        self._offline_since = None
        self._offline_alerted = False
        self._hopper_alerted = False
        self._missed_alerted = set()        # {(date_iso, "HH:MM")}
        self._expect_manual_until = 0

    def note_manual_feed(self):
        """Call right after a successful /feed so the next detected feed is 'manual'."""
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
            if self.notify(f"🍽️ Feeder unreachable for {self.offline_minutes}min+ "
                           f"— can't confirm feeding") is not False:
                self._offline_alerted = True

    def _detect_dispense(self, last_feed):
        if not last_feed or last_feed.get("ts") is None:
            return
        ts = last_feed["ts"]
        # +1s guard so an equal stored ts isn't re-logged (float round-trip slack)
        if self._last_logged_feed_ts is not None and ts <= self._last_logged_feed_ts + 1:
            return
        source = "manual" if self.now() <= self._expect_manual_until else "scheduled"
        store.log_feed_event(self.conn, last_feed.get("portions", 0), source, ts=ts)
        self._last_logged_feed_ts = ts
        if source == "manual":
            self._expect_manual_until = 0

    def _check_hopper(self, food_level):
        if food_level is None:
            return
        if food_level in self.low_food_levels:
            self._fire(f"🍽️ Feeder hopper {food_level} — refill soon "
                       f"(cats will run out)", "_hopper_alerted")
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
            deadline = meal + self.miss_grace_minutes * 60
            if now < deadline:
                continue                     # window still open
            if store.feed_in_window(self.conn, meal, deadline):
                self._missed_alerted.add(key)            # satisfied
            elif self.notify(f"🚨 Feeder MISSED the {hhmm} drop (no dispense by "
                             f"+{self.miss_grace_minutes}min) — check the feeder") is not False:
                self._missed_alerted.add(key)

    def poll_once(self):
        st = self.device.status()
        self._check_online(bool(st.get("online")))
        if not st.get("online"):
            return                           # can't trust other signals when offline
        self._detect_dispense(st.get("last_feed"))
        self._check_hopper(st.get("food_level"))
        self._check_missed_drops()

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:           # never let the feeder thread die
                print(f"[feeder-monitor] error: {e}", file=sys.stderr)
            time.sleep(self.poll_interval_s)
