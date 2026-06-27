"""Shared image utilities used across vision modules."""

import cv2
import numpy as np


def _roi(img, roi):
    h, w = img.shape[:2]
    x0, y0, x1, y1 = roi
    return img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


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
