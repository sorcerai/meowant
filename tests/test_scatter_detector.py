"""ScatterDetector: scoring+persist+alert core, and the enter/leave/idle flow
with a fake grabber (no live RTSP)."""
import os
import cv2
import numpy as np
import pytest

from mw import store
from mw.scatter_detector import ScatterDetector


# --- helpers ---------------------------------------------------------------
def _write(path, blob=False):
    im = np.full((200, 200, 3), 120, np.uint8)
    if blob:
        im[120:160, 70:110] = 255   # bright square inside the apron ROI
    cv2.imwrite(path, im)
    return path


class _Bus:
    def subscribe(self):
        import queue
        return queue.Queue()


def _detector(conn, out_dir, **kw):
    """A detector whose grabber writes synthetic frames: the rolling reference is
    clean gray; every post-leave frame carries a bright blob (= scatter)."""
    notes = []

    def grab(url, path):
        _write(path, blob="post_" in os.path.basename(path))
        return path

    det = ScatterDetector(
        _Bus(), conn, "rtsp://fake/meowcam3", out_dir,
        notify=notes.append, grabber=grab, sleep=lambda *_: None, **kw)
    return det, notes


def _db():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


# --- core: score_and_record -----------------------------------------------
def test_core_scores_persists_and_alerts(tmp_path):
    conn = _db()
    vid = store.open_visit(conn, 1000.0)
    det, notes = _detector(conn, str(tmp_path))
    ref = _write(str(tmp_path / "ref.jpg"))
    post = [_write(str(tmp_path / f"p{i}.jpg"), blob=True) for i in range(3)]

    result, msg = det.score_and_record(vid, ref, post)

    assert result["severity"] >= 1
    assert msg is not None and "sweep" in msg.lower()
    assert notes == [msg]                                  # alert fired once
    assert store.get_visit(conn, vid)["scatter_severity"] == result["severity"]


def test_core_clean_does_not_alert(tmp_path):
    conn = _db()
    vid = store.open_visit(conn, 1000.0)
    det, notes = _detector(conn, str(tmp_path))
    ref = _write(str(tmp_path / "ref.jpg"))
    post = [_write(str(tmp_path / f"p{i}.jpg")) for i in range(3)]   # all clean

    result, msg = det.score_and_record(vid, ref, post)

    assert result["severity"] == 0 and msg is None and notes == []
    assert store.get_visit(conn, vid)["scatter_severity"] == 0


# --- flow: enter / leave / idle -------------------------------------------
def test_full_visit_flow_alerts(tmp_path):
    conn = _db()
    open_vid = {"v": None}
    det, notes = _detector(
        conn, str(tmp_path), min_duration_s=20,
        presence_fn=lambda: False, visit_resolver=lambda: open_vid["v"])

    det._refresh_rolling_ref()                  # idle: clean reference grabbed
    assert det._rolling_ref is not None

    vid = store.open_visit(conn, 1000.0); open_vid["v"] = vid
    det._on_enter()                             # pin reference for this visit
    assert det._visit_ref[vid] == det._rolling_ref

    store.close_visit(conn, vid, 1030.0, 30); open_vid["v"] = None
    det._on_leave()                             # grab post frames, score, alert

    assert len(notes) == 1 and "sweep" in notes[0].lower()
    assert store.get_visit(conn, vid)["scatter_severity"] >= 1


def test_short_visit_skipped(tmp_path):
    conn = _db()
    det, notes = _detector(conn, str(tmp_path), min_duration_s=20,
                           presence_fn=lambda: False, visit_resolver=lambda: None)
    det._refresh_rolling_ref()
    vid = store.open_visit(conn, 1000.0)
    det._open_vid = vid
    det._visit_ref[vid] = det._rolling_ref
    store.close_visit(conn, vid, 1005.0, 5)     # 5s blip < 20s
    det._on_leave()
    assert notes == []
    assert store.get_visit(conn, vid)["scatter_severity"] is None


