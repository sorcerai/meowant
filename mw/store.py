"""SQLite persistence for events and visits."""
import json
import sqlite3
import threading
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
"""

# Columns added after the initial schema shipped; applied idempotently on init.
_MIGRATIONS = [
    ("captures", "label_source", "TEXT"),
    ("visits", "scatter_severity", "INTEGER"),  # 0-3 litter-scatter score (meowcam3 floor delta)
    ("visits", "scatter_pct", "REAL"),          # changed-% of the apron ROI
    ("visits", "scatter_area", "INTEGER"),       # scatter blob area, px
    ("visits", "notified", "INTEGER DEFAULT 0"), # 1 after the named elimination alert fires
]


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
            "AND visit_id IS NOT NULL AND ts < ?",  # match the labeler's actual queue
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
    """Record the per-visit litter-scatter score (post-leave meowcam3 floor delta)."""
    with _lock:
        conn.execute(
            "UPDATE visits SET scatter_severity=?, scatter_pct=?, scatter_area=? WHERE id=?",
            (severity, pct, area, visit_id))
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
            "AND COALESCE(notified,0)=0 AND leave_ts <= ? ORDER BY id", (before_iso,))
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
            "AND v.leave_ts > ? AND v.leave_ts <= ? "
            "AND NOT EXISTS (SELECT 1 FROM captures c WHERE c.visit_id = v.id) "
            "ORDER BY v.id",
            (after_iso, before_iso))
        return [dict(r) for r in cur.fetchall()]
