"""Background auto-labeler worker: sweep unlabeled visits, ask the labeler who
the cat is, and apply confident calls to the gallery — every decision recorded
with provenance so the trust channel can audit accuracy.
"""
import glob
import os
import sys
import time

from mw import store
from mw.labeler import decide, ERROR
from mw.catfilter import NullCatFilter


def discover_refs(gallery_dir, cat_names):
    """Map each known cat to ALL its reference photos at gallery/<name>/seed-*
    (case-insensitive dir match). Multiple angles per cat sharpen the hard
    Garfield-vs-Ucok call. Cats without a ref are simply omitted."""
    refs = {}
    for name in cat_names:
        hits = sorted(glob.glob(os.path.join(gallery_dir, name.lower(), "seed-*")))
        if hits:
            refs[name] = hits
    return refs


class AutoLabeler:
    def __init__(self, conn, labeler, refs, valid_cats, now_fn=time.time,
                 catfilter=None):
        self.conn = conn
        self.labeler = labeler
        self.refs = refs
        self.valid_cats = set(valid_cats)
        self.now = now_fn
        self.catfilter = catfilter or NullCatFilter()  # cheap cat/no-cat pre-filter

    def _process_visit(self, vid, rows, dry_run):
        """Examine one visit's untouched frames. EVERY frame gets a verdict
        recorded so it leaves the auto queue: a real label ('auto'), or an
        examined-but-unlabeled marker ('auto-none' empty / 'auto-conflict'
        ambiguous). Returns a summary dict (no DB writes if dry_run)."""
        by_path = {r["path"]: r["id"] for r in rows}
        # Stage 1 — cheap cat/no-cat filter: empties never reach the expensive
        # labeler and are marked examined-empty so the gallery stays clean.
        cat_rows, empty_rows = [], []
        for r in rows:
            if self.catfilter.has_cat(r["path"]):
                cat_rows.append(r)
            else:
                empty_rows.append(r)
        if not dry_run:
            for r in empty_rows:
                store.mark_capture_examined(self.conn, r["id"], "auto-none")
        if not cat_rows:
            return {"visit": vid, "status": "empty", "cat": None,
                    "applied": 0, "cats": [], "filtered": len(empty_rows)}
        # Stage 2 — label the frames that actually contain a cat.
        preds = self.labeler.predict_visit([r["path"] for r in cat_rows], self.refs)
        # A backend failure means our view is incomplete — skip the cat frames
        # WITHOUT marking them, so they retry next sweep (empties already handled).
        if any(p.get("cat") == ERROR for p in preds):
            return {"visit": vid, "status": "error", "cat": None,
                    "applied": 0, "cats": [], "filtered": len(empty_rows)}
        # A human may have already labeled part of this visit — that cat is
        # authoritative; the model may only agree with it, never override.
        established = store.visit_established_cat(self.conn, vid)
        d = decide(preds, self.valid_cats, established)
        applied_paths = {p for p, _, _ in d["apply"]}
        if not dry_run:
            cat_id = store.cat_id_by_name(self.conn, d["cat"]) if d["cat"] else None
            for path, cat, conf in d["apply"]:
                cid = by_path.get(path)
                if cid is not None:
                    store.apply_auto_label(self.conn, cid, cat_id, conf)  # no-clobber
            # Mark examined-but-unlabeled cat frames (minority/none) so they
            # don't get re-fed to the (expensive) model on the next sweep.
            marker = "auto-conflict" if d["status"] == "conflict" else "auto-none"
            for r in cat_rows:
                if r["path"] not in applied_paths:
                    store.mark_capture_examined(self.conn, r["id"], marker)
            # 6v5: attribute the VISIT row too (once per visit, not per frame),
            # so scatter-blame and health baselines read the right cat.
            store.sync_visit_cat(self.conn, vid)
        return {"visit": vid, "status": d["status"], "cat": d["cat"],
                "applied": len(d["apply"]), "cats": d["cats"], "filtered": len(empty_rows)}

    def run_once(self, dry_run=False):
        if not self.valid_cats:
            return []   # cats not seeded yet — don't retire frames as "no cat"
        vids = store.unlabeled_visit_ids(self.conn)
        groups = store.captures_by_visit(self.conn, vids)
        results = []
        for vid in vids:
            # Only UNTOUCHED frames (no label AND no prior auto verdict): an
            # already auto-conflict/auto-none frame must not be re-examined just
            # because a sibling frame arrived later on the same visit.
            rows = [r for r in groups.get(vid, [])
                    if r["label"] is None and r["label_source"] is None]
            if rows:
                results.append(self._process_visit(vid, rows, dry_run))
        return results

    def run(self, interval=300.0):
        while True:
            try:
                res = self.run_once()
                applied = sum(r["applied"] for r in res)
                conflicts = [r["visit"] for r in res if r["status"] == "conflict"]
                if applied or conflicts:
                    msg = f"[autolabel] applied {applied} label(s)"
                    if conflicts:
                        msg += f"; {len(conflicts)} visit(s) need review: {conflicts}"
                    print(msg, file=sys.stderr)
            except Exception as e:  # never let the worker thread die
                print(f"[autolabel] error: {e}", file=sys.stderr)
            time.sleep(interval)


def validate(conn, labeler, refs, valid_cats):
    """Run the labeler over frames a HUMAN already labeled and score it — the
    measure-don't-assert gate before trusting auto-apply. Returns
    {total, correct, wrong: [(path, human, predicted)], accuracy}."""
    with store._lock:
        rows = [dict(r) for r in conn.execute(
            "SELECT cap.id, cap.path, cap.visit_id, c.name AS human "
            "FROM captures cap JOIN cats c ON c.id=cap.label "
            "WHERE cap.label_source='human'").fetchall()]
    # group human-labeled frames by visit so the labeler sees a visit at a time
    by_visit = {}
    truth = {}
    for r in rows:
        by_visit.setdefault(r["visit_id"], []).append(r["path"])
        truth[r["path"]] = r["human"]
    correct, wrong = 0, []
    for vid, paths in by_visit.items():
        for p in labeler.predict_visit(paths, refs):
            pred = p.get("cat")
            human = truth.get(p["file"])
            if human is None:
                continue
            if pred == human:
                correct += 1
            else:
                wrong.append((p["file"], human, pred))
    total = correct + len(wrong)
    return {"total": total, "correct": correct, "wrong": wrong,
            "accuracy": (correct / total) if total else None}
