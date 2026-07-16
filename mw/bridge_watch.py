"""Watch the camera bridge (Proxmox host running cryze_v2 + MediaMTX) for the
two failure modes that have actually taken every camera down:

  1. Disk fill: the bridge's 19GB disk filled twice (Jul incidents, ~21h and
     ~14h of blackout) and killed all cameras when it hit 100%. There is no
     on-box alarm for this, so meowant has to watch it from the outside.
  2. Publisher death: the cryze_android_app container can stop feeding RTSP
     without the container itself dying, so MediaMTX's paths list goes to
     zero `ready` streams while docker still reports the container "up".

Both probes go through an injected `run_remote(cmd) -> str|None` so this
module never opens its own SSH connection (and tests never touch a real
bridge) — the caller wires it to `ssh aria@<bridge host>`. The verified
recovery action is `docker restart cryze_v2-cryze_android_app-1`, which
brings streams back in ~2-3 minutes; auto-heal is rate-limited so a bridge
that's dying repeatedly gets a human paged instead of an unbounded restart
loop.
"""
import json
import sys
import time


class BridgeWatch:
    def __init__(self, run_remote, notify, *, disk_warn_pct=80, disk_crit_pct=90,
                 streams_grace_s=900, remediate=True, max_remediations_per_day=2,
                 remediation_cooldown_s=3600, interval=300, now_fn=time.time,
                 state_get=None, state_set=None):
        self.run_remote = run_remote            # (cmd: str) -> str|None
        self.notify = notify                    # (msg: str) -> truthy|False
        self.disk_warn_pct = disk_warn_pct
        self.disk_crit_pct = disk_crit_pct
        self.streams_grace_s = streams_grace_s
        self.remediate = remediate
        self.max_remediations_per_day = max_remediations_per_day
        self.remediation_cooldown_s = remediation_cooldown_s
        self.interval = interval
        self.now_fn = now_fn
        # Default state store is just an in-memory dict — fine for a lone
        # instance, but real deployments inject store-backed get/set so
        # latches and the daily remediation budget survive a daemon restart.
        self._mem_state = {}
        self._state_get = state_get or (lambda: self._mem_state)
        self._state_set = state_set or self._set_mem_state

    def _set_mem_state(self, s):
        self._mem_state.clear()
        self._mem_state.update(s)

    # ---- probes -------------------------------------------------------
    @staticmethod
    def _parse_disk_pct(raw):
        if raw is None:
            return None
        try:
            return int(raw.strip().rstrip("%"))
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _parse_ready_count(raw):
        if raw is None:
            return None
        try:
            items = json.loads(raw).get("items", [])
            return sum(1 for it in items if it.get("ready"))
        except (ValueError, TypeError, AttributeError, KeyError):
            return None

    def _day_key(self, now):
        return time.strftime("%Y-%m-%d", time.localtime(now))

    # ---- disk -----------------------------------------------------------
    def _check_disk(self, state, pct):
        if pct >= self.disk_crit_pct:
            if not state.get("disk_crit_alerted"):
                msg = (f"🖴 CRIT: Bridge disk {pct}% — will kill cameras at 100%; "
                       f"log caps should hold, investigate NOW")
                if self.notify(msg) is not False:
                    state["disk_crit_alerted"] = True
                    state["disk_warn_alerted"] = True
        elif pct >= self.disk_warn_pct:
            if not state.get("disk_warn_alerted"):
                msg = (f"🖴 Bridge disk {pct}% — will kill cameras at 100%; "
                       f"log caps should hold, investigate")
                if self.notify(msg) is not False:
                    state["disk_warn_alerted"] = True
        elif pct < self.disk_warn_pct - 5:
            state["disk_warn_alerted"] = False
            state["disk_crit_alerted"] = False

    # ---- streams ----------------------------------------------------------
    def _check_streams(self, state, ready_count, now):
        if ready_count > 0:
            state["streams_first_zero"] = None
            if state.get("streams_dead_alerted"):
                msg = f"✅ bridge streams recovered ({ready_count} publishers)"
                if self.notify(msg) is not False:
                    state["streams_dead_alerted"] = False
                    state["budget_exhausted_alerted"] = False
            return

        if state.get("streams_first_zero") is None:
            state["streams_first_zero"] = now
        elapsed = now - state["streams_first_zero"]
        if elapsed < self.streams_grace_s:
            return

        if not state.get("streams_dead_alerted"):
            mins = int(elapsed // 60)
            msg = f"📷 Bridge streams DEAD ~{mins}min (0 publishers)"
            if self.notify(msg) is not False:
                state["streams_dead_alerted"] = True

        if not self.remediate:
            return

        day = self._day_key(now)
        if state.get("remediation_day") != day:
            state["remediation_day"] = day
            state["remediation_count"] = 0
        count = state.get("remediation_count", 0)
        if count >= self.max_remediations_per_day:
            if not state.get("budget_exhausted_alerted"):
                if self.notify("auto-heal budget exhausted — manual attention needed") is not False:
                    state["budget_exhausted_alerted"] = True
            return

        last_ts = state.get("last_remediation_ts")
        if last_ts is not None and now - last_ts < self.remediation_cooldown_s:
            return  # still cooling down from the last attempt

        self.run_remote("docker restart cryze_v2-cryze_android_app-1")
        state["remediation_count"] = count + 1
        state["last_remediation_ts"] = now
        self.notify("🔧 auto-restarted camera publisher — streams should return in ~3min")
        # Give the container time to boot before the grace clock counts again.
        state["streams_first_zero"] = now

    # ---- cycle --------------------------------------------------------
    def check_once(self):
        now = self.now_fn()
        state = self._state_get() or {}

        disk_raw = self.run_remote("df --output=pcent / | tail -1")
        streams_raw = self.run_remote("curl -s --max-time 5 http://127.0.0.1:9997/v3/paths/list")

        if disk_raw is None and streams_raw is None:
            if not state.get("unreachable_alerted"):
                if self.notify("🔌 bridge unreachable over SSH") is not False:
                    state["unreachable_alerted"] = True
            self._state_set(state)
            return
        if state.get("unreachable_alerted"):
            state["unreachable_alerted"] = False

        pct = self._parse_disk_pct(disk_raw)
        if pct is not None:
            self._check_disk(state, pct)

        ready_count = self._parse_ready_count(streams_raw)
        if ready_count is not None:
            self._check_streams(state, ready_count, now)

        self._state_set(state)

    def run(self):
        while True:
            try:
                self.check_once()
            except Exception as e:      # the thread must never die
                print(f"[bridge-watch] loop error: {e}", file=sys.stderr)
            time.sleep(self.interval)
