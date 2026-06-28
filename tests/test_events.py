from mw.events import (detect_events, CAT_ENTER, CAT_LEAVE, CLEAN_START,
                       CLEAN_DONE, BIN_FULL, BIN_CLEAR, ELIMINATION,
                       FAULT, FAULT_CLEAR)

def kinds(evs): return [e.kind for e in evs]

def test_cat_enter_and_leave():
    assert kinds(detect_events({"24": "standby"}, {"24": "cat_get_in"}, 1.0)) == [CAT_ENTER]
    assert kinds(detect_events({"24": "cat_get_in"}, {"24": "standby"}, 2.0)) == [CAT_LEAVE]

def test_clean_cycle():
    assert CLEAN_START in kinds(detect_events({"24": "waiting"}, {"24": "cleaning"}, 3.0))
    assert CLEAN_DONE in kinds(detect_events({"24": "cleaning"}, {"24": "clean_done"}, 4.0))

def test_bin_full_edge_only():
    assert kinds(detect_events({"21": 0}, {"21": 1}, 5.0)) == [BIN_FULL]
    assert detect_events({"21": 1}, {"21": 1}, 6.0) == []  # no repeat

def test_fault_asserted_emits_fault_with_bitmap():
    evs = detect_events({"22": 0}, {"22": 1}, 5.0)
    assert kinds(evs) == [FAULT]
    assert evs[0].detail["bitmap"] == 1

def test_fault_no_repeat_same_bitmap():
    assert detect_events({"22": 1}, {"22": 1}, 6.0) == []   # latched, no re-emit

def test_fault_code_change_re_emits():
    # E1 -> E1+E2 (bitmap 1 -> 3): still faulted, but a NEW code appeared.
    evs = detect_events({"22": 1}, {"22": 3}, 7.0)
    assert kinds(evs) == [FAULT] and evs[0].detail["bitmap"] == 3

def test_fault_cleared_emits_fault_clear():
    # nonzero -> 0 must emit FAULT_CLEAR (mirrors BIN_CLEAR), so "stuck since" resets.
    assert kinds(detect_events({"22": 1}, {"22": 0}, 8.0)) == [FAULT_CLEAR]

def test_partial_poll_missing_dp22_no_false_fault_clear():
    # `new` lacks "22" — a partial poll must not synthesize 0 and emit a false clear.
    evs = detect_events({"22": 1, "24": "standby"}, {"24": "cat_get_in"}, 9.5)
    assert FAULT_CLEAR not in kinds(evs)

def test_elimination_from_dp7_increment():
    assert ELIMINATION in kinds(detect_events({"7": 1}, {"7": 2}, 7.0))

def test_elimination_from_dp102_record():
    evs = detect_events({"102": None}, {"102": "ADcAAA=="}, 8.0)
    assert ELIMINATION in kinds(evs)
    assert evs[0].detail["use_record"] == 55

def test_no_change_no_events():
    assert detect_events({"24": "standby"}, {"24": "standby"}, 9.0) == []

def test_partial_poll_missing_dp21_no_false_clear():
    # `new` has no "21" key — a partial poll must not synthesize 0 and emit a clear
    evs = detect_events({"21": 1, "24": "standby"}, {"24": "cat_get_in"}, 10.0)
    assert BIN_CLEAR not in kinds(evs)

def test_single_elimination_when_dp7_and_dp102_both_change():
    evs = detect_events({"7": 1, "102": None}, {"7": 2, "102": "ADcAAA=="}, 11.0)
    elims = [e for e in evs if e.kind == ELIMINATION]
    assert len(elims) == 1
    assert elims[0].detail["use_record"] == 55
