"""Single source of truth for per-cat health status (UI + watcher must agree).

Mirrors health_watch.THRESHOLDS. Status from hours since the most recent
attributed, eliminated visit: ok < 0.75*threshold <= watch < threshold <= alert.
No attributed data => 'ok' with nulls (insufficient data is not an alarm).

Two guards (both mirror health_watch._check_no_go) downgrade a per-cat ALERT to
'watch' + attribution_uncertain so the dashboard doesn't cry wolf:
  - system-wide silence: NO cat used the box (real elimination) in >=8h ->
    camera/vision likely down, not the cats;
  - degraded attribution: box IS used but >=2 recent eliminations couldn't be
    confidently matched to a cat."""
import time
from datetime import datetime

from mw import store

THRESHOLDS = {"Ucok": 8, "Ella": 24, "Garfield": 24}


def cat_status(conn, now_fn=time.time):
    now = now_fn()
    last_by_cat = {name: store.last_attributed_elimination_ts(conn, name)
                   for name in THRESHOLDS}

    # System-wide silence guard (mirrors health_watch._check_no_go): if NO cat
    # has an attributed box use in >=8h, the camera/vision pipeline is likely
    # down (not the cats) — simultaneous multi-cat silence is far more probable
    # a sensor outage than every cat stopping at once. A vision outage does NOT
    # set daemon.stale, so without this the dashboard shows green LIVE + ALERT
    # on every cat (crying wolf). Suppress per-cat alarms when it engages.
    # Uses the SAME "real elimination" set as health_watch (excludes Garfield's
    # short re-entries) so a 30s re-entry can't reset the dashboard's silence
    # clock while Telegram's stays put.
    last_real_any = store.last_real_elimination_ts_any(conn)
    system_silence = last_real_any is not None and \
        (now - datetime.fromisoformat(last_real_any).timestamp()) / 3600.0 >= 8

    # Degraded-attribution hedge: box IS used but recent eliminations couldn't
    # be confidently matched to a cat (>=2 low-conf/unattributed in 24h).
    attribution_unreliable = store.attribution_unreliable(
        conn, store._iso(now - 24 * 3600))

    out = []
    for name, threshold in THRESHOLDS.items():
        last_ts = last_by_cat[name]
        count = store.eliminations_today_for_cat(conn, name, now=now)
        if last_ts is None:
            out.append({"name": name, "status": "ok", "last_litter_ts": None,
                        "hours_since": None, "threshold_h": threshold,
                        "litter_count_today": count, "attribution_uncertain": False})
            continue
        hours = (now - datetime.fromisoformat(last_ts).timestamp()) / 3600.0
        if hours >= threshold:
            status = "alert"
        elif hours >= 0.75 * threshold:
            status = "watch"
        else:
            status = "ok"
        attribution_uncertain = False
        if status == "alert" and (system_silence or attribution_unreliable):
            status = "watch"
            attribution_uncertain = True
        out.append({"name": name, "status": status, "last_litter_ts": last_ts,
                    "hours_since": round(hours, 2), "threshold_h": threshold,
                    "litter_count_today": count, "attribution_uncertain": attribution_uncertain})
    return out
