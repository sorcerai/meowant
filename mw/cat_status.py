"""Single source of truth for per-cat health status (UI + watcher must agree).

Mirrors health_watch.THRESHOLDS. Status from hours since the most recent
attributed, eliminated visit: ok < 0.75*threshold <= watch < threshold <= alert.
No attributed data => 'ok' with nulls (insufficient data is not an alarm)."""
import time
from datetime import datetime

from mw import store

THRESHOLDS = {"Ucok": 8, "Ella": 24, "Garfield": 24}


def cat_status(conn, now_fn=time.time):
    now = now_fn()
    out = []
    for name, threshold in THRESHOLDS.items():
        last_ts = store.last_attributed_elimination_ts(conn, name)
        count = store.eliminations_today_for_cat(conn, name, now=now)
        if last_ts is None:
            out.append({"name": name, "status": "ok", "last_litter_ts": None,
                        "hours_since": None, "threshold_h": threshold,
                        "litter_count_today": count})
            continue
        hours = (now - datetime.fromisoformat(last_ts).timestamp()) / 3600.0
        if hours >= threshold:
            status = "alert"
        elif hours >= 0.75 * threshold:
            status = "watch"
        else:
            status = "ok"
        out.append({"name": name, "status": status, "last_litter_ts": last_ts,
                    "hours_since": round(hours, 2), "threshold_h": threshold,
                    "litter_count_today": count})
    return out
