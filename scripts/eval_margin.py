"""Leave-one-visit-out margin sweep for the argmax+margin decoder.

Embeds the trusted frames once (cached to gallery_emb_cache.npz), then sweeps the
margin gate to print the commit / abstain / WRONG-cat tradeoff per imaging mode.
The wrong-cat rate is the safety number; pick the smallest margin that keeps it
under target while committing usefully.
"""
import os, sqlite3, sys
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mw import gallery as G

DB = os.path.expanduser("~/repos/meowant/meowant.db")
BASE = os.path.expanduser("~/repos/meowant")
CACHE = os.path.join(BASE, "gallery_emb_cache.npz")


def get_embeddings():
    if os.path.exists(CACHE):
        d = np.load(CACHE, allow_pickle=True)
        return list(zip(d["vid"], d["lab"], d["isir"], d["vec"]))
    from mw.embedder import DinoEmbedder
    conn = sqlite3.connect(DB)
    rows = conn.execute("""SELECT visit_id, label, path, is_ir FROM captures
        WHERE label_source IN ('human','human-propagated')
        AND label IS NOT NULL AND path IS NOT NULL AND visit_id IS NOT NULL""").fetchall()
    emb = DinoEmbedder()
    vid, lab, isir, vec = [], [], [], []
    for v, l, path, ir in rows:
        p = path if os.path.isabs(path) else os.path.join(BASE, path)
        e = emb.embed(p) if os.path.exists(p) else None
        if e is not None:
            vid.append(v); lab.append(l); isir.append(bool(ir)); vec.append(e)
    np.savez(CACHE, vid=np.array(vid), lab=np.array(lab),
             isir=np.array(isir), vec=np.stack(vec))
    print(f"cached {len(vid)} embeddings -> {CACHE}")
    return list(zip(vid, lab, isir, np.stack(vec)))


def main():
    data = get_embeddings()
    print(f"{len(data)} embedded frames")
    visits = sorted(set(int(d[0]) for d in data))

    def profile(margin, floor, ff):
        commit = abstain = wrong = scored = 0
        for held in visits:
            held_fr = [(int(l), v) for vid, l, ir, v in data if int(vid) == held and ff(ir)]
            if not held_fr:
                continue
            true = held_fr[0][0]
            train = defaultdict(list); tg = defaultdict(list)
            for vid, l, ir, v in data:
                if int(vid) != held:
                    train[int(l)].append(v); tg[int(l)].append(int(vid))
            gg = G.build_gallery(train, alpha=0.1, groups_by_cat=tg)
            if true not in gg.centroids:
                continue
            q = np.mean([v for _, v in held_fr], axis=0)
            cid, _ = gg.classify_nn(q, margin=margin, floor=floor)
            scored += 1
            if cid is None: abstain += 1
            elif cid == true: commit += 1
            else: wrong += 1
        return scored, commit, abstain, wrong

    for tag, ff in (("ALL", lambda i: True), ("DAYTIME", lambda i: not i), ("IR", lambda i: i)):
        print(f"\n=== {tag} (visit-level LOVO) ===")
        print(f"  {'margin':>6} {'commit':>7} {'abstain':>8} {'WRONG':>6} {'prec':>6}")
        for margin in (0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.15):
            n, c, a, w = profile(margin, floor=0.0, ff=ff)
            if not n: continue
            prec = c / (c + w) * 100 if (c + w) else 100.0
            print(f"  {margin:6.2f} {c/n*100:6.0f}% {a/n*100:7.0f}% {w/n*100:5.0f}% {prec:5.0f}%")


if __name__ == "__main__":
    main()
