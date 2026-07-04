#!/usr/bin/env python3
"""autolabel.py — drive the auto-labeler (the 'teacher' that names cats in frames).

    python3 autolabel.py --validate   # score claude -p vs our human labels (trust gate)
    python3 autolabel.py --dry-run    # propose labels for unlabeled visits, apply nothing
    python3 autolabel.py --once       # sweep once and auto-apply confident calls
    python3 autolabel.py --accuracy   # trust-channel scoreboard + recent auto-labels

The daemon also runs this worker continuously; this CLI is for validation and
on-demand sweeps.
"""
import argparse

from mw import store, config
from mw.labeler import ClaudeCliLabeler, AgyLabeler, LlamaCppLabeler, FallbackLabeler
from mw.autolabel import AutoLabeler, discover_refs, validate
from mw.catfilter import TorchvisionCatFilter, NullCatFilter
from mw.roi import RoiCropper, load_rois

DB = "meowant.db"
GALLERY = "gallery"
CONFIG = "config.json"


def _make_labeler(backend, model):
    if backend == "agy":
        # strong teacher (82% validated) falling back to local Gemma if Gemini/Agy fails
        return FallbackLabeler(AgyLabeler(), LlamaCppLabeler())
    return ClaudeCliLabeler(model=model)    # cheaper fallback (haiku ~45%)


def _setup(backend, model, use_filter=True):
    conn = store.connect(DB)
    store.init_db(conn)
    cats = list(store.gallery_counts(conn).keys())
    refs = discover_refs(GALLERY, cats)
    labeler = _make_labeler(backend, model)
    catfilter = TorchvisionCatFilter() if use_filter else NullCatFilter()
    # Same per-camera ROI as the daemon's auto-labeler — without it, a manual
    # sweep here reintroduces bystander theft (bowl cat stealing box visits).
    cfg = config.load(CONFIG)
    roi_cropper = RoiCropper(load_rois(config.get(cfg, "cameras", [])))
    return conn, AutoLabeler(conn, labeler, refs, cats, catfilter=catfilter,
                              roi_cropper=roi_cropper), labeler, refs, cats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true", help="score vs human labels")
    ap.add_argument("--dry-run", action="store_true", help="propose, apply nothing")
    ap.add_argument("--once", action="store_true", help="sweep once and auto-apply")
    ap.add_argument("--accuracy", action="store_true", help="trust-channel scoreboard")
    ap.add_argument("--backend", default="agy", choices=["agy", "claude"],
                    help="labeler backend (default: agy — stronger)")
    ap.add_argument("--model", default="haiku", help="claude model when --backend claude")
    ap.add_argument("--no-filter", action="store_true", help="disable the cat/no-cat pre-filter")
    args = ap.parse_args()

    conn, al, labeler, refs, cats = _setup(args.backend, args.model, use_filter=not args.no_filter)
    print(f"refs: " + ", ".join(f"{k}×{len(v)}" for k, v in refs.items()))

    if args.accuracy:
        acc = store.labeler_accuracy(conn)
        pct = f"{acc['auto_accuracy']*100:.0f}%" if acc["auto_accuracy"] is not None else "n/a"
        print(f"human={acc['human']}  auto={acc['auto']}  corrected={acc['corrected']}  "
              f"auto-accuracy={pct}")
        for r in store.recent_auto_labels(conn, 20):
            print(f"  [{r['label_source']}] {r['cat']:9s} {r['path']}")
        return

    if args.validate:
        rep = validate(conn, labeler, refs, cats)
        pct = f"{rep['accuracy']*100:.0f}%" if rep["accuracy"] is not None else "n/a"
        print(f"validation: {rep['correct']}/{rep['total']} correct ({pct})")
        for path, human, pred in rep["wrong"]:
            print(f"  MISS: said {pred!r}, truth {human!r}  {path}")
        return

    results = al.run_once(dry_run=args.dry_run)
    tag = "DRY-RUN" if args.dry_run else "applied"
    for r in results:
        print(f"  visit {r['visit']}: {r['status']}"
              + (f" -> {r['cat']} ({tag} {r['applied']})" if r["cat"] else "")
              + (f"  CONFLICT {r['cats']}" if r["status"] == "conflict" else ""))
    if not results:
        print("  nothing unlabeled.")


if __name__ == "__main__":
    main()
