"""Cross-camera validation — leave-one-CAMERA-out.

The decisive test of the scene-cueing worry: build per-cat centroids from frames
on all OTHER cameras, then classify the held-out camera's visits (tracklet-
averaged). If accuracy holds vs the mixed-camera bake-off (~95%), the embedder is
reading the CAT. If it collapses, it was keying on box/background. Runs both
embedders so we can see whether DINOv2's lead survives a background shift.

Litter cams meowcam1-4 only (meowcam5 has 1 frame). Reuses the trusted 453 labels.
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
CAMS = {"meowcam1", "meowcam2", "meowcam3", "meowcam4"}

def load_rows():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    cats = {r["id"]: r["name"] for r in c.execute("SELECT id,name FROM cats")}
    rows = c.execute("""SELECT visit_id, label, path, is_ir, camera FROM captures
                        WHERE label_source IN ('human','human-propagated')
                        AND label IS NOT NULL AND path IS NOT NULL AND visit_id IS NOT NULL""").fetchall()
    out = []
    for r in rows:
        if r["camera"] not in CAMS: continue
        p = r["path"] if os.path.isabs(r["path"]) else os.path.join(BASE, r["path"])
        if os.path.exists(p):
            out.append((r["visit_id"], cats[r["label"]], bool(r["is_ir"]), r["camera"], p))
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

def centroids(by_cat):
    out = {}
    for cat, vecs in by_cat.items():
        if vecs:
            m = np.mean(vecs, axis=0); out[cat] = m / (np.linalg.norm(m) + 1e-9)
    return out

def run(name, rows):
    emb = embedder(name)
    data = [(vid, cat, isir, cam, emb(crop(Image.open(p).convert("RGB")))) for vid, cat, isir, cam, p in rows]
    cams = sorted(set(d[3] for d in data))
    classes = sorted(set(d[1] for d in data))
    print(f"\n{'='*64}\n### {name}  (frames={len(data)})\n{'='*64}")

    overall_ok = overall_tot = 0
    for held in cams:
        # centroids from OTHER cameras
        train = defaultdict(list)
        for vid, cat, isir, cam, v in data:
            if cam != held: train[cat].append(v)
        cen = centroids(train)
        # classify held camera's VISITS (tracklet-averaged)
        held_visits = defaultdict(list)
        for vid, cat, isir, cam, v in data:
            if cam == held: held_visits[vid].append((cat, v))
        ok = tot = 0; conf = defaultdict(lambda: defaultdict(int))
        for vid, items in held_visits.items():
            true = items[0][0]
            if true not in cen: continue
            q = np.mean([v for _, v in items], axis=0); q /= (np.linalg.norm(q)+1e-9)
            pred = max(cen, key=lambda k: float(q @ cen[k]))
            ok += (pred == true); tot += 1; conf[true][pred]+=1
        overall_ok += ok; overall_tot += tot
        if tot:
            line = ", ".join(f"{c}:" + "/".join(f"{k}{conf[c][k]}" for k in sorted(conf[c])) for c in classes if conf.get(c))
            print(f"  held-out {held}: {ok}/{tot} visits = {ok/tot*100:.0f}%   [{line}]")
    if overall_tot:
        print(f"  >>> CROSS-CAMERA visit-level: {overall_ok}/{overall_tot} = {overall_ok/overall_tot*100:.1f}%")
    return overall_ok/overall_tot*100 if overall_tot else 0

if __name__ == "__main__":
    rows = load_rows()
    from collections import Counter
    print(f"loaded {len(rows)} trusted frames on litter cams; device={DEV}")
    print("frames/cam:", dict(Counter(r[3] for r in rows)))
    res = {}
    for name in ["vit_small_patch14_dinov2.lvd142m", "hf-hub:BVRA/MegaDescriptor-T-224"]:
        res[name] = run(name, rows)
    print("\n=== SUMMARY (cross-camera visit-level acc) ===")
    print("  reference: mixed-camera leave-one-visit-out was DINOv2 94.9% / Mega 89.7%")
    for k, v in res.items():
        print(f"  {k.split('/')[-1]:32} {v:.1f}%")
