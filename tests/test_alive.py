"""AliveHeartbeat: daily proof-of-life so a dead alert PIPE reads as silence
instead of being indistinguishable from a quiet day (Jul 15 lesson)."""
import time

from mw.alive import AliveHeartbeat


def _local_epoch(y, mo, d, h, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


class _State:
    """Stand-in for store.get_daemon_state/set_daemon_state, pre-bound to a conn."""
    def __init__(self):
        self.store = {}
    def get(self, key, default=None):
        return self.store.get(key, default)
    def set(self, key, value):
        self.store[key] = value


def test_fires_once_at_or_after_hour():
    st = _State()
    msgs = []
    clock = [_local_epoch(2026, 7, 15, 9, 0)]
    hb = AliveHeartbeat(msgs.append, hour_local=9, now_fn=lambda: clock[0],
                        state_get=st.get, state_set=st.set)
    hb.tick()
    assert len(msgs) == 1 and "alive" in msgs[0].lower() and "2026-07-15" in msgs[0]
    hb.tick()                                        # same day, same tick -> no repeat
    assert len(msgs) == 1


def test_does_not_fire_before_hour():
    st = _State()
    msgs = []
    clock = [_local_epoch(2026, 7, 15, 8, 59)]
    hb = AliveHeartbeat(msgs.append, hour_local=9, now_fn=lambda: clock[0],
                        state_get=st.get, state_set=st.set)
    hb.tick()
    assert msgs == []
    clock[0] = _local_epoch(2026, 7, 15, 9, 0)
    hb.tick()
    assert len(msgs) == 1


def test_fires_once_per_calendar_day_not_twice():
    st = _State()
    msgs = []
    clock = [_local_epoch(2026, 7, 15, 9, 0)]
    hb = AliveHeartbeat(msgs.append, hour_local=9, now_fn=lambda: clock[0],
                        state_get=st.get, state_set=st.set)
    hb.tick()
    clock[0] = _local_epoch(2026, 7, 15, 20, 0)       # later same day
    hb.tick()
    assert len(msgs) == 1
    clock[0] = _local_epoch(2026, 7, 16, 9, 0)        # next day -> fires again
    hb.tick()
    assert len(msgs) == 2
    assert "2026-07-16" in msgs[1]


def test_failed_send_retries_next_tick_and_does_not_record_date():
    st = _State()
    sends = iter([False, True])
    msgs = []
    def notify(m):
        ok = next(sends)
        if ok:
            msgs.append(m)
        return ok
    clock = [_local_epoch(2026, 7, 15, 9, 0)]
    hb = AliveHeartbeat(notify, hour_local=9, now_fn=lambda: clock[0],
                        state_get=st.get, state_set=st.set)
    hb.tick()                                         # send fails
    assert msgs == []
    assert st.get("alive.last_sent_date") is None      # date NOT recorded on failure
    hb.tick()                                          # retries same day
    assert len(msgs) == 1
    assert st.get("alive.last_sent_date") == "2026-07-15"


def test_date_persists_via_injected_state():
    st = _State()
    msgs = []
    clock = [_local_epoch(2026, 7, 15, 9, 0)]
    hb = AliveHeartbeat(msgs.append, hour_local=9, now_fn=lambda: clock[0],
                        state_get=st.get, state_set=st.set)
    hb.tick()
    assert len(msgs) == 1
    # a brand-new instance sharing the same persisted state must not re-send
    hb2 = AliveHeartbeat(msgs.append, hour_local=9, now_fn=lambda: clock[0],
                         state_get=st.get, state_set=st.set)
    hb2.tick()
    assert len(msgs) == 1                              # still just the one delivery
