import json, mw.config as C

def test_get_nested(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"device_id": "x", "smartclean": {"idle_seconds": 45}}))
    cfg = C.load(str(p))
    assert cfg["device_id"] == "x"
    assert C.get(cfg, "smartclean.idle_seconds", 90) == 45
    assert C.get(cfg, "smartclean.missing", 90) == 90
    assert C.get(cfg, "nope.deep", "d") == "d"
