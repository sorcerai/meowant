"""GalleryMatcher: embedder + conformal gallery -> Matcher.predict contract.
Uses a fake embedder so the wiring/abstain behavior is tested without torch.
Also covers DinoEmbedder's missing-file guard (must not load torch)."""
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mw import gallery as G
from mw.identify import GalleryMatcher
from mw.embedder import DinoEmbedder


def _unit(v):
    v = np.asarray(v, float)
    return v / (np.linalg.norm(v) + 1e-9)


class _FakeEmbedder:
    def __init__(self, by_path):
        self.by_path = by_path

    def embed(self, path):
        return self.by_path.get(path)


def _gallery_int_keyed():
    rng = np.random.RandomState(0)
    def cl(center, n, jit):
        return [_unit(np.asarray(center, float) + rng.randn(4) * jit) for _ in range(n)]
    emb = {
        1: cl([1, 0, 0, 0], 12, 0.02),   # Ucok
        2: cl([0, 1, 0, 0], 12, 0.02),   # Garfield
        3: cl([0, 0, 1, 0], 12, 0.02),   # Ella
    }
    return G.build_gallery(emb, alpha=0.1)


def test_predict_commits_clear_cat():
    g = _gallery_int_keyed()
    g.margin_color = 0.05
    m = GalleryMatcher(g, _FakeEmbedder({"u.jpg": _unit([1, 0.01, 0, 0])}),
                       is_ir_fn=lambda p: False)
    cid, conf = m.predict("u.jpg")
    assert cid == 1 and 0.0 <= conf <= 1.0


def test_predict_abstains_on_unembeddable_frame():
    g = _gallery_int_keyed()
    m = GalleryMatcher(g, _FakeEmbedder({}), is_ir_fn=lambda p: False)   # embed() -> None
    assert m.predict("missing.jpg") == (None, 0.0)


def test_predict_abstains_on_ambiguous_via_margin():
    # two close cats; a midpoint query has a tiny top1-top2 gap -> margin abstain
    rng = np.random.RandomState(1)
    def cl(center, n=20):
        return [_unit(np.asarray(center, float) + rng.randn(4) * 0.05) for _ in range(n)]
    g = G.build_gallery({1: cl([1, 0, 0, 0]), 2: cl([0.97, 0.10, 0, 0])}, alpha=0.1)
    g.margin_color = 0.10           # require a clear gap to commit
    m = GalleryMatcher(g, _FakeEmbedder({"mid.jpg": _unit([0.985, 0.05, 0, 0])}),
                       is_ir_fn=lambda p: False)
    cid, conf = m.predict("mid.jpg")
    assert cid is None


def test_predict_uses_tighter_ir_margin():
    # same borderline query commits under the loose color margin but abstains
    # under the tighter IR margin -> per-mode gate is wired through predict()
    rng = np.random.RandomState(2)
    def cl(center, n=20):
        return [_unit(np.asarray(center, float) + rng.randn(4) * 0.02) for _ in range(n)]
    g = G.build_gallery({1: cl([1, 0, 0, 0]), 2: cl([0, 1, 0, 0])}, alpha=0.1)
    g.margin_color, g.margin_ir = 0.10, 0.30
    q = _unit([0.8, 0.6, 0, 0])          # cos1~0.8 cos2~0.6 -> margin ~0.2
    color_m = GalleryMatcher(g, _FakeEmbedder({"q.jpg": q}), is_ir_fn=lambda p: False)
    ir_m = GalleryMatcher(g, _FakeEmbedder({"q.jpg": q}), is_ir_fn=lambda p: True)
    assert color_m.predict("q.jpg")[0] == 1      # loose color margin -> commit
    assert ir_m.predict("q.jpg")[0] is None      # tight IR margin -> abstain


def test_embedder_missing_file_returns_none_without_loading_torch():
    e = DinoEmbedder()
    assert e.embed("/nope/does/not/exist.jpg") is None
    assert e._model is None        # lazy: never loaded torch for a missing file
