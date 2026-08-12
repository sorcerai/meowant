"""Model bake-off for IR cat re-ID: does a different embedder cut the IR
tabby-collision wrong-rate that forces DINOv2 to abstain at night?

For each candidate model: embed the trusted corpus (same SSDLite cat-crop, model's
own timm transform, num_classes=0 pooled, L2-normalized), then run the SAME
leave-one-VISIT-out eval as scripts/eval_margin.py (production classify_nn rule),
split DAYTIME vs IR. The number that matters is IR WRONG-cat at a useful commit
rate — that's what the production floor is paying to suppress.

Usage: PYTHONPATH=. python3 scripts/bakeoff_ir.py [model_spec ...]
Validates by reproducing the cached DINOv2 baseline first.
"""
import os, sqlite3, sys
from collections import defaultdict
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mw import gallery as G
from mw.embedder import DinoEmbedder

DB = os.path.expanduser("~/repos/meowant/meowant.db")
BASE = os.path.expanduser("~/repos/meowant")

# (label, timm_model_name, cache_file)
MODELS = {
    "dinov2-s":  "vit_small_patch14_dinov2.lvd142m",
    "dinov3-s":  "vit_small_patch16_dinov3",
    "megadesc-t": "hf-hub:BVRA/MegaDescriptor-T-224",
}


def trusted_rows():
    conn = sqlite3.connect(DB)
    rows = conn.execute("""SELECT visit_id, label, path, is_ir FROM captures
        WHERE label_source IN ('human','human-propagated')
        AND label IS NOT NULL AND path IS NOT NULL AND visit_id IS NOT NULL""").fetchall()
    out = []
    for vid, lab, path, isir in rows:
        p = path if os.path.isabs(path) else os.path.join(BASE, path)
        if os.path.exists(p):
            out.append((int(vid), int(lab), int(bool(isir)), p))
    return out


def embed_corpus(model_name, rows, cache):
    if os.path.exists(cache):
        d = np.load(cache, allow_pickle=True)
        return list(zip(d["vid"], d["lab"], d["isir"], d["vec"]))
    emb = DinoEmbedder(model_name=model_name)
    vid, lab, isir, vec = [], [], [], []
    for i, (v, l, ir, p) in enumerate(rows):
        e = emb.embed(p)
        if e is not None:
            vid.append(v); lab.append(l); isir.append(ir); vec.append(e)
        if (i + 1) % 100 == 0:
            print(f"    embedded {i+1}/{len(rows)}", file=sys.stderr)
    vec = np.array(vec)
    np.savez(cache, vid=np.array(vid), lab=np.array(lab), isir=np.array(isir), vec=vec)
    return list(zip(vid, lab, isir, vec))


def lovo(data, ff, margin, floor):
    visits = sorted(set(int(d[0]) for d in data))
    commit = abstain = wrong = scored = 0
    for held in visits:
        held_fr = [(int(l), v) for vid, l, ir, v in data if int(vid) == held and ff(int(ir))]
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


def report(label, data):
    print(f"\n########## {label}  ({len(data)} frames) ##########")
    for tag, ff in (("DAYTIME", lambda i: not i), ("IR", lambda i: i)):
        print(f"  --- {tag} ---")
        print(f"  {'margin':>6} {'commit':>7} {'abstain':>8} {'WRONG':>6} {'prec':>6}")
        for margin in (0.0, 0.04, 0.06, 0.08, 0.10):
            n, c, a, w = lovo(data, ff, margin, floor=0.0)
            if not n: continue
            prec = c / (c + w) * 100 if (c + w) else 100.0
            print(f"  {margin:6.2f} {c/n*100:6.0f}% {a/n*100:7.0f}% {w/n*100:5.0f}% {prec:5.0f}%")


def main():
    rows = trusted_rows()
    print(f"trusted corpus: {len(rows)} frames")
    picks = sys.argv[1:] or list(MODELS.keys())
    for label in picks:
        name = MODELS.get(label, label)
        cache = os.path.join(BASE, f"bakeoff_emb_{label}.npz")
        print(f"\n[{label}] embedding with {name} ...", file=sys.stderr)
        try:
            data = embed_corpus(name, rows, cache)
            report(label, data)
        except Exception as e:
            print(f"[{label}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
