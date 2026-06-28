"""SQLite persistence for events and visits."""
import json
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime, date

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cats(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, notes TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS visits(
  id INTEGER PRIMARY KEY,
  enter_ts TEXT, leave_ts TEXT, duration_s INTEGER,
  cat_id INTEGER REFERENCES cats(id), confidence REAL,
  eliminated INTEGER DEFAULT 0, use_record INTEGER,
  contents_load_min INTEGER, contents_load_max INTEGER,
  frame_path TEXT);
CREATE TABLE IF NOT EXISTS captures(
  id INTEGER PRIMARY KEY, ts TEXT, visit_id INTEGER REFERENCES visits(id),
  camera TEXT, path TEXT, label INTEGER REFERENCES cats(id),
  pred INTEGER, pred_conf REAL, is_ir INTEGER,
  label_source TEXT);   -- 'human' | 'auto' | 'corrected' (NULL = unlabeled)
CREATE TABLE IF NOT EXISTS incidents(
  id INTEGER PRIMARY KEY, ts TEXT, kind TEXT,
  signal TEXT,            -- JSON: detection details
  action_taken TEXT,      -- what the playbook attempted
  outcome TEXT,           -- 'recovered' | 'escalated' | 'suppressed' | 'failed'
  notes TEXT);
CREATE TABLE IF NOT EXISTS feed_events(
  id INTEGER PRIMARY KEY, ts TEXT, portions INTEGER,
  source TEXT);          -- 'scheduled' | 'manual'
CREATE TABLE IF NOT EXISTS bowl_events(
  id INTEGER PRIMARY KEY, ts TEXT, state TEXT,
  source TEXT,            -- 'vision' | 'auto_feed'
  secs_since_feed INTEGER);
CREATE TABLE IF NOT EXISTS bowl_sessions(
  id INTEGER PRIMARY KEY, ts TEXT, location TEXT,
  cat TEXT, duration_s INTEGER);
CREATE TABLE IF NOT EXISTS weekly_reports(
  id INTEGER PRIMARY KEY, ts TEXT,
  period_start TEXT, period_end TEXT,
  facts_json TEXT, findings_json TEXT, narrative_json TEXT);
CREATE TABLE IF NOT EXISTS daemon_state(
  key TEXT PRIMARY KEY, value TEXT);   -- JSON blobs that must survive a restart
"""

# Columns added after the initial schema shipped; applied idempotently on init.
_MIGRATIONS = [
    ("captures", "is_ir", "INTEGER"),
    ("captures", "label_source", "TEXT"),
    ("visits", "scatter_severity", "INTEGER"),  # 0-3 litter-scatter score (meowcam3 floor delta)
    ("visits", "scatter_pct", "REAL"),          # changed-% of the apron ROI
    ("visits", "scatter_area", "INTEGER"),       # scatter blob area, px
    ("visits", "notified", "INTEGER DEFAULT 0"), # 1 after the named elimination alert fires
    ("feed_events", "feeder", "TEXT"),
    ("bowl_events", "location", "TEXT"),
]


def get_daemon_state(conn, key, default=None):
    """Read a JSON-decoded value previously saved under `key`, or `default`.
    Backs the cross-restart latches/cursors that otherwise reset to in-memory
    defaults on every daemon restart (re-answering commands, re-firing alarms)."""
    with _lock:
        row = conn.execute(
            "SELECT value FROM daemon_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def set_daemon_state(conn, key, value):
    """Upsert a JSON-serializable value under `key` so it survives a restart."""
    with _lock:
        conn.execute(
            "INSERT INTO daemon_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))
        conn.commit()


def _migrate(conn):
    for table, col, decl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def _iso(ts):
    # local time so "today" (date prefix) matches the user's day, not UTC
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def connect(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    with _lock:
        conn.executescript(SCHEMA)
        _migrate(conn)   # bring older DBs up to the current column set
        conn.commit()


def insert_event(conn, ev):
    with _lock:
        conn.execute("INSERT INTO events(ts, kind, detail) VALUES(?,?,?)",
                     (_iso(ev.ts), ev.kind, json.dumps(ev.detail)))
        conn.commit()


def open_visit(conn, enter_ts):
    with _lock:
        cur = conn.execute("INSERT INTO visits(enter_ts) VALUES(?)", (_iso(enter_ts),))
        conn.commit()
        return cur.lastrowid


def close_visit(conn, visit_id, leave_ts, duration_s):
    with _lock:
        conn.execute("UPDATE visits SET leave_ts=?, duration_s=? WHERE id=?",
                     (_iso(leave_ts), duration_s, visit_id))
        conn.commit()


def reconcile_open_visits(conn):
    """Close any visit left open (NULL leave_ts) by a prior crash/restart."""
    with _lock:
        conn.execute("UPDATE visits SET leave_ts=enter_ts, duration_s=0 "
                     "WHERE leave_ts IS NULL")
        conn.commit()


def mark_elimination(conn, visit_id, use_record=None):
    with _lock:
        conn.execute(
            "UPDATE visits SET eliminated=1, use_record=COALESCE(?, use_record) WHERE id=?",
            (use_record, visit_id))
        conn.commit()


def bin_full_since(conn):
    """ISO ts of the most recent bin_full NOT followed by a bin_clear, else None.
    Ordering by autoincrement id is monotonic and tz-immune. None => bin is clear."""
    with _lock:
        full = conn.execute(
            "SELECT id, ts FROM events WHERE kind='bin_full' ORDER BY id DESC LIMIT 1").fetchone()
        if not full:
            return None
        clear = conn.execute(
            "SELECT id FROM events WHERE kind='bin_clear' ORDER BY id DESC LIMIT 1").fetchone()
        if clear and clear["id"] > full["id"]:
            return None
        return full["ts"]


def last_bin_clear_ts(conn):
    """ISO ts of the most recent bin_clear (the start of the current fill cycle), else None."""
    with _lock:
        row = conn.execute(
            "SELECT ts FROM events WHERE kind='bin_clear' ORDER BY id DESC LIMIT 1").fetchone()
        return row["ts"] if row else None


def cleans_since(conn, after_iso):
    """Count clean_done events strictly after after_iso — cleans into the current fill cycle."""
    with _lock:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='clean_done' "
            "AND CAST(strftime('%s', ts) AS INTEGER) > CAST(strftime('%s', ?) AS INTEGER)",
            (after_iso,)).fetchone()["n"]


def bin_fill_capacity(conn, window=12, pct=20):
    """Learned capacity: a low percentile of the clean_done count per fill cycle
    (between a bin_clear and the following bin_full), over the most recent `window`
    complete cycles. None if no complete positive cycle yet.

    Why percentile-over-recent-window, not global MIN: a single fluke short cycle
    under a global MIN sticks forever, dragging the approaching-full heads-up down
    so it nags every cycle (approaching_margin spans the whole capacity) — nag
    fatigue / poison. A low percentile ignores an isolated outlier, and the recent
    window lets capacity track current litter/usage as it drifts. Stays
    conservative: for a handful of cycles the percentile degrades toward the min,
    preserving the early heads-up when there isn't enough history to filter."""
    with _lock:
        rows = conn.execute(
            "SELECT kind FROM events WHERE kind IN ('bin_clear','bin_full','clean_done') "
            "ORDER BY id").fetchall()
    cycles, cleans, armed = [], 0, False
    for r in rows:
        k = r["kind"]
        if k == "bin_clear":
            cleans, armed = 0, True
        elif k == "clean_done":
            if armed:
                cleans += 1
        elif k == "bin_full":
            if armed:
                cycles.append(cleans)
            armed = False
    cycles = [c for c in cycles if c > 0]   # skip degenerate zero-clean cycles
    if not cycles:
        return None
    recent = sorted(cycles[-window:])       # recency window, ascending
    idx = int(round((pct / 100.0) * (len(recent) - 1)))   # nearest-rank low pctile
    return recent[idx]


def last_elimination_ts(conn):
    """enter_ts of the most recent eliminated visit, or None — drives the no-go alarm."""
    with _lock:
        row = conn.execute(
            "SELECT enter_ts FROM visits WHERE eliminated=1 "
            "ORDER BY enter_ts DESC LIMIT 1").fetchone()
        return row["enter_ts"] if row else None


def last_attributed_elimination_ts(conn, cat_name):
    """enter_ts of the most recent eliminated+attributed visit for one cat, or None."""
    with _lock:
        row = conn.execute(
            "SELECT v.enter_ts FROM visits v JOIN cats c ON c.id=v.cat_id "
            "WHERE v.eliminated=1 AND c.name=? ORDER BY v.enter_ts DESC LIMIT 1",
            (cat_name,)).fetchone()
        return row["enter_ts"] if row else None


def last_real_elimination_ts_any(conn):
    """enter_ts of the most recent REAL eliminated visit across ALL cats, or None.

    Mirrors health_watch._check_no_go's 'latest' set: excludes Garfield's
    deliberate short re-entries (use_record IS NULL OR duration_s <= 40), which
    game the auto-clean timer rather than register a real elimination. This is
    the input to the system-wide silence guard, so it MUST match health_watch's
    notion of 'box was used' — otherwise a 30s Garfield re-entry resets the
    dashboard's silence clock while Telegram's stays put. Orders by epoch math
    so mixed naive-local / legacy +00:00 timestamps compare correctly."""
    with _lock:
        row = conn.execute(
            "SELECT v.enter_ts FROM visits v JOIN cats c ON c.id=v.cat_id "
            "WHERE v.eliminated=1 "
            "AND NOT (c.name='Garfield' AND (v.use_record IS NULL OR v.duration_s <= 40)) "
            "ORDER BY CAST(strftime('%s', v.enter_ts) AS INTEGER) DESC LIMIT 1").fetchone()
        return row["enter_ts"] if row else None


def last_eliminated_ts(conn):
    """enter_ts of the most recent eliminated visit, ATTRIBUTED OR NOT — the
    attribution-INDEPENDENT 'was the box used for elimination at all' signal.

    Unlike last_real_elimination_ts_any (which JOINs cats and so only sees
    attributed visits), this counts visits whose cat couldn't be identified, so a
    labeler outage doesn't make it look like the box went unused. Garfield's
    deliberate short re-entries don't fire dp102, so they're eliminated=0 and are
    excluded automatically. This is the input to the deadman's whole-system
    no-go catch-all."""
    with _lock:
        row = conn.execute(
            "SELECT enter_ts FROM visits WHERE eliminated=1 "
            "ORDER BY CAST(strftime('%s', enter_ts) AS INTEGER) DESC LIMIT 1").fetchone()
        return row["enter_ts"] if row else None


def eliminations_today_for_cat(conn, cat_name, now=None):
    """Count of this cat's eliminated visits since local midnight today."""
    now = now if now is not None else time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    with _lock:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM visits v JOIN cats c ON c.id=v.cat_id "
            "WHERE v.eliminated=1 AND c.name=? "
            "AND CAST(strftime('%s', v.enter_ts) AS INTEGER) >= CAST(strftime('%s', ?) AS INTEGER)",
            (cat_name, _iso(midnight))).fetchone()["n"]


