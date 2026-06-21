"""Single owner of the SC10 socket (real) + a test double."""
import sys
import threading


class TuyaDevice:
    def __init__(self, cfg):
        import tinytuya
        self._lock = threading.Lock()
        self._cfg = cfg
        self._dev = None
        self._tinytuya = tinytuya

    def _device(self):
        if self._dev is None:
            self._dev = self._tinytuya.Device(
                dev_id=self._cfg["device_id"], address=self._cfg["address"],
                local_key=self._cfg["local_key"], version=float(self._cfg["version"]))
            self._dev.set_socketPersistent(True)
            self._dev.set_socketTimeout(5)  # don't let a hung socket hold the lock
        return self._dev

    def status_dps(self):
        with self._lock:
            for _ in (1, 2):
                try:
                    data = self._device().status()
                    dps = data.get("dps", {}) if isinstance(data, dict) else {}
                    if dps:
                        return dps
                except Exception:
                    self._dev = None
            return {}

    def clean(self):
        with self._lock:
            try:
                self._device().set_value(24, "cleaning")
            except Exception as e:
                print(f"[meowantd] device clean failed: {e}", file=sys.stderr)
                self._dev = None

    def set_value(self, dp, value):
        with self._lock:
            try:
                self._device().set_value(dp, value)
            except Exception as e:
                print(f"[meowantd] device set_value({dp}) failed: {e}", file=sys.stderr)
                self._dev = None
                raise


class FakeDevice:
    """Replays a list of dps snapshots; records clean() calls."""
    def __init__(self, snapshots):
        self._snaps = list(snapshots)
        self._i = 0
        self.clean_calls = 0
        self.set_values = []

    def status_dps(self):
        if self._i < len(self._snaps):
            dps = self._snaps[self._i]
            self._i += 1
            return dict(dps)
        return dict(self._snaps[-1]) if self._snaps else {}

    def clean(self):
        self.clean_calls += 1

    def set_value(self, dp, value):
        self.set_values.append((dp, value))
