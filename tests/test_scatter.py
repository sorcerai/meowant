"""Scatter detector: severity bands, ROI delta, cross-frame consensus, and a
real-pair check gated on the local calibration frames."""
import os
import cv2
import numpy as np
import pytest

from mw import scatter


def _img(tmp, name, blobs=()):
    """A uniform gray 'floor' (200x200); blobs = (x, y, size, value) squares."""
    im = np.full((200, 200, 3), 120, np.uint8)
    for (x, y, s, val) in blobs:
        im[y:y + s, x:x + s] = val
    p = str(tmp / name)
    cv2.imwrite(p, im)
    return p


def test_severity_bands():
    assert scatter.severity_from_pct(0.0) == 0
    assert scatter.severity_from_pct(0.3) == 0
    assert scatter.severity_from_pct(1.0) == 1
    assert scatter.severity_from_pct(3.0) == 2
    assert scatter.severity_from_pct(8.0) == 3


def test_clean_vs_clean_is_zero(tmp_path):
    ref = _img(tmp_path, "ref.jpg")
    post = _img(tmp_path, "post.jpg")
    r = scatter.score([post], ref, consensus=1)
    assert r["severity"] == 0 and r["changed_pct"] == 0.0


def test_detects_scatter(tmp_path):
    ref = _img(tmp_path, "ref.jpg")
    post = _img(tmp_path, "post.jpg", blobs=[(70, 120, 40, 255)])  # inside ROI
    r = scatter.score([post], ref, consensus=1)
    assert r["severity"] >= 1 and r["area"] > 0


def test_consensus_drops_transient(tmp_path):
    ref = _img(tmp_path, "ref.jpg")
    p1 = _img(tmp_path, "p1.jpg", blobs=[(70, 120, 40, 255)])
    p2 = _img(tmp_path, "p2.jpg")
    p3 = _img(tmp_path, "p3.jpg")
    # blob in only 1 of 3 frames -> consensus=2 rejects it (noise)
    assert scatter.score([p1, p2, p3], ref, consensus=2)["area"] == 0
    # blob in 2 of 3 -> kept (real)
    p2b = _img(tmp_path, "p2b.jpg", blobs=[(70, 120, 40, 255)])
    assert scatter.score([p1, p2b, p3], ref, consensus=2)["area"] > 0


def test_min_blob_filter(tmp_path):
    ref = _img(tmp_path, "ref.jpg")
    post = _img(tmp_path, "post.jpg", blobs=[(70, 120, 3, 255)])  # tiny speck
    assert scatter.score([post], ref, consensus=1, min_blob=40)["area"] == 0


_REFS = os.path.expanduser("~/repos/meowant/gallery/refs")
_CLEAN = os.path.join(_REFS, "meowcam3_pair_clean.jpg")
_MESSY = os.path.join(_REFS, "meowcam3_pair_messy.jpg")


@pytest.mark.skipif(not (os.path.exists(_CLEAN) and os.path.exists(_MESSY)),
                    reason="local calibration frames not present")
def test_real_calibration_pair():
    # the discriminating test: fires on the real messy floor, silent on clean
    assert scatter.score([_MESSY], _CLEAN, consensus=1)["severity"] >= 2
    assert scatter.score([_CLEAN], _CLEAN, consensus=1)["severity"] == 0
