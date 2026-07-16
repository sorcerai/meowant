"""Remote power-cycle for the SC10 via a Wyze smart plug sitting behind it.

The litterbox has jammed physically before with no remote fix — the only cure
was a human power-cycling the box. This wraps a Wyze plug so the daemon (and
the owner, via Telegram /powercycle) can force a full power-cycle without a
house visit. wyze_sdk is imported lazily, inside _get_client(), so the package
doesn't need to be installed unless a WyzePlug actually talks to Wyze — tests
inject a client_factory and never touch the real SDK.
"""
import time


class WyzePlug:
    def __init__(self, cfg, client_factory=None):
        self.cfg = cfg
        self._client_factory = client_factory
        self._client = None

    def _get_client(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import wyze_sdk
                try:
                    self._client = wyze_sdk.Client(
                        email=self.cfg["email"], password=self.cfg["password"],
                        key_id=self.cfg["key_id"], api_key=self.cfg["api_key"])
                except Exception as e:
                    raise RuntimeError(f"wyze login failed: {e}")
        return self._client

    def _list_plugs(self, client):
        # wyze-sdk's plug-listing API has shipped under both a flat
        # plugs_list() and a nested plugs.list() — tolerate either.
        if hasattr(client, "plugs_list"):
            return client.plugs_list()
        return client.plugs.list()

    def _find_plug(self):
        client = self._get_client()
        plugs = list(self._list_plugs(client))
        wanted = str(self.cfg["plug_name"]).strip().lower()
        for p in plugs:
            if str(getattr(p, "nickname", "")).strip().lower() == wanted:
                return p
        names = ", ".join(str(getattr(p, "nickname", "?")) for p in plugs) or "(none found)"
        raise RuntimeError(
            f"plug '{self.cfg['plug_name']}' not found — available: {names}")

    def _model(self, plug):
        return getattr(plug, "product", None) or getattr(plug, "model", None)

    def power_cycle(self, off_seconds=10, sleep=time.sleep):
        plug = self._find_plug()
        client = self._client
        mac, model = plug.mac, self._model(plug)
        client.plugs.turn_off(device_mac=mac, device_model=model)
        try:
            sleep(off_seconds)
        finally:
            # Never leave the plug off on exceptions between off and on —
            # a double turn_on (e.g. on a retry) is harmless.
            client.plugs.turn_on(device_mac=mac, device_model=model)
        try:
            verify = self._find_plug()
        except Exception:
            verify = None
        if verify is not None and getattr(verify, "is_on", None) is False:
            raise RuntimeError("plug did not come back ON — CHECK THE BOX")
        return f"power-cycled {plug.nickname} ({off_seconds}s off)"

    def status(self):
        plug = self._find_plug()
        is_on = getattr(plug, "is_on", None)
        state = "ON" if is_on is True else ("OFF" if is_on is False else "UNKNOWN")
        return f"plug {plug.nickname}: {state}"


def powercycle_command(plug, arg=""):
    """/powercycle bot-command handler body. Owner-only — see meowantd wiring."""
    if arg.strip() != "box":
        return ("usage: /powercycle box — cuts power to the SC10 for 10s "
                 "(forces drum re-home). Use when jammed.")
    try:
        return plug.power_cycle()
    except Exception as e:
        return f"❌ powercycle failed: {e}"
