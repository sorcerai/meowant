#!/usr/bin/env python3
"""Materialize a browsable per-cat view of labeled frames under
gallery/labeled/<cat>/ as symlinks. Labels live in the DB (captures.label);
this is just a human-friendly mirror (and a class-subfolder layout an ML
pipeline can train from). Re-run anytime to refresh.

    python3 export_labeled.py
"""
import os
import shutil

from mw import store

DB = "meowant.db"
OUT = "gallery/labeled"


def main():
    conn = store.connect(DB)
    store.init_db(conn)
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)                       # rebuild fresh each run
    with store._lock:
        rows = conn.execute(
            "SELECT cap.path AS path, c.name AS name FROM captures cap "
            "JOIN cats c ON c.id = cap.label WHERE cap.label IS NOT NULL").fetchall()
    counts = {}
    for r in rows:
        src = os.path.abspath(r["path"])
        if not os.path.exists(src):
            continue
        d = os.path.join(OUT, r["name"])
        os.makedirs(d, exist_ok=True)
        link = os.path.join(d, os.path.basename(r["path"]))
        if not os.path.lexists(link):
            os.symlink(src, link)
        counts[r["name"]] = counts.get(r["name"], 0) + 1
    for cat, n in sorted(counts.items()):
        print(f"  {cat:10s} {n}")
    print(f"-> {os.path.abspath(OUT)}")


if __name__ == "__main__":
    main()
