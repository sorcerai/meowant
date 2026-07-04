"""Per-camera ROI cropping: restrict what the labeler/detector sees to the
litterbox region, so a bystander cat at the in-frame food bowl (meowcam3)
can't steal a box visit's attribution."""
import os
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
from mw.roi import camera_of, RoiCropper, RoiMatcher, RoiCatFilter, load_rois


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


# ---- RoiMatcher: the attribution choke point --------------------------------

class _StubMatcher:
    def __init__(self):
        self.calls = []

    def predict(self, path):
        self.calls.append(path)
        return ("Ucok", 0.9)


def test_roi_matcher_routes_predict_through_cropper(tmp_path):
    src = _img(str(tmp_path / "1783_meowcam3_0.jpg"))
    cropper = RoiCropper({"meowcam3": (0.0, 0.0, 0.5, 0.5)}, cache_dir=str(tmp_path / "c"))
    matcher = _StubMatcher()
    rm = RoiMatcher(matcher, cropper)
    result = rm.predict(src)
    assert result == ("Ucok", 0.9)
    assert matcher.calls == [cropper.path_for(src)]
    assert matcher.calls[0] != src           # attribution only ever sees the crop


def test_roi_matcher_passes_through_when_no_roi(tmp_path):
    src = _img(str(tmp_path / "1783_meowcam1_0.jpg"))
    cropper = RoiCropper({}, cache_dir=str(tmp_path / "c"))
    matcher = _StubMatcher()
    rm = RoiMatcher(matcher, cropper)
    rm.predict(src)
    assert matcher.calls == [src]


# ---- RoiCatFilter: crop has_cat, pass is_clear through untouched -----------

class _StubCatFilter:
    def __init__(self):
        self.has_cat_calls = []
        self.is_clear_calls = []

    def has_cat(self, path):
        self.has_cat_calls.append(path)
        return True

    def is_clear(self, path):
        self.is_clear_calls.append(path)
        return True

    def some_other_attr(self):
        return "delegated"


def test_roi_catfilter_crops_has_cat(tmp_path):
    src = _img(str(tmp_path / "1783_meowcam3_0.jpg"))
    cropper = RoiCropper({"meowcam3": (0.0, 0.0, 0.5, 0.5)}, cache_dir=str(tmp_path / "c"))
    cf = _StubCatFilter()
    rcf = RoiCatFilter(cf, cropper)
    assert rcf.has_cat(src) is True
    assert cf.has_cat_calls == [cropper.path_for(src)]
    assert cf.has_cat_calls[0] != src


def test_roi_catfilter_passes_original_path_to_is_clear(tmp_path):
    """is_clear is a floor/scatter check — cropping to the box ROI would hide
    the very scatter it's meant to detect, so it must see the FULL frame."""
    src = _img(str(tmp_path / "1783_meowcam3_0.jpg"))
    cropper = RoiCropper({"meowcam3": (0.0, 0.0, 0.5, 0.5)}, cache_dir=str(tmp_path / "c"))
    cf = _StubCatFilter()
    rcf = RoiCatFilter(cf, cropper)
    assert rcf.is_clear(src) is True
    assert cf.is_clear_calls == [src]        # ORIGINAL path, not cropped


def test_roi_catfilter_delegates_unknown_attrs():
    cf = _StubCatFilter()
    rcf = RoiCatFilter(cf, RoiCropper({}))
    assert rcf.some_other_attr() == "delegated"


# ---- atomic cache writes -----------------------------------------------------

def test_path_for_leaves_no_tmp_files_behind(tmp_path):
    src = _img(str(tmp_path / "1783_meowcam3_0.jpg"))
    cache_dir = tmp_path / "c"
    c = RoiCropper({"meowcam3": (0.0, 0.0, 0.5, 0.5)}, cache_dir=str(cache_dir))
    out = c.path_for(src)
    assert out != src
    leftover = [f for f in os.listdir(cache_dir) if "tmp" in f]
    assert leftover == []


def test_path_for_never_recrops_its_own_cached_output(tmp_path):
    """A caller that hands the cropper's own output back to path_for (e.g. a
    consumer composing a cropped-path result with a second ROI-aware wrapper)
    must get it back unchanged — never crop an already-cropped frame again."""
    src = _img(str(tmp_path / "1783_meowcam3_0.jpg"), w=100, h=80)
    cache_dir = tmp_path / "c"
    c = RoiCropper({"meowcam3": (0.0, 0.0, 0.5, 0.5)}, cache_dir=str(cache_dir))
    out = c.path_for(src)
    again = c.path_for(out)
    assert again == out
    assert Image.open(out).size == (50, 40)  # not shrunk further


# ---- prune ------------------------------------------------------------------

def test_prune_deletes_old_files_keeps_fresh(tmp_path):
    cache_dir = tmp_path / "c"
    cache_dir.mkdir()
    old = cache_dir / "roi_old.jpg"
    fresh = cache_dir / "roi_fresh.jpg"
    old.write_bytes(b"x")
    fresh.write_bytes(b"y")
    old_time = time.time() - 8 * 86400
    os.utime(old, (old_time, old_time))
    c = RoiCropper({}, cache_dir=str(cache_dir))
    c.prune(max_age_days=7)
    assert not old.exists()
    assert fresh.exists()


def test_prune_never_raises_on_missing_dir(tmp_path):
    c = RoiCropper({}, cache_dir=str(tmp_path / "does_not_exist"))
    c.prune()  # must not raise


# ---- wiring: the two consumers the bystander-theft review flagged as missed -

def test_shadow_scorer_is_wired_through_roi_matcher():
    """ShadowScorer live-writes visits.cat_id off the DINOv2 matcher — it must
    see the ROI-wrapped matcher, not the raw one, or a bowl bystander on
    meowcam3 can get written into visits.cat_id directly."""
    import inspect
    import meowantd
    src = inspect.getsource(meowantd)
    assert "gallery_matcher = RoiMatcher(_matcher, roi_cropper)" in src
    assert "ShadowScorer(\n                    conn, gallery_matcher," in src


def test_jam_watch_is_wired_through_roi_catfilter():
    """A bowl bystander must not read as 'cat seen at the box' and mask a real
    jam; frames_per_visit must default to None (sweep all) in production."""
    import inspect
    import meowantd
    src = inspect.getsource(meowantd)
    assert "RoiCatFilter(catfilter, roi_cropper)" in src
    assert 'jam_watch.frames_per_visit", None)' in src


def test_preroll_and_elim_notify_keep_raw_catfilter():
    """Approach frames legitimately show cats outside the box ROI — these two
    must NOT be switched to the cropped filter."""
    import inspect
    import meowantd
    src = inspect.getsource(meowantd)
    assert "catfilter=catfilter)" in src           # PrerollRing
    assert "matcher=gallery_matcher, catfilter=catfilter," in src  # elim-notify


def test_pruner_prunes_roi_cache():
    import inspect
    import meowantd
    src = inspect.getsource(meowantd)
    assert "roi_cropper.prune()" in src


def test_autolabel_cli_wires_roi_cropper():
    import inspect
    import autolabel
    src = inspect.getsource(autolabel)
    assert "RoiCropper(" in src
    assert "roi_cropper=roi_cropper" in src
