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