def uncertain_eliminations_since(conn, after_iso, conf_floor=0.7):
    """Count eliminated visits that are unreliable to attribute: unattributed
    (cat_id IS NULL) or low-confidence (confidence < conf_floor), at/after
    after_iso. A nonzero result means the box was recently used but we can't
    confidently say by whom — per-cat 'hasn't gone' claims over that window are
    unreliable and must not fire as a confident alert."""
    with _lock:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM visits "
            "WHERE eliminated=1 AND (cat_id IS NULL OR confidence < ?) "
            "AND CAST(strftime('%s', enter_ts) AS INTEGER) >= CAST(strftime('%s', ?) AS INTEGER)",
            (conf_floor, after_iso)).fetchone()["n"]


def attribution_unreliable(conn, after_iso, conf_floor=0.7, min_count=2):
    """True when recent attribution is too unreliable to trust a per-cat 'hasn't
    gone' signal — the shared gate for BOTH the dashboard (cat_status) and the
    Telegram watcher (health_watch), so they never disagree."""
    return uncertain_eliminations_since(conn, after_iso, conf_floor) >= min_count


def log_incident(conn, kind, signal, action_taken, outcome, notes="", ts=None):
    """Append one watchdog episode to the incidents audit log. `signal` is any
    JSON-serializable dict; `ts` is an epoch float (None -> wall-clock now)."""
    stamp = _iso(ts) if ts is not None else datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn.execute(
            "INSERT INTO incidents(ts, kind, signal, action_taken, outcome, notes) "
            "VALUES(?,?,?,?,?,?)",
            (stamp, kind, json.dumps(signal), action_taken, outcome, notes))
        conn.commit()


