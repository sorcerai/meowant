"""Syncs each feeder's meal schedule from the Tuya cloud into the running
FeederMonitor + config.json, so editing feed times in the Tuya app updates
missed-drop detection without a manual config edit.

The feeder's LOCAL dp1 (meal_plan) reports None (a Tuya quirk); the cloud
getstatus() returns it reliably as base64. Cloud is the only read path.
"""
import base64
import json
import os
import sys
import tempfile
import time

RECORD_LEN = 5


def decode_meal_plan(b64):
    """Decode a meal_plan base64 blob into a list of
    {"time": "HH:MM", "portions": int, "enabled": bool, "days": int} records.
    Never raises — malformed/short/missing input returns []."""
    if not b64:
        return []
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return []
    records = []
    for i in range(0, len(raw) - len(raw) % RECORD_LEN, RECORD_LEN):
        days, hour, minute, portions, enabled = raw[i:i + RECORD_LEN]
        records.append({
            "time": f"{hour:02d}:{minute:02d}",
            "portions": portions,
            "enabled": bool(enabled),
            "days": days,
        })
    return records


def enabled_mealtimes(b64):
    """Sorted, de-duplicated "HH:MM" list of the enabled records only."""
    return sorted({r["time"] for r in decode_meal_plan(b64) if r["enabled"]})


class FeedPlanSync:
    """Polls the Tuya cloud for each feeder's meal_plan and, on a change,
    updates the live FeederMonitor and persists it into config.json."""

    def __init__(self, fetch_plan, feeders, monitors, notify, config_path,
                 interval_s=3600, now_fn=time.time):
        self.fetch_plan = fetch_plan
        self.feeders = feeders
        self.monitors = monitors
        self.notify = notify
        self.config_path = config_path
        self.interval_s = interval_s
        self.now = now_fn

    def sync_once(self):
        changed = 0
        for f_cfg in self.feeders:
            label = f_cfg.get("label")
            device_id = f_cfg.get("device_id")
            mon = self.monitors.get(label)
            if not device_id or mon is None:
                continue
            try:
                b64 = self.fetch_plan(device_id)
            except Exception as e:
                print(f"[feed-plan-sync {label}] fetch failed: {e}", file=sys.stderr)
                continue
            if not b64:
                # Never wipe a schedule to empty on a bad/missing read.
                print(f"[feed-plan-sync {label}] empty/no cloud read, keeping current schedule",
                      file=sys.stderr)
                continue
            new_times = enabled_mealtimes(b64)
            if not new_times:
                print(f"[feed-plan-sync {label}] decode produced no enabled times, "
                      f"keeping current schedule", file=sys.stderr)
                continue
            if new_times == mon.mealtimes:
                continue
            mon.mealtimes = new_times
            mon._missed_alerted = set()   # a removed time must not stay suppressed;
                                            # a new time must be watchable immediately
            self._persist(label, new_times)
            self.notify(f"🍽️ {label} feeder schedule changed → {', '.join(new_times)} "
                        f"(auto-synced from the app)")
            changed += 1
        return changed

    def run(self):
        while True:
            try:
                self.sync_once()
            except Exception as e:
                print(f"[feed-plan-sync] error: {e}", file=sys.stderr)
            time.sleep(self.interval_s)

    def _persist(self, label, mealtimes):
        try:
            with open(self.config_path) as f:
                cfg = json.load(f)
            for f_cfg in cfg.get("feeders", []):
                if f_cfg.get("label") == label:
                    f_cfg["mealtimes"] = mealtimes
                    break
            d = os.path.dirname(os.path.abspath(self.config_path))
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".config-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(cfg, f, indent=2)
                os.replace(tmp, self.config_path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except Exception as e:
            print(f"[feed-plan-sync {label}] failed to persist config: {e}", file=sys.stderr)
