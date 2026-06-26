"""Phase 3b prerequisites: per-cat thresholds and quiet hours become
config-driven so the Settings panel can edit them. Thresholds keep the 29e
single-source (cat_status.THRESHOLDS) via in-place load; quiet-window math is
shared by health_watch + deadman."""
import time

from mw import cat_status, schedule


def test_load_thresholds_applies_config_over_defaults_in_place(tmp_path):
    original = dict(cat_status.THRESHOLDS)
    try:
        ref = cat_status.THRESHOLDS                     # the shared object importers hold
        cat_status.load_thresholds({"thresholds": {"Ucok": 6, "Ella": 30}})
        assert cat_status.THRESHOLDS["Ucok"] == 6       # config wins
        assert cat_status.THRESHOLDS["Ella"] == 30
        assert cat_status.THRESHOLDS["Garfield"] == 24  # default preserved
        assert cat_status.THRESHOLDS is ref             # mutated in place (importers see it)
    finally:
        cat_status.THRESHOLDS.clear(); cat_status.THRESHOLDS.update(original)


def test_load_thresholds_empty_config_keeps_defaults(tmp_path):
    original = dict(cat_status.THRESHOLDS)
    try:
        cat_status.load_thresholds({})
        assert cat_status.THRESHOLDS == {"Ucok": 8, "Ella": 24, "Garfield": 24}
    finally:
        cat_status.THRESHOLDS.clear(); cat_status.THRESHOLDS.update(original)


def test_load_thresholds_ignores_invalid_values(tmp_path):
    original = dict(cat_status.THRESHOLDS)
    try:
        cat_status.load_thresholds({"thresholds": {"Ucok": 0, "Ella": -5, "Garfield": "x"}})
        # invalid (non-positive / non-numeric) entries are dropped, defaults kept
        assert cat_status.THRESHOLDS == {"Ucok": 8, "Ella": 24, "Garfield": 24}
    finally:
        cat_status.THRESHOLDS.clear(); cat_status.THRESHOLDS.update(original)


def _at(h, m=0):
    lt = list(time.localtime()); lt[3] = h; lt[4] = m
    return time.mktime(tuple(lt))


def test_is_quiet_normal_window():
    assert schedule.is_quiet(_at(23), "22:00", "08:00") is True   # overnight
    assert schedule.is_quiet(_at(3), "22:00", "08:00") is True
    assert schedule.is_quiet(_at(12), "22:00", "08:00") is False  # midday


def test_is_quiet_same_day_window():
    # a non-wrapping window (e.g. 09:00-17:00)
    assert schedule.is_quiet(_at(12), "09:00", "17:00") is True
    assert schedule.is_quiet(_at(8), "09:00", "17:00") is False


def test_is_quiet_custom_window_respected():
    # owner narrows quiet to 23:00-06:00: 22:00 is now NOT quiet
    assert schedule.is_quiet(_at(22), "23:00", "06:00") is False
    assert schedule.is_quiet(_at(23, 30), "23:00", "06:00") is True
