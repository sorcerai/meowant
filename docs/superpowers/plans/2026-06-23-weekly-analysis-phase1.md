# Weekly Analysis — Phase 1 (deterministic gated table) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the deterministic, LLM-free core of the weekly report — consolidate 7-day per-cat data, statistically gate what counts as "drift" vs noise, render a table with error margins + sample banners + an attribution signal, persist a snapshot, and push it to Telegram on a weekly cadence.

**Architecture:** Three pure functions plus one watcher class in a new `mw/weekly.py`: `collect_facts` (Layer 1, pure SQL) → `assess` (Layer 2, the Statistical Gatekeeper — pure, takes facts + prior findings, returns typed findings) → `facts_only_text` (deterministic markdown). `WeeklyAnalyst` is a daemon-thread watcher (deadman state-file pattern) that runs them weekly, persists via new `store.weekly_reports` helpers, and notifies. **No `claude -p` in Phase 1** — the LLM layer (Phase 2/3) is deliberately absent.

**Tech Stack:** Python 3, stdlib only (`sqlite3`, `json`, `math`, `datetime`, `threading`, `time`), pytest. No new dependencies.

## Global Constraints

- New tables go in `store.SCHEMA` (the `IF NOT EXISTS` block), **never** in `_MIGRATIONS` — copied verbatim from the existing store convention.
- All DB writes/reads in `store.py` go through `with _lock:` (global `threading.Lock`).
- Timestamps are stored as ISO strings; epoch→ISO uses `store._iso(ts)` (naive **local** time, so date prefixes match the owner's day).
- Mixed timestamp formats exist in live rows: most are naive-local, a few legacy rows carry a `+00:00` UTC offset. **Always compute time math with SQLite `strftime('%s', ts)`** (parses both correctly) — never string-compare or string-`MAX` raw `enter_ts`.
- A watchdog `notify(msg)` returns `bool|None`; treat `is not False` as "delivered" (None-returning stubs count as delivered) — verbatim from the existing watcher convention.
- Daemon restart for deploy uses `launchctl kickstart -k gui/$(id -u)/com.meowant.daemon` — never stop/start. (Operational note; no code depends on it.)
- `config.json` is **gitignored** and holds secrets — never commit it; never print its contents.
- The three cats are `("Ucok", "Ella", "Garfield")`.
- Garfield's real eliminations are weight+duration-filtered: a **real void = `eliminated=1` AND `use_record IS NOT NULL` AND `duration_s > 40`**. This filter applies to Garfield's counts/bands ONLY (it excludes his deliberate <15s timer-reset pokes). Ucok and Ella use plain `eliminated=1`.
- Spec: `docs/superpowers/specs/2026-06-23-weekly-analysis-design.md`. This plan implements its **Phasing step 1** only.

---

### Task 1: `weekly_reports` table + store helpers

**Files:**
- Modify: `mw/store.py` (add table to `SCHEMA`; add 3 functions near the incident helpers, ~line 124)
- Test: `tests/test_store.py` (append; mirror existing `tmp_path`/`:memory:` style)

**Interfaces:**
- Consumes: `store.connect`, `store.init_db`, `store._iso`, `store._lock` (existing).
- Produces:
  - `store.log_weekly_report(conn, period_start, period_end, facts_json, findings_json, narrative_json=None, ts=None) -> int` (returns new row id; `period_start`/`period_end` are ISO strings; `*_json` are strings or None; `ts` is epoch float or None→wall-clock now).
  - `store.latest_weekly_report(conn) -> dict | None` (newest row as a dict, or None).
  - `store.recent_weekly_reports(conn, limit=8) -> list[dict]` (newest-first).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py  (append)
def test_weekly_report_log_latest_recent():
    conn = store.connect(":memory:")
    store.init_db(conn)
    rid = store.log_weekly_report(
        conn, "2026-06-16T00:00:00", "2026-06-23T00:00:00",
        '{"period":{"days":7}}', '[{"cat":"Ucok","severity":"nominal"}]',
        None, ts=1_000_000.0)
    assert isinstance(rid, int)
    latest = store.latest_weekly_report(conn)
    assert latest["period_start"] == "2026-06-16T00:00:00"
    assert latest["facts_json"] == '{"period":{"days":7}}'
    assert latest["narrative_json"] is None
    store.log_weekly_report(conn, "2026-06-09T00:00:00", "2026-06-16T00:00:00",
                            "{}", "[]", None, ts=900_000.0)
    recent = store.recent_weekly_reports(conn, limit=8)
    assert len(recent) == 2
    assert recent[0]["period_end"] == "2026-06-23T00:00:00"   # newest first
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_store.py::test_weekly_report_log_latest_recent -v`
Expected: FAIL — `AttributeError: module 'mw.store' has no attribute 'log_weekly_report'`.

- [ ] **Step 3: Add the table to `SCHEMA`**

In `mw/store.py`, inside the `SCHEMA = """ ... """` string, after the `bowl_events` table block, add:

```sql
CREATE TABLE IF NOT EXISTS weekly_reports(
  id INTEGER PRIMARY KEY, ts TEXT,
  period_start TEXT, period_end TEXT,
  facts_json TEXT, findings_json TEXT, narrative_json TEXT);
```

- [ ] **Step 4: Add the three functions**

In `mw/store.py`, after `incident_rollup` (~line 168), add:

```python
def log_weekly_report(conn, period_start, period_end, facts_json,
                      findings_json, narrative_json=None, ts=None):
    """Persist one weekly snapshot. facts/findings/narrative are JSON strings
    (narrative None in Phase 1). ts is epoch float (None -> wall-clock now)."""
    stamp = _iso(ts) if ts is not None else datetime.now().isoformat(timespec="seconds")
    with _lock:
        cur = conn.execute(
            "INSERT INTO weekly_reports(ts, period_start, period_end, "
            "facts_json, findings_json, narrative_json) VALUES(?,?,?,?,?,?)",
            (stamp, period_start, period_end, facts_json, findings_json, narrative_json))
        conn.commit()
        return cur.lastrowid


def latest_weekly_report(conn):
    """Newest weekly report as a dict, or None."""
    with _lock:
        row = conn.execute(
            "SELECT * FROM weekly_reports ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def recent_weekly_reports(conn, limit=8):
    """Newest-first weekly reports."""
    with _lock:
        rows = conn.execute(
            "SELECT * FROM weekly_reports ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ~/repos/meowant && python -m pytest tests/test_store.py::test_weekly_report_log_latest_recent -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mw/store.py tests/test_store.py
git commit -m "feat(weekly): weekly_reports table + log/latest/recent helpers"
```

---

### Task 2: `collect_facts` — Layer 1 consolidation (pure SQL)

**Files:**
- Create: `mw/weekly.py`
- Test: `tests/test_weekly.py`

**Interfaces:**
- Consumes: `store.connect`, `store.init_db`, `store.seed_cats`, `store.cat_id_by_name` (existing, for test seeding).
- Produces: `weekly.collect_facts(conn, now, *, cats=("Ucok","Ella","Garfield")) -> dict`. `now` is an epoch float. Returns the facts dict with this exact shape (later tasks depend on these keys):

```
{
  "period": {"start": iso, "end": iso, "days": 7},
  "per_cat": {
    "<name>": {
      "voids": int,                                  # real voids this week
      "per_day": float,                              # voids/7.0, rounded 2dp
      "gap_h": {"mean": float|None, "min": float|None, "max": float|None,
                "se": float|None, "n": int},         # n = number of gaps (voids-1)
      "weight": {"mean": float|None, "se": float|None, "n": int},
      "circadian": {"night": int, "morn": int, "aft": int, "eve": int},
      "prev": {"voids": int, "gap_mean_h": float|None, "gap_se": float|None,
               "gap_n": int, "weight_mean": float|None, "weight_se": float|None,
               "weight_n": int},                     # SAME metrics for the prior 7d
    }, ...
  },
  "system": {
    "total_visits": int, "attributed": int, "unattributed": int,
    "attribution_pct": float,                        # attributed/total*100, 0 if total==0
    "prev_attribution_pct": float|None,
    "flicker_fragments": int,                        # unattributed, dur<=10, no weight
  }
}
```

Helper (module-private, also produced for reuse by Task 3 tests): `weekly._stats(values) -> dict` returning `{"mean","min","max","se","n"}` (`se = stdev/sqrt(n)` via `statistics.stdev` when `n>=2` else `0.0`; all-None when `n==0`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_weekly.py
from datetime import datetime
from mw import store, weekly


def _conn():
    conn = store.connect(":memory:")
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Ella", "Garfield"])
    return conn


def _add_void(conn, cat, enter_epoch, dur, weight, *, eliminated=1):
    """Insert one visit row directly (bypasses the live pipeline)."""
    cid = store.cat_id_by_name(conn, cat) if cat else None
    iso = datetime.fromtimestamp(enter_epoch).isoformat(timespec="seconds")
    leave = datetime.fromtimestamp(enter_epoch + dur).isoformat(timespec="seconds")
    with store._lock:
        conn.execute(
            "INSERT INTO visits(enter_ts, leave_ts, duration_s, cat_id, confidence, "
            "eliminated, use_record) VALUES(?,?,?,?,?,?,?)",
            (iso, leave, dur, cid, 1.0 if cid else None, eliminated, weight))
        conn.commit()


def test_collect_facts_counts_and_gaps():
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    h = 3600.0
    # Ucok: 3 voids in the last week, ~4h apart
    _add_void(conn, "Ucok", now - 12 * h, 55, 50)
    _add_void(conn, "Ucok", now - 8 * h, 60, 55)
    _add_void(conn, "Ucok", now - 4 * h, 58, 52)
    facts = weekly.collect_facts(conn, now)
    u = facts["per_cat"]["Ucok"]
    assert u["voids"] == 3
    assert u["gap_h"]["n"] == 2
    assert abs(u["gap_h"]["mean"] - 4.0) < 0.01
    assert facts["period"]["days"] == 7


def test_collect_facts_garfield_pokes_excluded():
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    h = 3600.0
    _add_void(conn, "Garfield", now - 5 * h, 6, 3)     # poke: dur<=40 -> excluded
    _add_void(conn, "Garfield", now - 4 * h, 90, 88)   # real void
    facts = weekly.collect_facts(conn, now)
    assert facts["per_cat"]["Garfield"]["voids"] == 1   # only the real one


def test_collect_facts_attribution_and_flicker():
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    h = 3600.0
    _add_void(conn, "Ucok", now - 4 * h, 55, 50)            # attributed
    _add_void(conn, None, now - 3 * h, 5, None, eliminated=0)  # flicker fragment
    facts = weekly.collect_facts(conn, now)
    s = facts["system"]
    assert s["total_visits"] == 2 and s["attributed"] == 1 and s["unattributed"] == 1
    assert abs(s["attribution_pct"] - 50.0) < 0.01
    assert s["flicker_fragments"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_weekly.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mw.weekly'`.

- [ ] **Step 3: Implement `collect_facts` + `_stats`**

Create `mw/weekly.py`:

```python
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
    _, _, prev_pct = _attribution_pct(conn, prev_start, start)
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
                   "prev_attribution_pct": (prev_pct if (prev_start_has := True) else None),
                   "flicker_fragments": flicker},
    }
```

Note: replace the `prev_attribution_pct` line with a clean version — the walrus above is a deliberate red flag for you to simplify: compute `_, prev_total, prev_pct = _attribution_pct(...)` and set `"prev_attribution_pct": prev_pct if prev_total else None`.

- [ ] **Step 4: Fix the `prev_attribution_pct` line**

Change the `_attribution_pct(conn, prev_start, start)` call to capture the total, and set the field cleanly:

```python
    _, prev_total, prev_pct = _attribution_pct(conn, prev_start, start)
    ...
                   "prev_attribution_pct": prev_pct if prev_total else None,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_weekly.py -v`
Expected: PASS (all 3 tests).

- [ ] **Step 6: Commit**

```bash
git add mw/weekly.py tests/test_weekly.py
git commit -m "feat(weekly): collect_facts — Layer 1 per-cat 7d consolidation"
```

---

### Task 3: `assess` — Layer 2 Statistical Gatekeeper (pure)

**Files:**
- Modify: `mw/weekly.py`
- Test: `tests/test_weekly.py` (append)

**Interfaces:**
- Consumes: the `facts` dict from `collect_facts` (Task 2).
- Produces: `weekly.assess(facts, prev_findings=(), *, min_void_n=5, sigma_k=2.0, attribution_drop_pp=15.0) -> list[dict]`. Each finding:

```
{"cat": str|None, "metric": str, "severity": str, "value": float|None,
 "margin": float|None, "delta": float|None, "evidence": str}
```
`severity ∈ {"nominal","watch","drift","insufficient_data"}`. `metric ∈ {"frequency","weight","attribution"}`. `cat` is None for system findings (`attribution`).

Gatekeeper rules (exact):
1. **Sample adequacy:** if a cat's `voids < min_void_n` → one finding `{metric:"frequency", severity:"insufficient_data", value:voids}`, and **no** drift/weight findings for that cat.
2. **Frequency drift** (cat has enough voids): compare this week's `gap_h.mean` vs `prev.gap_mean_h`. If no prev (`prev.gap_n < 2` or means None) → `nominal` (establishing baseline). Else `delta = gap_mean - prev_gap_mean`; `combined_se = sqrt(se^2 + prev_se^2)`. If `abs(delta) <= sigma_k * combined_se` → `nominal`. Else **significant**: `watch`, unless a matching `prev_findings` entry (same cat+metric, severity in {watch,drift}, same `delta` sign) exists → escalate to `drift` (persistence).
3. **Weight drift:** identical logic on `weight.mean` vs `prev.weight_mean` (needs `weight.n>=2` and `prev.weight_n>=2`, else `nominal`).
4. **Attribution (system):** if `prev_attribution_pct` is not None and `(prev_attribution_pct - attribution_pct) >= attribution_drop_pp` → `{cat:None, metric:"attribution", severity:"watch", value:attribution_pct, delta: attribution_pct - prev_attribution_pct}` (a rising unattributed rate may mean a cat is going unidentified). Else `nominal`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_weekly.py  (append)
def _facts(per_cat, system=None):
    base_sys = {"total_visits": 0, "attributed": 0, "unattributed": 0,
                "attribution_pct": 100.0, "prev_attribution_pct": None,
                "flicker_fragments": 0}
    if system:
        base_sys.update(system)
    return {"period": {"start": "x", "end": "y", "days": 7},
            "per_cat": per_cat, "system": base_sys}


def _cat(voids, gap_mean, gap_se, gap_n, prev_gap_mean=None, prev_gap_se=0.0,
         prev_gap_n=0, weight_mean=None, weight_se=0.0, weight_n=0,
         prev_weight_mean=None, prev_weight_se=0.0, prev_weight_n=0):
    return {"voids": voids, "per_day": round(voids / 7.0, 2),
            "gap_h": {"mean": gap_mean, "min": None, "max": None, "se": gap_se, "n": gap_n},
            "weight": {"mean": weight_mean, "se": weight_se, "n": weight_n},
            "circadian": {"night": 0, "morn": 0, "aft": 0, "eve": 0},
            "prev": {"voids": 0, "gap_mean_h": prev_gap_mean, "gap_se": prev_gap_se,
                     "gap_n": prev_gap_n, "weight_mean": prev_weight_mean,
                     "weight_se": prev_weight_se, "weight_n": prev_weight_n}}


def test_assess_insufficient_data():
    f = _facts({"Ucok": _cat(voids=3, gap_mean=3.0, gap_se=0.1, gap_n=2)})
    out = [x for x in weekly.assess(f) if x["cat"] == "Ucok"]
    assert out == [{"cat": "Ucok", "metric": "frequency",
                    "severity": "insufficient_data", "value": 3,
                    "margin": None, "delta": None,
                    "evidence": "N=3 voids this week — too few to judge drift"}]


def test_assess_nominal_within_noise():
    f = _facts({"Ucok": _cat(voids=20, gap_mean=3.2, gap_se=0.2, gap_n=19,
                             prev_gap_mean=3.0, prev_gap_se=0.2, prev_gap_n=18)})
    freq = [x for x in weekly.assess(f) if x["cat"] == "Ucok" and x["metric"] == "frequency"][0]
    assert freq["severity"] == "nominal"   # 0.2 delta < 2*sqrt(.2^2+.2^2)=0.566


def test_assess_watch_on_significant_delta():
    f = _facts({"Ucok": _cat(voids=20, gap_mean=6.0, gap_se=0.2, gap_n=19,
                             prev_gap_mean=3.0, prev_gap_se=0.2, prev_gap_n=18)})
    freq = [x for x in weekly.assess(f) if x["cat"] == "Ucok" and x["metric"] == "frequency"][0]
    assert freq["severity"] == "watch" and freq["delta"] == 3.0


def test_assess_drift_on_persistence():
    f = _facts({"Ucok": _cat(voids=20, gap_mean=6.0, gap_se=0.2, gap_n=19,
                             prev_gap_mean=3.0, prev_gap_se=0.2, prev_gap_n=18)})
    prev = [{"cat": "Ucok", "metric": "frequency", "severity": "watch", "delta": 2.5}]
    freq = [x for x in weekly.assess(f, prev_findings=prev)
            if x["cat"] == "Ucok" and x["metric"] == "frequency"][0]
    assert freq["severity"] == "drift"   # same cat+metric+sign, was watch -> escalates


def test_assess_attribution_drop():
    f = _facts({}, system={"attribution_pct": 40.0, "prev_attribution_pct": 70.0})
    attr = [x for x in weekly.assess(f) if x["metric"] == "attribution"][0]
    assert attr["severity"] == "watch" and attr["delta"] == -30.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/repos/meowant && python -m pytest tests/test_weekly.py -k assess -v`
Expected: FAIL — `AttributeError: module 'mw.weekly' has no attribute 'assess'`.

- [ ] **Step 3: Implement `assess`**

Append to `mw/weekly.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_weekly.py -k assess -v`
Expected: PASS (all 5 assess tests).

- [ ] **Step 5: Commit**

```bash
git add mw/weekly.py tests/test_weekly.py
git commit -m "feat(weekly): assess — statistical gatekeeper (adequacy/significance/persistence)"
```

---

### Task 4: `facts_only_text` — deterministic table renderer

**Files:**
- Modify: `mw/weekly.py`
- Test: `tests/test_weekly.py` (append)

**Interfaces:**
- Consumes: `facts` (Task 2), `findings` (Task 3).
- Produces: `weekly.facts_only_text(facts, findings) -> str` — a Telegram-friendly markdown report. Per cat: severity emoji + name, voids/day, gap mean ± margin (or "insufficient data, N=x" banner), weight. Then a system line with attribution (flagged if its finding is `watch`) and flicker count.

Severity → emoji map (module constant `SEV_EMOJI`): `nominal:"✅"`, `watch:"⚠️"`, `drift:"🚨"`, `insufficient_data:"❓"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_weekly.py  (append)
def test_facts_only_text_renders_cats_and_system():
    f = _facts(
        {"Ucok": _cat(voids=20, gap_mean=6.0, gap_se=0.2, gap_n=19,
                      prev_gap_mean=3.0, prev_gap_se=0.2, prev_gap_n=18),
         "Ella": _cat(voids=3, gap_mean=10.0, gap_se=0.5, gap_n=2)},
        system={"total_visits": 40, "attributed": 30, "unattributed": 10,
                "attribution_pct": 75.0, "prev_attribution_pct": 78.0,
                "flicker_fragments": 8})
    findings = weekly.assess(f)
    txt = weekly.facts_only_text(f, findings)
    assert "Ucok" in txt and "Ella" in txt
    assert "🚨" in txt or "⚠️" in txt        # Ucok frequency watch
    assert "❓" in txt                          # Ella insufficient_data
    assert "insufficient" in txt.lower() and "N=3" in txt
    assert "75.0%" in txt                       # attribution line
    assert "8" in txt                           # flicker count


def test_facts_only_text_attribution_flagged_on_drop():
    f = _facts({}, system={"total_visits": 10, "attributed": 4, "unattributed": 6,
                           "attribution_pct": 40.0, "prev_attribution_pct": 70.0,
                           "flicker_fragments": 2})
    txt = weekly.facts_only_text(f, weekly.assess(f))
    assert "⚠️" in txt and "unidentified" in txt.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/repos/meowant && python -m pytest tests/test_weekly.py -k facts_only_text -v`
Expected: FAIL — `AttributeError: ... has no attribute 'facts_only_text'`.

- [ ] **Step 3: Implement `facts_only_text`**

Append to `mw/weekly.py`:

```python
SEV_EMOJI = {"nominal": "✅", "watch": "⚠️", "drift": "🚨", "insufficient_data": "❓"}


def _cat_findings(findings, cat):
    return {f["metric"]: f for f in findings if f["cat"] == cat}


def facts_only_text(facts, findings):
    p = facts["period"]
    lines = [f"📊 Weekly cat report ({p['start'][:10]} → {p['end'][:10]})", ""]
    for cat, c in facts["per_cat"].items():
        fm = _cat_findings(findings, cat)
        freq = fm.get("frequency", {"severity": "nominal"})
        emoji = SEV_EMOJI.get(freq["severity"], "•")
        if freq["severity"] == "insufficient_data":
            lines.append(f"{emoji} {cat}: insufficient data (N={c['voids']} voids this week)")
            continue
        g = c["gap_h"]
        margin = freq.get("margin")
        gap_txt = (f"{g['mean']}h between voids" if g["mean"] is not None else "—")
        if margin:
            gap_txt += f" (±{margin})"
        line = f"{emoji} {cat}: {c['voids']} voids ({c['per_day']}/day), {gap_txt}"
        w = fm.get("weight")
        if w and w["severity"] in ("watch", "drift"):
            line += f" — {SEV_EMOJI[w['severity']]} weight {w['evidence']}"
        if freq["severity"] in ("watch", "drift"):
            line += f"\n    {SEV_EMOJI[freq['severity']]} {freq['evidence']}"
        lines.append(line)
    s = facts["system"]
    attr_f = next((f for f in findings if f["metric"] == "attribution"), None)
    attr_emoji = SEV_EMOJI.get(attr_f["severity"], "✅") if attr_f else "✅"
    attr_line = f"{attr_emoji} Attribution: {s['attribution_pct']}% ({s['unattributed']} unattributed), {s['flicker_fragments']} flicker fragments"
    if attr_f and attr_f["severity"] == "watch":
        attr_line += f" — {attr_f['evidence']}"
    lines += ["", attr_line]
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_weekly.py -k facts_only_text -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mw/weekly.py tests/test_weekly.py
git commit -m "feat(weekly): facts_only_text — deterministic table with margins + banners"
```

---

### Task 5: `WeeklyAnalyst` — weekly-cadence watcher

**Files:**
- Modify: `mw/weekly.py`
- Test: `tests/test_weekly.py` (append)

**Interfaces:**
- Consumes: `collect_facts`, `assess`, `facts_only_text` (Tasks 2-4); `store.log_weekly_report`, `store.latest_weekly_report` (Task 1).
- Produces: `weekly.WeeklyAnalyst(conn, notify, now_fn=time.time, *, state_path="weekly_state.json", interval_days=7, min_void_n=5, cats=CATS)` with:
  - `due(now) -> bool`
  - `run_once(now) -> bool` (True if it produced a report this call)
  - `run()` (infinite loop; never raises out)

Behavior: `run_once` returns False when not `due`. When due: collect → load prior findings from `latest_weekly_report().findings_json` → assess → render → **persist the report and stamp last_run (cadence advances regardless of delivery)** → `notify(text)` best-effort. Rationale for diverging from the acute-alert latch: the weekly digest is **pull-recoverable** via `/weekly` (Task 6), so a transient Telegram failure must NOT cause a re-run that double-persists or skips the week. State file uses the deadman `_load_state`/`_save_state` pattern (corruption-tolerant).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_weekly.py  (append)
import os


def test_weekly_analyst_not_due_is_noop(tmp_path):
    conn = _conn()
    sp = str(tmp_path / "wk.json")
    sent = []
    a = weekly.WeeklyAnalyst(conn, lambda m: sent.append(m) or True,
                             now_fn=lambda: 1000.0, state_path=sp)
    a._save_state({"last_run": 1000.0})           # just ran
    assert a.run_once(1000.0 + 3600) is False      # 1h later -> not due
    assert sent == [] and store.latest_weekly_report(conn) is None


def test_weekly_analyst_due_persists_and_notifies(tmp_path):
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    for k in range(6):                              # 6 Ucok voids this week
        _add_void(conn, "Ucok", now - (20 - 2 * k) * 3600, 55, 50)
    sp = str(tmp_path / "wk.json")
    sent = []
    a = weekly.WeeklyAnalyst(conn, lambda m: sent.append(m) or True,
                             now_fn=lambda: now, state_path=sp)
    assert a.run_once(now) is True                  # no prior state -> due
    assert len(sent) == 1 and "Ucok" in sent[0]
    rep = store.latest_weekly_report(conn)
    assert rep is not None and rep["narrative_json"] is None
    assert a._load_state().get("last_run") == now   # stamped


def test_weekly_analyst_stamps_even_if_notify_fails(tmp_path):
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    sp = str(tmp_path / "wk.json")
    a = weekly.WeeklyAnalyst(conn, lambda m: False,    # delivery fails
                             now_fn=lambda: now, state_path=sp)
    assert a.run_once(now) is True
    assert a._load_state().get("last_run") == now      # week still recorded (pull-recoverable)
    assert store.latest_weekly_report(conn) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/repos/meowant && python -m pytest tests/test_weekly.py -k analyst -v`
Expected: FAIL — `AttributeError: ... has no attribute 'WeeklyAnalyst'`.

- [ ] **Step 3: Implement `WeeklyAnalyst`**

Append to `mw/weekly.py`:

```python
class WeeklyAnalyst:
    def __init__(self, conn, notify, now_fn=time.time, *,
                 state_path="weekly_state.json", interval_days=7,
                 min_void_n=5, cats=CATS):
        self.conn = conn
        self.notify = notify
        self.now = now_fn
        self.state_path = state_path
        self.interval_s = interval_days * 24 * 3600
        self.min_void_n = min_void_n
        self.cats = cats

    def _load_state(self):
        try:
            with open(self.state_path) as f:
                s = json.load(f)
            return s if isinstance(s, dict) else {}
        except Exception:
            return {}

    def _save_state(self, state):
        try:
            with open(self.state_path, "w") as f:
                json.dump(state, f)
        except Exception as e:
            print(f"[weekly] state save failed: {e}", file=sys.stderr)

    def due(self, now):
        last = self._load_state().get("last_run")
        return last is None or (now - last) >= self.interval_s

    def run_once(self, now):
        if not self.due(now):
            return False
        facts = collect_facts(self.conn, now, cats=self.cats)
        prev = store.latest_weekly_report(self.conn)
        prev_findings = json.loads(prev["findings_json"]) if (prev and prev["findings_json"]) else ()
        findings = assess(facts, prev_findings, min_void_n=self.min_void_n)
        text = facts_only_text(facts, findings)
        store.log_weekly_report(
            self.conn, facts["period"]["start"], facts["period"]["end"],
            json.dumps(facts), json.dumps(findings), None, ts=now)
        # Cadence advances regardless of delivery: the report is in the DB and
        # /weekly can re-fetch it, so a transient notify failure must not re-run
        # the week (which would double-persist) nor skip it.
        state = self._load_state()
        state["last_run"] = now
        self._save_state(state)
        if self.notify(text) is False:
            print("[weekly] notify failed; report persisted, use /weekly", file=sys.stderr)
        return True

    def run(self):
        while True:
            try:
                self.run_once(self.now())
            except Exception as e:
                print(f"[weekly] error: {e}", file=sys.stderr)
            time.sleep(6 * 3600)     # coarse poll; due() enforces the weekly cadence
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_weekly.py -v`
Expected: PASS (entire weekly suite).

- [ ] **Step 5: Commit**

```bash
git add mw/weekly.py tests/test_weekly.py
git commit -m "feat(weekly): WeeklyAnalyst — weekly cadence, persist+notify, pull-recoverable"
```

---

### Task 6: Wire into the daemon + `/weekly` command + report glue

**Files:**
- Modify: `meowantd.py` (add a gated `WeeklyAnalyst` thread in the watcher block, ~after the canary block near line 130)
- Modify: `mw/report.py` (add `weekly_status_text(conn)` for the `/weekly` reply)
- Modify: `mw/telegram_bot.py` — no code change if handlers are passed in from `meowantd.py`; verify by reading how the handler dict is built. If `meowantd.py` builds the `handlers` dict, register `/weekly` there.
- Test: `tests/test_report.py` (append — `weekly_status_text`); `tests/test_meowantd_wiring.py` if it exists, else a small wiring test in `tests/test_weekly.py`.

**Interfaces:**
- Consumes: `weekly.WeeklyAnalyst`, `store.latest_weekly_report`, `store.recent_weekly_reports`.
- Produces: `report.weekly_status_text(conn) -> str` (latest stored report text, or a "no report yet" message). Config keys (all under `weekly.`): `enabled` (default False), `interval_days` (7), `min_void_n` (5), `state_path` ("weekly_state.json").

- [ ] **Step 1: Write the failing test for the report helper**

```python
# tests/test_report.py  (append)
def test_weekly_status_text_no_report(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    assert "no weekly" in report.weekly_status_text(conn).lower()


def test_weekly_status_text_returns_latest(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.log_weekly_report(conn, "2026-06-16T00:00:00", "2026-06-23T00:00:00",
                            "{}", "[]", None, ts=1_000_000.0)
    # Phase 1 stores no narrative; the rendered table is rebuilt on demand from
    # facts — but for the no-narrative case we surface a pointer to the period.
    txt = report.weekly_status_text(conn)
    assert "2026-06-23" in txt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/repos/meowant && python -m pytest tests/test_report.py -k weekly_status -v`
Expected: FAIL — `AttributeError: module 'mw.report' has no attribute 'weekly_status_text'`.

- [ ] **Step 3: Implement `weekly_status_text`**

The stored report holds `facts_json` + `findings_json`; rebuild the table on demand (DRY — reuse `weekly.facts_only_text`). Append to `mw/report.py`:

```python
def weekly_status_text(conn):
    """Reply for /weekly: rebuild the latest stored weekly table on demand."""
    rep = store.latest_weekly_report(conn)
    if not rep:
        return "📊 No weekly report yet (first one lands after a full week)."
    import json
    from mw import weekly
    try:
        facts = json.loads(rep["facts_json"])
        findings = json.loads(rep["findings_json"]) if rep["findings_json"] else []
        return weekly.facts_only_text(facts, findings)
    except Exception:
        return f"📊 Weekly report for {rep['period_start'][:10]} → {rep['period_end'][:10]} (stored)."
```

Note: the second test's `facts_json="{}"` will hit the `except` path (missing `period`/`per_cat` keys) and return the pointer line containing `2026-06-23` — that satisfies the test and exercises the fallback.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_report.py -k weekly_status -v`
Expected: PASS.

- [ ] **Step 5: Wire the daemon thread + `/weekly` handler**

In `meowantd.py`, locate the canary gated block (search `canary.enabled`, ~line 120). After that block's `print(...)`, add:

```python
        # Weekly per-cat consolidation + statistical gatekeeper (chronic-drift
        # report). Deterministic (no LLM in Phase 1). Pull-recoverable via /weekly.
        if config.get(cfg, "weekly.enabled", False):
            from mw.weekly import WeeklyAnalyst
            analyst = WeeklyAnalyst(
                conn, make_notify(lambda k: config.get(cfg, k)),
                state_path=config.get(cfg, "weekly.state_path", "weekly_state.json"),
                interval_days=config.get(cfg, "weekly.interval_days", 7),
                min_void_n=config.get(cfg, "weekly.min_void_n", 5))
            threading.Thread(target=analyst.run, daemon=True).start()
            print("weekly-analyst: per-cat 7d consolidation + gatekeeper")
```

Then find where the Telegram `handlers` dict is built (search `handlers = {` or `"/bowl"` in `meowantd.py`) and add the `/weekly` entry alongside the others:

```python
            "/weekly": (lambda: report.weekly_status_text(conn)),
```

(If the bot is constructed with a different handler-registration mechanism, match it — the existing `/bowl` and `/feedstatus` commands are the template. `_dispatch` is already arg-aware, so a zero-arg lambda is correct here.)

- [ ] **Step 6: Verify the full suite passes**

Run: `cd ~/repos/meowant && python -m pytest -q`
Expected: PASS (all prior tests + the new weekly suite). If a `meowantd` import-time wiring test exists, it should still pass; if not, confirm `python -c "import meowantd"` exits 0.

Run: `cd ~/repos/meowant && python -c "import meowantd"`
Expected: exit 0, no traceback.

- [ ] **Step 7: Commit**

```bash
git add meowantd.py mw/report.py tests/test_report.py
git commit -m "feat(weekly): wire WeeklyAnalyst thread + /weekly command (gated, default off)"
```

---

## Self-Review

**1. Spec coverage (Phasing step 1 = deterministic gated table):**
- Layer 1 consolidate → Task 2 `collect_facts` ✅
- Layer 2 Statistical Gatekeeper (sample adequacy, error margins, significance, persistence) → Task 3 `assess` ✅
- Attribution as a health signal → Task 3 attribution finding + Task 4 render ✅ (implemented at **system** level — per-cat attribution of *missed* visits is impossible since unattributed rows have no cat; this is the honest form, noted in the plan's Global Constraints discussion and bead `meowant-oac`).
- Deterministic table with ± margins + sample banner → Task 4 ✅
- Persist snapshot (durable longitudinal record) → Task 1 + Task 5 ✅
- Weekly cadence watcher, restart-safe, never dies → Task 5 ✅
- Telegram delivery + `/weekly` pull → Task 6 ✅
- **Deliberately deferred to Phase 2/3 (out of scope here):** `claude -p` narrate, `validate_llm_output`, shadow mode, model-drift regression suite. Spec's "Phasing" explicitly sequences these later. ✅ (no gap — intentional)

**2. Placeholder scan:** No "TBD"/"handle edge cases"/bare "write tests". The one intentional red flag (walrus in Task 2 Step 3) is immediately corrected in Step 4 with exact code. ✅

**3. Type consistency:** `facts` keys produced in Task 2 are consumed with the same names in Tasks 3/4/5 (`per_cat`, `gap_h.{mean,se,n}`, `prev.{gap_mean_h,gap_se,gap_n,weight_mean,weight_se,weight_n}`, `system.{attribution_pct,prev_attribution_pct,unattributed,flicker_fragments}`). Finding keys (`cat,metric,severity,value,margin,delta,evidence`) are identical across `assess`, `facts_only_text`, and the persistence reader. `store.log_weekly_report` signature matches every call site (Tasks 1, 5). `SEV_EMOJI` severities match the strings `assess` emits. ✅

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-23-weekly-analysis-phase1.md`. Two execution options:

1. **Subagent-Driven (recommended)** — fresh implementer subagent per task, two-stage review (spec + quality) between tasks, then a whole-branch review.
2. **Inline Execution** — execute tasks in this session with checkpoints.

Which approach?
