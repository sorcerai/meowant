"""Weekly consolidation + statistical gatekeeping for the per-cat health report.

Phase 1 is fully deterministic — NO LLM. Three pure functions (collect_facts ->
assess -> facts_only_text) plus a WeeklyAnalyst watcher. The gatekeeper (assess)
refuses to call a small-sample wobble 'drift': it needs sample adequacy, a
significant week-over-week delta, AND persistence across weeks.
"""
import json
import math
import statistics
import sys
import time
from datetime import datetime
from mw import store

CATS = ("Ucok", "Ella", "Garfield")
WEEK_S = 7 * 24 * 3600

# Garfield's deliberate timer-reset pokes are short and weightless; a real void
# clears this duration floor. Applied to Garfield only.
POKE_DUR_FLOOR_S = 40


def _stats(values):
    """Summary stats for a list of floats. se = stdev/sqrt(n) (0.0 when n<2)."""
    n = len(values)
    if n == 0:
        return {"mean": None, "min": None, "max": None, "se": None, "n": 0}
    mean = sum(values) / n
    se = (statistics.stdev(values) / math.sqrt(n)) if n >= 2 else 0.0
    return {"mean": round(mean, 2), "min": round(min(values), 2),
            "max": round(max(values), 2), "se": round(se, 3), "n": n}


def _void_rows(conn, cat, start_epoch, end_epoch):
    """(enter_epoch, duration_s, weight) real voids for `cat` in [start,end).
    Garfield filtered to weight-present, duration>floor (drops timer-reset pokes)."""
    extra = ""
    if cat == "Garfield":
        extra = f" AND use_record IS NOT NULL AND duration_s > {POKE_DUR_FLOOR_S}"
    sql = (
        "SELECT CAST(strftime('%s', enter_ts) AS INT) AS s, duration_s, use_record "
        "FROM visits WHERE cat_id=(SELECT id FROM cats WHERE name=?) "
        "AND eliminated=1 AND use_record IS NOT NULL "
        "AND CAST(strftime('%s', enter_ts) AS INT) >= ? "
        "AND CAST(strftime('%s', enter_ts) AS INT) < ?" + extra)
    with store._lock:
        rows = conn.execute(sql, (cat, int(start_epoch), int(end_epoch))).fetchall()
    return [(r["s"], r["duration_s"], r["use_record"]) for r in rows]


def _gaps_h(sorted_epochs):
    """Hours between consecutive eliminations."""
    return [(sorted_epochs[i] - sorted_epochs[i - 1]) / 3600.0
            for i in range(1, len(sorted_epochs))]


def _circadian(epochs):
    buckets = {"night": 0, "morn": 0, "aft": 0, "eve": 0}
    for e in epochs:
        h = datetime.fromtimestamp(e).hour
        if h < 6:
            buckets["night"] += 1
        elif h < 12:
            buckets["morn"] += 1
        elif h < 18:
            buckets["aft"] += 1
        else:
            buckets["eve"] += 1
    return buckets


def _cat_window(conn, cat, start_epoch, end_epoch):
    rows = _void_rows(conn, cat, start_epoch, end_epoch)
    epochs = sorted(r[0] for r in rows)
    weights = [r[2] for r in rows]
    gaps = _gaps_h(epochs)
    return {"voids": len(rows), "epochs": epochs, "weights": weights, "gaps": gaps}


def _attribution_pct(conn, start_epoch, end_epoch):
    sql = ("SELECT "
           "SUM(CASE WHEN cat_id IS NOT NULL THEN 1 ELSE 0 END) AS attr, "
           "COUNT(*) AS total "
           "FROM visits WHERE CAST(strftime('%s', enter_ts) AS INT) >= ? "
           "AND CAST(strftime('%s', enter_ts) AS INT) < ?")
    with store._lock:
        r = conn.execute(sql, (int(start_epoch), int(end_epoch))).fetchone()
    attr = r["attr"] or 0
    total = r["total"] or 0
    pct = round(attr / total * 100, 2) if total else 0.0
    return attr, total, pct


def collect_facts(conn, now, *, cats=CATS):
    end = now
    start = now - WEEK_S
    prev_start = start - WEEK_S
    per_cat = {}
    for cat in cats:
        cur = _cat_window(conn, cat, start, end)
        prev = _cat_window(conn, cat, prev_start, start)
        gs, ws = _stats(cur["gaps"]), _stats(cur["weights"])
        pgs, pws = _stats(prev["gaps"]), _stats(prev["weights"])
        per_cat[cat] = {
            "voids": cur["voids"],
            "per_day": round(cur["voids"] / 7.0, 2),
            "gap_h": gs,
            "weight": {"mean": ws["mean"], "se": ws["se"], "n": ws["n"]},
            "circadian": _circadian(cur["epochs"]),
            "prev": {"voids": prev["voids"],
                     "gap_mean_h": pgs["mean"], "gap_se": pgs["se"], "gap_n": pgs["n"],
                     "weight_mean": pws["mean"], "weight_se": pws["se"], "weight_n": pws["n"]},
        }
    attr, total, pct = _attribution_pct(conn, start, end)
    _, prev_total, prev_pct = _attribution_pct(conn, prev_start, start)
    with store._lock:
        flicker = conn.execute(
            "SELECT COUNT(*) AS n FROM visits WHERE cat_id IS NULL "
            "AND duration_s <= 10 AND use_record IS NULL "
            "AND CAST(strftime('%s', enter_ts) AS INT) >= ? "
            "AND CAST(strftime('%s', enter_ts) AS INT) < ?",
            (int(start), int(end))).fetchone()["n"]
    return {
        "period": {"start": store._iso(start), "end": store._iso(end), "days": 7},
        "per_cat": per_cat,
        "system": {"total_visits": total, "attributed": attr,
                   "unattributed": total - attr, "attribution_pct": pct,
                   "prev_attribution_pct": prev_pct if prev_total else None,
                   "flicker_fragments": flicker},
    }
