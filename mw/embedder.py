"""DINOv2-S frame embedder — turns a camera frame into a unit embedding for the
gallery matcher (mw/gallery.py).

DINOv2-S was chosen over MegaDescriptor-T by a leave-one-VISIT-out bake-off plus a
leave-one-CAMERA-out cross-camera test (94.5% cross-camera vs 89-94% mixed; it
reads the cat, not the box/background). Each frame is cat-cropped (SSDLite COCO
detector, same as TorchvisionCatFilter) before embedding so background is
suppressed; an undetected cat falls back to the whole frame.

Torch/timm are imported lazily (first embed), so importing this module — or
mw/identify.py — stays cheap when the matcher is not in use. Runs on MPS if
available, else CPU.
"""
import os
import sys

_COCO_CAT = 17


class DinoEmbedder:
    def __init__(self, model_name="vit_small_patch14_dinov2.lvd142m",
                 crop=True, det_thresh=0.3):
        self.model_name = model_name
        self.crop = crop
        self.det_thresh = det_thresh
        self._model = None
        self._tf = None
        self._det = None
        self._device = None

    def _ensure(self):
        if self._model is not None:
            return
        import torch, timm, torchvision
        from timm.data import resolve_model_data_config, create_transform
        self._device = "mps" if torch.backends.mps.is_available() else "cpu"
        self._model = timm.create_model(self.model_name, pretrained=True,
                                        num_classes=0).eval().to(self._device)
        self._tf = create_transform(**resolve_model_data_config(self._model),
                                    is_training=False)
        if self.crop:
            w = torchvision.models.detection.SSDLite320_MobileNet_V3_Large_Weights.DEFAULT
            self._det = (torchvision.models.detection
                         .ssdlite320_mobilenet_v3_large(weights=w)
                         .eval().to(self._device))

    def _crop(self, pil):
        import torch
        from torchvision.transforms.functional import to_tensor
        with torch.no_grad():
            pred = self._det([to_tensor(pil).to(self._device)])[0]
        best, bs = None, self.det_thresh
        for b, l, s in zip(pred["boxes"], pred["labels"], pred["scores"]):
            if int(l) == _COCO_CAT and float(s) > bs:
                best, bs = b, float(s)
        if best is None:
            return pil                          # no cat detected -> whole frame
        x0, y0, x1, y1 = [int(v) for v in best.tolist()]
        W, H = pil.size
        return pil.crop((max(0, x0), max(0, y0), min(W, x1), min(H, y1)))

    def embed(self, image_path):
        """Return a unit (L2-normalized) numpy embedding, or None if the frame is
        missing/unreadable (so callers can skip rather than crash — a frame we
        can't embed must never become a confident ID)."""
        if not image_path or not os.path.exists(image_path):
            return None
        try:
            import torch, numpy as np
            from PIL import Image
            self._ensure()
            pil = Image.open(image_path).convert("RGB")
            if self.crop:
                pil = self._crop(pil)
            x = self._tf(pil).unsqueeze(0).to(self._device)
            with torch.no_grad():
                f = self._model(x)
            f = torch.nn.functional.normalize(f, dim=-1).squeeze(0).float().cpu().numpy()
            return f
        except Exception as e:
            print(f"[embedder] {image_path} failed ({e})", file=sys.stderr)
            return None
