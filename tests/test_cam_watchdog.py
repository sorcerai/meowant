"""Tests for mw.cam_watchdog — state machine driven with injected clock/fns.

All tests drive check_once() directly; no real SSH, no real images (except the
frame_healthy cv2 tests which use cv2.imwrite into tmp_path).
"""
import os
import time

import cv2
import numpy as np
import pytest

from mw.cam_watchdog import CamWatchdog, frame_healthy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wd(healthy_ref, restart_calls, notifies, *,
             fail_grace_s=300, cooldown_s=1800, reboot_after_fails=2,
             t=None):
    """Build a CamWatchdog with injected dependencies.

    healthy_ref  -- list of length 1: [True/False]; flip it to change health.
    restart_calls -- list that restart_fn appends level strings to.
    notifies     -- list that notify appends messages to.
    t            -- mutable clock list of length 1 (default: [1000.0]).
    """
    if t is None:
        t = [1000.0]

    def is_healthy():
        return healthy_ref[0]

    def restart_fn(level):
        restart_calls.append(level)
        return True

    return CamWatchdog(
        cam_name="testcam",
        is_healthy_fn=is_healthy,
        restart_fn=restart_fn,
        notify=notifies.append,
        fail_grace_s=fail_grace_s,
        cooldown_s=cooldown_s,
        reboot_after_fails=reboot_after_fails,
        poll_s=0,
        now_fn=lambda: t[0],
        sleep=lambda _: None,
    ), t


# ---------------------------------------------------------------------------
# Scenario 1 — healthy: no restart, returns "healthy"
# ---------------------------------------------------------------------------

def test_healthy_no_restart():
    healthy = [True]
    calls, notifies = [], []
    wd, _ = _make_wd(healthy, calls, notifies)

    result = wd.check_once()

    assert result == "healthy"
    assert calls == []


# ---------------------------------------------------------------------------
# Scenario 2 — unhealthy but within grace period: "grace", no restart
# ---------------------------------------------------------------------------

def test_unhealthy_within_grace():
    healthy = [False]
    calls, notifies = [], []
    t = [1000.0]
    wd, _ = _make_wd(healthy, calls, notifies, t=t)

    # First call: sets _unhealthy_since = 1000.0, elapsed = 0 < 300 → grace
    result = wd.check_once()

    assert result == "grace"
    assert calls == []
    assert notifies == []


# ---------------------------------------------------------------------------
# Scenario 3 — unhealthy >= fail_grace_s: first restart ("service"), notify
# ---------------------------------------------------------------------------

def test_unhealthy_past_grace_triggers_service_restart():
    healthy = [False]
    calls, notifies = [], []
    t = [1000.0]
    wd, _ = _make_wd(healthy, calls, notifies, t=t)

    # Prime: set _unhealthy_since
    wd.check_once()           # t=1000, grace

    # Advance past grace (300 s)
    t[0] = 1000.0 + 301

    result = wd.check_once()

    assert result == "restart:service"
    assert calls == ["service"]
    assert len(notifies) == 1
    assert "service" in notifies[0]


# ---------------------------------------------------------------------------
# Scenario 4 — still unhealthy within cooldown: "cooldown", no second restart
# ---------------------------------------------------------------------------

def test_still_unhealthy_within_cooldown():
    healthy = [False]
    calls, notifies = [], []
    t = [1000.0]
    wd, _ = _make_wd(healthy, calls, notifies, t=t)

    # Prime + first restart
    wd.check_once()           # grace
    t[0] = 1000.0 + 301
    wd.check_once()           # restart:service; cooldown_until = 1301 + 1800 = 3101

    # Still unhealthy, inside cooldown
    t[0] = 1301 + 500         # 1801 < 3101
    result = wd.check_once()

    assert result == "cooldown"
    assert len(calls) == 1    # no second restart


# ---------------------------------------------------------------------------
# Scenario 5 — after cooldown expires: second attempt escalates to reboot
# ---------------------------------------------------------------------------

def test_second_attempt_escalates_to_reboot():
    healthy = [False]
    calls, notifies = [], []
    t = [1000.0]
    wd, _ = _make_wd(healthy, calls, notifies, t=t, reboot_after_fails=2)

    # Prime
    wd.check_once()           # grace
    t[0] = 1000.0 + 301
    wd.check_once()           # attempt 1 → service; cooldown_until = 1301 + 1800 = 3101

    # Advance past cooldown
    t[0] = 3101 + 1

    result = wd.check_once()

    assert result == "restart:reboot"
    assert calls == ["service", "reboot"]
    assert any("reboot" in n for n in notifies)


# ---------------------------------------------------------------------------
# Scenario 6 — recovers after restart: "healthy", attempts reset, notify fired
# ---------------------------------------------------------------------------

def test_recovery_after_restart_resets_state():
    healthy = [False]
    calls, notifies = [], []
    t = [1000.0]
    wd, _ = _make_wd(healthy, calls, notifies, t=t)

    # Get a real restart on the books
    wd.check_once()           # grace
    t[0] = 1000.0 + 301
    wd.check_once()           # restart:service

    # Now cam comes back
    healthy[0] = True
    t[0] = 1000.0 + 400
    result = wd.check_once()

    assert result == "healthy"
    assert wd._attempts == 0
    assert wd._unhealthy_since is None
    assert any("recovered" in n for n in notifies)


# ---------------------------------------------------------------------------
# Scenario 7 — transient: unhealthy then healthy before grace expires
#   → no restart, state fully resets, no "recovered" notify (attempts == 0)
# ---------------------------------------------------------------------------

def test_transient_unhealthy_before_grace_no_restart():
    healthy = [False]
    calls, notifies = [], []
    t = [1000.0]
    wd, _ = _make_wd(healthy, calls, notifies, t=t)

    # First check: unhealthy, sets _unhealthy_since, still in grace
    result1 = wd.check_once()
    assert result1 == "grace"

    # Recover before grace expires
    healthy[0] = True
    t[0] = 1000.0 + 100      # still < 300 s of grace
    result2 = wd.check_once()

    assert result2 == "healthy"
    assert calls == []
    # No recovered notify because _attempts was 0 when we recovered
    assert not any("recovered" in n for n in notifies)
    assert wd._unhealthy_since is None
    assert wd._attempts == 0


# ---------------------------------------------------------------------------
# frame_healthy tests
# ---------------------------------------------------------------------------

def test_frame_healthy_zero_byte(tmp_path):
    p = str(tmp_path / "empty.jpg")
    open(p, "w").close()      # 0-byte file
    assert frame_healthy(p) is False


def test_frame_healthy_fresh_jpeg(tmp_path):
    p = str(tmp_path / "frame.jpg")
    img = np.full((64, 64, 3), 128, np.uint8)
    cv2.imwrite(p, img)
    # Use real time.time — file was just written so it's fresh
    assert frame_healthy(p) is True


def test_frame_healthy_stale_jpeg(tmp_path):
    p = str(tmp_path / "stale.jpg")
    img = np.full((64, 64, 3), 128, np.uint8)
    cv2.imwrite(p, img)
    # Pretend clock is far in the future relative to mtime
    far_future = os.path.getmtime(p) + 9999
    assert frame_healthy(p, max_age_s=300, now_fn=lambda: far_future) is False
