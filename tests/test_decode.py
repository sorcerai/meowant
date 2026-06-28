from mw import decode

def test_hhmm():
    assert decode.hhmm(1320) == "22:00"
    assert decode.hhmm(480) == "08:00"

def test_decode_bits():
    assert decode.decode_bits(0, decode.NOTIFY_BITS) == ["none"]
    assert decode.decode_bits(1, decode.NOTIFY_BITS) == ["garbage_box_full"]

def test_decode_dp102():
    assert decode.decode_dp102("ADcAAA==") == 55

def test_fault_summary_e1_is_human_readable():
    # dp22 bit0 (=1) is E1 = "infrared protection" per the Meowant app. The summary
    # must name the code AND say what to do, so an alert is actionable by a sitter.
    s = decode.fault_summary(1)
    assert "E1" in s
    assert "infrared protection" in s.lower()

def test_fault_summary_unknown_code_still_labeled(tmp_path=None):
    # An undocumented fault bit must still surface its E-code, never a bare number.
    s = decode.fault_summary(0b100)        # bit2 -> E3, meaning unknown
    assert "E3" in s

def test_fault_summary_none_when_no_fault():
    assert decode.fault_summary(0) is None
    assert decode.decode_dp102("AOMAAA==") == 227

def test_status_values():
    assert "cat_get_in" in decode.STATUS_VALUES
    assert "clean_done" in decode.STATUS_VALUES

def test_label():
    assert decode.label(24) == "status"
    assert decode.label("101") == "contents_load"
    assert decode.label(999) == "dp999"

def test_named_maps_and_decodes():
    n = decode.named({"24": "standby", "101": 314, "102": "ADcAAA=="})
    assert n["status"] == "standby"
    assert n["contents_load"] == 314
    assert n["use_record"] == 55      # dp102 base64 decoded to mass
