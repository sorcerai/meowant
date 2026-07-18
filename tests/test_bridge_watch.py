"""Tests for mw.bridge_watch — driven with a scripted fake run_remote + a
notify recorder + a fixed clock. No real ssh, no real docker."""
from mw.bridge_watch import BridgeWatch

DISK_CMD = "df --output=pcent / | tail -1"
RESTART_CMD = "docker restart cryze_v2-cryze_android_app-1"

BASE_T = 1_720_000_000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_run_remote(box):
    """box is a mutable dict controlling scripted responses:
      box['disk'] -- int pct, or None to fail the disk probe
      box['streams'] -- int ready count, or None to fail the streams probe
      box['calls'] -- list of every cmd invoked (for assertions)
    """
    box.setdefault("calls", [])

    def run_remote(cmd):
        box["calls"].append(cmd)
        if cmd == DISK_CMD:
            pct = box.get("disk")
            return None if pct is None else f"{pct}%\n"
        if "ffprobe" in cmd:                     # the on-bridge stream sweep
            n = box.get("streams")
            if n is None:
                return None                      # ssh/probe infrastructure failed
            # n cams answered: the sweep echoes one cam name per up stream.
            # n == 0 -> "" (a REAL zero, distinct from None).
            return "".join(f"meowcam{i + 1}\n" for i in range(n))
        if cmd == RESTART_CMD:
            return "ok"
        return None
    return run_remote


def make_state_store(initial=None):
    box = {"state": dict(initial or {})}

    def get():
        return dict(box["state"])

    def set_(s):
        box["state"] = dict(s)
    return get, set_


def make_watch(box, notifies, *, state_get=None, state_set=None, t=None, **kwargs):
    if t is None:
        t = [BASE_T]
    if state_get is None or state_set is None:
        state_get, state_set = make_state_store()
    watch = BridgeWatch(
        make_run_remote(box), notifies.append,
        now_fn=lambda: t[0], state_get=state_get, state_set=state_set,
        **kwargs)
    return watch, t


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------

def test_disk_warn_fires_once_then_stays_silent():
    box = {"disk": 85, "streams": 3}
    notifies = []
    watch, _ = make_watch(box, notifies)

    watch.check_once()
    assert len(notifies) == 1
    assert "85%" in notifies[0]

    watch.check_once()   # stays 85 -- no repeat
    assert len(notifies) == 1


def test_disk_rearms_below_warn_minus_5_then_crit_escalates():
    box = {"disk": 85, "streams": 3}
    notifies = []
    watch, _ = make_watch(box, notifies)

    watch.check_once()          # warn fires
    assert len(notifies) == 1

    box["disk"] = 70             # < 80 - 5 -> re-arm
    watch.check_once()
    assert len(notifies) == 1    # no notify just for dropping

    box["disk"] = 92             # crit
    watch.check_once()
    assert len(notifies) == 2
    assert "CRIT" in notifies[1]
    assert "92%" in notifies[1]


# ---------------------------------------------------------------------------
# Streams: grace period, dead alert, remediation, cooldown, budget, recovery
# ---------------------------------------------------------------------------

def test_streams_zero_within_grace_does_nothing():
    box = {"disk": 50, "streams": 0}
    notifies = []
    watch, t = make_watch(box, notifies, streams_grace_s=900)

    watch.check_once()
    t[0] += 500                  # still < 900s grace
    watch.check_once()

    assert notifies == []
    assert RESTART_CMD not in box["calls"]


def test_streams_dead_past_grace_alerts_and_remediates_once():
    box = {"disk": 50, "streams": 0}
    notifies = []
    watch, t = make_watch(box, notifies, streams_grace_s=900,
                           remediation_cooldown_s=3600, max_remediations_per_day=2)

    watch.check_once()           # t=BASE_T, first-zero recorded
    t[0] += 900                  # grace elapsed
    watch.check_once()

    assert any("DEAD" in m for m in notifies)
    assert any("🔧" in m for m in notifies)
    assert box["calls"].count(RESTART_CMD) == 1