def recent_incidents(conn, limit=20):
    """Newest-first incidents, with `signal` parsed back to a dict."""
    with _lock:
        rows = conn.execute(
            "SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["signal"] = json.loads(d["signal"]) if d["signal"] else {}
        out.append(d)
    return out


def incidents_since(conn, kind, after_iso, outcomes=None):
    """Count incidents of `kind` at/after `after_iso`, optionally restricted to
    a tuple of `outcomes` — the rate-limit primitive for the Remediator."""
    q = "SELECT COUNT(*) AS n FROM incidents WHERE kind=? AND CAST(strftime('%s', ts) AS INTEGER) >= CAST(strftime('%s', ?) AS INTEGER)"
    params = [kind, after_iso]
    if outcomes:
        q += " AND outcome IN (%s)" % ",".join("?" * len(outcomes))
        params.extend(outcomes)
    with _lock:
        return conn.execute(q, params).fetchone()["n"]


def incident_rollup(conn):
    """(kind, outcome) counts, busiest first — the 'how are things going' view."""
    with _lock:
        rows = conn.execute(
            "SELECT kind, outcome, COUNT(*) AS n FROM incidents "
            "GROUP BY kind, outcome ORDER BY n DESC").fetchall()
        return [dict(r) for r in rows]


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
            "SELECT * FROM weekly_reports ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def recent_weekly_reports(conn, limit=8):
    """Newest-first weekly reports."""
    with _lock:
        rows = conn.execute(
            "SELECT * FROM weekly_reports ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def recent_visits(conn, limit=20):
    with _lock:
        cur = conn.execute("SELECT * FROM visits ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


def _parse_ts(s):
    # DB stamps are naive local-ISO (see _iso); one legacy row may carry a tz — drop it.
    return datetime.fromisoformat(s).replace(tzinfo=None)


def _can_merge(s, v, gap_s):
    if s["leave_ts"] is None or v["enter_ts"] is None:
        return False
    gap = max(0.0, (_parse_ts(v["enter_ts"]) - _parse_ts(s["leave_ts"])).total_seconds())
    if gap >= gap_s:
        return False
    sc, vc = s["cat_id"], v["cat_id"]
    if sc is not None and vc is not None and sc != vc:
        return False
    return bool(s["eliminated"]) != bool(v["eliminated"])   # exactly one dp102 anchor


def _new_session(v):
    return {
        "visit_ids": [v["id"]],
        "enter_ts": v["enter_ts"],
        "leave_ts": v["leave_ts"],
        "duration_s": v["duration_s"] or 0,
        "cat_id": v["cat_id"],
        "cat": None,   # resolved in sessions() after folding
        "eliminated": int(bool(v["eliminated"])),
        "use_record": v["use_record"],
        "scatter_severity": v["scatter_severity"],
        "scatter_pct": v["scatter_pct"],
        "n_fragments": 1,
    }


def _absorb(s, v):
    s["visit_ids"].append(v["id"])
    s["leave_ts"] = v["leave_ts"]
    if s["leave_ts"] is not None:
        s["duration_s"] = int(
            (_parse_ts(s["leave_ts"]) - _parse_ts(s["enter_ts"])).total_seconds())
    s["eliminated"] = int(s["eliminated"] or bool(v["eliminated"]))
    if s["cat_id"] is None and v["cat_id"] is not None:
        s["cat_id"] = v["cat_id"]
    if s["use_record"] is None:
        s["use_record"] = v["use_record"]
    vs = v["scatter_severity"]
    if vs is not None and (s["scatter_severity"] is None or vs > s["scatter_severity"]):
        s["scatter_severity"] = vs
        s["scatter_pct"] = v["scatter_pct"]
    s["n_fragments"] = len(s["visit_ids"])


def sessions(conn, gap_s=30):
    """Collapse IR-flicker visit fragments into logical sessions WITHOUT mutating any
    row. Two fragments merge only when a single dp102 elimination anchors them (XOR on
    `eliminated`) and they are close in time (< gap_s) and cat-compatible. This keeps a
    senior cat's weight-shift flicker as one trip, while a gaming cat's no-elimination
    blips and two genuinely separate pees stay distinct. Read-time so the async vision
    cat_id is available. Newest-first, like recent_visits."""
    with _lock:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM visits ORDER BY enter_ts ASC, id ASC").fetchall()]

    out = []          # sessions, oldest-first while folding
    for v in rows:
        s = out[-1] if out else None
        if s is not None and _can_merge(s, v, gap_s):
            _absorb(s, v)
        else:
            out.append(_new_session(v))

    # Resolve cat names after folding (conn-free _absorb can't call cat_name_by_id)
    for s in out:
        s["cat"] = cat_name_by_id(conn, s["cat_id"]) if s["cat_id"] else None

    out.reverse()     # newest-first
    return out


def eliminations_today(conn, day=None):
    """Count today's eliminations from OUR tracking (dp102-driven) — the box's
    own dp7 counter is unreliable. day defaults to the local date."""
    prefix = (day or date.today().isoformat())
    with _lock:
        row = conn.execute(
            "SELECT COUNT(*) FROM visits WHERE eliminated=1 AND enter_ts LIKE ?",
            (prefix + "%",)).fetchone()
        return row[0]


def log_feed_event(conn, portions, source, feeder=None, ts=None):
    """Record one dispense (from the dp-118 feed record or a manual /feed)."""
    stamp = _iso(ts) if ts is not None else datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn.execute("INSERT INTO feed_events(ts, portions, source, feeder) VALUES(?,?,?,?)",
                     (stamp, int(portions), source, feeder))
        conn.commit()


def last_feed_event_ts(conn, feeder=None):
    """Epoch of the most recent feed event, or None — drives new-feed detection."""
    with _lock:
        if feeder:
            row = conn.execute("SELECT MAX(ts) AS m FROM feed_events WHERE feeder=?", (feeder,)).fetchone()
        else:
            row = conn.execute("SELECT MAX(ts) AS m FROM feed_events").fetchone()
    if not row or row["m"] is None:
        return None
    return datetime.fromisoformat(row["m"]).timestamp()


def feed_in_window(conn, start_epoch, end_epoch, feeder=None):
    """True if any feed event landed in [start, end] (inclusive)."""
    with _lock:
        if feeder:
            row = conn.execute(
                "SELECT 1 FROM feed_events WHERE CAST(strftime('%s', ts) AS INTEGER) >= CAST(strftime('%s', ?) AS INTEGER) "
                "AND CAST(strftime('%s', ts) AS INTEGER) <= CAST(strftime('%s', ?) AS INTEGER) AND feeder=? LIMIT 1",
                (_iso(start_epoch), _iso(end_epoch), feeder)).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM feed_events WHERE CAST(strftime('%s', ts) AS INTEGER) >= CAST(strftime('%s', ?) AS INTEGER) "
                "AND CAST(strftime('%s', ts) AS INTEGER) <= CAST(strftime('%s', ?) AS INTEGER) LIMIT 1",
                (_iso(start_epoch), _iso(end_epoch))).fetchone()
        return row is not None


def feed_events_today(conn, day=None, feeder=None):
    """(meals, total_portions) for the given local day (default today)."""
    day = day or date.today().isoformat()
    with _lock:
        if feeder:
            row = conn.execute(
                "SELECT COUNT(*) AS meals, COALESCE(SUM(portions),0) AS portions "
                "FROM feed_events WHERE ts LIKE ? AND feeder=?", (day + "%", feeder)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS meals, COALESCE(SUM(portions),0) AS portions "
                "FROM feed_events WHERE ts LIKE ?", (day + "%",)).fetchone()
        return row["meals"], row["portions"]


def recent_feed_events(conn, limit=20, feeder=None):
    with _lock:
        if feeder:
            rows = conn.execute("SELECT * FROM feed_events WHERE feeder=? ORDER BY id DESC LIMIT ?",
                                (feeder, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM feed_events ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()
        return [dict(r) for r in rows]


def log_bowl_event(conn, state, source="vision", secs_since_feed=None, location=None, ts=None):
    """Record a bowl observation ('vision') or an auto-feed bookkeeping row."""
    stamp = _iso(ts) if ts is not None else datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn.execute(
            "INSERT INTO bowl_events(ts, state, source, secs_since_feed, location) "
            "VALUES(?,?,?,?,?)", (stamp, state, source, secs_since_feed, location))
        conn.commit()


def last_bowl_state(conn, location=None):
    """Most recent vision-observed bowl state (ignores auto_feed bookkeeping rows)."""
    with _lock:
        if location:
            row = conn.execute("SELECT state FROM bowl_events WHERE source='vision' AND location=? ORDER BY id DESC LIMIT 1", (location,)).fetchone()
        else:
            row = conn.execute("SELECT state FROM bowl_events WHERE source='vision' ORDER BY id DESC LIMIT 1").fetchone()
        return row["state"] if row else None


def auto_feeds_today(conn, location=None):
    """Count of auto-feed dispenses today — the BowlWatch rate-limit primitive."""
    today = date.today().isoformat()
    with _lock:
        if location:
            row = conn.execute("SELECT COUNT(*) AS n FROM bowl_events WHERE source='auto_feed' AND ts LIKE ? AND location=?", (today + "%", location)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM bowl_events WHERE source='auto_feed' AND ts LIKE ?", (today + "%",)).fetchone()
        return row["n"]


def last_consumption_secs(conn, location=None):
    """secs_since_feed of the most recent vision 'empty' event that has one."""
    with _lock:
        if location:
            row = conn.execute("SELECT secs_since_feed FROM bowl_events WHERE source='vision' AND state='empty' AND secs_since_feed IS NOT NULL AND location=? ORDER BY id DESC LIMIT 1", (location,)).fetchone()
        else:
            row = conn.execute("SELECT secs_since_feed FROM bowl_events WHERE source='vision' AND state='empty' AND secs_since_feed IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return row["secs_since_feed"] if row else None


def recent_bowl_events(conn, limit=20):
    with _lock:
        rows = conn.execute("SELECT * FROM bowl_events ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
        return [dict(r) for r in rows]


def log_bowl_session(conn, location, cat, duration_s, ts=None):
    stamp = _iso(ts) if ts is not None else datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn.execute(
            "INSERT INTO bowl_sessions(ts, location, cat, duration_s) VALUES(?,?,?,?)",
            (stamp, location, cat, duration_s))
        conn.commit()


def recent_bowl_sessions(conn, limit=20, location=None):
    with _lock:
        if location:
            rows = conn.execute("SELECT * FROM bowl_sessions WHERE location=? ORDER BY id DESC LIMIT ?",
                                (location, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM bowl_sessions ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()
        return [dict(r) for r in rows]



def elimination_attribution_stats(conn, after_iso, before_iso):
    """For eliminated visits with after_iso <= enter_ts < before_iso, return
    (raw, attributed): raw = all eliminated=1; attributed = those carrying a
    cat_id. `before_iso` should be earlier than now by the labeler's grace window
    so visits too recent to have been labeled are not counted as 'dropped'."""
    with _lock:
        row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN (SELECT COUNT(*) FROM captures WHERE visit_id=visits.id) > 0 THEN 1 ELSE 0 END) AS framed_raw, "
            "  SUM(CASE WHEN (SELECT COUNT(*) FROM captures WHERE visit_id=visits.id) = 0 THEN 1 ELSE 0 END) AS frameless_raw, "
            "  SUM(CASE WHEN cat_id IS NOT NULL AND (SELECT COUNT(*) FROM captures WHERE visit_id=visits.id) > 0 THEN 1 ELSE 0 END) AS attributed "
            "FROM visits WHERE eliminated=1 "
            "AND CAST(strftime('%s', enter_ts) AS INTEGER) >= CAST(strftime('%s', ?) AS INTEGER) "
            "AND CAST(strftime('%s', enter_ts) AS INTEGER) < CAST(strftime('%s', ?) AS INTEGER)",
            (after_iso, before_iso)).fetchone()
        return (row["framed_raw"] or 0), (row["attributed"] or 0), (row["frameless_raw"] or 0)


def seed_cats(conn, names):
    with _lock:
        for n in names:
            conn.execute("INSERT OR IGNORE INTO cats(name) VALUES(?)", (n,))
        conn.commit()


def insert_capture(conn, ts, visit_id, camera, path, is_ir=None):
    with _lock:
        cur = conn.execute(
            "INSERT INTO captures(ts, visit_id, camera, path, is_ir) VALUES(?,?,?,?,?)",
            (_iso(ts), visit_id, camera, path, is_ir))
        conn.commit()
        return cur.lastrowid


def latest_open_visit_id(conn):
    with _lock:
        row = conn.execute(
            "SELECT id FROM visits WHERE leave_ts IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None


def captures_for_visit(conn, visit_id):
    with _lock:
        cur = conn.execute("SELECT * FROM captures WHERE visit_id=? ORDER BY id", (visit_id,))
        return [dict(r) for r in cur.fetchall()]


def eliminated_visits_after(conn, last_id, limit=50):
    """Completed (eliminated) visits with id > last_id, oldest first. Used by the
    shadow matcher to score newly-finished visits without touching attribution.
    Returns (id, cat_id) rows — cat_id is the live/committed attribution."""
    with _lock:
        cur = conn.execute(
            "SELECT id, cat_id FROM visits WHERE eliminated=1 AND id>? ORDER BY id LIMIT ?",
            (last_id, limit))
        return [(r["id"], r["cat_id"]) for r in cur.fetchall()]


def cat_id_by_name(conn, name):
    with _lock:
        row = conn.execute("SELECT id FROM cats WHERE name=?", (name,)).fetchone()
        return row["id"] if row else None


def cat_name_by_id(conn, cat_id):
    with _lock:
        row = conn.execute("SELECT name FROM cats WHERE id=?", (cat_id,)).fetchone()
        return row["name"] if row else None


def visit_is_eliminated(conn, visit_id):
    """True if this visit recorded an elimination (use_record fired) — ground
    truth that a cat was present, so the cat/no-cat filter must not veto it."""
    with _lock:
        row = conn.execute("SELECT eliminated FROM visits WHERE id=?", (visit_id,)).fetchone()
        return bool(row["eliminated"]) if row else False


def visit_established_cat(conn, visit_id):
    """The single cat a HUMAN established for this visit (human/corrected labels),
    or None if none or conflicting. This is authoritative context: a visit is one
    cat, so the auto-labeler must not tag the visit's other frames as a different
    cat than the human already confirmed."""
    with _lock:
        rows = conn.execute(
            "SELECT DISTINCT label FROM captures WHERE visit_id=? AND label IS NOT NULL "
            "AND label_source IN ('human','corrected')", (visit_id,)).fetchall()
    cats = [r["label"] for r in rows]
    return cat_name_by_id(conn, cats[0]) if len(cats) == 1 else None


def stale_unlabeled_count(conn, before_iso):
    """Captures completely untouched (no label, no auto verdict) older than
    before_iso — frames the auto-labeler should have processed but hasn't
    (labeler stuck/dead). The liveness signal."""
    with _lock:
        row = conn.execute(
            "SELECT COUNT(*) FROM captures WHERE label IS NULL AND label_source IS NULL "
            "AND visit_id IS NOT NULL AND CAST(strftime('%s', ts) AS INTEGER) < CAST(strftime('%s', ?) AS INTEGER)",  # match the labeler's actual queue
            (before_iso,)).fetchone()
        return row[0]


def set_capture_label(conn, capture_id, cat_id, source="human"):
    """Apply a gallery label. `source`: 'human' (label.py), 'auto' (labeler),
    or 'corrected' (human overriding a prior auto-label). Provenance is the
    trust channel — it's how we audit and measure the auto-labeler.

    Keeps the visit row consistent: a frame label always re-syncs its visit's
    cat_id (low-volume human/corrected path — the per-frame auto path batches
    via the labeler instead, see sync_visit_cat)."""
    with _lock:
        conn.execute("UPDATE captures SET label=?, label_source=? WHERE id=?",
                     (cat_id, source, capture_id))
        row = conn.execute("SELECT visit_id FROM captures WHERE id=?",
                           (capture_id,)).fetchone()
        conn.commit()
    if row and row["visit_id"] is not None:
        sync_visit_cat(conn, row["visit_id"])


def captures_by_visit(conn, visit_ids):
    """All capture rows for the given visit ids, grouped {visit_id: [rows]}."""
    out = {}
    with _lock:
        for vid in visit_ids:
            cur = conn.execute("SELECT * FROM captures WHERE visit_id=? ORDER BY id", (vid,))
            out[vid] = [dict(r) for r in cur.fetchall()]
    return out


def unlabeled_visit_ids(conn, limit=200):
    """Visit ids with at least one UNTOUCHED capture — the auto-labeler work
    queue. A frame is untouched only if no human label AND no prior auto verdict
    (label_source IS NULL); this is what stops the worker from re-running an
    expensive model on empty/conflict frames every sweep."""
    with _lock:
        cur = conn.execute(
            "SELECT DISTINCT visit_id FROM captures "
            "WHERE label IS NULL AND label_source IS NULL AND visit_id IS NOT NULL "
            "ORDER BY visit_id LIMIT ?", (limit,))
        return [r["visit_id"] for r in cur.fetchall()]


def mark_capture_examined(conn, capture_id, source):
    """Record an auto verdict that produced NO gallery label ('auto-none' for
    empty/no-cat, 'auto-conflict' for an ambiguous visit) so the frame leaves
    the auto queue. Only touches still-untouched rows, so a human label applied
    during the (slow) inference window is never clobbered."""
    with _lock:
        conn.execute(
            "UPDATE captures SET label_source=? WHERE id=? AND label_source IS NULL",
            (source, capture_id))
        conn.commit()


def apply_auto_label(conn, capture_id, cat_id, conf):
    """Atomically apply an AUTO label + prediction, but only if the frame is
    still untouched (no human label landed during inference). Returns True if
    it was applied."""
    with _lock:
        cur = conn.execute(
            "UPDATE captures SET label=?, label_source='auto', pred=?, pred_conf=? "
            "WHERE id=? AND label IS NULL AND label_source IS NULL",
            (cat_id, cat_id, conf, capture_id))
        conn.commit()
        return cur.rowcount > 0


def review_queue(conn, limit=100):
    """Frames the auto-labeler punted to a human (ambiguous visits)."""
    with _lock:
        cur = conn.execute(
            "SELECT id, visit_id, camera, path FROM captures "
            "WHERE label_source='auto-conflict' ORDER BY id LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


def recent_auto_labels(conn, limit=50):
    """Auto-applied labels for the trust channel — newest first, with the cat
    name and whether a human later corrected it."""
    with _lock:
        cur = conn.execute(
            "SELECT cap.id, cap.path, cap.camera, cap.pred_conf, cap.label_source, "
            "       c.name AS cat FROM captures cap LEFT JOIN cats c ON c.id=cap.label "
            "WHERE cap.label_source IN ('auto','corrected') "
            "ORDER BY cap.id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


def labeler_accuracy(conn):
    """Trust-channel scoreboard: counts by provenance, plus how often an
    auto-label disagreed with the model's own prediction after human review
    (i.e. corrected). 'corrected' rows are confirmed auto-labeler misses."""
    with _lock:
        rows = conn.execute(
            "SELECT label_source, COUNT(*) AS n FROM captures "
            "WHERE label IS NOT NULL GROUP BY label_source").fetchall()
        by_source = {r["label_source"]: r["n"] for r in rows}
        auto = by_source.get("auto", 0)
        corrected = by_source.get("corrected", 0)
        reviewed = auto + corrected
        return {
            "human": by_source.get("human", 0),
            "auto": auto,
            "corrected": corrected,
            "auto_accuracy": (auto / reviewed) if reviewed else None,
        }


def set_capture_prediction(conn, capture_id, cat_id, conf):
    """Per-view model prediction (pre-fusion), kept for auditing the matcher."""
    with _lock:
        conn.execute("UPDATE captures SET pred=?, pred_conf=? WHERE id=?",
                     (cat_id, conf, capture_id))
        conn.commit()


def set_visit_identity(conn, visit_id, cat_id, confidence):
    with _lock:
        conn.execute("UPDATE visits SET cat_id=?, confidence=? WHERE id=?",
                     (cat_id, confidence, visit_id))
        conn.commit()


def get_visit(conn, visit_id):
    """The full visit row as a dict, or None."""
    with _lock:
        row = conn.execute("SELECT * FROM visits WHERE id=?", (visit_id,)).fetchone()
        return dict(row) if row else None


def set_visit_scatter(conn, visit_id, severity, pct, area):
    """Record the per-visit litter-scatter score. KEEPS THE WORST zone: two detectors
    (apron via meowcam3 + fling zone via meowcam4) both score the same visit, so only
    overwrite when this severity is higher — the visit row reflects the worst scatter
    seen, and the writes are order-independent."""
    with _lock:
        conn.execute(
            "UPDATE visits SET scatter_severity=?, scatter_pct=?, scatter_area=? "
            "WHERE id=? AND (scatter_severity IS NULL OR scatter_severity < ?)",
            (severity, pct, area, visit_id, severity))
        conn.commit()


def per_cat_scatter(conn):
    """Per-cat scatter blame — the 'who keeps making the mess' tally. For each
    cat with at least one scored visit: how many visits were scored, how many
    were actual scatter (severity>=1), and the average changed-%. Derived from
    visits.cat_id (set by the labeler) joined to the per-visit scatter score, so
    it only firms up once a visit is attributed. Ordered worst-first."""
    with _lock:
        cur = conn.execute(
            "SELECT c.name AS name, COUNT(v.id) AS scored, "
            "  SUM(CASE WHEN v.scatter_severity>=1 THEN 1 ELSE 0 END) AS messes, "
            "  AVG(v.scatter_pct) AS avg_pct "
            "FROM cats c JOIN visits v ON v.cat_id=c.id "
            "WHERE v.scatter_severity IS NOT NULL "
            "GROUP BY c.id ORDER BY messes DESC, avg_pct DESC")
        return [dict(r) for r in cur.fetchall()]


def human_attribute_visit(conn, visit_id, cat_id):
    """Attribute a whole visit to a cat from a HUMAN decision (the Telegram tap
    when auto-ID failed). Writes the human label on the visit's first capture, which
    (a) syncs visits.cat_id via set_capture_label and (b) makes visit_established_cat
    return this cat so the auto-labeler never overrides it. Returns False if the
    visit has no captures."""
    with _lock:
        row = conn.execute(
            "SELECT id FROM captures WHERE visit_id=? ORDER BY id LIMIT 1",
            (visit_id,)).fetchone()
    if row is None:
        return False
    set_capture_label(conn, row["id"], cat_id, source="human")  # syncs the visit too
    return True


def propagate_visit_label(conn, visit_id, cat_id, capture_ids=None, source="human"):
    """Bulk label MANY frames of one visit at once — the Telegram-tap multiplier.

    Confirming a visit's identity confirms the cat for EVERY frame in that visit,
    so this writes the label onto each capture in `capture_ids` (or all of the
    visit's captures when None), then syncs the visit once. `capture_ids` lets the
    caller restrict to cat-positive frames (run the detector first) so empty
    pre/post frames don't pollute the gallery. The `AND visit_id=?` guard means a
    capture id belonging to another visit is silently ignored, never mislabeled.
    Returns the number of frames actually labeled.

    Contrast `human_attribute_visit`, which labels only the visit's FIRST frame.

    WIRING STATUS: NOT currently called by the live Telegram tap — `meowantd._label_cb`
    still uses `human_attribute_visit` (one frame per tap). This was used for a one-off
    bulk-propagation that grew the gallery's label set; promoting it into the live tap
    is a deliberate post-trip change (it alters gallery composition), not yet done."""
    with _lock:
        if capture_ids is None:
            rows = conn.execute(
                "SELECT id FROM captures WHERE visit_id=?", (visit_id,)).fetchall()
            ids = [r["id"] for r in rows]
        else:
            ids = list(capture_ids)
        if not ids:
            return 0
        cur = conn.executemany(
            "UPDATE captures SET label=?, label_source=? WHERE id=? AND visit_id=?",
            [(cat_id, source, cid, visit_id) for cid in ids])
        n = cur.rowcount
        conn.commit()
    if n:
        sync_visit_cat(conn, visit_id)
    return n


def human_mark_no_cat(conn, visit_id):
    """Human says no real cat used the box (false dp102 / the dog / nothing). Clear the
    elimination so it stops counting as a real use — keeps daily counts and the no-go
    alarm's 'last use' honest. The raw dp102 event stays in the immutable log."""
    with _lock:
        conn.execute(
            "UPDATE visits SET eliminated=0, use_record=NULL WHERE id=?", (visit_id,))
        conn.commit()
    return True


def sync_visit_cat(conn, visit_id):
    """Attribute the VISIT to a cat from the MAJORITY of its labeled captures
    (captures.label is the source of truth). Confidence = agreement ratio.
    No-op returning None if the visit has no labeled frames yet.

    Fixes 6v5: the auto-labeler wrote captures.label but never visits.cat_id,
    so any visit-level reader (scatter blame, per-cat health baselines) saw a
    stale/empty attribution. Returns (cat_id, confidence) when it sets one."""
    with _lock:
        rows = conn.execute(
            "SELECT label FROM captures WHERE visit_id=? AND label IS NOT NULL",
            (visit_id,)).fetchall()
    labels = [r["label"] for r in rows]
    if not labels:
        return None
    cat_id, n = Counter(labels).most_common(1)[0]
    conf = n / len(labels)
    set_visit_identity(conn, visit_id, cat_id, conf)
    return cat_id, conf


def unlabeled_captures(conn, limit=500):
    with _lock:
        cur = conn.execute(
            "SELECT * FROM captures WHERE label IS NULL ORDER BY id LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


def gallery_counts(conn):
    """Labeled-capture count per cat name — shows how built-out the gallery is."""
    with _lock:
        cur = conn.execute(
            "SELECT c.name AS name, COUNT(cap.id) AS n FROM cats c "
            "LEFT JOIN captures cap ON cap.label = c.id "
            "GROUP BY c.id ORDER BY c.name")
        return {r["name"]: r["n"] for r in cur.fetchall()}


def capture_paths_around(conn, before_iso, window_s=120, limit=12):
    """Capture file paths from the window (before_iso - window_s, before_iso]. Used to
    recover frames for an ELIMINATED visit that captured none of its own: IR-flicker
    splits one physical visit into fragments, and the frames can land on a sibling
    fragment while the dp102 elimination lands on a frameless one. Newest first."""
    after = datetime.fromtimestamp(
        datetime.fromisoformat(before_iso).timestamp() - window_s).isoformat(timespec="seconds")
    with _lock:
        cur = conn.execute(
            "SELECT path FROM captures WHERE CAST(strftime('%s', ts) AS INTEGER) > CAST(strftime('%s', ?) AS INTEGER) "
            "AND CAST(strftime('%s', ts) AS INTEGER) <= CAST(strftime('%s', ?) AS INTEGER) AND path IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (after, before_iso, limit))
        return [r["path"] for r in cur.fetchall()]


def pop_empty_captures(conn):
    """Remove all auto-none capture rows (examined, no cat found) and return
    their file paths so the caller can delete them from disk. Safe to call at
    any time — auto-none rows are only written after the labeler has finished
    with a frame, so there's no race with in-flight inference."""
    with _lock:
        rows = conn.execute(
            "SELECT path FROM captures WHERE label_source='auto-none' AND path IS NOT NULL"
        ).fetchall()
        paths = [r["path"] for r in rows]
        conn.execute("DELETE FROM captures WHERE label_source='auto-none'")
        conn.commit()
    return paths


def pending_elimination_notifications(conn, before_iso):
    """Eliminated, closed visits not yet alerted and old enough that their capture
    frames have settled (leave_ts <= before_iso). Oldest-first so alerts are ordered."""
    with _lock:
        cur = conn.execute(
            "SELECT * FROM visits WHERE eliminated=1 AND leave_ts IS NOT NULL "
            "AND COALESCE(notified,0)=0 AND CAST(strftime('%s', leave_ts) AS INTEGER) <= CAST(strftime('%s', ?) AS INTEGER) ORDER BY id", (before_iso,))
        return [dict(r) for r in cur.fetchall()]


def mark_notified(conn, visit_id):
    with _lock:
        conn.execute("UPDATE visits SET notified=1 WHERE id=?", (visit_id,))
        conn.commit()


def eliminated_visits_missing_captures(conn, after_iso, before_iso):
    """Eliminated visits closed within (after_iso, before_iso] that have zero
    capture rows — capture likely failed (stale thread or dead stream). The
    `before_iso` bound is the settle window (don't flag in-flight grabs);
    `after_iso` keeps the sweep to recent visits so a restart can't re-flag
    ancient history. leave_ts is a local-ISO string, so lexical order == time
    order for the comparison — except during the DST fall-back hour, where a
    visit in the repeated hour could fall outside the window (worst case: one
    missed alert/year; the loud proactive stream probe still covers the source
    going down)."""
    with _lock:
        cur = conn.execute(
            "SELECT v.id, v.enter_ts, v.leave_ts FROM visits v "
            "WHERE v.eliminated=1 AND v.leave_ts IS NOT NULL "
            "AND CAST(strftime('%s', v.leave_ts) AS INTEGER) > CAST(strftime('%s', ?) AS INTEGER) "
            "AND CAST(strftime('%s', v.leave_ts) AS INTEGER) <= CAST(strftime('%s', ?) AS INTEGER) "
            "AND NOT EXISTS (SELECT 1 FROM captures c WHERE c.visit_id = v.id) "
            "ORDER BY v.id",
            (after_iso, before_iso))
        return [dict(r) for r in cur.fetchall()]
