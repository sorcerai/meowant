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


def test_breaker_fully_closes_after_successful_probe():
    """Fix 3: after breaker opens (2 errors, fail_threshold=2) and cooldown elapses,
    a SUCCESSFUL probe returns it to true CLOSED (_open_until=0). A SINGLE subsequent
    error must NOT re-trip — primary is still tried on the next frame, fallback only
    used for the errored frame (effective fail_threshold stays at 2, not 1)."""
    clock = [1000.0]
    # Deterministic per-frame result so the double-call in _StubLabeler doesn't matter
    frame_results = {
        "a.jpg": ERROR, "b.jpg": ERROR,   # open the breaker
        "c.jpg": "Ucok",                  # half-open probe succeeds → CLOSED
        "d.jpg": ERROR,                   # one error in CLOSED state (must NOT re-trip)
        "e.jpg": "Ucok",                  # primary still active after single error
    }
    primary = _StubLabeler(lambda p: frame_results[p])
    fallback = _StubLabeler(lambda p: "Ella")
    fl = FallbackLabeler(primary, fallback, fail_threshold=2,
                         cooldown_s=600, now_fn=lambda: clock[0])

    # Open the breaker (2 consecutive errors)
    fl.predict_visit(["a.jpg", "b.jpg"], {})
    assert primary.seen == ["a.jpg", "b.jpg"]

    # Advance past cooldown → half-open
    clock[0] = 1601.0
    primary.seen.clear(); fallback.seen.clear()

    # Successful half-open probe → CLOSED
    res = fl.predict_visit(["c.jpg"], {})
    assert "c.jpg" in primary.seen
    assert res[0]["cat"] == "Ucok"
    assert fallback.seen == []      # fallback not used when primary succeeds

    # One error in CLOSED state: fail_threshold still 2, so breaker does NOT re-trip
    primary.seen.clear(); fallback.seen.clear()
    fl.predict_visit(["d.jpg"], {})
    assert "d.jpg" in primary.seen   # primary was tried
    assert "d.jpg" in fallback.seen  # fallback rescued this errored frame

    # Primary still tried on the next frame (breaker NOT re-tripped by a single error)
    primary.seen.clear(); fallback.seen.clear()
    res2 = fl.predict_visit(["e.jpg"], {})
    assert "e.jpg" in primary.seen   # primary still active (CLOSED)
    assert res2[0]["cat"] == "Ucok"  # primary's result (success)
    assert fallback.seen == []       # fallback not used
