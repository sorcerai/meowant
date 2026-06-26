"""Safe, validated, atomic writes of the owner-editable config subset (Settings
panel). The hard rules: secrets never leave the process (read_safe) and never get
clobbered (apply_edits merges into the on-disk file); only the allowlisted fields
are writable; every value is validated BEFORE anything touches disk; the write is
atomic so a rejected edit can't leave a half-written / bricked config."""
import json
import os
import tempfile

from mw.cat_status import _DEFAULT_THRESHOLDS

# Top-level keys the Settings panel may edit. Anything else (device_id,
# local_key, cloud, cameras, ...) is rejected outright.
ALLOWED_TOP = {"quiet_start", "quiet_end", "smartclean", "feeders", "thresholds"}


def _valid_hhmm(s):
    if not isinstance(s, str) or len(s) != 5 or s[2] != ":":
        return False
    try:
        h, m = int(s[:2]), int(s[3:])
    except ValueError:
        return False
    return 0 <= h <= 23 and 0 <= m <= 59


def read_safe(cfg):
    """The editable subset only — never secrets. Thresholds merge the code
    defaults so the panel always shows every cat."""
    sc = cfg.get("smartclean", {}) or {}
    thresholds = dict(_DEFAULT_THRESHOLDS)
    thresholds.update({k: v for k, v in (cfg.get("thresholds") or {}).items()})
    return {
        "quiet_start": cfg.get("quiet_start", "22:00"),
        "quiet_end": cfg.get("quiet_end", "08:00"),
        "smartclean": {
            "enabled": bool(sc.get("enabled", False)),
            "idle_seconds": sc.get("idle_seconds", 60),
        },
        "feeders": [{"label": f.get("label"), "mealtimes": f.get("mealtimes", [])}
                    for f in (cfg.get("feeders") or [])],
        "thresholds": thresholds,
    }


def _validate(edits, cfg):
    """Return a list of human-readable errors (empty == valid)."""
    errs = []
    bad_keys = set(edits) - ALLOWED_TOP
    if bad_keys:
        errs.append(f"not editable: {', '.join(sorted(bad_keys))}")

    if "quiet_start" in edits and not _valid_hhmm(edits["quiet_start"]):
        errs.append("quiet_start must be HH:MM")
    if "quiet_end" in edits and not _valid_hhmm(edits["quiet_end"]):
        errs.append("quiet_end must be HH:MM")

    if "smartclean" in edits:
        sc = edits["smartclean"]
        if not isinstance(sc, dict):
            errs.append("smartclean must be an object")
        else:
            if "enabled" in sc and not isinstance(sc["enabled"], bool):
                errs.append("smartclean.enabled must be true/false")
            if "idle_seconds" in sc:
                v = sc["idle_seconds"]
                if not isinstance(v, int) or isinstance(v, bool) or not (10 <= v <= 3600):
                    errs.append("smartclean.idle_seconds must be 10..3600")

    if "thresholds" in edits:
        th = edits["thresholds"]
        if not isinstance(th, dict):
            errs.append("thresholds must be an object")
        else:
            for cat, v in th.items():
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not (1 <= v <= 168):
                    errs.append(f"threshold for {cat} must be 1..168 hours")

    if "feeders" in edits:
        known = {f.get("label") for f in (cfg.get("feeders") or [])}
        if not isinstance(edits["feeders"], list):
            errs.append("feeders must be a list")
        else:
            for f in edits["feeders"]:
                label = (f or {}).get("label")
                if label not in known:
                    errs.append(f"unknown feeder '{label}'")
                    continue
                mt = f.get("mealtimes")
                if not isinstance(mt, list) or not all(_valid_hhmm(t) for t in mt):
                    errs.append(f"feeder '{label}' mealtimes must be a list of HH:MM")
    return errs


def _merge(cfg, edits):
    """Deep-merge the validated edits into a copy of cfg, preserving every key not
    being edited (secrets, feeder device fields, smartclean.max_wait_seconds, ...)."""
    out = json.loads(json.dumps(cfg))   # deep copy
    for k in ("quiet_start", "quiet_end"):
        if k in edits:
            out[k] = edits[k]
    if "smartclean" in edits:
        out.setdefault("smartclean", {}).update(edits["smartclean"])
    if "thresholds" in edits:
        out.setdefault("thresholds", {}).update(edits["thresholds"])
    if "feeders" in edits:
        by_label = {f["label"]: f for f in edits["feeders"]}
        for f in out.get("feeders", []):
            if f.get("label") in by_label:
                f["mealtimes"] = sorted(set(by_label[f["label"]]["mealtimes"]))
    return out


def apply_edits(path, edits):
    """Validate `edits`, merge into the config at `path`, write atomically.
    Raises ValueError (nothing written) if any field is invalid. Returns the new
    safe subset on success."""
    with open(path) as f:
        cfg = json.load(f)
    errs = _validate(edits, cfg)
    if errs:
        raise ValueError("; ".join(errs))
    merged = _merge(cfg, edits)
    # atomic: write to a temp file in the same dir, then os.replace (rename).
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return read_safe(merged)
