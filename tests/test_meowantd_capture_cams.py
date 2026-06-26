from meowantd import litterbox_cameras


def test_litterbox_cameras_excludes_bowl_cams():
    cams = [{"name": "meowcam1"}, {"name": "meowcam2"}, {"name": "meowcam3"},
            {"name": "meowcam4"}, {"name": "meowcam5"}, {"name": "meowcam6"}]
    bowls = [{"camera": "meowcam6"}, {"camera": "meowcam5"}]
    out = [c["name"] for c in litterbox_cameras(cams, bowls)]
    assert out == ["meowcam1", "meowcam2", "meowcam3", "meowcam4"]


def test_litterbox_cameras_no_bowls_returns_all():
    cams = [{"name": "meowcam1"}, {"name": "meowcam2"}]
    assert litterbox_cameras(cams, []) == cams


def test_litterbox_cameras_ignores_bowl_without_camera_key():
    cams = [{"name": "meowcam1"}, {"name": "meowcam3"}]
    bowls = [{"location": "upstairs"}]      # malformed: no 'camera'
    assert litterbox_cameras(cams, bowls) == cams   # nothing dropped
