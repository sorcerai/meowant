"""Bake-off v2 — leave-one-VISIT-out (no within-visit leakage), 453-label set.

Tests the real deployment question: given a visit you've never seen, can the
embedder name the cat? Centroids are built from all OTHER visits; the held-out
visit is tracklet-averaged (mean of its frame embeddings) and matched to the
nearest cat centroid. Frame-level LOVO is also reported (each frame scored
against centroids that exclude its own visit).
"""
import os, sqlite3
import numpy as np, torch, torchvision, timm
from torchvision.transforms.functional import to_tensor
from timm.data import resolve_model_data_config, create_transform
from PIL import Image
from collections import defaultdict

DB = os.path.expanduser("~/repos/meowant/meowant.db")
BASE = os.path.expanduser("~/repos/meowant")
DEV = "mps" if torch.backends.mps.is_available() else "cpu"

def load_rows():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    cats = {r["id"]: r["name"] for r in c.execute("SELECT id,name FROM cats")}
    rows = c.execute("""SELECT visit_id, label, path, is_ir FROM captures
                        WHERE label_source IN ('human','human-propagated')
                        AND label IS NOT NULL AND path IS NOT NULL AND visit_id IS NOT NULL""").fetchall()
    out = []
    for r in rows:
        p = r["path"] if os.path.isabs(r["path"]) else os.path.join(BASE, r["path"])
        if os.path.exists(p):
            out.append((r["visit_id"], cats[r["label"]], bool(r["is_ir"]), p))
    return out

_DET = None
def crop(pil):
    global _DET
    if _DET is None:
        w = torchvision.models.detection.SSDLite320_MobileNet_V3_Large_Weights.DEFAULT
        _DET = torchvision.models.detection.ssdlite320_mobilenet_v3_large(weights=w).eval().to(DEV)
    with torch.no_grad(): pr = _DET([to_tensor(pil).to(DEV)])[0]
    best, bs = None, 0.3
    for b, l, s in zip(pr["boxes"], pr["labels"], pr["scores"]):
        if int(l) == 17 and float(s) > bs: best, bs = b, float(s)
    if best is None: return pil
    x0, y0, x1, y1 = [int(v) for v in best.tolist()]; W, H = pil.size
    return pil.crop((max(0, x0), max(0, y0), min(W, x1), min(H, y1)))

def embedder(name):
    m = timm.create_model(name, pretrained=True, num_classes=0).eval().to(DEV)
    tf = create_transform(**resolve_model_data_config(m), is_training=False)
    def emb(pil):
        x = tf(pil.convert("RGB")).unsqueeze(0).to(DEV)
        with torch.no_grad(): f = m(x)
        return torch.nn.functional.normalize(f, dim=-1).squeeze(0).float().cpu().numpy()
    return emb

def centroids(emb_by_cat):
    out = {}
    for cat, vecs in emb_by_cat.items():
        if vecs:
            m = np.mean(vecs, axis=0); out[cat] = m / (np.linalg.norm(m) + 1e-9)
    return out

def run(name, rows):
    emb = embedder(name)
    data = []  # (visit, cat, is_ir, vec)
    for vid, cat, isir, p in rows:
        data.append((vid, cat, isir, emb(crop(Image.open(p).convert("RGB")))))
    classes = sorted(set(d[1] for d in data))
    visits = sorted(set(d[0] for d in data))
    dim = len(data[0][3])
    print(f"\n{'='*64}\n### {name}  (dim={dim}, frames={len(data)}, visits={len(visits)})\n{'='*64}")

    # ---- VISIT-level LOVO (tracklet-averaged) ----
    def visit_eval(frame_filter):
        conf = defaultdict(lambda: defaultdict(int)); ok = tot = 0
        for held in visits:
            held_frames = [d for d in data if d[0] == held and frame_filter(d)]
            if not held_frames: continue
            true = held_frames[0][1]
            train = defaultdict(list)
            for vid, cat, isir, v in data:
                if vid != held: train[cat].append(v)
            cen = centroids(train)
            if true not in cen: continue   # cat has no other visit -> can't score
            q = np.mean([d[3] for d in held_frames], axis=0); q /= (np.linalg.norm(q)+1e-9)
            pred = max(cen, key=lambda k: float(q @ cen[k]))
            ok += (pred == true); tot += 1; conf[true][pred] += 1
        return ok, tot, conf

    for tag, ff in (("ALL", lambda d: True), ("DAYTIME", lambda d: not d[2]), ("IR", lambda d: d[2])):
        ok, tot, conf = visit_eval(ff)
        if not tot:
            print(f"\n[visit-level {tag}] no scorable visits"); continue
        print(f"\n[visit-level {tag}]  acc={ok/tot*100:.1f}%  ({ok}/{tot} visits)")
        for c in classes:
            row = conf.get(c)
            if row: print(f"    {c:9} -> " + ", ".join(f"{k}:{v}" for k,v in sorted(row.items())))

    # ---- FRAME-level LOVO (each frame vs centroids excluding its visit) ----
    # precompute per-cat sum so we can exclude a visit cheaply
    for tag, ff in (("DAYTIME", lambda d: not d[2]), ("IR", lambda d: d[2])):
        conf = defaultdict(lambda: defaultdict(int)); ok = tot = 0
        for vid, cat, isir, v in data:
            if not ff((vid,cat,isir,v)): continue
            train = defaultdict(list)
            for vid2, cat2, isir2, v2 in data:
                if vid2 != vid: train[cat2].append(v2)
            cen = centroids(train)
            if cat not in cen: continue
            pred = max(cen, key=lambda k: float(v @ cen[k]))
            ok += (pred == cat); tot += 1; conf[cat][pred]+=1
        if tot:
            print(f"\n[frame-level {tag}]  acc={ok/tot*100:.1f}%  ({ok}/{tot} frames)")
            for c in classes:
                row=conf.get(c)
                if row: print(f"    {c:9} -> " + ", ".join(f"{k}:{v}" for k,v in sorted(row.items())))

if __name__ == "__main__":
    rows = load_rows()
    from collections import Counter
    print(f"loaded {len(rows)} trusted frames; device={DEV}")
    print("frames/cat:", dict(Counter(r[1] for r in rows)))
    print("visits/cat:", {cat: len(set(r[0] for r in rows if r[1]==cat)) for cat in set(r[1] for r in rows)})
    for name in ["hf-hub:BVRA/MegaDescriptor-T-224", "vit_small_patch14_dinov2.lvd142m"]:
        try: run(name, rows)
        except Exception as e:
            import traceback; traceback.print_exc()