def test_second_zero_streak_within_cooldown_does_not_restart_again():
    box = {"disk": 50, "streams": 0}
    notifies = []
    watch, t = make_watch(box, notifies, streams_grace_s=900,
                           remediation_cooldown_s=3600, max_remediations_per_day=2)

    watch.check_once()
    t[0] += 900
    watch.check_once()           # first remediation, count=1
    assert box["calls"].count(RESTART_CMD) == 1

    dead_alerts_before = sum(1 for m in notifies if "DEAD" in m)

    # streams stay 0; grace elapses again (first-zero was reset by the
    # remediation) but we're still well inside the 1h cooldown.
    t[0] += 900
    watch.check_once()

    assert box["calls"].count(RESTART_CMD) == 1   # no second restart
    dead_alerts_after = sum(1 for m in notifies if "DEAD" in m)
    assert dead_alerts_after == dead_alerts_before   # no repeat DEAD alert either


def test_remediation_budget_exhausted_notifies_once_no_restart():
    box = {"disk": 50, "streams": 0}
    notifies = []
    watch, t = make_watch(box, notifies, streams_grace_s=900,
                           remediation_cooldown_s=1000, max_remediations_per_day=2)

    watch.check_once()
    t[0] += 900
    watch.check_once()           # remediation #1
    assert box["calls"].count(RESTART_CMD) == 1

    t[0] += 1000                 # past cooldown
    watch.check_once()           # remediation #2 (budget now exhausted)
    assert box["calls"].count(RESTART_CMD) == 2

    t[0] += 1000                 # past cooldown again, but budget is spent
    watch.check_once()
    assert box["calls"].count(RESTART_CMD) == 2   # no third restart
    assert sum(1 for m in notifies if "budget exhausted" in m) == 1

    t[0] += 1000
    watch.check_once()
    assert sum(1 for m in notifies if "budget exhausted" in m) == 1   # not repeated


def test_recovery_notifies_and_resets_latches():
    box = {"disk": 50, "streams": 0}
    notifies = []
    watch, t = make_watch(box, notifies, streams_grace_s=900,
                           remediation_cooldown_s=3600, max_remediations_per_day=2)

    watch.check_once()
    t[0] += 900
    watch.check_once()           # DEAD + remediation #1

    box["streams"] = 5
    watch.check_once()

    assert any("recovered" in m for m in notifies)

    # A fresh zero-streak after recovery should alert DEAD again (latches reset).
    box["streams"] = 0
    t[0] += 900
    watch.check_once()
    t[0] += 900
    watch.check_once()
    assert sum(1 for m in notifies if "DEAD" in m) == 2


def test_next_day_resets_remediation_budget():
    box = {"disk": 50, "streams": 0}
    notifies = []
    watch, t = make_watch(box, notifies, streams_grace_s=900,
                           remediation_cooldown_s=100, max_remediations_per_day=1)

    watch.check_once()
    t[0] += 900
    watch.check_once()           # remediation #1, budget (1/day) now spent
    assert box["calls"].count(RESTART_CMD) == 1

    t[0] += 900                  # grace elapses again; cooldown clears too,
    watch.check_once()           # but budget is still exhausted for today
    assert box["calls"].count(RESTART_CMD) == 1
    assert any("budget exhausted" in m for m in notifies)

    t[0] += 86400                 # next calendar day -> budget resets
    watch.check_once()
    assert box["calls"].count(RESTART_CMD) == 2


# ---------------------------------------------------------------------------
# Stream probe construction / parsing
# ---------------------------------------------------------------------------

