"""Self-heal a camera whose stream has wedged.

The thingino cam's prudynt streamer can keep running while emitting a garbage
stream (h264 with no decodable params), so the warm reader writes 0-byte /
undecodable frames. The cam's own watchdog only catches full hangs, not bad
output — but meowant SEES the bad warm frame, so it's the right detector. On a
sustained failure we SSH the cam: a light `service restart prudynt` first, then
a full `reboot` if that doesn't take. Rate-limited, with notify escalation."""
import os
import subprocess
import sys
import time

try:
    import cv2
except Exception:                      # cv2 optional; degrade to size/age checks
    cv2 = None


def frame_healthy(path, max_age_s=300, now_fn=time.time):
    """True if the warm frame exists, is non-empty, decodes, and is fresh."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    if now_fn() - os.path.getmtime(path) > max_age_s:
        return False
    if cv2 is not None and cv2.imread(path) is None:
        return False
    return True


def make_ssh_restart(host, user, password, timeout=20):
    """Return restart(level) -> bool. level 'reboot' -> full reboot; anything else
    -> `service restart prudynt`. Uses sshpass via the SSHPASS env var (password
    not placed in argv). Returns True on exit code 0."""
    def restart(level):
        cmd = "reboot" if level == "reboot" else "service restart prudynt"
        env = dict(os.environ, SSHPASS=password)
        try:
            r = subprocess.run(
                ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=8", f"{user}@{host}", cmd],
                capture_output=True, text=True, timeout=timeout, env=env)
            return r.returncode == 0
        except Exception as e:
            print(f"[cam_watchdog] ssh {level} failed: {e}", file=sys.stderr)
            return False
    return restart


class CamWatchdog:
    """Polls one camera's frame health; on sustained failure, restarts it
    (escalating service-restart -> reboot), rate-limited by a cooldown."""

    def __init__(self, cam_name, is_healthy_fn, restart_fn, *, notify=None,
                 fail_grace_s=300, cooldown_s=1800, reboot_after_fails=2,
                 poll_s=60, now_fn=time.time, sleep=time.sleep):
        self.cam_name = cam_name
        self.is_healthy_fn = is_healthy_fn      # () -> bool
        self.restart_fn = restart_fn            # (level: str) -> bool
        self.notify = notify                    # optional (msg) -> any
        self.fail_grace_s = fail_grace_s        # must be unhealthy this long before acting
        self.cooldown_s = cooldown_s            # min gap between restart attempts
        self.reboot_after_fails = reboot_after_fails  # attempt# at which we escalate to reboot
        self.poll_s = poll_s
        self.now = now_fn
        self._sleep = sleep
        self._unhealthy_since = None
        self._attempts = 0
        self._cooldown_until = 0.0
        self._stop = False

    def check_once(self):
        now = self.now()
        if self.is_healthy_fn():
            if self._attempts > 0 and self.notify:
                self.notify(f"✅ {self.cam_name} stream recovered")
            self._unhealthy_since = None
            self._attempts = 0
            return "healthy"
        if self._unhealthy_since is None:
            self._unhealthy_since = now
        if now - self._unhealthy_since < self.fail_grace_s:
            return "grace"
        if now < self._cooldown_until:
            return "cooldown"
        self._attempts += 1
        level = "reboot" if self._attempts >= self.reboot_after_fails else "service"
        ok = self.restart_fn(level)
        self._cooldown_until = now + self.cooldown_s
        if self.notify:
            self.notify(f"⚠️ {self.cam_name} stream dead "
                        f">{self.fail_grace_s // 60}min — issued {level} restart "
                        f"(attempt {self._attempts}, ok={ok})")
        return f"restart:{level}"

    def run(self):
        while not self._stop:
            try:
                self.check_once()
            except Exception as e:           # the thread must never die
                print(f"[cam_watchdog] loop error: {e}", file=sys.stderr)
            self._sleep(self.poll_s)

    def stop(self):
        self._stop = True
