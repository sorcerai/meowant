"""Bowl fullness via ROI diff-from-empty (mirrors scatter)."""
import cv2
import numpy as np

from mw import bowl


def _img(tmp_path, name, fill, patch=None):
    """A 200x200 gray frame; optionally a bright square patch in the ROI center."""
    a = np.full((200, 200, 3), fill, dtype=np.uint8)
    if patch:
        a[80:120, 80:120] = patch          # center, inside DEFAULT_ROI
    p = str(tmp_path / name)
    cv2.imwrite(p, a)
    return p


def test_empty_matches_reference(tmp_path):
    ref = _img(tmp_path, "ref.jpg", 100)
    cur = _img(tmp_path, "cur.jpg", 100)               # identical -> empty
    assert bowl.fullness(cur, ref) == bowl.EMPTY
    assert bowl.changed_pct(cur, ref) is not None and bowl.changed_pct(cur, ref) <= 5.0


def test_full_differs_a_lot_from_reference(tmp_path):
    ref = _img(tmp_path, "ref.jpg", 100)
    cur = _img(tmp_path, "cur.jpg", 100, patch=255)    # big bright food patch -> full
    assert bowl.fullness(cur, ref) == bowl.FULL


def test_unreadable_returns_none(tmp_path):
    ref = _img(tmp_path, "ref.jpg", 100)
    assert bowl.fullness("/nonexistent.jpg", ref) is None
    assert bowl.changed_pct("/nonexistent.jpg", ref) is None


def test_some_is_between_bands(tmp_path):
    ref = _img(tmp_path, "ref.jpg", 100)
    # a small patch -> mid changed-% -> 'some' (tune bands so this lands between)
    cur = _img(tmp_path, "cur.jpg", 100, patch=255)
    # shrink the patch by using explicit bands that put this frame in 'some'
    pct = bowl.changed_pct(cur, ref)
    state = bowl.fullness(cur, ref, empty_max=pct - 1, full_min=pct + 1)
    assert state == bowl.SOME
