import os
from mw.harvester import Harvester

class _FakeSource:
    def __init__(self, d): self.d = d
    def frame_path(self, name): return os.path.join(self.d, f"{name}.jpg")

class _FakeFilter:
    def __init__(self, verdicts): self.verdicts = verdicts   # path-substr -> bool
    def has_cat(self, path):
        return any(v for k, v in self.verdicts.items() if k in path)

def _touch(p, content=b"x"):
    with open(p, "wb") as f: f.write(content)

def test_saves_only_cat_frames(tmp_path):
    src_d = tmp_path / "warm"; src_d.mkdir()
    out = tmp_path / "harvest"
    _touch(str(src_d / "meowcam1.jpg"), b"CAT")
    _touch(str(src_d / "meowcam2.jpg"), b"EMPTY")
    cams = [{"name": "meowcam1"}, {"name": "meowcam2"}]
    h = Harvester(cams, _FakeSource(str(src_d)),
                  _FakeFilter({"meowcam1": True, "meowcam2": False}),
                  str(out), now_fn=lambda: 100.0, sleep=lambda s: None)
    saved = h.harvest_once()
    files = os.listdir(out)
    assert saved == 1 and len(files) == 1 and "meowcam1" in files[0]

def test_dedup_skips_unchanged_frame(tmp_path):
    src_d = tmp_path / "warm"; src_d.mkdir(); out = tmp_path / "harvest"
    _touch(str(src_d / "meowcam1.jpg"), b"SAME")
    cams = [{"name": "meowcam1"}]
    t = [100.0]
    h = Harvester(cams, _FakeSource(str(src_d)), _FakeFilter({"meowcam1": True}),
                  str(out), now_fn=lambda: t[0], sleep=lambda s: None)
    assert h.harvest_once() == 1
    t[0] = 105.0
    assert h.harvest_once() == 0          # identical bytes -> skipped
    _touch(str(src_d / "meowcam1.jpg"), b"DIFFERENT")
    t[0] = 110.0
    assert h.harvest_once() == 1          # changed -> saved

def test_retention_caps_total_files(tmp_path):
    src_d = tmp_path / "warm"; src_d.mkdir(); out = tmp_path / "harvest"; out.mkdir()
    for i in range(5): _touch(str(out / f"old_{i}.jpg"))
    _touch(str(src_d / "meowcam1.jpg"), b"NEW")
    cams = [{"name": "meowcam1"}]
    h = Harvester(cams, _FakeSource(str(src_d)), _FakeFilter({"meowcam1": True}),
                  str(out), retention=3, now_fn=lambda: 200.0, sleep=lambda s: None)
    h.harvest_once()
    assert len(os.listdir(out)) <= 3     # retention enforced
