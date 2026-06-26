"""Litter-scatter detector.

Per-visit DELTA on the floor-apron ROI of the floor camera (meowcam3): compare
post-leave frames against a clean reference pinned at cat-enter. Because the two
are minutes apart the lighting is matched, so a plain ROI abs-diff isolates only
what changed — the litter on the floor — and cancels wood grain and fixed
clutter.

Calibrated on a real pair (2026-06-21, bare floor):
  clean(day) vs clean(evening) = 0.03%   (false-positive floor)
  clean       vs handful-tossed = 4.72%   (true positive)
A threshold anywhere in ~0.4-2% separates cleanly (~150x margin).

The detector is reference-relative, so it is mat-agnostic: whatever is normally
on the floor (bare wood or a mat) lives in the reference and cancels; scatter is
only the delta on top.
"""
import cv2
import numpy as np

from mw.imgutil import _roi

# ROI as fractions of the frame: floor apron in front of the box opening,
# excluding the box (left), the storage bin, the mop bucket (bottom-right) and
# the shelving. (x0, y0, x1, y1)
DEFAULT_ROI = (0.31, 0.33, 0.47, 0.57)

# changed-% of the ROI -> severity. v1 bands, calibrated on one real sample;
# refine as more mess sizes are observed. The alert gate (severity >= 1) sits
# far above the 0.03% clean floor.
def severity_from_pct(pct):
    if pct < 1.5:
        return 0
    if pct < 8.0:
        return 1   # light — a few stray granules
    if pct < 20.0:
        return 2   # moderate — a handful
    return 3       # heavy


def _changed(cur_gray, ref_gray, delta):
    cur = cv2.GaussianBlur(cur_gray, (5, 5), 0)
    ref = cv2.GaussianBlur(ref_gray, (5, 5), 0)
    d = cv2.absdiff(cur, ref)
    _, m = cv2.threshold(d, delta, 255, cv2.THRESH_BINARY)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return (m > 0).astype(np.uint8)


def score(post_paths, reference_path, roi=DEFAULT_ROI, delta=22,
          min_blob=40, consensus=2):
    """Score scatter from post-leave frames against a clean reference.

    Cross-frame consensus: a pixel counts as scatter only if it changed in at
    least `consensus` of the post-leave frames (capped at the frame count) — a
    real granule persists across frames, JPEG/shake noise does not. Blobs
    smaller than `min_blob` px are dropped.

    Returns {severity, changed_pct, area, frames}. severity/changed_pct are 0
    when no frame could be read.
    """
    ref = cv2.imread(reference_path)
    if ref is None:
        return {"severity": 0, "changed_pct": 0.0, "area": 0, "frames": 0}
    rg = _roi(cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), roi)

    acc = None
    n = 0
    for p in post_paths:
        cur = cv2.imread(p)
        if cur is None:
            continue
        cg = _roi(cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY), roi)
        if cg.shape != rg.shape:
            continue
        m = _changed(cg, rg, delta)
        acc = m if acc is None else acc + m
        n += 1
    if n == 0 or acc is None:
        return {"severity": 0, "changed_pct": 0.0, "area": 0, "frames": 0}

    keep = (acc >= min(consensus, n)).astype(np.uint8) * 255
    cnts, _ = cv2.findContours(keep, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    area = sum(cv2.contourArea(c) for c in cnts if cv2.contourArea(c) >= min_blob)
    pct = 100.0 * area / keep.size
    return {"severity": severity_from_pct(pct), "changed_pct": round(pct, 2),
            "area": int(area), "frames": n}
