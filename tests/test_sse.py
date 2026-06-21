import json
from werkzeug.test import EnvironBuilder
from mw import store
from mw.device import FakeDevice
from mw.daemon import Daemon
from mw.smartclean import SmartClean
from mw.bus import EventBus
from mw.events import Event, BIN_FULL
from mw.api import create_app


def test_events_endpoint_streams_published_event(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    dev = FakeDevice([{"24": "standby"}])
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0)
    bus = EventBus()
    app = create_app(d, conn, bus=bus)

    captured = {}
    def start_response(status, headers, exc_info=None):
        captured["headers"] = dict(headers)

    # Drive the WSGI app directly so the body iterator stays lazy.
    # client.get(buffered=False) would block because the test client primes the
    # first chunk (running gen() → q.get()) before .get() returns, deadlocking
    # against the in-view bus.subscribe() that hasn't published yet.
    app_iter = app(EnvironBuilder(method="GET", path="/events").get_environ(),
                   start_response)
    gen = iter(app_iter)                          # subscription already happened
    assert captured["headers"]["Content-Type"].startswith("text/event-stream")

    bus.publish(Event(BIN_FULL, 1.0, {}))         # now lands in the subscribed queue
    chunk = next(gen)                             # runs gen() once, returns the frame
    text = chunk.decode() if isinstance(chunk, bytes) else chunk
    assert text.startswith("data: ")
    assert "bin_full" in text
    payload = json.loads(text[len("data: "):].strip())
    assert payload["kind"] == "bin_full"
    gen.close()                                   # fires finally -> bus.unsubscribe
