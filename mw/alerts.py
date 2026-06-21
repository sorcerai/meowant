"""Subscribe to the event bus and dispatch notifications for alert-worthy events."""
import queue
import shutil
import subprocess
import urllib.request

from mw.events import BIN_FULL, CHUTE_FULL, FAULT, ELIMINATION

_MESSAGES = {
    BIN_FULL: lambda e: "🪣 Litter bin full — time to empty it",
    CHUTE_FULL: lambda e: "⚠️ Waste chute full or blocked",
    FAULT: lambda e: f"❌ SC10 fault: {e.detail.get('bitmap')}",
    ELIMINATION: lambda e: "🐈 A cat used the litter box",
}


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


def ntfy_notify(msg, topic, server="https://ntfy.sh"):
    """Push to a phone via ntfy. Subscribe to <topic> in the ntfy app to receive."""
    try:
        req = urllib.request.Request(
            f"{server}/{topic}", data=msg.encode("utf-8"), method="POST",
            headers={"Title": "Meowant SC10", "Tags": "cat"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[alert] ntfy failed ({e}); msg: {msg}")


def make_notify(cfg_get):
    """Pick the notify transport from config: ntfy (phone) if alerts.ntfy_topic set, else macOS."""
    topic = cfg_get("alerts.ntfy_topic")
    if topic:
        return lambda m: ntfy_notify(m, topic)
    return macos_notify


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
