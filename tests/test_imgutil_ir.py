import cv2, numpy as np
from mw import imgutil

def _write(tmp, name, img):
    p = str(tmp / name); cv2.imwrite(p, img); return p

def test_grayscale_frame_is_ir(tmp_path):
    gray = np.random.randint(0, 255, (120, 160), np.uint8)
    img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)   # 3 equal channels = IR look
    assert imgutil.is_grayscale(_write(tmp_path, "ir.jpg", img)) is True

def test_color_frame_is_not_ir(tmp_path):
    img = np.zeros((120, 160, 3), np.uint8)
    img[:, :, 0] = 200  # strong blue channel only -> saturated color
    img[:, :, 2] = 30
    assert imgutil.is_grayscale(_write(tmp_path, "color.jpg", img)) is False

def test_unreadable_returns_none(tmp_path):
    assert imgutil.is_grayscale(str(tmp_path / "nope.jpg")) is None
