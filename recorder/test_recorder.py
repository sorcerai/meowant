"""Size-cap pruner + mount-guard for the Mac-side NVR recorder."""
import os
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mac_recorder import prune_to_cap, mount_ok


def _seg(root, cam, name, size, age_s, now):
    d = os.path.join(root, cam)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(b"\0" * size)
    t = now - age_s
    os.utime(p, (t, t))
    return p


def test_prune_deletes_oldest_until_under_cap(tmp_path):
    root = str(tmp_path)
    now = 1_000_000.0
    old = _seg(root, "meowcam1", "a.mp4", 100, age_s=3000, now=now)
    mid = _seg(root, "meowcam1", "b.mp4", 100, age_s=2000, now=now)
    new = _seg(root, "meowcam2", "c.mp4", 100, age_s=1000, now=now)
    # cap 250 bytes: total 300 -> must drop the single oldest (100) to get under
    deleted, freed = prune_to_cap(root, cap_bytes=250, now_fn=lambda: now)
    assert not os.path.exists(old)        # oldest gone
    assert os.path.exists(mid) and os.path.exists(new)
    assert deleted == 1 and freed == 100


def test_prune_noop_when_under_cap(tmp_path):
    root = str(tmp_path)
    _seg(root, "meowcam1", "a.mp4", 100, age_s=1000, now=1_000_000.0)
    deleted, freed = prune_to_cap(root, cap_bytes=10_000, now_fn=lambda: 1_000_000.0)
    assert deleted == 0 and freed == 0


def test_prune_crosses_cameras_by_age(tmp_path):
    """Oldest-first is global across cameras, not per-camera."""
    root = str(tmp_path)
    now = 1_000_000.0
    a = _seg(root, "meowcam3", "old.mp4", 100, age_s=5000, now=now)   # oldest, other cam
    b = _seg(root, "meowcam1", "newer.mp4", 100, age_s=100, now=now)
    deleted, freed = prune_to_cap(root, cap_bytes=150, now_fn=lambda: now)
    assert not os.path.exists(a) and os.path.exists(b)
    assert deleted == 1


def test_prune_never_deletes_the_only_active_segment(tmp_path):
    """A currently-writing segment (mtime within active_grace_s) is spared even
    if over cap, so we never yank the file ffmpeg is appending to."""
    root = str(tmp_path)
    now = 1_000_000.0
    active = _seg(root, "meowcam1", "live.mp4", 10_000, age_s=2, now=now)  # being written
    deleted, freed = prune_to_cap(root, cap_bytes=1, now_fn=lambda: now, active_grace_s=30)
    assert os.path.exists(active)         # spared despite being over cap
    assert deleted == 0


def test_prune_ignores_non_recording_files(tmp_path):
    root = str(tmp_path)
    now = 1_000_000.0
    keep = os.path.join(root, "README.txt")
    with open(keep, "w") as f:
        f.write("x")
    _seg(root, "meowcam1", "a.mp4", 100, age_s=1000, now=now)
    prune_to_cap(root, cap_bytes=1, now_fn=lambda: now)
    assert os.path.exists(keep)           # only *.mp4 segments are pruned


def test_mount_ok_true_for_real_mountpoint(tmp_path):
    # a normal dir that IS a mountpoint check: os.path.ismount on '/' is True
    assert mount_ok("/") is True


def test_mount_ok_false_for_plain_dir(tmp_path):
    assert mount_ok(str(tmp_path)) is False   # a regular dir is not a mountpoint
