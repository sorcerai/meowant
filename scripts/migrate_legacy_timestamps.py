#!/usr/bin/env python3
"""One-shot, idempotent migration: normalize legacy timestamp rows to the
canonical naive-local format (meowant-cft).

Background: store._iso writes naive-LOCAL ISO ("2026-06-26T14:50:42"), but a
handful of early rows were written as true-UTC with an offset suffix
("2026-06-21T00:21:10.372317+00:00"). Time-window queries compare a column
against a naive-local bound; for the legacy rows that comparison is wrong by
the full tz offset under BOTH raw-string AND strftime epoch math (SQLite reads
a naive bound as UTC, so the offset never cancels for a true-UTC row). _iso is
the only writer, so the legacy rows are a closed set — normalize them once and
every time-window query becomes correct retroactively.

Idempotent: only rewrites values containing an offset ('+' or trailing 'Z');
re-running finds none. Safe to run against the live DB while the daemon runs —
it only touches ancient rows the daemon never rewrites, and uses a busy timeout
so it waits out any concurrent write lock instead of erroring.
"""
import sys
from datetime import datetime

from mw import store

# (table, column) pairs that hold ISO timestamps compared in time-window queries.
TS_COLUMNS = [
    ("events", "ts"),
    ("visits", "enter_ts"),
    ("visits", "leave_ts"),
]


def _to_naive_local(val):
    """'2026-06-21T00:21:10.372317+00:00' -> '2026-06-21T00:21:10' (local)."""
    return store._iso(datetime.fromisoformat(val).timestamp())


def normalize_legacy_timestamps(conn):
    """Rewrite any offset-bearing timestamp to canonical naive-local. Returns
    the number of cells changed. Idempotent."""
    changed = 0
    with store._lock:
        for table, col in TS_COLUMNS:
            rows = conn.execute(
                f"SELECT rowid AS rid, {col} AS v FROM {table} "
                f"WHERE {col} LIKE '%+%' OR {col} LIKE '%Z'").fetchall()
            for r in rows:
                conn.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?",
                             (_to_naive_local(r["v"]), r["rid"]))
                changed += 1
        conn.commit()
    return changed


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "meowant.db"
    conn = store.connect(db)
    conn.execute("PRAGMA busy_timeout=10000")  # wait out the daemon's writes
    n = normalize_legacy_timestamps(conn)
    print(f"normalized {n} legacy timestamp cell(s) in {db}")


if __name__ == "__main__":
    main()
