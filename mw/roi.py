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
import tempfile
import time

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
        # Never re-crop our own cached output: a caller composing two
        # ROI-aware wrappers (e.g. a cropped matcher fed an already-cropped
        # path) must get it back unchanged, not nested-cropped into a
        # shrinking, off-center sliver.
        if os.path.dirname(os.path.abspath(src_path)) == os.path.abspath(self.cache_dir):
            return src_path
        cam = camera_of(src_path)
        roi = self.roi_map.get(cam) if cam else None
        if roi is None:
            return src_path
        dst = os.path.join(self.cache_dir, "roi_" + os.path.basename(src_path))
        tmp = None
        try:
            if (os.path.exists(dst)
                    and os.path.getmtime(dst) >= os.path.getmtime(src_path)):
                return dst                       # cached, still fresh
            from PIL import Image
            fd, tmp = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
            os.close(fd)
            with Image.open(src_path) as im:
                im = im.convert("RGB")
                w, h = im.size
                x0, y0, x1, y1 = roi
                box = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
                # save() can't infer a format from the ".tmp" extension —
                # name it explicitly (captures are always JPEG).
                im.crop(box).save(tmp, format="JPEG")
            os.replace(tmp, dst)   # atomic: a reader never sees a half-written crop
            return dst
        except Exception as e:
            print(f"[roi] crop {src_path} failed ({e}); using full frame",
                  file=sys.stderr)
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return src_path

    def prune(self, max_age_days=7):
        """Delete cached crops untouched for max_age_days. Runs in the daily
        maintenance loop alongside the empty-capture pruner; a bad cache_dir
        or a permission error must never take that loop down."""
        try:
            if not os.path.isdir(self.cache_dir):
                return
            cutoff = time.time() - max_age_days * 86400
            for name in os.listdir(self.cache_dir):
                p = os.path.join(self.cache_dir, name)
                try:
                    if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except OSError:
                    pass
        except Exception as e:
            print(f"[roi] prune failed ({e})", file=sys.stderr)


class RoiMatcher:
    """Wraps a gallery matcher so EVERY predict() is forced through the ROI
    crop first. This is the single choke point for attribution: any consumer
    holding this object, however it got it, can only ever see the box
    region — not a per-call opt-in a future consumer can forget."""

    def __init__(self, matcher, cropper):
        self.matcher = matcher
        self.cropper = cropper

    def predict(self, path):
        return self.matcher.predict(self.cropper.path_for(path))


class RoiCatFilter:
    """Wraps a CatFilter so has_cat() (presence/attribution) is ROI-cropped,
    while is_clear() (the floor/scatter check) sees the ORIGINAL full frame —
    cropping to the box would hide scatter lying outside it. Unrecognized
    attributes delegate to the wrapped filter, so this drops in wherever a
    plain CatFilter is expected."""

    def __init__(self, catfilter, cropper):
        self.catfilter = catfilter
        self.cropper = cropper

    def has_cat(self, path):
        return self.catfilter.has_cat(self.cropper.path_for(path))

    def is_clear(self, path):
        return self.catfilter.is_clear(path)

    def __getattr__(self, name):
        return getattr(self.catfilter, name)
