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


def _http_send_markup(token, chat_id, text, markup):
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "reply_markup": _json.dumps(markup),
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
    urllib.request.urlopen(req, timeout=10)


def _post_photo(token, chat_id, path, caption=None, reply_markup=None):
    boundary = "----meowant" + str(abs(hash(path)) % 10**8)
    parts = []
    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    field("chat_id", str(chat_id))
    if caption: field("caption", caption)
    if reply_markup is not None: field("reply_markup", _json.dumps(reply_markup))
    with open(path, "rb") as f:
        img = f.read()
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                  f"filename=\"f.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n").encode() + img + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=20)


def send_label_request(token, chat_id, vid, frame_paths, cats, when):
    """Up to 3 photos, then a buttons message asking who used the box."""
    import os
    for p in [p for p in frame_paths if os.path.exists(p)][:3]:
        try:
            _post_photo(token, chat_id, p)
        except Exception as e:
            print(f"[telegram] photo {p} failed: {e}", file=sys.stderr)
    row = [{"text": c, "callback_data": f"lbl:{vid}:{c}"} for c in cats]
    markup = {"inline_keyboard": [row, [{"text": "skip", "callback_data": f"lbl:{vid}:skip"}]]}
    try:
        _http_send_markup(token, chat_id,
                          f"🐈 Who used the box at {when}? (couldn't auto-ID)", markup)
    except Exception as e:
        print(f"[telegram] label-request failed: {e}", file=sys.stderr)


class TelegramBot:
    def __init__(self, token, chat_id, handlers, *,
                 getter=_http_get_updates, sender=_http_send,
                 sleep=time.sleep, poll_timeout=30, label_cb=None):
        self.token = token
        self.allowed = str(chat_id)          # the allowlist (single owner chat)
        self.handlers = handlers             # {"/cmd": () -> str}
        self._get = getter
        self._send = sender
        self._sleep = sleep
        self.poll_timeout = poll_timeout
        self._offset = 0                     # getUpdates cursor (skip processed)
        self._warned_intruder = False        # ping the owner once per intruder episode
        self.label_cb = label_cb             # (vid: int, cat: str) -> str, or None

    def _reply(self, text):
        try:
            self._send(self.token, self.allowed, text)
        except Exception as e:
            print(f"[telegram] send failed: {e}", file=sys.stderr)

    def _answer_callback(self, cq_id):
        """Dismiss Telegram's spinner for a callback query. Thin network call — swallow errors."""
        try:
            data = urllib.parse.urlencode({"callback_query_id": cq_id}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                data=data, method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"[telegram] answerCallbackQuery failed: {e}", file=sys.stderr)

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
            cq = u.get("callback_query")
            if cq is not None:
                self._answer_callback(cq.get("id"))            # dismiss Telegram's spinner
                frm = str((cq.get("from") or {}).get("id"))
                if frm != self.allowed:
                    continue                                   # allowlist taps too
                data = cq.get("data") or ""
                if data.startswith("lbl:"):
                    _, vid, cat = data.split(":", 2)
                    if cat == "skip" or self.label_cb is None:
                        self._reply(f"⏭️ Skipped visit {vid}")
                    else:
                        self._reply(self.label_cb(int(vid), cat))
                continue
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
