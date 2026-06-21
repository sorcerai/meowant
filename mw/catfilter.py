"""Cheap, local cat/no-cat pre-filter — drops empty-box frames before the
expensive agy labeler runs.

Continuous capture grabs frames as a cat enters and leaves, so a chunk of every
visit is empty box. Sending those to agy wastes ~50s each AND risks them being
mislabeled. A pretrained COCO detector answers "is there a cat in this frame?"
in tens of ms, locally — empties get marked examined-empty and never reach agy.
It's a FILTER (favor recall): keep anything that might be a cat; the agy stage
is the precise one.
"""
import sys


class CatFilter:
    """Interface: True if a cat appears to be present in the frame."""

    def has_cat(self, image_path):
        raise NotImplementedError


class NullCatFilter(CatFilter):
    """Disabled filter — everything passes (legacy behavior / tests)."""

    def has_cat(self, image_path):
        return True


# COCO class index for "cat" in torchvision detection models (91-class indexing).
_COCO_CAT = 17


class TorchvisionCatFilter(CatFilter):
    """Pretrained SSDLite-MobileNetV3 (COCO) — True if a 'cat' is detected above
    `score_thresh`. Lazy-loads the model on first use and keeps it resident;
    runs on MPS if available, else CPU (the model is small, fine on CPU for the
    occasional frame)."""

    def __init__(self, score_thresh=0.4):
        self.score_thresh = score_thresh
        self._model = None
        self._device = None
        self._preprocess = None

    def _ensure_model(self):
        if self._model is not None:
            return
        import torch
        import torchvision
        self._device = "mps" if torch.backends.mps.is_available() else "cpu"
        weights = torchvision.models.detection.SSDLite320_MobileNet_V3_Large_Weights.DEFAULT
        self._preprocess = weights.transforms()
        self._model = (torchvision.models.detection
                       .ssdlite320_mobilenet_v3_large(weights=weights)
                       .eval().to(self._device))

    def has_cat(self, image_path):
        try:
            import torch
            from PIL import Image
            self._ensure_model()
            img = Image.open(image_path).convert("RGB")
            x = self._preprocess(img).to(self._device)
            with torch.no_grad():
                out = self._model([x])[0]
            for label, score in zip(out["labels"].tolist(), out["scores"].tolist()):
                if label == _COCO_CAT and score >= self.score_thresh:
                    return True
            return False
        except Exception as e:
            # On any failure, fail OPEN (assume a cat) so we never silently drop
            # a real visit — the agy stage will sort it out.
            print(f"[catfilter] {image_path} failed ({e}); passing through", file=sys.stderr)
            return True
