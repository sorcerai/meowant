# meowantd Phase 1 — SSE events + alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) tracking.

**Goal:** Let the daemon broadcast live events (so TUI/web/alerts can subscribe to one socket-owner) and dispatch notifications for bin-full / fault / cat-used-box.

**Architecture:** A thread-safe in-process `EventBus` (pub/sub). The daemon already has an `on_event` hook — wire it to `bus.publish`. The Flask API gains a `GET /events` SSE endpoint that streams from a bus subscription. An `Alerts` service subscribes to the bus and calls an injectable `notify(msg)` (default: macOS notification; optional ntfy webhook from config).

**Tech Stack:** Python 3.10, Flask, stdlib `queue`/`threading`, pytest. Beads: meowant-k03 (SSE), meowant-2kn (alerts).

## Global Constraints
- Run from repo root `~/repos/meowant`, `python3` (3.10). Tests: `python3 -m pytest`.
- Only the daemon owns the device; these components are pure in-process consumers of the event bus — they never touch the device or DB connection.
- `Event` is `mw.events.Event(kind, ts, detail)`. Event kinds: cat_enter, cat_leave, clean_start, clean_done, bin_full, bin_clear, fault, elimination.
- Do not block the daemon: bus publish must be non-blocking (drop on full subscriber queue).

---

### Task 1: EventBus (`mw/bus.py`)

**Files:** Create `mw/bus.py`, Test `tests/test_bus.py`

**Interfaces:**
- Produces: `EventBus()` with `subscribe() -> queue.Queue`, `unsubscribe(q)`, `publish(event) -> None` (non-blocking; drops into full queues without raising). Thread-safe.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bus.py
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
```

- [ ] **Step 2: Run test, verify fail** — `python3 -m pytest tests/test_bus.py -v` → `ModuleNotFoundError: No module named 'mw.bus'`

- [ ] **Step 3: Implement**

```python
# mw/bus.py
"""Thread-safe in-process pub/sub for daemon events. Publish never blocks."""
import queue
import threading


class EventBus:
    def __init__(self, maxsize=100):
        self._subs = []
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def subscribe(self):
        q = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # slow consumer drops events rather than stalling the daemon
```

- [ ] **Step 4: Run test, verify pass** — `python3 -m pytest tests/test_bus.py -v`

- [ ] **Step 5: Commit** — `git add mw/bus.py tests/test_bus.py && git commit -m "feat: thread-safe in-process EventBus"`

---

### Task 2: Alerts service (`mw/alerts.py`)

**Files:** Create `mw/alerts.py`, Test `tests/test_alerts.py`

**Interfaces:**
- Consumes: `EventBus`, `mw.events.Event`.
- Produces: `alert_message(event) -> str | None` (pure mapping, easy to test); `Alerts(bus, notify)` with `run_once()` (drain current queue, dispatch) and `run()` (loop, for the daemon thread). `notify` is a callable `(str) -> None`. Also `macos_notify(msg)` default transport using `osascript`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_alerts.py
from mw.bus import EventBus
from mw.events import Event, BIN_FULL, ELIMINATION, FAULT, CAT_ENTER
from mw.alerts import alert_message, Alerts

def test_alert_message_mapping():
    assert "bin" in alert_message(Event(BIN_FULL, 1.0)).lower()
    assert alert_message(Event(ELIMINATION, 1.0, {})) is not None
    assert "fault" in alert_message(Event(FAULT, 1.0, {"bitmap": 2})).lower()
    assert alert_message(Event(CAT_ENTER, 1.0)) is None  # not alert-worthy

def test_alerts_dispatches_via_notify():
    bus = EventBus(); sent = []
    a = Alerts(bus, notify=sent.append)
    bus.publish(Event(BIN_FULL, 1.0))
    bus.publish(Event(CAT_ENTER, 2.0))   # ignored
    a.run_once()
    assert len(sent) == 1 and "bin" in sent[0].lower()
```

- [ ] **Step 2: Run test, verify fail** — `ModuleNotFoundError: No module named 'mw.alerts'`

- [ ] **Step 3: Implement**

```python
# mw/alerts.py
"""Subscribe to the event bus and dispatch notifications for alert-worthy events."""
import queue
import shutil
import subprocess

from mw.events import BIN_FULL, CHUTE_FULL, FAULT, ELIMINATION

_MESSAGES = {
    BIN_FULL: lambda e: "🪣 Litter bin full — time to empty it",
    FAULT: lambda e: f"❌ SC10 fault: {e.detail.get('bitmap')}",
    ELIMINATION: lambda e: "🐈 A cat used the litter box",
}
# CHUTE_FULL may not exist yet (added when the drawer-pull experiment lands);
# guard so this module imports cleanly either way.
try:
    _MESSAGES[CHUTE_FULL] = lambda e: "⚠️ Waste chute full or blocked"
except NameError:
    pass


def alert_message(event):
    fn = _MESSAGES.get(event.kind)
    return fn(event) if fn else None


def macos_notify(msg):
    if shutil.which("osascript"):
        subprocess.run(
            ["osascript", "-e", f'display notification {msg!r} with title "Meowant SC10"'],
            check=False)
    else:
        print(f"[alert] {msg}")


class Alerts:
    def __init__(self, bus, notify=macos_notify):
        self.bus = bus
        self.notify = notify
        self._q = bus.subscribe()

    def run_once(self):
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                return
            msg = alert_message(ev)
            if msg:
                self.notify(msg)

    def run(self):
        while True:
            ev = self._q.get()
            msg = alert_message(ev)
            if msg:
                self.notify(msg)
```

