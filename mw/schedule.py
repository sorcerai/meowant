"""Quiet-hours / time-window math, shared by health_watch + deadman so the two
surfaces agree on when overnight alert suppression applies (config-driven)."""
import time


def hhmm_to_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def is_quiet(now, quiet_start, quiet_end):
    """True if the local time at `now` (epoch) falls in [quiet_start, quiet_end).
    Handles wraparound windows (e.g. 22:00-08:00 spanning midnight)."""
    lt = time.localtime(now)
    cur = lt.tm_hour * 60 + lt.tm_min
    s, e = hhmm_to_min(quiet_start), hhmm_to_min(quiet_end)
    return (s <= cur < e) if s <= e else (cur >= s or cur < e)
