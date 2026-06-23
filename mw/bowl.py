"""Bowl fullness via ROI diff-from-empty (mirrors mw/scatter.py).

A bowl with kibble differs from the pinned empty-bowl reference inside the bowl
ROI; an empty bowl matches it. So changed-% vs the EMPTY reference is a fullness
proxy: high = food present, low = empty. Reference-relative, so fixed background
and matched lighting cancel; calibrate empty_max / full_min / roi at build
against real empty/some/full frames.
"""
import cv2

FULL, SOME, EMPTY = "full", "some", "empty"
DEFAULT_ROI = (0.30, 0.30, 0.70, 0.70)   # placeholder — calibrate to the bowl


def _roi(img, roi):
    h, w = img.shape[:2]
    x0, y0, x1, y1 = roi
    return img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def changed_pct(frame_path, empty_ref_path, roi=DEFAULT_ROI, delta=22):
    """Percent of the bowl ROI differing from the empty reference, or None."""
    cur = cv2.imread(frame_path)
    ref = cv2.imread(empty_ref_path)
    if cur is None or ref is None:
        return None
    cg = cv2.GaussianBlur(_roi(cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY), roi), (5, 5), 0)
    rg = cv2.GaussianBlur(_roi(cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), roi), (5, 5), 0)
    if cg.shape != rg.shape:
        rg = cv2.resize(rg, (cg.shape[1], cg.shape[0]))
    d = cv2.absdiff(cg, rg)
    _, m = cv2.threshold(d, delta, 255, cv2.THRESH_BINARY)
    return 100.0 * float((m > 0).sum()) / m.size


def fullness(frame_path, empty_ref_path, roi=DEFAULT_ROI, delta=22,
             empty_max=5.0, full_min=20.0):
    """Classify bowl state from diff-vs-empty: 'full'|'some'|'empty', or None."""
    pct = changed_pct(frame_path, empty_ref_path, roi, delta)
    if pct is None:
        return None
    if pct <= empty_max:
        return EMPTY
    if pct >= full_min:
        return FULL
    return SOME
