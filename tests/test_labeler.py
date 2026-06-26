from mw.labeler import FallbackLabeler, ERROR


class _StubLabeler:
    """Returns a scripted result per frame; counts how many frames it saw."""
    def __init__(self, result_for):   # result_for: callable(path) -> cat string
        self.result_for = result_for
        self.seen = []
    def predict_visit(self, frame_paths, refs):
        self.seen.extend(frame_paths)
        return [{"file": p, "cat": self.result_for(p), "confidence":
                 0.0 if self.result_for(p) in (ERROR, "none") else 1.0}
                for p in frame_paths]


def test_fallback_uses_fallback_only_on_primary_error():
    primary = _StubLabeler(lambda p: ERROR if "bad" in p else "Ucok")
    fallback = _StubLabeler(lambda p: "Ella")
    fl = FallbackLabeler(primary, fallback, now_fn=lambda: 1000.0)
    out = fl.predict_visit(["good1.jpg", "bad1.jpg"], {})
    assert out[0]["cat"] == "Ucok"          # primary handled the good frame
    assert out[1]["cat"] == "Ella"          # fallback rescued the errored frame
    assert fallback.seen == ["bad1.jpg"]    # fallback only saw the error frame


def test_breaker_opens_after_threshold_and_skips_primary():
    primary = _StubLabeler(lambda p: ERROR)         # primary is fully down
    fallback = _StubLabeler(lambda p: "Ella")
    fl = FallbackLabeler(primary, fallback, fail_threshold=2,
                         cooldown_s=1800, now_fn=lambda: 1000.0)
    fl.predict_visit(["a.jpg", "b.jpg", "c.jpg", "d.jpg"], {})
    # After 2 consecutive primary errors the breaker opens; frames c,d skip primary.
    assert primary.seen == ["a.jpg", "b.jpg"]       # primary tried only twice, then skipped
    assert fallback.seen == ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]  # all rescued


def test_breaker_half_opens_after_cooldown():
    clock = [1000.0]
    primary = _StubLabeler(lambda p: ERROR)
    fallback = _StubLabeler(lambda p: "Ella")
    fl = FallbackLabeler(primary, fallback, fail_threshold=1,
                         cooldown_s=600, now_fn=lambda: clock[0])
    fl.predict_visit(["a.jpg"], {})                 # trips open
    primary.seen.clear()
    clock[0] = 1000.0 + 599                          # still in cooldown
    fl.predict_visit(["b.jpg"], {})
    assert primary.seen == []                        # skipped — breaker open
    clock[0] = 1000.0 + 601                          # cooldown elapsed
    fl.predict_visit(["c.jpg"], {})
    assert primary.seen == ["c.jpg"]                 # half-open: primary retried


def test_breaker_re_trips_on_half_open_failure():
    clock = [1000.0]
    primary = _StubLabeler(lambda p: ERROR)
    fallback = _StubLabeler(lambda p: "Ella")
    fl = FallbackLabeler(primary, fallback, fail_threshold=1,
                         cooldown_s=600, now_fn=lambda: clock[0])
    fl.predict_visit(["a.jpg"], {})        # trips open
    clock[0] = 1000.0 + 601                 # cooldown elapsed
    primary.seen.clear()
    fl.predict_visit(["b.jpg"], {})         # half-open probe: fails -> re-trips
    assert primary.seen == ["b.jpg"]        # probed once
    primary.seen.clear()
    fl.predict_visit(["c.jpg"], {})         # still open -> primary skipped
    assert primary.seen == []
