import json, os, sys

def load(path="config.json"):
    if not os.path.exists(path):
        sys.exit(f"Missing config at {path} — copy config.example.json and fill it in.")
    with open(path) as f:
        return json.load(f)

def get(cfg, dotted, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
