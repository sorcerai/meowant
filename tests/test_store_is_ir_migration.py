"""Regression: captures.is_ir must be present in _MIGRATIONS so a pre-existing
DB created before is_ir was added to the inline CREATE TABLE gets the column via
ALTER TABLE on init_db — without it, insert_capture / backfill raise
'no such column: is_ir'."""
import sqlite3

from mw import store


def _old_db():
    """Return an in-memory connection with a captures table that LACKS is_ir,
    simulating a DB created before the column was introduced."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Minimal schema without is_ir — mirrors what an old DB would have.
    conn.executescript("""
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
          pred INTEGER, pred_conf REAL,
          label_source TEXT);
        CREATE TABLE IF NOT EXISTS incidents(
          id INTEGER PRIMARY KEY, ts TEXT, kind TEXT,
          signal TEXT, action_taken TEXT, outcome TEXT, notes TEXT);
        CREATE TABLE IF NOT EXISTS feed_events(
          id INTEGER PRIMARY KEY, ts TEXT, portions INTEGER, source TEXT);
        CREATE TABLE IF NOT EXISTS bowl_events(
          id INTEGER PRIMARY KEY, ts TEXT, state TEXT,
          source TEXT, secs_since_feed INTEGER);
        CREATE TABLE IF NOT EXISTS bowl_sessions(
          id INTEGER PRIMARY KEY, ts TEXT, location TEXT,
          cat TEXT, duration_s INTEGER);
        CREATE TABLE IF NOT EXISTS weekly_reports(
          id INTEGER PRIMARY KEY, ts TEXT,
          period_start TEXT, period_end TEXT,
          facts_json TEXT, findings_json TEXT, narrative_json TEXT);
        CREATE TABLE IF NOT EXISTS daemon_state(
          key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.commit()
    return conn


def test_is_ir_migration_adds_column():
    """init_db on an old DB (no is_ir) must add the column via _migrate."""
    conn = _old_db()
    # Confirm the column is absent before migration.
    cols_before = {r["name"] for r in conn.execute("PRAGMA table_info(captures)")}
    assert "is_ir" not in cols_before, "test precondition: old DB must not have is_ir"

    store.init_db(conn)

    cols_after = {r["name"] for r in conn.execute("PRAGMA table_info(captures)")}
    assert "is_ir" in cols_after, "init_db must add is_ir via _MIGRATIONS"


def test_is_ir_present_on_fresh_db():
    """A brand-new DB must also carry is_ir (inline CREATE TABLE path)."""
    conn = store.connect(":memory:")
    store.init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(captures)")}
    assert "is_ir" in cols
