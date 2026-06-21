from mw import decode

def test_hhmm():
    assert decode.hhmm(1320) == "22:00"
    assert decode.hhmm(480) == "08:00"

def test_decode_bits():
    assert decode.decode_bits(0, decode.NOTIFY_BITS) == ["none"]
    assert decode.decode_bits(1, decode.NOTIFY_BITS) == ["garbage_box_full"]

def test_decode_dp102():
    assert decode.decode_dp102("ADcAAA==") == 55
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
