"""Conformal per-cat gallery: the abstain-or-commit core of the DINOv2 matcher.

Pure-numpy (no torch) so the safety-critical logic is fully unit-tested. The
gallery holds per-cat centroids + a per-cat conformal threshold tau_c; a query
embeds to a prediction SET {c : 1-cos(x,mu_c) <= tau_c} and only commits when
that set is a singleton — the Garfield/Ucok collision lands in BOTH sets and
abstains instead of guessing.
"""
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mw import gallery as G


def _unit(v):
    v = np.asarray(v, float)
    return v / (np.linalg.norm(v) + 1e-9)


def _cluster(center, n, jitter=0.02, seed=0):
    rng = np.random.RandomState(seed)
    return [_unit(np.asarray(center, float) + rng.randn(len(center)) * jitter) for _ in range(n)]


def test_singleton_commits_confident_cat():
    # three well-separated cats in 4-D
    emb = {
        "Ucok": _cluster([1, 0, 0, 0], 12, seed=1),
        "Garfield": _cluster([0, 1, 0, 0], 12, seed=2),
        "Ella": _cluster([0, 0, 1, 0], 12, seed=3),
    }
    g = G.build_gallery(emb, alpha=0.1)
    # a clear Ucok query
    cat, conf = g.classify(_unit([1, 0.01, 0, 0]))
    assert cat == "Ucok"
    assert 0.0 <= conf <= 1.0 and conf > 0.5


def test_ambiguous_between_two_cats_abstains():
    # Garfield and Ucok overlap (the tabby collision); Ella separate
    emb = {
        "Ucok": _cluster([1, 0, 0, 0], 20, jitter=0.25, seed=1),
        "Garfield": _cluster([1, 0.05, 0, 0], 20, jitter=0.25, seed=2),
        "Ella": _cluster([0, 0, 1, 0], 20, jitter=0.02, seed=3),
    }
    g = G.build_gallery(emb, alpha=0.1)
    # a point right between the two tabbies -> in both sets -> abstain
    q = _unit([1, 0.025, 0, 0])
    s = g.predict_set(q)
    assert {"Ucok", "Garfield"} <= s          # both tabbies admitted
    cat, conf = g.classify(q)
    assert cat is None                          # singleton rule -> abstain


def test_far_outlier_abstains_empty_set():
    emb = {
        "Ucok": _cluster([1, 0, 0, 0], 12, seed=1),
        "Garfield": _cluster([0, 1, 0, 0], 12, seed=2),
        "Ella": _cluster([0, 0, 1, 0], 12, seed=3),
    }
    g = G.build_gallery(emb, alpha=0.05)
    cat, conf = g.classify(_unit([0, 0, 0, 1]))   # orthogonal to all -> empty set
    assert cat is None


def test_tau_tighter_alpha_admits_fewer():
    emb = {
        "Ucok": _cluster([1, 0, 0, 0], 30, jitter=0.15, seed=1),
        "Garfield": _cluster([0, 1, 0, 0], 30, jitter=0.15, seed=2),
    }
    loose = G.build_gallery(emb, alpha=0.30)
    tight = G.build_gallery(emb, alpha=0.01)
    # higher alpha = smaller tau (more abstention); lower alpha = larger tau
    assert tight.tau["Ucok"] >= loose.tau["Ucok"]


def test_group_aware_tau_looser_than_per_frame_on_correlated_data():
    # Two visits per cat; frames WITHIN a visit are near-identical (correlated),
    # but the two visits sit a bit apart. Per-frame tau (optimistic) is tight;
    # group-aware (leave-one-visit-out) tau must be LOOSER, fixing over-abstention.
    a1 = _cluster([1, 0, 0, 0], 8, jitter=0.005, seed=1)   # visit A
    a2 = _cluster([0.9, 0.2, 0, 0], 8, jitter=0.005, seed=2)  # visit B (shifted)
    emb = {"Ucok": a1 + a2}
    groups = {"Ucok": [["A"] * 8 + ["B"] * 8][0]}
    per_frame = G.build_gallery(emb, alpha=0.1)
    grouped = G.build_gallery(emb, alpha=0.1, groups_by_cat=groups)
    assert grouped.tau["Ucok"] > per_frame.tau["Ucok"]


def test_save_load_roundtrip(tmp_path):
    emb = {
        "Ucok": _cluster([1, 0, 0, 0], 10, seed=1),
        "Garfield": _cluster([0, 1, 0, 0], 10, seed=2),
    }
    g = G.build_gallery(emb, alpha=0.1)
    p = str(tmp_path / "gal.npz")
    g.save(p)
    g2 = G.Gallery.load(p)
    assert g2.cats == g.cats
    q = _unit([1, 0.02, 0, 0])
    c1, conf1 = g.classify(q)
    c2, conf2 = g2.classify(q)
    assert c1 == c2                       # same decision survives the roundtrip
    assert abs(conf1 - conf2) < 1e-9      # float drift through npz is acceptable
    assert g2.alpha == g.alpha