Note: `mw/events.py` does not currently define `CHUTE_FULL`. Add it as a constant `CHUTE_FULL = "chute_full"` in `mw/events.py` (no detection logic yet — that arrives with the drawer-pull experiment) so the import in `alerts.py` resolves. Adjust the `try/except NameError` to a direct import once added.

- [ ] **Step 2b:** Add `CHUTE_FULL = "chute_full"` to `mw/events.py` constants and `from mw.events import ... CHUTE_FULL` in alerts; drop the try/except.

- [ ] **Step 3b: Run test, verify pass** — `python3 -m pytest tests/test_alerts.py -v`

- [ ] **Step 4: Commit** — `git add mw/alerts.py mw/events.py tests/test_alerts.py && git commit -m "feat: alerts service with injectable notify transport"`

---

### Task 3: Wire bus into daemon + SSE endpoint + meowantd

**Files:** Modify `mw/api.py`, `meowantd.py`, Test `tests/test_sse.py`

**Interfaces:**
- `create_app(daemon, conn, bus=None)` — when `bus` is provided, add `GET /events` (SSE, `text/event-stream`) that streams JSON `{kind, ts, detail}` per event from a bus subscription, cleaning up the subscription on disconnect.
- `meowantd.py`: build `bus = EventBus()`, set `daemon.on_event = bus.publish`, start `Alerts(bus, notify).run` on a daemon thread, pass `bus` to `create_app`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sse.py
import json
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
    client = create_app(d, conn, bus=bus).test_client()
    # open the stream, publish, read one SSE frame
    resp = client.get("/events", buffered=False)
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    bus.publish(Event(BIN_FULL, 1.0, {}))
    chunk = next(resp.response)  # first streamed frame
    text = chunk.decode() if isinstance(chunk, bytes) else chunk
    assert "bin_full" in text
    assert text.startswith("data: ")
    payload = json.loads(text[len("data: "):].strip())
    assert payload["kind"] == "bin_full"
```

- [ ] **Step 2: Run test, verify fail** — the `/events` route does not exist (404 / KeyError).

- [ ] **Step 3: Implement the SSE route**

In `mw/api.py`: add `import json` and `from flask import Response` at top if missing. Change `create_app(daemon, conn)` to `create_app(daemon, conn, bus=None)`. Inside, after the other routes, add:

```python
    if bus is not None:
        @app.get("/events")
        def events_stream():
            q = bus.subscribe()

            def gen():
                try:
                    while True:
                        ev = q.get()
                        yield ("data: " + json.dumps(
                            {"kind": ev.kind, "ts": ev.ts, "detail": ev.detail}) + "\n\n")
                finally:
                    bus.unsubscribe(q)

            return Response(gen(), mimetype="text/event-stream")
```

- [ ] **Step 4: Run test, verify pass** — `python3 -m pytest tests/test_sse.py -v`

- [ ] **Step 5: Wire meowantd.py**

In `meowantd.py`, add imports `from mw.bus import EventBus` and `from mw.alerts import Alerts, macos_notify`. After building `daemon`:

```python
    bus = EventBus()
    daemon.on_event = bus.publish
    alerts = Alerts(bus, notify=macos_notify)
    threading.Thread(target=alerts.run, daemon=True).start()
```

Change the app build to `app = create_app(daemon, conn, bus=bus)`. Keep everything else.

- [ ] **Step 6: Run full suite** — `python3 -m pytest tests/ -q` → all pass.

- [ ] **Step 7: Commit** — `git add mw/api.py meowantd.py tests/test_sse.py && git commit -m "feat: SSE /events endpoint + wire bus/alerts into meowantd"`

---

## Self-Review
- SSE (meowant-k03): Task 1 (bus) + Task 3 (endpoint + wiring). ✅
- Alerts (meowant-2kn): Task 2 + wiring in Task 3. ✅
- Chute alert: `CHUTE_FULL` constant added, message wired, but detection deferred to the drawer-pull experiment (meowant-76h / meowant-apk) — documented, not built. ✅
- Non-blocking publish (daemon never stalls on a slow SSE client): EventBus drops on full queue. ✅
- Placeholder scan: none. Type consistency: `Event(kind, ts, detail)`, `bus.publish/subscribe/unsubscribe`, `create_app(daemon, conn, bus=None)` consistent across tasks.
