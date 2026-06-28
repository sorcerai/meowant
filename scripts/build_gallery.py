"""Build the DINOv2-S conformal gallery artifact from trusted labels, and report
the leave-one-VISIT-out conformal safety profile (commit / abstain / WRONG-cat).

Usage:  PYTHONPATH=. python3 scripts/build_gallery.py [--alpha 0.1] [--out gallery.npz]

The wrong-cat rate is the number that matters: conformal abstain should push the
Garfield/Ucok collision into 'abstain', not 'confident wrong'. Trusted labels are
propagated (correlated within a visit), so the eval groups by visit.
"""
import argparse, os, sqlite3
from collections import defaultdict
import numpy as np

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mw import gallery as G
from mw.embedder import DinoEmbedder

DB = os.path.expanduser("~/repos/meowant/meowant.db")
BASE = os.path.expanduser("~/repos/meowant")


def load(conn):
    cats = {r[0]: r[1] for r in conn.execute("SELECT id,name FROM cats")}
    rows = conn.execute("""SELECT visit_id, label, path, is_ir FROM captures
                           WHERE label_source IN ('human','human-propagated')
                           AND label IS NOT NULL AND path IS NOT NULL AND visit_id IS NOT NULL""").fetchall()
    out = []
    for vid, lab, path, isir in rows:
        p = path if os.path.isabs(path) else os.path.join(BASE, path)
        if os.path.exists(p):
            out.append((vid, lab, bool(isir), p))
    return cats, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alpha", type=float, default=0.1)
    ap.add_argument("--out", default=os.path.join(BASE, "gallery.npz"))
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    cats, rows = load(conn)
    print(f"embedding {len(rows)} trusted frames (DINOv2-S)...")
    emb = DinoEmbedder()
    vecs = []
    for vid, lab, isir, p in rows:
        v = emb.embed(p)
        if v is not None:
            vecs.append((vid, lab, isir, v))
    print(f"embedded {len(vecs)} frames")

    by_cat = defaultdict(list)
    grp_by_cat = defaultdict(list)
    for vid, lab, isir, v in vecs:
        by_cat[lab].append(v)
        grp_by_cat[lab].append(vid)            # visit id = calibration group
    g = G.build_gallery(by_cat, alpha=args.alpha, groups_by_cat=grp_by_cat)
    # Calibrated operating point (leave-one-visit-out, scripts/eval_margin.py):
    #   floor 0.40 (drops OOD/junk frames that else default to Garfield),
    #   color margin 0.04 -> 78% commit @ 0% wrong; IR margin 0.05 -> 71% commit,
    #   ~5% residual (Garfield/Ucok IR collision -> abstains to deadman/co-presence).
    #   The floor — not a tight IR margin — is what controls wrong-cat; loosening
    #   IR margin from 0.10->0.05 doubled IR commits at the same wrong rate.
    g.margin_color, g.margin_ir, g.floor = 0.04, 0.05, 0.40
    g.save(args.out)
    print(f"\nsaved gallery -> {args.out}  (alpha={args.alpha})")
    for cid in g.cats:
        print(f"   {cats.get(cid,cid):10} n={len(by_cat[cid]):3}  tau={g.tau[cid]:.4f}")

    # ---- leave-one-visit-out conformal safety profile ----
    # NOTE: this profile uses the OFFLINE conformal singleton rule (gg.classify),
    # which is NOT what production runs. The deployed decoder is the argmax+margin
    # rule (Gallery.classify_nn); for the operating-point profile that matches
    # production, see scripts/eval_margin.py. This print is diagnostic only.
    print("\n=== leave-one-visit-out conformal profile [OFFLINE rule, not prod] ===")
    visits = sorted(set(v[0] for v in vecs))
    for tag, ff in (("ALL", lambda i: True), ("DAYTIME", lambda i: not i), ("IR", lambda i: i)):
        commit = abstain = wrong = scored = 0
        for held in visits:
            held_frames = [(lab, v) for vid, lab, isir, v in vecs if vid == held and ff(isir)]
            if not held_frames:
                continue
            true = held_frames[0][0]
            train = defaultdict(list)
            train_grp = defaultdict(list)
            for vid2, lab2, isir2, v2 in vecs:
                if vid2 != held:
                    train[lab2].append(v2)
                    train_grp[lab2].append(vid2)
            gg = G.build_gallery(train, alpha=args.alpha, groups_by_cat=train_grp)
            if true not in gg.centroids:
                continue
            q = np.mean([v for _, v in held_frames], axis=0)
            cid, conf = gg.classify(q)
            scored += 1
            if cid is None:
                abstain += 1
            elif cid == true:
                commit += 1
            else:
                wrong += 1
        if scored:
            print(f"  [{tag:7}] n={scored:3}  commit={commit/scored*100:4.0f}%  "
                  f"abstain={abstain/scored*100:4.0f}%  WRONG={wrong/scored*100:4.0f}%  "
                  f"(precision-when-committed={commit/(commit+wrong)*100:.0f}%)" if (commit+wrong) else
                  f"  [{tag:7}] n={scored:3}  commit=0 abstain={abstain}")


if __name__ == "__main__":
    main()
