"""BowlWatch: cat-free gate + diff + agy-confirm + debounce -> alert/auto-feed."""
from mw import store, bowl
from mw.bowl_watch import BowlWatch

T = 1_000_000.0


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


class _Cat:
    def __init__(self, clear=True):
        self.clear = clear

    def is_clear(self, path):
        return self.clear


class _Feeder:
    def __init__(self, ok=True):
        self.ok = ok
        self.fed = []
        self.label = "test_feeder"

    def feed(self, n):
        self.fed.append(n)
        return self.ok


def _watch(tmp_path, conn, **kw):
    # grab returns a fixed path; fullness is stubbed via monkeypatch in each test
    kw.setdefault("empty_ref", "ref.jpg")
    return BowlWatch(grab=lambda: "frame.jpg", catfilter=_Cat(), conn=conn,
                     notify=kw.pop("notify"), now_fn=lambda: T, **kw)


def test_cat_present_frame_is_skipped(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    msgs = []
    w = BowlWatch(grab=lambda: "f.jpg", catfilter=_Cat(clear=False), conn=conn,
                  notify=msgs.append, now_fn=lambda: T, empty_ref="ref.jpg",
                  confirm_empty=lambda p: True)
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once()
    assert msgs == []                              # cat at bowl -> no judgment
    assert store.recent_bowl_events(conn) == []


def test_two_confirmed_empties_alert_once_then_latch(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    msgs = []
    w = _watch(tmp_path, conn, notify=msgs.append, confirm_empty=lambda p: True)
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once()                                  # streak 1 -> no alert (debounce)
    assert msgs == []
    w.poll_once()                                  # streak 2 -> alert
    w.poll_once()                                  # still empty -> latched, no repeat
    assert len(msgs) == 1 and "empty" in msgs[0].lower()


def test_single_empty_then_food_does_not_alert(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    msgs = []
    w = _watch(tmp_path, conn, notify=msgs.append, confirm_empty=lambda p: True)
    states = iter([bowl.EMPTY, bowl.FULL])
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: next(states))
    w.poll_once()                                  # empty streak 1
    w.poll_once()                                  # full -> resets streak
    assert msgs == []


def test_agy_says_food_blocks_empty_action(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    msgs = []
    w = _watch(tmp_path, conn, notify=msgs.append, confirm_empty=lambda p: False)  # agy: has food
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once(); w.poll_once()
    assert msgs == []                              # diff said empty, agy overruled


def test_auto_feed_dispenses_and_rate_limits(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    msgs = []
    feeder = _Feeder(ok=True)
    w = BowlWatch(grab=lambda: "f.jpg", catfilter=_Cat(), conn=conn,
                  notify=msgs.append, feeder=feeder, now_fn=lambda: T,
                  empty_ref="ref.jpg", confirm_empty=lambda p: True,
                  auto_feed=True, auto_feed_portions=2, auto_feed_max_per_day=1)
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once(); w.poll_once()                   # confirmed empty -> auto-feed once
    assert feeder.fed == [2]
    assert store.auto_feeds_today(conn) == 1
    # next empty episode after a refill: rate limit (1/day) reached -> alert, no feed
    w._empty_alerted = False; w._prev_state = bowl.FULL   # simulate a refill+empty again
    w.poll_once()
    assert feeder.fed == [2]                        # not fed again
    assert any("limit" in m.lower() for m in msgs)


def test_consumption_logged_on_full_to_empty(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    store.log_feed_event(conn, 2, "scheduled", ts=T - 7200)   # fed 2h before
    msgs = []
    w = _watch(tmp_path, conn, notify=msgs.append, confirm_empty=lambda p: True)
    w._prev_state = bowl.FULL
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once(); w.poll_once()
    assert store.last_consumption_secs(conn) == 7200          # ~2h to empty


def test_failed_delivery_does_not_latch(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    sent = []

    def _notify(m):
        sent.append(m)
        return False

    w = _watch(tmp_path, conn, notify=_notify, confirm_empty=lambda p: True)
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once(); w.poll_once()                    # streak 2 -> tries to alert (fails)
    w.poll_once()                                   # still empty -> retries (not latched)
    assert len(sent) == 2


def test_readout_reflects_current_state_not_stuck_empty(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    w = _watch(tmp_path, conn, notify=lambda m: None, confirm_empty=lambda p: True)
    seq = iter([bowl.FULL, bowl.EMPTY, bowl.EMPTY, bowl.FULL])
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: next(seq))
    w.poll_once()                       # full  -> logs 'full'
    assert store.last_bowl_state(conn) == "full"
    w.poll_once(); w.poll_once()        # empty x2 -> confirmed empty
    assert store.last_bowl_state(conn) == "empty"
    w.poll_once()                       # full again -> readout must follow, not stay 'empty'
    assert store.last_bowl_state(conn) == "full"


def test_non_empty_logged_only_on_change(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    w = _watch(tmp_path, conn, notify=lambda m: None, confirm_empty=lambda p: True)
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.FULL)
    w.poll_once(); w.poll_once(); w.poll_once()    # 3 identical 'full' reads
    rows = [r for r in store.recent_bowl_events(conn) if r["source"] == "vision"]
    assert len(rows) == 1               # logged once (on change), not every poll


def test_feed_fail_latches_alert_but_keeps_retrying(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    sent = []
    feeder = _Feeder(ok=False)
    def _notify(m):
        sent.append(m)
        return True
    w = BowlWatch(grab=lambda: "f.jpg", catfilter=_Cat(), conn=conn,
                  notify=_notify, feeder=feeder, now_fn=lambda: T,
                  empty_ref="ref.jpg", confirm_empty=lambda p: True,
                  auto_feed=True, auto_feed_portions=2)
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once(); w.poll_once()                    # tries to feed, fails -> alerts FAILED
    assert len(sent) == 1
    assert "FAILED" in sent[-1]
    assert len(feeder.fed) == 1
    assert not w._empty_alerted                     # should not latch empty episode so it retries
    assert w._feed_fail_alerted                     # but the failure alert IS latched
    w.poll_once()                                   # retries
    assert len(feeder.fed) == 2                     # keeps retrying
    assert len(sent) == 1                           # but does not re-alert


def test_agy_returns_none_skips_action(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    msgs = []
    w = _watch(tmp_path, conn, notify=msgs.append, confirm_empty=lambda p: None)
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once(); w.poll_once()
    assert msgs == []
    assert w._empty_streak == 0


def test_auto_feed_false_with_feeder_only_alerts(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    msgs = []
    feeder = _Feeder(ok=True)
    w = BowlWatch(grab=lambda: "f.jpg", catfilter=_Cat(), conn=conn,
                  notify=msgs.append, feeder=feeder, now_fn=lambda: T,
                  empty_ref="ref.jpg", confirm_empty=lambda p: True,
                  auto_feed=False)
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once(); w.poll_once()
    assert msgs
    assert not feeder.fed                           # never calls feeder
    assert w._empty_alerted                         # latches normally