def test_streams_probe_is_ffprobe_sweep_not_dead_api():
    """The MediaMTX :9997 API is disabled on the bridge (`api: false` in
    mediamtx.yml) — probing it always got connection-refused and silently
    disabled the streams check from deployment until 2026-07-17. The probe
    must ffprobe the RTSP streams themselves and never touch 9997."""
    box = {"disk": 50, "streams": 2}
    notifies = []
    watch, _ = make_watch(box, notifies, probe_cams=["meowcam1", "meowcam9"])
    cmd = watch._streams_probe_cmd()
    assert "ffprobe" in cmd
    assert "rtsp://127.0.0.1:8554/" in cmd
    assert "meowcam1" in cmd and "meowcam9" in cmd
    assert "9997" not in cmd
    # nonzero last-iteration exit must not turn all-cams-down into ssh-failure
    assert cmd.rstrip().endswith("true")


def test_probe_cmd_rejects_shell_metacharacters_in_cam_names():
    """Cam names interpolate into a remote shell command; anything beyond
    [A-Za-z0-9_-] must raise locally instead of executing on the bridge."""
    import pytest
    from mw.bridge_watch import streams_probe_cmd
    for evil in (["$(touch /tmp/owned)"], ["meow cam"], ["a;b"], ["ok", ""]):
        with pytest.raises(ValueError):
            streams_probe_cmd(evil)
    assert "meowcam1" in streams_probe_cmd(["meowcam1", "cam_2-b"])


def test_parse_ready_count_empty_is_real_zero():
    assert BridgeWatch._parse_ready_count(None) is None       # probe failed
    assert BridgeWatch._parse_ready_count("") == 0            # ran, none up
    assert BridgeWatch._parse_ready_count("meowcam1\nmeowcam3\n") == 2


# ---------------------------------------------------------------------------
# Unreachable bridge
# ---------------------------------------------------------------------------

def test_both_probes_none_alerts_unreachable_once_then_rearms():
    box = {"disk": None, "streams": None}
    notifies = []
    watch, t = make_watch(box, notifies)

    watch.check_once()
    watch.check_once()   # still unreachable -- no repeat
    assert sum(1 for m in notifies if "unreachable" in m) == 1

    box["disk"] = 50
    box["streams"] = 3
    watch.check_once()   # recovered -- re-armed silently

    box["disk"] = None
    box["streams"] = None
    watch.check_once()
    assert sum(1 for m in notifies if "unreachable" in m) == 2


def test_one_probe_failing_is_not_unreachable_and_not_counted_dead():
    # streams probe fails (None) but disk succeeds -- NOT "unreachable", and
    # the unparseable streams reading must not be treated as 0 dead publishers.
    box = {"disk": 50, "streams": None}
    notifies = []
    watch, t = make_watch(box, notifies, streams_grace_s=900)

    watch.check_once()
    t[0] += 1000
    watch.check_once()

    assert notifies == []
    assert RESTART_CMD not in box["calls"]


# ---------------------------------------------------------------------------
# Failed notify -> latch not set -> retried next cycle
# ---------------------------------------------------------------------------

def test_failed_notify_does_not_latch_and_is_retried():
    box = {"disk": 85, "streams": 3}
    results = [False, True]   # first notify fails, second succeeds

    def flaky_notify(msg):
        return results.pop(0)

    state_get, state_set = make_state_store()
    watch = BridgeWatch(make_run_remote(box), flaky_notify,
                         now_fn=lambda: BASE_T,
                         state_get=state_get, state_set=state_set)

    watch.check_once()   # notify returns False -> latch not set
    watch.check_once()   # retried, notify returns True -> latch set

    assert results == []   # both calls consumed (i.e. it really retried)


# ---------------------------------------------------------------------------
# State persistence across instances
# ---------------------------------------------------------------------------

def test_state_round_trips_through_injected_store():
    box = {"disk": 85, "streams": 3}
    state_get, state_set = make_state_store()
    notifies = []

    watch1, t = make_watch(box, notifies, state_get=state_get, state_set=state_set)
    watch1.check_once()
    assert len(notifies) == 1

    # A brand new instance sharing the same persisted state must not re-alert.
    watch2, _ = make_watch(box, notifies, state_get=state_get, state_set=state_set, t=t)
    watch2.check_once()
    assert len(notifies) == 1
