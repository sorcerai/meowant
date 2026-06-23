"""PLAF103 feeder: local Tuya control + the dp-118 feed-record decode.

Confirmed against the live device (a real test feed). Local control only — the
cloud `manual_feed` returns 1109. NON-persistent socket: a persistent socket
returns partial dp pushes; a fresh status() returns the full dp set we need
(food_level + the dp-118 feed record).
"""
import base64
import sys
import threading
from datetime import datetime

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
