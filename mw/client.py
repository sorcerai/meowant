"""Minimal HTTP client for the meowantd API (stdlib only)."""
import json
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8765"


def get_state(base=DEFAULT_BASE, timeout=5):
    with urllib.request.urlopen(f"{base}/state", timeout=timeout) as r:
        return json.loads(r.read().decode())


def send_command(base, action, value=None, timeout=5):
    body = json.dumps({"action": action, "value": value}).encode()
    req = urllib.request.Request(f"{base}/command", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())
