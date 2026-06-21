#!/usr/bin/env python3
"""label.py — tag captured frames with the cat that made them (builds the Phase-3 gallery).

The recognizer learns from these labels, so this is the ~15-min bootstrap step
the design calls for. Three modes:

    python3 label.py --status          # how built-out is the gallery?
    python3 label.py                   # interactive: walk unlabeled frames, tag each
    python3 label.py --from-tsv f.tsv  # batch: lines of "<capture_id>\\t<cat_name>"

Interactive keys per frame: a cat name (Ucok/Garfield/Ella), 'o' to open the
image, 's' to skip, 'q' to quit. Frames live in gallery/captures/.
"""
import argparse
import subprocess
import sys

from mw import store

DB = "meowant.db"


def show_status(conn):
    counts = store.gallery_counts(conn)
    pending = len(store.unlabeled_captures(conn))
    print("Gallery (labeled frames per cat):")
    for name, n in counts.items():
        print(f"  {name:10s} {n}")
    print(f"Unlabeled frames waiting: {pending}")


def batch_from_tsv(conn, path):
    applied, bad = 0, 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                cap_id_s, name = line.split("\t")
                cid = store.cat_id_by_name(conn, name.strip())
                if cid is None:
                    print(f"  ! unknown cat {name!r} (line: {line})", file=sys.stderr)
                    bad += 1
                    continue
                store.set_capture_label(conn, int(cap_id_s), cid)
                applied += 1
            except ValueError:
                print(f"  ! malformed line: {line!r}", file=sys.stderr)
                bad += 1
    print(f"Labeled {applied} frame(s), {bad} skipped.")


def interactive(conn):
    names = list(store.gallery_counts(conn).keys())
    print(f"Cats: {', '.join(names)}   |   o=open  s=skip  q=quit\n")
    pending = store.unlabeled_captures(conn)
    if not pending:
        print("No unlabeled frames — nothing to do. (Capture builds these per visit.)")
        return
    for cap in pending:
        while True:
            ans = input(f"[{cap['id']}] {cap['camera']}  {cap['path']}  -> ").strip()
            if ans == "q":
                return
            if ans == "s" or ans == "":
                break
            if ans == "o":
                subprocess.run(["open", cap["path"]], check=False)  # macOS preview
                continue
            cid = store.cat_id_by_name(conn, ans)
            if cid is None:
                print(f"    unknown cat {ans!r}; try one of: {', '.join(names)}")
                continue
            store.set_capture_label(conn, cap["id"], cid)
            break


def main():
    ap = argparse.ArgumentParser(description="Label captured frames to build the cat gallery.")
    ap.add_argument("--status", action="store_true", help="show gallery counts and exit")
    ap.add_argument("--from-tsv", metavar="FILE", help="batch-label from <capture_id>\\t<cat_name> lines")
    args = ap.parse_args()

    conn = store.connect(DB)
    store.init_db(conn)
    if args.status:
        show_status(conn)
    elif args.from_tsv:
        batch_from_tsv(conn, args.from_tsv)
    else:
        interactive(conn)


if __name__ == "__main__":
    main()
