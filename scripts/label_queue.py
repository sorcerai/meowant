"""Rank recent unlabeled visits by how much gallery value a human label would add.

Why this exists: gallery.npz was built 2026-06-27 and every trusted frame predates
Jun 28, so the matcher scores current frames at 0.21-0.35 (floor is 0.40) and
abstains on everything. The fix is human labels on CURRENT-scene frames, then a
gallery rebuild. But a label is only worth taps if the visit actually contains a
frame the matcher could ever match: the gallery is "cat in the box, large and
close", and most captured frames are an empty box or a cat blurring past.

So rank by the same criterion the matcher uses — mw.embedder crops with SSDLite
(COCO cat, det_thresh 0.3) and falls back to the WHOLE FRAME when it finds no
cat, which is exactly why those frames score like background. A visit is worth
labeling when SSDLite finds a big cat box in at least one of its frames.

Ranking is IR-first (night attribution is what's broken) and boosts visits the
teacher thinks are Cat 2 (only 14 trusted IR frames, the starved class).

Usage: PYTHONPATH=. python3 scripts/label_queue.py [--since 2026-06-28] [--top 25]
Writes label_queue.json; prints a table. Read-only w.r.t. the DB.
"""
import argparse, json, os, sqlite3, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "meowant.db")
_COCO_CAT = 17


def candidate_visits(conn, since):
    """Eliminated visits since `since` that have frames on disk and no human
    label yet. Already-labeled visits are skipped — re-labeling them adds no
    new gallery rows."""
    rows = conn.execute(
        """SELECT v.id, v.enter_ts,
             (SELECT COUNT(*) FROM captures c WHERE c.visit_id=v.id
                AND c.label_source IN ('human','human-propagated')) AS human
           FROM visits v
           WHERE v.eliminated=1 AND v.enter_ts >= ?
           ORDER BY v.id DESC""", (since,)).fetchall()
    return [(r[0], r[1]) for r in rows if not r[2]]


def visit_frames(conn, vid):
    """Frames the teacher believes hold a cat, newest-labeled first. auto-none
    is excluded: the teacher already examined those and found nothing, and the
    daily pruner deletes them anyway."""
    rows = conn.execute(
        """SELECT path, pred, COALESCE(is_ir,0), label_source FROM captures
           WHERE visit_id=? AND path IS NOT NULL
             AND label_source IN ('auto','auto-conflict')""", (vid,)).fetchall()
    return [r for r in rows if os.path.exists(r[0])]


def subsample(seq, n):
    """Evenly spaced picks so a visit is sampled across its whole span rather
    than only its opening seconds."""
    if len(seq) <= n:
        return seq
    step = len(seq) / float(n)
    return [seq[int(i * step)] for i in range(n)]


class Detector:
    """SSDLite cat detector, mirroring mw.embedder.DinoEmbedder's crop stage."""

    def __init__(self, det_thresh=0.3):
        self.det_thresh = det_thresh
        self._det = None
        self._device = None

    def _ensure(self):
        if self._det is not None:
            return
        import torch, torchvision
        self._device = "mps" if torch.backends.mps.is_available() else "cpu"
        w = torchvision.models.detection.SSDLite320_MobileNet_V3_Large_Weights.DEFAULT
        self._det = (torchvision.models.detection
                     .ssdlite320_mobilenet_v3_large(weights=w)
                     .eval().to(self._device))

    def best_cat(self, path):
        """(score, area_fraction) of the highest-scoring cat box, or (0,0)."""
        import torch
        from PIL import Image
        from torchvision.transforms.functional import to_tensor
        try:
            self._ensure()
            pil = Image.open(path).convert("RGB")
        except Exception:
            return (0.0, 0.0)
        W, H = pil.size
        with torch.no_grad():
            pred = self._det([to_tensor(pil).to(self._device)])[0]
        best, bs = None, self.det_thresh
        for b, l, s in zip(pred["boxes"], pred["labels"], pred["scores"]):
            if int(l) == _COCO_CAT and float(s) > bs:
                best, bs = b, float(s)
        if best is None:
            return (0.0, 0.0)
        x0, y0, x1, y1 = [float(v) for v in best.tolist()]
        area = max(0.0, x1 - x0) * max(0.0, y1 - y0) / float(W * H)
        return (bs, area)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-28",
                    help="only visits at/after this date (gallery was built Jun 27)")
    ap.add_argument("--scan-per-visit", type=int, default=10)
    ap.add_argument("--min-area", type=float, default=0.05,
                    help="min cat-box area fraction for a visit to be worth labeling")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--out", default=os.path.join(BASE, "label_queue.json"))
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cands = candidate_visits(conn, args.since)
    print(f"candidate visits since {args.since}: {len(cands)}", file=sys.stderr)

    det = Detector()
    scored = []
    for i, (vid, ets) in enumerate(cands):
        frames = visit_frames(conn, vid)
        if not frames:
            continue
        picks = subsample(frames, args.scan_per_visit)
        rated = []
        for path, pred, is_ir, src in picks:
            s, area = det.best_cat(path)
            if area > 0:
                rated.append(dict(path=path, pred=pred, is_ir=int(is_ir),
                                  det=round(s, 3), area=round(area, 4)))
        if (i + 1) % 10 == 0:
            print(f"  scanned {i+1}/{len(cands)}", file=sys.stderr)
        if not rated:
            continue
        rated.sort(key=lambda r: -r["area"])
        best = rated[0]
        if best["area"] < args.min_area:
            continue
        ir_frac = sum(r["is_ir"] for r in rated) / len(rated)
        preds = [r["pred"] for r in rated if r["pred"] is not None]
        scored.append(dict(
            visit_id=vid, enter_ts=ets, quality=best["area"],
            best_det=best["det"], ir_frac=round(ir_frac, 2),
            teacher_preds=sorted(set(preds)),
            n_rated=len(rated), best_frames=[r["path"] for r in rated[:3]]))

    def rank(v):
        # IR first (night is the broken regime), then Cat 2 (14 trusted IR
        # frames — the starved class), then biggest/clearest cat.
        return (-(v["ir_frac"] > 0.5), -(2 in v["teacher_preds"]), -v["quality"])

    scored.sort(key=rank)
    queue = scored[: args.top]
    with open(args.out, "w") as f:
        json.dump(queue, f, indent=1)

    print(f"\nqueue: {len(queue)} visits (of {len(scored)} above min-area)  -> {args.out}\n")
    print(f"{'#':>3} {'visit':>6} {'when':16} {'area':>6} {'det':>5} {'ir':>5} {'teacher':>9} {'frames':>6}")
    for i, v in enumerate(queue, 1):
        print(f"{i:3} {v['visit_id']:6} {v['enter_ts'][:16]:16} {v['quality']:6.3f} "
              f"{v['best_det']:5.2f} {v['ir_frac']:5.2f} {str(v['teacher_preds']):>9} {v['n_rated']:6}")
    if scored:
        ir = sum(1 for v in queue if v["ir_frac"] > 0.5)
        c2 = sum(1 for v in queue if 2 in v["teacher_preds"])
        print(f"\nIR visits in queue: {ir}/{len(queue)}   with a Cat-2 teacher guess: {c2}")


if __name__ == "__main__":
    main()
