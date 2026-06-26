"""Canonical DPS maps + decoders for the Meowant SC10 (Tuya category msp)."""
import base64

DOCUMENTED = {
    4: "auto_clean", 5: "delay_clean_time", 7: "excretion_times_day",
    10: "sleep", 11: "sleep_start_time", 12: "sleep_end_time",
    21: "notification", 22: "fault", 23: "factory_reset", 24: "status",
}
STATUS_VALUES = ["standby", "cat_get_in", "waiting", "cleaning", "clean_done"]
VENDOR = {
    101: "contents_load", 102: "use_record", 103: "flag_103?", 104: "substate_a",
    105: "flag_105?", 106: "substate_b", 107: "phase", 108: "flag_108?",
    109: "flag_109?", 111: "flag_111?",
}
PHASE_VALUES = ["enter", "finish_clean"]
NOTIFY_BITS = ["garbage_box_full", "E1", "E2", "E3", "E4", "E5"]


def hhmm(m):
    try:
        m = int(m)
        return f"{m // 60:02d}:{m % 60:02d}"
    except (TypeError, ValueError):
        return str(m)


def decode_bits(val, labels):
    val = int(val or 0)
    on = [labels[i] for i in range(len(labels)) if val & (1 << i)]
    return on or ["none"]


def decode_dp102(b64):
    try:
        return int.from_bytes(base64.b64decode(b64)[:2], "big")
    except Exception:
        return None


def classify_waste(use_record, pee_threshold=80, poop_threshold=130):
    """Pee vs poop from dp102 use_record (waste magnitude). Validated
    on real labeled visits: pees 39-76, the one poop 140.
    Returns 'pee' | 'poop' | 'uncertain' | None.
    Boundary values (pee_threshold < x < poop_threshold) are uncertain."""
    if use_record is None:
        return None
    if use_record <= pee_threshold:
        return "pee"
    if use_record >= poop_threshold:
        return "poop"
    return "uncertain"


def label(k):
    """Human-readable name for a DP key (string or int); falls back to dp<k>."""
    try:
        ik = int(k)
    except (TypeError, ValueError):
        return f"dp{k}"
    return DOCUMENTED.get(ik) or VENDOR.get(ik) or f"dp{ik}"


def named(dps):
    """Map a raw DPS dict {"24": "standby", ...} to {"status": "standby", ...}.
    use_record (dp102) is also decoded from its base64 to the integer mass."""
    out = {}
    for k, v in dps.items():
        name = label(k)
        if str(k) == "102" and isinstance(v, str):
            v = decode_dp102(v)
        out[name] = v
    return out
