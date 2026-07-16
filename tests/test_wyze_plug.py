"""WyzePlug: remote power-cycle primitive for the Wyze plug behind the SC10
(the only prior fix for a physical jam was a human power-cycle), and the
/powercycle bot-command handler. All tests use fake clients/plugs — no real
wyze_sdk import, no network."""
import sys
import types

import pytest

from mw.wyze_plug import WyzePlug, powercycle_command


class FakePlug:
    def __init__(self, nickname, mac, product="WLPP1", is_on=True):
        self.nickname = nickname
        self.mac = mac
        self.product = product
        self.is_on = is_on


class FakePlugs:
    """Mirrors wyze-sdk's client.plugs.{list,turn_off,turn_on}."""
    def __init__(self, plugs):
        self._plugs = plugs
        self.calls = []

    def list(self):
        return self._plugs

    def turn_off(self, device_mac=None, device_model=None):
        self.calls.append(("turn_off", device_mac, device_model))
        for p in self._plugs:
            if p.mac == device_mac:
                p.is_on = False

    def turn_on(self, device_mac=None, device_model=None):
        self.calls.append(("turn_on", device_mac, device_model))
        for p in self._plugs:
            if p.mac == device_mac:
                p.is_on = True


class FakeClient:
    def __init__(self, plugs):
        self.plugs = FakePlugs(plugs)


class FlatListClient:
    """Some wyze-sdk versions expose plugs_list() directly on the client
    instead of plugs.list() — the code must tolerate both shapes."""
    def __init__(self, plugs):
        self.plugs = FakePlugs(plugs)   # turn_off/turn_on still go through here

    def plugs_list(self):
        return self.plugs.list()


def _cfg(plug_name="Litterbox Plug"):
    return {"email": "a@b.com", "password": "x", "key_id": "k", "api_key": "a",
            "plug_name": plug_name}


def _wp(plugs, plug_name="Litterbox Plug", client_cls=FakeClient):
    client = client_cls(plugs)
    return WyzePlug(_cfg(plug_name), client_factory=lambda: client), client


def _sleep_recorder(raise_on_call=False):
    calls = []
    def sleep(secs):
        calls.append(secs)
        if raise_on_call:
            raise RuntimeError("sleep interrupted")
    return sleep, calls


def test_finds_plug_case_insensitively():
    wp, _ = _wp([FakePlug("litterbox plug", "AA:BB")])
    p = wp._find_plug()
    assert p.mac == "AA:BB"


def test_find_plug_not_found_lists_available_names():
    wp, _ = _wp([FakePlug("Living Room Lamp", "AA:BB")])
    with pytest.raises(RuntimeError) as exc:
        wp._find_plug()
    assert "Living Room Lamp" in str(exc.value)


def test_find_plug_tolerates_flat_plugs_list_shape():
    wp, _ = _wp([FakePlug("Litterbox Plug", "AA:BB")], client_cls=FlatListClient)
    p = wp._find_plug()
    assert p.mac == "AA:BB"


def test_power_cycle_sequence_and_summary():
    wp, client = _wp([FakePlug("Litterbox Plug", "AA:BB", product="WLPP1")])
    sleep, sleep_calls = _sleep_recorder()
    summary = wp.power_cycle(off_seconds=10, sleep=sleep)
    assert [c[0] for c in client.plugs.calls] == ["turn_off", "turn_on"]
    assert client.plugs.calls[0][1] == "AA:BB"        # device_mac passed through
    assert sleep_calls == [10]
    assert "Litterbox Plug" in summary and "10s" in summary


def test_power_cycle_turns_on_even_if_sleep_raises():
    wp, client = _wp([FakePlug("Litterbox Plug", "AA:BB")])
    sleep, _ = _sleep_recorder(raise_on_call=True)
    with pytest.raises(RuntimeError, match="sleep interrupted"):
        wp.power_cycle(off_seconds=10, sleep=sleep)
    assert [c[0] for c in client.plugs.calls] == ["turn_off", "turn_on"]


def test_power_cycle_raises_if_plug_stays_off():
    plug_obj = FakePlug("Litterbox Plug", "AA:BB", is_on=False)
    client = FakeClient([plug_obj])

    def turn_on_but_stay_off(device_mac=None, device_model=None):
        client.plugs.calls.append(("turn_on", device_mac, device_model))
        # plug_obj.is_on stays False — simulates the plug failing to come back on

    client.plugs.turn_on = turn_on_but_stay_off
    wp = WyzePlug(_cfg(), client_factory=lambda: client)
    sleep, _ = _sleep_recorder()
    with pytest.raises(RuntimeError, match="CHECK THE BOX"):
        wp.power_cycle(off_seconds=10, sleep=sleep)


def test_status_reports_on_off():
    wp, _ = _wp([FakePlug("Litterbox Plug", "AA:BB", is_on=True)])
    assert wp.status() == "plug Litterbox Plug: ON"


def test_lazy_import_with_client_factory_never_imports_wyze_sdk():
    assert "wyze_sdk" not in sys.modules      # sanity: not already loaded elsewhere
    wp, _ = _wp([FakePlug("Litterbox Plug", "AA:BB")])
    wp.power_cycle(off_seconds=0, sleep=lambda s: None)
    wp.status()
    assert "wyze_sdk" not in sys.modules


def test_default_client_factory_wraps_login_errors(monkeypatch):
    fake_mod = types.ModuleType("wyze_sdk")

    class ExplodingClient:
        def __init__(self, **kwargs):
            raise ValueError("bad creds")

    fake_mod.Client = ExplodingClient
    monkeypatch.setitem(sys.modules, "wyze_sdk", fake_mod)
    wp = WyzePlug(_cfg())                     # no client_factory -> lazy default path
    with pytest.raises(RuntimeError, match="wyze login failed"):
        wp._find_plug()


def test_powercycle_command_wrong_arg_returns_usage():
    class BoomPlug:
        def power_cycle(self):
            raise AssertionError("should not be called")
    assert "usage:" in powercycle_command(BoomPlug(), "")
    assert "usage:" in powercycle_command(BoomPlug(), "other")


def test_powercycle_command_box_calls_power_cycle():
    calls = []
    class OkPlug:
        def power_cycle(self):
            calls.append(1)
            return "power-cycled Litterbox Plug (10s off)"
    result = powercycle_command(OkPlug(), "box")
    assert calls == [1]
    assert result == "power-cycled Litterbox Plug (10s off)"


def test_powercycle_command_exception_returns_error_string():
    class FailPlug:
        def power_cycle(self):
            raise RuntimeError("plug did not come back ON — CHECK THE BOX")
    result = powercycle_command(FailPlug(), "box")
    assert result.startswith("❌") and "CHECK THE BOX" in result
