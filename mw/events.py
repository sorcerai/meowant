"""Diff successive DPS snapshots into semantic events."""
from dataclasses import dataclass, field
from mw.decode import decode_dp102

CAT_ENTER = "cat_enter"
CAT_LEAVE = "cat_leave"
CLEAN_START = "clean_start"
CLEAN_DONE = "clean_done"
BIN_FULL = "bin_full"
BIN_CLEAR = "bin_clear"
CHUTE_FULL = "chute_full"
FAULT = "fault"
ELIMINATION = "elimination"


@dataclass
class Event:
    kind: str
    ts: float
    detail: dict = field(default_factory=dict)


def _g(d, k):
    return d.get(str(k))


def detect_events(prev, new, ts):
    evs = []
    if "24" in new:
        o24, n24 = _g(prev, 24), _g(new, 24)
        if n24 is not None and n24 != o24:
            if n24 == "cat_get_in":
                evs.append(Event(CAT_ENTER, ts, {"from": o24}))
            elif o24 == "cat_get_in":
                evs.append(Event(CAT_LEAVE, ts, {"to": n24}))
            if n24 == "cleaning":
                evs.append(Event(CLEAN_START, ts))
            if n24 == "clean_done":
                evs.append(Event(CLEAN_DONE, ts))

    if "21" in new:
        o21, n21 = int(_g(prev, 21) or 0), int(_g(new, 21) or 0)
        if (n21 & 1) and not (o21 & 1):
            evs.append(Event(BIN_FULL, ts))
        if (o21 & 1) and not (n21 & 1):
            evs.append(Event(BIN_CLEAR, ts))

    if "22" in new:
        o22, n22 = int(_g(prev, 22) or 0), int(_g(new, 22) or 0)
        if n22 and n22 != o22:
            evs.append(Event(FAULT, ts, {"bitmap": n22}))

    # Dedupe elimination: at most one ELIMINATION per tick, preferring the
    # dp102 record (carries use_record) over the dp7 count increment.
    elim = None
    if "7" in new:
        o7, n7 = _g(prev, 7), _g(new, 7)
        if o7 is not None and n7 is not None and int(n7) > int(o7):
            elim = Event(ELIMINATION, ts, {"count": int(n7)})

    if "102" in new:
        o102, n102 = _g(prev, 102), _g(new, 102)
        if n102 and n102 != o102:
            elim = Event(ELIMINATION, ts, {"use_record": decode_dp102(n102)})

    if elim is not None:
        evs.append(elim)

    return evs