def test_no_reference_skipped(tmp_path):
    conn = _db()
    det, notes = _detector(conn, str(tmp_path), min_duration_s=20,
                           presence_fn=lambda: False, visit_resolver=lambda: None)
    vid = store.open_visit(conn, 1000.0)
    det._open_vid = vid                          # entered without a pinned ref
    store.close_visit(conn, vid, 1030.0, 30)
    det._on_leave()
    assert notes == []


def test_rolling_ref_skips_when_busy(tmp_path):
    conn = _db()
    # presence_fn True (cat present) -> must NOT refresh the reference
    det, _ = _detector(conn, str(tmp_path),
                       presence_fn=lambda: True, visit_resolver=lambda: None)
    det._refresh_rolling_ref()
    assert det._rolling_ref is None


def test_cat_returns_midgrab_discards(tmp_path):
    conn = _db()
    # A cat is back on the apron during the post-leave window -> frames would be
    # contaminated, so scoring is abandoned (no alert, no persisted score).
    det, notes = _detector(conn, str(tmp_path), min_duration_s=20,
                           presence_fn=lambda: True, visit_resolver=lambda: None)
    det._rolling_ref = _write(str(tmp_path / "rolling_clean.jpg"))
    vid = store.open_visit(conn, 1000.0)
    det._open_vid = vid
    det._visit_ref[vid] = det._rolling_ref
    store.close_visit(conn, vid, 1030.0, 30)
    det._on_leave()
    assert notes == []
    assert store.get_visit(conn, vid)["scatter_severity"] is None


def test_contaminated_post_frames_skipped(tmp_path):
    conn = _db()
    # clear_fn rejects every frame (a cat/dog/person on the floor) -> no score
    det, notes = _detector(conn, str(tmp_path), min_duration_s=20,
                           presence_fn=lambda: False, visit_resolver=lambda: None,
                           clear_fn=lambda p: False)
    det._rolling_ref = _write(str(tmp_path / "rolling_clean.jpg"))
    vid = store.open_visit(conn, 1000.0)
    det._open_vid = vid
    det._visit_ref[vid] = det._rolling_ref
    store.close_visit(conn, vid, 1030.0, 30)
    det._on_leave()
    assert notes == []
    assert store.get_visit(conn, vid)["scatter_severity"] is None


def test_rolling_ref_requires_clear(tmp_path):
    conn = _db()
    # idle, but the grabbed frame has an animal -> reference must NOT be pinned
    det, _ = _detector(conn, str(tmp_path), presence_fn=lambda: False,
                       visit_resolver=lambda: None, clear_fn=lambda p: False)
    det._refresh_rolling_ref()
    assert det._rolling_ref is None


# --- real calibration pair (gated on local frames) ------------------------
_REFS = os.path.expanduser("~/repos/meowant/gallery/refs")
_CLEAN = os.path.join(_REFS, "meowcam3_pair_clean.jpg")
_MESSY = os.path.join(_REFS, "meowcam3_pair_messy.jpg")


@pytest.mark.skipif(not (os.path.exists(_CLEAN) and os.path.exists(_MESSY)),
                    reason="local calibration frames not present")
def test_core_on_real_pair(tmp_path):
    conn = _db()
    vid = store.open_visit(conn, 1000.0)
    det, notes = _detector(conn, str(tmp_path))
    result, msg = det.score_and_record(vid, _CLEAN, [_MESSY])
    assert result["severity"] >= 2 and msg is not None and len(notes) == 1


def test_zone_label_in_alert(tmp_path):
    det, _ = _detector(conn=_db(), out_dir=str(tmp_path), presence_fn=lambda: False,
                       visit_resolver=lambda: None, zone_label="Garfield's fling zone")
    msg = det._format_alert({"severity": 3, "changed_pct": 12.0})
    assert "Garfield's fling zone" in msg and "sweep" in msg.lower()
