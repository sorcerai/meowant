"""Backfill captures.is_ir for rows where it's NULL (the existing week of data
predates is_ir detection). Idempotent: only touches NULL rows."""
import os
import sys

from mw import store
from mw.imgutil import is_grayscale


def backfill(conn):
    with store._lock:
        rows = [(r["id"], r["path"]) for r in
                conn.execute("SELECT id, path FROM captures WHERE is_ir IS NULL").fetchall()]
    updates = []
    for cid, path in rows:
        if not os.path.exists(path):
            continue
        g = is_grayscale(path)
        if g is None:
            continue
        updates.append((g, cid))  # sqlite3 converts bool -> 0/1
    if updates:
        with store._lock:
            conn.executemany("UPDATE captures SET is_ir=? WHERE id=?", updates)
            conn.commit()
    return len(updates)


if __name__ == "__main__":
    conn = store.connect(sys.argv[1] if len(sys.argv) > 1 else "meowant.db")
    print(f"[backfill_is_ir] updated {backfill(conn)} rows")
