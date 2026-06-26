"""Regression: AgyLabeler must hand agy ABSOLUTE paths. agy reads image files via
its file tools and (per its own docstring) only works with absolute paths; passing
the DB's relative paths (gallery/captures/X.jpg) made every call hang->ERROR->silent
fallback to Gemma. That was the real 'agy is down'."""
import os
from mw.labeler import AgyLabeler

def test_agy_prompt_uses_absolute_frame_path():
    p = AgyLabeler()._prompt("gallery/captures/x.jpg", {})
    assert os.path.abspath("gallery/captures/x.jpg") in p
    assert "read the file): gallery/captures/x.jpg\n" not in p   # not the bare relative

def test_agy_prompt_uses_absolute_ref_paths():
    p = AgyLabeler()._prompt("gallery/x.jpg", {"Garfield": ["gallery/garfield/r.jpg"]})
    assert os.path.abspath("gallery/garfield/r.jpg") in p
