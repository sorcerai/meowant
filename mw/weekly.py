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
        extra = f" AND duration_s > {POKE_DUR_FLOOR_S}"
    sql = (
        "SELECT CAST(strftime('%s', enter_ts) AS INT) AS s, duration_s, use_record "
        "FROM visits WHERE cat_id=(SELECT id FROM cats WHERE name=?) "
        "AND eliminated=1 AND use_record IS NOT NULL "
        "AND strftime('%s', enter_ts) >= strftime('%s', ?) "
        "AND strftime('%s', enter_ts) < strftime('%s', ?)" + extra)
    with store._lock:
        rows = conn.execute(
            sql, (cat, store._iso(start_epoch), store._iso(end_epoch))).fetchall()
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
           "FROM visits WHERE strftime('%s', enter_ts) >= strftime('%s', ?) "
           "AND strftime('%s', enter_ts) < strftime('%s', ?)")
    with store._lock:
        r = conn.execute(
            sql, (store._iso(start_epoch), store._iso(end_epoch))).fetchone()
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
            "AND strftime('%s', enter_ts) >= strftime('%s', ?) "
            "AND strftime('%s', enter_ts) < strftime('%s', ?)",
            (store._iso(start), store._iso(end))).fetchone()["n"]
    return {
        "period": {"start": store._iso(start), "end": store._iso(end), "days": 7},
        "per_cat": per_cat,
        "system": {"total_visits": total, "attributed": attr,
                   "unattributed": total - attr, "attribution_pct": pct,
                   "prev_attribution_pct": prev_pct if prev_total else None,
                   "flicker_fragments": flicker},
    }


def _significant(delta, se_a, se_b, sigma_k):
    combined = math.sqrt((se_a or 0.0) ** 2 + (se_b or 0.0) ** 2)
    margin = round(sigma_k * combined, 3)
    return abs(delta) > margin, margin


def _persists(prev_findings, cat, metric, delta):
    for p in prev_findings or ():
        if (p.get("cat") == cat and p.get("metric") == metric
                and p.get("severity") in ("watch", "drift")
                and (p.get("delta") or 0.0) * delta > 0):   # same direction
            return True
    return False


def _drift_finding(cat, metric, cur_mean, cur_se, cur_n, prev_mean, prev_se,
                   prev_n, prev_findings, sigma_k, unit):
    if cur_mean is None or prev_mean is None or cur_n < 2 or prev_n < 2:
        return {"cat": cat, "metric": metric, "severity": "nominal",
                "value": cur_mean, "margin": None, "delta": None,
                "evidence": f"{metric}: establishing baseline"}
    delta = round(cur_mean - prev_mean, 3)
    sig, margin = _significant(delta, cur_se, prev_se, sigma_k)
    if not sig:
        return {"cat": cat, "metric": metric, "severity": "nominal",
                "value": cur_mean, "margin": margin, "delta": delta,
                "evidence": f"{metric} Δ {delta:+}{unit} within noise (±{margin})"}
    severity = "drift" if _persists(prev_findings, cat, metric, delta) else "watch"
    arrow = "up" if delta > 0 else "down"
    return {"cat": cat, "metric": metric, "severity": severity,
            "value": cur_mean, "margin": margin, "delta": delta,
            "evidence": f"{metric} {arrow} {delta:+}{unit} vs last week (>±{margin})"}


def assess(facts, prev_findings=(), *, min_void_n=5, sigma_k=2.0,
           attribution_drop_pp=15.0):
    findings = []
    for cat, c in facts["per_cat"].items():
        if c["voids"] < min_void_n:
            findings.append({"cat": cat, "metric": "frequency",
                             "severity": "insufficient_data", "value": c["voids"],
                             "margin": None, "delta": None,
                             "evidence": f"N={c['voids']} voids this week — too few to judge drift"})
            continue
        g, p = c["gap_h"], c["prev"]
        findings.append(_drift_finding(
            cat, "frequency", g["mean"], g["se"], g["n"],
            p["gap_mean_h"], p["gap_se"], p["gap_n"], prev_findings, sigma_k, "h"))
        w = c["weight"]
        findings.append(_drift_finding(
            cat, "weight", w["mean"], w["se"], w["n"],
            p["weight_mean"], p["weight_se"], p["weight_n"], prev_findings, sigma_k, "g"))
    s = facts["system"]
    prev_pct = s.get("prev_attribution_pct")
    if prev_pct is not None and (prev_pct - s["attribution_pct"]) >= attribution_drop_pp:
        delta = round(s["attribution_pct"] - prev_pct, 2)
        findings.append({"cat": None, "metric": "attribution", "severity": "watch",
                         "value": s["attribution_pct"], "margin": None, "delta": delta,
                         "evidence": f"attribution fell {delta}pp — a cat may be going "
                                     f"unidentified (sick cats move differently)"})
    else:
        findings.append({"cat": None, "metric": "attribution", "severity": "nominal",
                         "value": s["attribution_pct"], "margin": None, "delta": None,
                         "evidence": f"attribution {s['attribution_pct']}%"})
    return findings
