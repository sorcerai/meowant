"""Per-camera region-of-interest cropping.

The bystander-theft bug (Jul 3): the downstairs food bowl sits inside
meowcam3's frame, so when any cat used the box, the labeler read whichever
cat was *eating* at the bowl and stamped the visit with it — Ucok, who lives
at that bowl, collected everyone else's box visits and Ella vanished from the
box record entirely.

An ROI restricts what the labeler/detector SEES to the litterbox region of a
given camera's frame. The bowl (and any other 'unnecessary area') is cropped
away before classification, so a cat over there is simply not in view. Full
frames are still written to disk untouched — the ROI only affects the copy
handed to the model, never the archive.

Cropper.path_for(src) parses the camera from the filename
(`<ts>_<camera>_<idx>.jpg`), so any consumer holding a frame path gets the
ROI-restricted version with no extra plumbing. Cameras without an ROI pass
through unchanged, so this is a no-op until a camera is configured.
"""
import os
import re
import sys

_CAM_RE = re.compile(r"_(meowcam\w+?)_")


def camera_of(path):
    """Extract the camera name embedded in a capture filename, or None."""
    m = _CAM_RE.search(os.path.basename(path))
    return m.group(1) if m else None


def load_rois(cameras):
    """Build {camera_name: (x0,y0,x1,y1)} from camera config entries carrying an
    optional normalized `roi` [x0,y0,x1,y1] (0..1, x0<x1, y0<y1). Malformed or
    out-of-range ROIs are dropped (logged) rather than crashing startup."""
    out = {}
    for c in cameras or []:
        roi = c.get("roi")
        name = c.get("name")
        if roi is None or name is None:
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in roi)
        except (TypeError, ValueError):
            print(f"[roi] {name}: malformed roi {roi!r} — ignored", file=sys.stderr)
            continue
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            print(f"[roi] {name}: roi out of range {roi!r} — ignored", file=sys.stderr)
            continue
        out[name] = (x0, y0, x1, y1)
    return out


class RoiCropper:
    """Maps a capture path to an ROI-cropped copy for its camera (cached), or
    returns the original path when the camera has no ROI or cropping fails."""

    def __init__(self, roi_map, cache_dir="roi_cache"):
        self.roi_map = dict(roi_map or {})
        self.cache_dir = cache_dir
        if self.roi_map:
            os.makedirs(cache_dir, exist_ok=True)

    def path_for(self, src_path):
        if not self.roi_map:
            return src_path
        cam = camera_of(src_path)
        roi = self.roi_map.get(cam) if cam else None
        if roi is None:
            return src_path
        dst = os.path.join(self.cache_dir, "roi_" + os.path.basename(src_path))
        try:
            if (os.path.exists(dst)
                    and os.path.getmtime(dst) >= os.path.getmtime(src_path)):
                return dst                       # cached, still fresh
            from PIL import Image
            with Image.open(src_path) as im:
                im = im.convert("RGB")
                w, h = im.size
                x0, y0, x1, y1 = roi
                box = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
                im.crop(box).save(dst)
            return dst
        except Exception as e:
            print(f"[roi] crop {src_path} failed ({e}); using full frame",
                  file=sys.stderr)
            return src_path
