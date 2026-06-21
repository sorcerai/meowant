from mw.bus import EventBus

def test_publish_reaches_subscribers():
    bus = EventBus()
    q1 = bus.subscribe(); q2 = bus.subscribe()
    bus.publish("e1")
    assert q1.get_nowait() == "e1"
    assert q2.get_nowait() == "e1"

def test_unsubscribe_stops_delivery():
    bus = EventBus()
    q = bus.subscribe(); bus.unsubscribe(q)
    bus.publish("x")
    import queue
    try:
        q.get_nowait(); assert False, "should be empty"
    except queue.Empty:
        pass

def test_publish_never_raises_on_full_queue():
    bus = EventBus(maxsize=1)
    q = bus.subscribe()
    bus.publish("a"); bus.publish("b")  # second would overflow — must not raise
    assert q.get_nowait() == "a"
