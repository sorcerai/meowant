"""Per-camera ROI cropping: restrict what the labeler/detector sees to the
litterbox region, so a bystander cat at the in-frame food bowl (meowcam3)
can't steal a box visit's attribution."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
from mw.roi import camera_of, RoiCropper, load_rois


def _img(path, w=100, h=80, color=(120, 120, 120)):
    Image.new("RGB", (w, h), color).save(path)
    return path


def test_camera_of_parses_filename():
    assert camera_of("gallery/captures/1783_meowcam3_5.jpg") == "meowcam3"
    assert camera_of("1783_meowcam1_pre0.jpg") == "meowcam1"
    assert camera_of("1783_meowcam2_t7.jpg") == "meowcam2"
    assert camera_of("weird.jpg") is None


def test_no_roi_returns_original_path(tmp_path):
    src = _img(str(tmp_path / "1783_meowcam1_0.jpg"))
    c = RoiCropper({}, cache_dir=str(tmp_path / "c"))
    assert c.path_for(src) == src            # untouched: no crop, no copy


def test_roi_crops_to_region(tmp_path):
    src = _img(str(tmp_path / "1783_meowcam3_0.jpg"), w=100, h=80)
    c = RoiCropper({"meowcam3": (0.0, 0.0, 0.5, 0.5)}, cache_dir=str(tmp_path / "c"))
    out = c.path_for(src)
    assert out != src
    assert Image.open(out).size == (50, 40)  # left-top quarter


def test_roi_only_applies_to_configured_camera(tmp_path):
    rois = {"meowcam3": (0.0, 0.0, 0.5, 1.0)}
    c = RoiCropper(rois, cache_dir=str(tmp_path / "c"))
    cam1 = _img(str(tmp_path / "1783_meowcam1_0.jpg"))
    cam3 = _img(str(tmp_path / "1783_meowcam3_0.jpg"))
    assert c.path_for(cam1) == cam1          # cam1 has no ROI: original
    assert c.path_for(cam3) != cam3          # cam3 cropped


def test_crop_is_cached_not_recomputed(tmp_path):
    src = _img(str(tmp_path / "1783_meowcam3_0.jpg"))
    c = RoiCropper({"meowcam3": (0.1, 0.1, 0.9, 0.9)}, cache_dir=str(tmp_path / "c"))
    a = c.path_for(src)
    mtime = os.path.getmtime(a)
    b = c.path_for(src)
    assert a == b and os.path.getmtime(b) == mtime   # same file, not rewritten


def test_unreadable_image_falls_back_to_original(tmp_path):
    p = str(tmp_path / "1783_meowcam3_0.jpg")
    open(p, "wb").write(b"not a jpeg")
    c = RoiCropper({"meowcam3": (0.0, 0.0, 0.5, 0.5)}, cache_dir=str(tmp_path / "c"))
    assert c.path_for(p) == p                # never crash the caller


def test_load_rois_from_camera_config():
    cams = [
        {"name": "meowcam1", "url": "u"},
        {"name": "meowcam3", "url": "u", "roi": [0.0, 0.3, 0.6, 1.0]},
    ]
    rois = load_rois(cams)
    assert "meowcam1" not in rois
    assert rois["meowcam3"] == (0.0, 0.3, 0.6, 1.0)


def test_load_rois_ignores_malformed():
    cams = [{"name": "meowcam3", "roi": [0.0, 0.3]},       # too few
            {"name": "meowcam2", "roi": "nope"},
            {"name": "meowcam1", "roi": [0.0, 0.0, 1.5, 1.0]}]  # out of range
    assert load_rois(cams) == {}
