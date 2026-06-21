#!/usr/bin/env python3
"""
tui.py — terminal dashboard with full control for the Meowant SC10.

Live-updating Textual TUI. Fetches state from the meowantd HTTP API.

    python3 tui.py             # requires meowantd running (default http://127.0.0.1:8765)
    MEOWANTD_URL=http://...:8765 python3 tui.py

Keys:  c clean   a auto-clean toggle   s sleep toggle
       [ / ] delay -/+   r refresh now   q quit
"""
import os
import urllib.error

from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.widgets import Footer, Static

import meowant  # reuse DPS maps and decode helpers
from mw import client

BASE = os.environ.get("MEOWANTD_URL", client.DEFAULT_BASE)


class Dashboard(App):
    CSS = """
    Screen { align: center middle; }
    #status { width: 64; height: auto; }
    #msg { width: 64; height: 1; color: $text-muted; content-align: center middle; }
    """
    BINDINGS = [
        ("c", "clean", "Clean now"),
        ("a", "autoclean", "Auto-clean"),
        ("s", "sleep", "Sleep"),
        ("left_square_bracket", "delay(-1)", "Delay -"),
        ("right_square_bracket", "delay(+1)", "Delay +"),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.dps = {}
        self.uses_today = None   # from daemon's reliable count, not dp7
        self.msg = "connecting…"

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield Static(self.msg, id="msg")
        yield Footer()

    def on_mount(self):
        self.set_interval(3.0, self.action_refresh)
        self.action_refresh()

    # ---- rendering -------------------------------------------------------
    def render_panel(self):
        g = lambda k: self.dps.get(str(k))
        cleaning = g(24) == "cleaning"
        t = Table.grid(padding=(0, 2))
        t.add_column(justify="right", style="bright_black")
        t.add_column()

        dot = Text("● ", style="cyan" if cleaning else "green")
        t.add_row("status", dot + Text(str(g(24) or "?")))
        auto = bool(g(4))
        t.add_row("auto-clean", Text("ON", style="green") if auto else Text("OFF", style="red"))
        t.add_row("clean delay", Text(f"{g(5)} min  (after cat leaves)"))
        ut = self.uses_today if self.uses_today is not None else (g(7) or 0)
        t.add_row("uses today", Text(str(ut), style="bold"))
        t.add_row("sleep now", Text("yes", style="yellow") if g(10) else Text("no"))
        t.add_row("quiet hours", Text(f"{meowant.hhmm(g(11))} → {meowant.hhmm(g(12))}"))
        binfull = (g(21) or 0) & 1
        t.add_row("waste bin", Text("FULL ⚠", style="bold red") if binfull else Text("ok", style="green"))
        faults = [f for f in meowant.decode_bits(g(22), ["E1", "E2", "E3", "E4", "E5"]) if f != "none"]
        t.add_row("faults", Text(", ".join(faults), style="red") if faults else Text("none", style="green"))
        t.add_row("presence", Text(str(g(107) or "—"), style="cyan"))

        t.add_row("", "")
        def _vv(k):
            v = g(k)
            if k == 102 and isinstance(v, str):   # decode use_record base64 → mass
                v = meowant.decode_dp102(v)
            return v
        t.add_row("[dim]vendor[/dim]", Text(
            "  ".join(f"{meowant.VENDOR[k]}={_vv(k)}" for k in meowant.VENDOR), style="bright_black"))

        title = "🐈 MEOWANT SC10" + ("  [cyan]CLEANING[/cyan]" if cleaning else "")
        return Panel(t, title=title, border_style="cyan" if cleaning else "green",
                     subtitle="c clean · a auto · s sleep · [ ] delay · r refresh · q quit")

    def repaint(self):
        self.query_one("#status", Static).update(self.render_panel())
        self.query_one("#msg", Static).update(self.msg)

    # ---- workers ---------------------------------------------------------
    @work(thread=True, exclusive=True, group="poll")
    def action_refresh(self):
        try:
            state = client.get_state(BASE)
            self.call_from_thread(self._apply, state)
        except (urllib.error.URLError, OSError):
            self.call_from_thread(self._set_offline)

    def _apply(self, state):
        # accepts a full /state dict, or a bare raw-dps dict (tests)
        self.dps = state.get("raw", state)
        self.uses_today = state.get("uses_today")
        if self.msg in ("connecting…", ) or self.msg.startswith("daemon offline"):
            self.msg = ""
        self.repaint()

    def _set_offline(self):
        self.msg = f"daemon offline ({BASE})"
        self.repaint()

    @work(thread=True, group="cmd")
    def _cmd(self, action, value, label):
        try:
            r = client.send_command(BASE, action, value)
            ok = r.get("ok", False)
            err = r.get("error") if not ok else None
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            ok = False
            err = str(e)
        self.call_from_thread(self._after, ok, err, label)

    def _set_msg(self, msg):
        self.msg = msg
        self.repaint()

    def _after(self, ok, err, label):
        self.msg = f"✗ {label}: {err}" if not ok else f"✓ {label}"
        self.repaint()
        self.action_refresh()

    # ---- actions ---------------------------------------------------------
    def action_clean(self):
        self.msg = "triggering clean…"; self.repaint()
        self._cmd("clean", None, "clean")

    def action_autoclean(self):
        new = not bool(self.dps.get("4"))
        self._cmd("autoclean", new, f"auto-clean {'ON' if new else 'OFF'}")

    @work(thread=True, group="cmd")
    def action_sleep(self):
        new = not bool(self.dps.get("10"))
        try:
            r = client.send_command(BASE, "sleep", new)
            ok = r.get("ok", False)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            ok = False
        if ok:
            self.call_from_thread(self._after, True, None, f"sleep {'ON' if new else 'OFF'}")
        else:
            self.call_from_thread(self._set_msg, "sleep not supported by daemon")

    def action_delay(self, step: int):
        cur = int(self.dps.get("5") or 3)
        new = max(1, min(60, cur + step))
        self._cmd("delay", new, f"delay {new}m")

    def action_refresh_now(self):
        self.action_refresh()


if __name__ == "__main__":
    Dashboard().run()
