"""Light test for the refactored tui.py: verify _apply drives the render path."""
import pytest
import tui


CANNED_RAW = {
    "24": "standby",
    "4": True,
    "5": 3,
    "7": 2,
    "10": False,
    "11": 1320,
    "12": 480,
    "21": 0,
    "22": 0,
    "107": "none",
}


@pytest.mark.asyncio
async def test_apply_renders_state(monkeypatch):
    """Monkeypatch client.get_state; mount app; call _apply; verify dps is set."""

    def fake_get_state(base, timeout=5):
        return {"raw": CANNED_RAW}

    monkeypatch.setattr(tui.client, "get_state", fake_get_state)

    app = tui.Dashboard()
    async with app.run_test() as pilot:
        # Call _apply directly with the canned raw dict
        app._apply(CANNED_RAW)
        await pilot.pause()
        # dps must be set to exactly the canned raw
        assert app.dps == CANNED_RAW
        # msg should be cleared (was "connecting…" before _apply)
        assert app.msg == ""
        # The status widget should have been updated without error
        status_widget = app.query_one("#status")
        assert status_widget is not None
