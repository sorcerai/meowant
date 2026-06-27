import os

import cv2
import numpy as np

from mw.harvester import Harvester


class _FakeSource:
    def __init__(self, d): self.d = d
    def frame_path(self, name): return os.path.join(self.d, f"{name}.jpg")


class _FakeFilter:
    def __init__(self, verdicts): self.verdicts = verdicts   # path-substr -> bool
    def has_cat(self, path):
        return any(v for k, v in self.verdicts.items() if k in path)


def _img(path, seed=0):
    """Write a real JPEG so cv2.imread can decode it in the fail-closed guard."""
    im = np.full((40, 50, 3), seed % 256, np.uint8)
    cv2.imwrite(str(path), im)


def test_saves_only_cat_frames(tmp_path):
    src_d = tmp_path / "warm"; src_d.mkdir()
    out = tmp_path / "harvest"
    _img(str(src_d / "meowcam1.jpg"), seed=1)
    _img(str(src_d / "meowcam2.jpg"), seed=2)
    cams = [{"name": "meowcam1"}, {"name": "meowcam2"}]
    h = Harvester(cams, _FakeSource(str(src_d)),
                  _FakeFilter({"meowcam1": True, "meowcam2": False}),
                  str(out), now_fn=lambda: 100.0, sleep=lambda s: None)
    saved = h.harvest_once()
    files = os.listdir(out)
    assert saved == 1 and len(files) == 1 and "meowcam1" in files[0]


def test_dedup_skips_unchanged_frame(tmp_path):
    src_d = tmp_path / "warm"; src_d.mkdir(); out = tmp_path / "harvest"
    _img(str(src_d / "meowcam1.jpg"), seed=0)   # "same" frame
    cams = [{"name": "meowcam1"}]
    t = [100.0]
    h = Harvester(cams, _FakeSource(str(src_d)), _FakeFilter({"meowcam1": True}),
                  str(out), now_fn=lambda: t[0], sleep=lambda s: None)
    assert h.harvest_once() == 1
    t[0] = 105.0
    assert h.harvest_once() == 0          # identical bytes -> skipped
    _img(str(src_d / "meowcam1.jpg"), seed=99)  # different frame
    t[0] = 110.0
    assert h.harvest_once() == 1          # changed -> saved


def test_retention_caps_total_files(tmp_path):
    src_d = tmp_path / "warm"; src_d.mkdir(); out = tmp_path / "harvest"; out.mkdir()
    # Pre-seed output dir with non-image files — they're only counted by retention
    for i in range(5):
        (out / f"old_{i}.jpg").write_bytes(b"x")
    _img(str(src_d / "meowcam1.jpg"), seed=5)   # real image for the guard
    cams = [{"name": "meowcam1"}]
    h = Harvester(cams, _FakeSource(str(src_d)), _FakeFilter({"meowcam1": True}),
                  str(out), retention=3, now_fn=lambda: 200.0, sleep=lambda s: None)
    h.harvest_once()
    assert len(os.listdir(out)) <= 3     # retention enforced


def test_no_tmp_files_remain_after_harvest(tmp_path):
    """Temp files used for atomic copy must always be cleaned up after harvest_once."""
    src_d = tmp_path / "warm"; src_d.mkdir()
    out = tmp_path / "harvest"
    _img(str(src_d / "meowcam1.jpg"), seed=1)
    cams = [{"name": "meowcam1"}]
    h = Harvester(cams, _FakeSource(str(src_d)),
                  _FakeFilter({"meowcam1": True}),
                  str(out), now_fn=lambda: 100.0, sleep=lambda s: None)
    h.harvest_once()
    tmp_files = [f for f in os.listdir(out) if f.endswith(".tmp")]
    assert tmp_files == [], f"Leftover .tmp files: {tmp_files}"


def test_skips_unreadable_frame_even_if_has_cat(tmp_path):
    """Fail-closed: a 0-byte or undecodable frame must NOT be saved even when
    catfilter returns True (simulating TorchvisionCatFilter failing open)."""
    src_d = tmp_path / "warm"; src_d.mkdir()
    out = tmp_path / "harvest"
    cams = [{"name": "meowcam1"}]

    # --- variant 1: 0-byte file (meowcam4 bug) ---
    open(str(src_d / "meowcam1.jpg"), "wb").close()  # 0-byte
    h = Harvester(cams, _FakeSource(str(src_d)),
                  _FakeFilter({"meowcam1": True}),   # fail-open simulation
                  str(out), now_fn=lambda: 100.0, sleep=lambda s: None)
    assert h.harvest_once() == 0
    assert not os.path.exists(str(out)) or len(os.listdir(str(out))) == 0

    # --- variant 2: garbage bytes (non-decodable) ---
    src_d2 = tmp_path / "warm2"; src_d2.mkdir()
    out2 = tmp_path / "harvest2"
    with open(str(src_d2 / "meowcam1.jpg"), "wb") as f:
        f.write(b"notanimage\xff\xd8garbage")
    h2 = Harvester(cams, _FakeSource(str(src_d2)),
                   _FakeFilter({"meowcam1": True}),  # fail-open simulation
                   str(out2), now_fn=lambda: 100.0, sleep=lambda s: None)
    assert h2.harvest_once() == 0
    assert not os.path.exists(str(out2)) or len(os.listdir(str(out2))) == 0
