"""Shared image utilities used across vision modules."""

import cv2
import numpy as np


def _roi(img, roi):
    h, w = img.shape[:2]
    x0, y0, x1, y1 = roi
    return img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def spread_sample(items, n):
    """Evenly-spread sample of up to n items, always including the FIRST and
    LAST element when there are more items than n. A naive int(i*len/n) index
    formula can never land on the last item — exactly the exit-tail frame that
    matters most for identification. round(i*(len-1)/(n-1)) hits index 0 and
    len-1 exactly at i=0 and i=n-1, with the rest spread evenly between."""
    items = list(items)
    if n <= 0:
        return []
    if len(items) <= n:
        return items
    if n == 1:
        return [items[0]]
    idx = sorted({round(i * (len(items) - 1) / (n - 1)) for i in range(n)})
    return [items[j] for j in idx]


def is_grayscale(image_path, sat_thresh=10.0):
    """True if the frame is effectively grayscale (IR night mode), False if it
    carries real color, None if unreadable. JPEG color-noise keeps channels from
    being bit-identical, so we threshold the mean per-pixel channel spread rather
    than test exact equality."""
    img = cv2.imread(image_path)
    if img is None:
        return None
    b, g, r = (c.astype(np.int16) for c in cv2.split(img))
    spread = np.maximum(np.maximum(np.abs(r - g), np.abs(g - b)), np.abs(r - b))
    return float(spread.mean()) < sat_thresh
