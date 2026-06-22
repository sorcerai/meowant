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
    """Interface: cat presence (has_cat) + floor-clear check (is_clear)."""

    def has_cat(self, image_path):
        raise NotImplementedError

    def is_clear(self, image_path):
        """True if NO person/cat/dog is in the frame — a floor frame safe to
        score for litter scatter."""
        raise NotImplementedError


class NullCatFilter(CatFilter):
    """Disabled filter — everything passes (legacy behavior / tests)."""

    def has_cat(self, image_path):
        return True

    def is_clear(self, image_path):
        return True


# COCO class indices (torchvision 91-class indexing). A floor frame is only safe
# to score for scatter if NONE of these living things appear in it — an animal
# body reads as a huge false-positive 'scatter' in the reference diff.
_COCO_PERSON = 1
_COCO_CAT = 17
_COCO_DOG = 18
_COCO_LIVING = {_COCO_PERSON, _COCO_CAT, _COCO_DOG}


class TorchvisionCatFilter(CatFilter):
    """Pretrained SSDLite-MobileNetV3 (COCO) — True if a 'cat' is detected above
    `score_thresh`. Lazy-loads the model on first use and keeps it resident;
    runs on MPS if available, else CPU (the model is small, fine on CPU for the
    occasional frame)."""

    def __init__(self, score_thresh=0.4, clear_thresh=0.3):
        self.score_thresh = score_thresh   # cat detection: favor recall
        self.clear_thresh = clear_thresh   # animal rejection: lower = more cautious
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

    def _labels_above(self, image_path, thresh):
        import torch
        from PIL import Image
        self._ensure_model()
        img = Image.open(image_path).convert("RGB")
        x = self._preprocess(img).to(self._device)
        with torch.no_grad():
            out = self._model([x])[0]
        return {label for label, score in zip(out["labels"].tolist(), out["scores"].tolist())
                if score >= thresh}

    def has_cat(self, image_path):
        try:
            return _COCO_CAT in self._labels_above(image_path, self.score_thresh)
        except Exception as e:
            # Fail OPEN (assume a cat) so we never silently drop a real visit —
            # the agy stage will sort it out.
            print(f"[catfilter] {image_path} failed ({e}); passing through", file=sys.stderr)
            return True

    def is_clear(self, image_path):
        """True only if NO person/cat/dog appears above clear_thresh. Fails CLOSED
        (returns False) so a frame we can't verify is never scored as scatter — an
        animal body would otherwise read as a massive false positive."""
        try:
            return _COCO_LIVING.isdisjoint(self._labels_above(image_path, self.clear_thresh))
        except Exception as e:
            print(f"[catfilter] is_clear {image_path} failed ({e}); skipping", file=sys.stderr)
            return False
