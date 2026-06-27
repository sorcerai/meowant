"""Conformal per-cat gallery — the abstain-or-commit core of the embedding matcher.

Holds one L2-normalized centroid per cat plus a per-cat conformal threshold
`tau_c`. A query embedding x produces a prediction SET
    S(x) = { c : 1 - cos(x, mu_c) <= tau_c }
and we COMMIT only when S(x) is a singleton. Two cats admitting the same query
(the Garfield/Ucok tabby collision) yields |S|=2 -> abstain; an outlier yields
|S|=0 -> abstain. This is Mondrian (class-conditional) split conformal: each cat
gets per-class coverage P(c in S | true=c) >= 1-alpha.

Pure numpy on purpose: the safety-critical logic stays unit-testable without
loading torch. The embedder (DINOv2) lives in mw/embedder.py and feeds this.

Calibration note: tau_c is calibrated on the SAME frames used to build mu_c
(training frames sit closer to their own centroid than unseen frames would), so
tau_c is mildly optimistic -> sets run slightly TIGHTER -> slightly MORE
abstention. That errs toward "can't confirm", the safe direction. A held-out
calibration split is the post-validation refinement.
"""
import numpy as np


def _unit(v):
    v = np.asarray(v, dtype=float)
    return v / (np.linalg.norm(v) + 1e-9)


def _conformal_tau(scores, alpha):
    """Split-conformal threshold: the smallest s such that at least a
    (1-alpha) fraction of calibration scores are <= s, with the finite-sample
    (n+1) correction. Returns +inf when n is too small to bound at this alpha
    (admit-always for that cat — safe, since a too-permissive single cat still
    needs to WIN the singleton test against the others)."""
    s = np.sort(np.asarray(scores, dtype=float))
    n = len(s)
    if n == 0:
        return float("inf")
    k = int(np.ceil((n + 1) * (1.0 - alpha)))
    if k > n:
        return float("inf")
    return float(s[k - 1])


class Gallery:
    def __init__(self, centroids, tau, alpha):
        # centroids: {cat: unit-vector}; tau: {cat: float}
        self.centroids = {c: _unit(v) for c, v in centroids.items()}
        self.tau = dict(tau)
        self.alpha = float(alpha)
        self.cats = sorted(self.centroids.keys())

    def _scores(self, x):
        x = _unit(x)
        return {c: 1.0 - float(x @ self.centroids[c]) for c in self.cats}

    def predict_set(self, x):
        """The conformal prediction set: every cat whose nonconformity is within
        its own tau. May be empty, singleton, or larger."""
        sc = self._scores(x)
        return {c for c in self.cats if sc[c] <= self.tau[c]}

    def classify(self, x):
        """Commit only on a singleton set. Returns (cat, confidence) where
        confidence is cosine-to-centroid in 0..1; abstains as (None, best_cos)
        so callers can log how close a miss was."""
        sc = self._scores(x)
        S = {c for c in self.cats if sc[c] <= self.tau[c]}
        best_cos = max((1.0 - sc[c] for c in self.cats), default=0.0)
        best_cos = float(min(1.0, max(0.0, best_cos)))
        if len(S) == 1:
            c = next(iter(S))
            return c, float(min(1.0, max(0.0, 1.0 - sc[c])))
        return None, (best_cos if S else 0.0)

    def save(self, path):
        cats = self.cats
        mat = np.stack([self.centroids[c] for c in cats]) if cats else np.zeros((0, 0))
        taus = np.array([self.tau[c] for c in cats], dtype=float)
        np.savez(path, cats=np.array(cats, dtype=object), centroids=mat,
                 tau=taus, alpha=self.alpha)

    @classmethod
    def load(cls, path):
        d = np.load(path, allow_pickle=True)
        cats = [str(c) for c in d["cats"]]
        centroids = {c: d["centroids"][i] for i, c in enumerate(cats)}
        tau = {c: float(d["tau"][i]) for i, c in enumerate(cats)}
        return cls(centroids, tau, float(d["alpha"]))


def build_gallery(embeddings_by_cat, alpha=0.1):
    """Build centroids + per-cat conformal tau from labeled embeddings.

    embeddings_by_cat: {cat: list/array of embedding vectors (any norm)}.
    alpha: target per-cat miss rate (1-alpha coverage). Higher alpha -> smaller
    tau -> more abstention.
    """
    centroids, tau = {}, {}
    for cat, vecs in embeddings_by_cat.items():
        arr = np.stack([_unit(v) for v in vecs]) if len(vecs) else np.zeros((0, 0))
        if len(arr) == 0:
            continue
        mu = _unit(arr.mean(axis=0))
        centroids[cat] = mu
        scores = 1.0 - arr @ mu               # nonconformity of each own-cat frame
        tau[cat] = _conformal_tau(scores, alpha)
    return Gallery(centroids, tau, alpha)
