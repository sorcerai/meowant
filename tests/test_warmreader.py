"""Warm readers keep RTSP streams hot so capture never pays a cold-open.

One persistent ffmpeg per camera writes the latest frame to <dir>/<cam>.jpg
continuously; capture copies that always-fresh file (file_grab). The pool must
launch one reader per cam, restart a reader whose process died, and file_grab
must treat a MISSING or STALE frame as a failure (so capture's retry + health
paths fire) rather than silently copying an ancient frame.
"""
import os
import time

import pytest

from mw.warmreader import WarmReaderPool, file_grab


class FakeProc:
    """Stand-in for a Popen: alive until killed; records terminate()."""
    def __init__(self, url, out_path):
        self.url = url
        self.out_path = out_path
        self._alive = True
        self.terminated = False

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        self.terminated = True
        self._alive = False

    def die(self):
        self._alive = False


def _fake_launcher():
    launched = []

    def launch(url, out_path, fps, hwaccel=None):
        p = FakeProc(url, out_path)
        p.hwaccel = hwaccel
        launched.append(p)
        return p

    return launch, launched


def test_pool_launches_one_reader_per_camera(tmp_path):
    cams = [{"name": f"meowcam{i}", "url": f"rtsp://x/meowcam{i}"} for i in range(1, 7)]
    launch, launched = _fake_launcher()
    pool = WarmReaderPool(cams, str(tmp_path), launch=launch)
    pool.start()
    assert len(launched) == 6
    assert {p.url for p in launched} == {c["url"] for c in cams}


def test_hwaccel_passed_to_launcher(tmp_path):
    launch, launched = _fake_launcher()
    pool = WarmReaderPool([{"name": "a", "url": "ua"}], str(tmp_path),
                          launch=launch, hwaccel="videotoolbox")
    pool.start()
    assert launched[0].hwaccel == "videotoolbox"


def test_frame_path_per_camera(tmp_path):
    pool = WarmReaderPool([{"name": "meowcam4", "url": "u"}], str(tmp_path))
    assert pool.frame_path("meowcam4") == os.path.join(str(tmp_path), "meowcam4.jpg")


def test_supervise_restarts_dead_reader(tmp_path):
    cams = [{"name": "a", "url": "ua"}, {"name": "b", "url": "ub"}]
    launch, launched = _fake_launcher()
    pool = WarmReaderPool(cams, str(tmp_path), launch=launch)
    pool.start()
    assert len(launched) == 2
    launched[0].die()              # reader 'a' crashes
    pool.supervise_once()
    assert len(launched) == 3      # exactly one relaunch
    assert launched[2].url == "ua" # and it's the dead one that came back


def test_supervise_leaves_healthy_readers_alone(tmp_path):
    cams = [{"name": "a", "url": "ua"}]
    launch, launched = _fake_launcher()
    pool = WarmReaderPool(cams, str(tmp_path), launch=launch)
    pool.start()
    pool.supervise_once()
    assert len(launched) == 1      # still alive -> no relaunch


def test_stop_terminates_all(tmp_path):
    cams = [{"name": "a", "url": "ua"}, {"name": "b", "url": "ub"}]
    launch, launched = _fake_launcher()
    pool = WarmReaderPool(cams, str(tmp_path), launch=launch)
    pool.start()
    pool.stop()
    assert all(p.terminated for p in launched)


def test_file_grab_copies_fresh_frame(tmp_path):
    src = tmp_path / "meowcam1.jpg"
    src.write_bytes(b"\xff\xd8\xffFRAME")
    out = str(tmp_path / "out.jpg")
    file_grab(str(src), out, now_fn=lambda: os.path.getmtime(str(src)) + 1.0)
    with open(out, "rb") as f:
        assert f.read() == b"\xff\xd8\xffFRAME"


def test_file_grab_missing_frame_raises(tmp_path):
    with pytest.raises(Exception):
        file_grab(str(tmp_path / "nope.jpg"), str(tmp_path / "out.jpg"))


def test_file_grab_stale_frame_raises(tmp_path):
    src = tmp_path / "meowcam1.jpg"
    src.write_bytes(b"old")
    # now is 60s after the file's mtime -> stale beyond max_age_s
    with pytest.raises(Exception):
        file_grab(str(src), str(tmp_path / "out.jpg"),
                  max_age_s=10.0, now_fn=lambda: os.path.getmtime(str(src)) + 60.0)
