"""Subscribe to the event bus and dispatch notifications for alert-worthy events."""
import queue
import shutil
import subprocess
import urllib.parse
import urllib.request

from mw.events import CHUTE_FULL

_MESSAGES = {
    CHUTE_FULL: lambda e: "⚠️ Waste chute full or blocked",
    # BIN_FULL and FAULT are NOT here — BoxHealthWatch owns both (re-nag + UNUSABLE
    # escalation, with human-readable E-code text for faults); Alerts must not
    # double-ping them.
    # ELIMINATION is NOT here — named alerts are sent by EliminationNotifier
    # (label-on-leave, ~30s delayed) so the cat's name resolves before the push.
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
    return True


def ntfy_notify(msg, topic, server="https://ntfy.sh"):
    """Push to a phone via ntfy. Subscribe to <topic> in the ntfy app to receive.
    Returns True on confirmed delivery, False on failure (so the dead-man's switch
    can refrain from latching an alert it never actually sent)."""
    try:
        req = urllib.request.Request(
            f"{server}/{topic}", data=msg.encode("utf-8"), method="POST",
            headers={"Title": "Meowant SC10", "Tags": "cat"})
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        print(f"[alert] ntfy failed ({e}); msg: {msg}")
        return False


def telegram_notify(msg, token, chat_id):
    """Push via the Telegram Bot API. Messages carry an absolute send time in the
    client, so (unlike ntfy) the 'when' never collapses to a vague 'yesterday'.

    `chat_id` may be a single id or a list (owner + sitter, for unattended care).
    Returns True if delivered to AT LEAST ONE recipient — so an alert that reached
    a human latches — and False only if EVERY recipient failed, so a total outage
    keeps retrying instead of the dead-man's switch latching an unsent alert."""
    ids = chat_id if isinstance(chat_id, (list, tuple)) else [chat_id]
    ok_any = False
    for cid in ids:
        try:
            data = urllib.parse.urlencode(
                {"chat_id": cid, "text": msg}).encode("utf-8")
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=data, method="POST")
            urllib.request.urlopen(req, timeout=5)
            ok_any = True
        except Exception as e:
            print(f"[alert] telegram to {cid} failed ({e}); msg: {msg}")
    return ok_any


def make_notify(cfg_get, owner_only=False):
    """Pick the notify transport from config, best-channel first: Telegram (if a bot
    token + at least one chat id are set) > ntfy (if a topic is set) > macOS desktop.

    Recipients = alerts.telegram_chat_id (owner) + alerts.telegram_chat_ids (extra,
    e.g. a sitter while away), deduped, owner first. A single id still works; adding
    a sitter is just appending to telegram_chat_ids in config.

    owner_only=True ignores the extra recipients — for routine/technical pings that
    should reach only the owner, not the sitter (who gets just the important ones)."""
    token = cfg_get("alerts.telegram_bot_token")
    primary = cfg_get("alerts.telegram_chat_id")
    extra = [] if owner_only else (cfg_get("alerts.telegram_chat_ids") or [])
    if isinstance(extra, str):
        extra = [extra]
    seen, recipients = set(), []
    for c in ([primary] if primary else []) + list(extra):
        if c and c not in seen:
            seen.add(c)
            recipients.append(c)
    if token and recipients:
        return lambda m: telegram_notify(m, token, recipients)
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
