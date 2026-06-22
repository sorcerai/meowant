"""Inbound Telegram command bot: text the bot /cats, /status, /health and get
the live report back.

SECURITY: every update is gated on a chat_id ALLOWLIST. Only the configured
owner chat is ever answered or acted on; messages from any other chat are
dropped (and the owner is pinged once about the intruder). This is the real
security boundary — the bot token grants send access, but the allowlist is what
stops a stranger who finds the bot from querying your data or triggering actions.

Long-polls getUpdates in a daemon thread; replies via the Telegram Bot API.
Outbound alerts (mw.alerts) use the same bot independently — getUpdates only
consumes inbound command messages, sendMessage handles both.
"""
import sys
import time
import urllib.parse
import urllib.request
import json as _json


def _http_get_updates(token, offset, timeout):
    url = (f"https://api.telegram.org/bot{token}/getUpdates"
           f"?offset={offset}&timeout={timeout}")
    with urllib.request.urlopen(url, timeout=timeout + 10) as r:
        return _json.loads(r.read()).get("result", [])


def _http_send(token, chat_id, text):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
    urllib.request.urlopen(req, timeout=10)


class TelegramBot:
    def __init__(self, token, chat_id, handlers, *,
                 getter=_http_get_updates, sender=_http_send,
                 sleep=time.sleep, poll_timeout=30):
        self.token = token
        self.allowed = str(chat_id)          # the allowlist (single owner chat)
        self.handlers = handlers             # {"/cmd": () -> str}
        self._get = getter
        self._send = sender
        self._sleep = sleep
        self.poll_timeout = poll_timeout
        self._offset = 0                     # getUpdates cursor (skip processed)
        self._warned_intruder = False        # ping the owner once per intruder episode

    def _reply(self, text):
        try:
            self._send(self.token, self.allowed, text)
        except Exception as e:
            print(f"[telegram] send failed: {e}", file=sys.stderr)

    def _help(self):
        cmds = ", ".join(sorted(self.handlers))
        return f"Commands: {cmds}"

    def _dispatch(self, text):
        cmd = text.split()[0].lower() if text.strip() else ""
        cmd = cmd.split("@")[0]              # strip @botname suffix Telegram adds in groups
        fn = self.handlers.get(cmd)
        try:
            return fn() if fn else self._help()
        except Exception as e:               # a broken handler must not kill the bot
            print(f"[telegram] handler {cmd} error: {e}", file=sys.stderr)
            return f"⚠️ {cmd} failed: {e}"

    def process(self, updates):
        """Handle a batch of getUpdates results. Returns the number of OWNER
        commands answered (intruder messages are dropped, not counted)."""
        answered = 0
        for u in updates:
            self._offset = max(self._offset, u.get("update_id", -1) + 1)
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat", {})
            text = msg.get("text") or ""
            if str(chat.get("id")) != self.allowed:
                # ALLOWLIST: not the owner — drop it, warn the owner once.
                if not self._warned_intruder:
                    self._reply(f"🔒 Ignored a command from an unauthorized chat "
                                f"(id {chat.get('id')}).")
                    self._warned_intruder = True
                continue
            self._reply(self._dispatch(text))
            answered += 1
        return answered

    def poll_once(self):
        updates = self._get(self.token, self._offset, self.poll_timeout)
        return self.process(updates)

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:           # network blip etc — back off, keep going
                print(f"[telegram] poll error: {e}", file=sys.stderr)
                self._sleep(5)
