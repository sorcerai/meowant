"""Per-cat identification scaffold (model-independent).

The recognizer itself — a cat detector that crops the frame plus an embedding
model matched against a per-cat gallery — is deferred until we have a labeled
gallery (Phase 3 of the design). This module is everything *around* that model:
the `Matcher` interface a real model plugs into, multi-view fusion across
cameras, and the backfill that writes an identity onto a visit.

With `NullMatcher` the whole pipeline runs end-to-end and yields "unknown", so
the plumbing is exercised before any model exists. When the embedding model
lands it implements `Matcher.predict` and drops in — no plumbing changes.
"""
from mw import store


class Matcher:
    """A real model implements this: one camera frame -> (cat_id, confidence).

    cat_id is None for 'unknown' / below the model's own threshold; confidence
    is a calibrated 0..1 score (e.g. cosine similarity to the nearest gallery
    embedding)."""

    def predict(self, image_path):
        raise NotImplementedError


class NullMatcher(Matcher):
    """Placeholder until the embedding model lands: everything is unknown.
    Lets backfill/live-attribution run today without a confident wrong guess."""

    def predict(self, image_path):
        return (None, 0.0)


def fuse_views(predictions):
    """Combine per-camera predictions for one visit into a single identity.

    `predictions`: list of (cat_id, confidence). Strategy: among the views that
    named a cat, sum confidence per cat and pick the highest — a
    confidence-weighted vote, so a clean head-shot on one camera can outweigh a
    hindquarter-only glimpse on another (the hooded-globe occlusion case the
    two-camera setup exists to solve). Returns (cat_id, confidence), or
    (None, 0.0) if no view named a cat. The reported confidence is the winner's
    BEST single-view score (calibrated 0..1), not the unbounded summed vote.
    """
    scores = {}
    for cid, conf in predictions:
        if cid is not None:
            scores[cid] = scores.get(cid, 0.0) + conf
    if not scores:
        return (None, 0.0)
    best = max(scores, key=scores.get)
    best_conf = max((conf for cid, conf in predictions if cid == best), default=0.0)
    return (best, best_conf)


def identify_visit(conn, visit_id, matcher, threshold=0.0):
    """Run the matcher over a visit's captures, persist each per-view
    prediction, fuse them, and write the visit identity if the fused confidence
    clears `threshold`. Returns the fused (cat_id, confidence) regardless, so
    the caller can log/alert on low-confidence cases without committing a guess.
    """
    caps = store.captures_for_visit(conn, visit_id)
    preds = []
    for c in caps:
        cid, conf = matcher.predict(c["path"])
        store.set_capture_prediction(conn, c["id"], cid, conf)
        preds.append((cid, conf))
    cat_id, conf = fuse_views(preds)
    if cat_id is not None and conf >= threshold:
        store.set_visit_identity(conn, visit_id, cat_id, conf)
    return (cat_id, conf)
