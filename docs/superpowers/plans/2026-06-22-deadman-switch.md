# Dead-Man's Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An independent, dumb watchdog process that screams to Telegram if the cats stop being monitored — the actual cat-safety net (Component 1 of `docs/superpowers/specs/2026-06-22-self-healing-design.md`).

**Architecture:** A standalone `deadmand.py` entry, run **once-and-exit** by its own launchd job (`StartInterval`), so it can't wedge the way a long-lived loop could — every run is fresh. Each run reads `meowant.db` directly (a file, no dependency on the meowant daemon) for the no-go/per-cat checks, and probes `:8765/state` for daemon-liveness. It fires Telegram on any failed check, de-duped by a tiny JSON latch file so a standing condition re-alarms every few hours rather than every run. It fails LOUD: any exception in its own logic still fires an alert. It is completely independent of, and unmodifiable by, the (future) remediation layer.

**Tech Stack:** Python 3.10 stdlib (`sqlite3` via existing `mw.store`, `urllib`, `datetime`, `json`), launchd, pytest. No new dependencies.

## Global Constraints

- The switch must NOT depend on the meowant daemon being alive for the no-go/per-cat checks — it opens `meowant.db` directly via `store.connect`.
- Fail LOUD: an exception anywhere in a run still results in a Telegram alert ("better a false alarm than silence").
- No-go threshold default **12h**; honor quiet hours (`quiet_start`/`quiet_end` in config, default 22:00–08:00) by suppressing the no-go alarm during the quiet window.
- Per-cat silence check is **best-effort and DEFAULT-OFF** (`deadman.per_cat_enabled=false`) because the unmonitored second litter box makes it false-alarm-prone until consolidated. Global no-go is the always-on floor.
- Re-alarm cadence: a standing condition re-fires at most every `realarm_hours` (default 3), tracked in a JSON latch file (gitignored).
- Reuse `mw.alerts.make_notify` for delivery and `mw.config` for settings. Match `mw/health_watch.py` style and the existing `tests/` style.
- Secrets stay in gitignored `config.json`; never commit them.

---

### Task 1: `DeadManSwitch` core + global no-go check

**Files:**
- Create: `mw/deadman.py`
- Test: `tests/test_deadman.py`

**Interfaces:**
- Produces:
  - `DeadManSwitch(conn, notify, now_fn=time.time, no_go_hours=12, quiet_start="22:00", quiet_end="08:00", per_cat_enabled=False, per_cat_hours=24, liveness_stale_s=180, realarm_hours=3, state_path="deadman_state.json", state_probe=None)`
  - `.check_no_go() -> str | None` — returns an alert message if the box has gone unused beyond `no_go_hours` (outside quiet hours), else None.
  - helper `_in_quiet(now_epoch) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_deadman.py
import time
from datetime import datetime
from mw import store
from mw.deadman import DeadManSwitch


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return conn


def _elim(conn, epoch, cat=None):
    v = store.open_visit(conn, epoch); store.mark_elimination(conn, v, 60)
    store.close_visit(conn, v, epoch + 60, 60)
    if cat:
        store.set_visit_identity(conn, v, store.cat_id_by_name(conn, cat), 1.0)
    return v


def _sw(conn, now, **kw):
    return DeadManSwitch(conn, notify=lambda m: None, now_fn=lambda: now,
                         state_path=kw.pop("state_path", "/tmp/_dm_unused.json"), **kw)


def test_no_go_fires_past_threshold(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 13 * 3600)                      # last use 13h ago
    sw = _sw(conn, base, no_go_hours=12)
    msg = sw.check_no_go()
    assert msg is not None and "13" in msg


def test_no_go_quiet_for_recent(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 2 * 3600)                       # 2h ago — fine
    assert _sw(conn, base, no_go_hours=12).check_no_go() is None


def test_no_go_suppressed_during_quiet_hours(tmp_path):
    conn = _db(tmp_path)
    # 03:00 local, inside 22:00–08:00 quiet window; last use 13h ago
    base = time.mktime(time.strptime("2026-06-22 03:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 13 * 3600)
    assert _sw(conn, base, no_go_hours=12).check_no_go() is None   # deferred until quiet ends


def test_no_go_none_on_empty_db(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    assert _sw(conn, base).check_no_go() is None       # no data -> no alarm
```

- [ ] **Step 2: Run, verify fail** — `cd ~/repos/meowant && python3 -m pytest tests/test_deadman.py -q` → ModuleNotFoundError / AttributeError.

- [ ] **Step 3: Implement `mw/deadman.py`:**

```python
"""Independent dead-man's switch: a dumb watchdog that screams to Telegram if the
cats stop being monitored. Run once-and-exit by its own launchd job so it can't
wedge; reads meowant.db directly (no dependency on the meowant daemon) and probes
:8765/state for liveness. Fails LOUD — an exception still fires an alert."""
import json
import os
import sys
import time
from datetime import datetime

from mw import store


def _hhmm_to_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


class DeadManSwitch:
    def __init__(self, conn, notify, now_fn=time.time, no_go_hours=12,
                 quiet_start="22:00", quiet_end="08:00", per_cat_enabled=False,
                 per_cat_hours=24, liveness_stale_s=180, realarm_hours=3,
                 state_path="deadman_state.json", state_probe=None):
        self.conn = conn
        self.notify = notify
        self.now = now_fn
        self.no_go_hours = no_go_hours
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self.per_cat_enabled = per_cat_enabled
        self.per_cat_hours = per_cat_hours
        self.liveness_stale_s = liveness_stale_s
        self.realarm_hours = realarm_hours
        self.state_path = state_path
        self.state_probe = state_probe        # () -> dict|None ; None => default HTTP probe

    def _in_quiet(self, now):
        lt = time.localtime(now)
        cur = lt.tm_hour * 60 + lt.tm_min
        s, e = _hhmm_to_min(self.quiet_start), _hhmm_to_min(self.quiet_end)
        return (s <= cur < e) if s <= e else (cur >= s or cur < e)

    def check_no_go(self):
        ts = store.last_elimination_ts(self.conn)
        if ts is None:
            return None
        now = self.now()
        if self._in_quiet(now):
            return None                       # don't alarm overnight; recheck after quiet
        hours = (now - datetime.fromisoformat(ts).timestamp()) / 3600.0
        if hours >= self.no_go_hours:
            since = ts[5:16].replace("T", " ")
            return (f"🚨 DEAD-MAN: no litter box use in {hours:.0f}h (since {since}) "
                    f"— check on the cats")
        return None
```

- [ ] **Step 4: Run, verify pass** — `python3 -m pytest tests/test_deadman.py -q`
- [ ] **Step 5: Commit**

```bash
git add mw/deadman.py tests/test_deadman.py
git commit -m "feat(deadman): global no-go check with quiet-hours suppression"
```

---

### Task 2: daemon-liveness check

**Files:**
- Modify: `mw/deadman.py`
- Test: `tests/test_deadman.py` (append)

**Interfaces:**
- Consumes: `state_probe` (a `() -> dict|None` injected in tests; returns the parsed `/state` JSON, or None if unreachable).
- Produces: `.check_liveness() -> str | None` — alert if `/state` is unreachable OR its `last_ok_ts` is older than `liveness_stale_s` (the daemon is wedged). A default `_http_probe()` is used when `state_probe` is None.

- [ ] **Step 1: Write failing tests**

```python
def test_liveness_fires_when_unreachable(tmp_path):
    conn = _db(tmp_path)
    sw = _sw(conn, 10_000.0, state_probe=lambda: None)     # daemon down
    assert "daemon" in sw.check_liveness().lower()


def test_liveness_fires_when_wedged(tmp_path):
    conn = _db(tmp_path)
    now = 10_000.0
    sw = _sw(conn, now, liveness_stale_s=180,
             state_probe=lambda: {"last_ok_ts": now - 600})  # last poll 10min ago
    assert sw.check_liveness() is not None


def test_liveness_ok_when_fresh(tmp_path):
    conn = _db(tmp_path)
    now = 10_000.0
    sw = _sw(conn, now, liveness_stale_s=180,
             state_probe=lambda: {"last_ok_ts": now - 5})    # polled 5s ago
    assert sw.check_liveness() is None
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** (append to `DeadManSwitch`, and add the default probe helper at module level):

```python
# module level
def _http_probe(url="http://localhost:8765/state", timeout=5):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None
```

```python
    # method on DeadManSwitch
    def check_liveness(self):
        probe = self.state_probe or _http_probe
        st = probe()
        if st is None:
            return "🚨 DEAD-MAN: meowant daemon unreachable (:8765 down) — monitoring is OFF"
        last_ok = st.get("last_ok_ts")
        if last_ok is None or (self.now() - last_ok) > self.liveness_stale_s:
            age = "unknown" if last_ok is None else f"{(self.now()-last_ok)/60:.0f}min"
            return (f"🚨 DEAD-MAN: daemon wedged — no device poll in {age} "
                    f"— monitoring may be stalled")
        return None
```

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit**

```bash
git add mw/deadman.py tests/test_deadman.py
git commit -m "feat(deadman): daemon-liveness check via /state probe"
```

---

### Task 3: per-cat silence check (best-effort, default-off)

**Files:**
- Modify: `mw/deadman.py`
- Test: `tests/test_deadman.py` (append)

**Interfaces:**
- Produces: `.check_per_cat() -> list[str]` — one alert per cat that has prior history but whose last elimination is older than `per_cat_hours` WHILE at least one other cat eliminated more recently (so it's a single-cat gap, not a system-wide quiet period). Returns `[]` when `per_cat_enabled` is False.

- [ ] **Step 1: Write failing tests**

```python
def test_per_cat_off_by_default(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 30 * 3600, cat="Ella")          # Ella silent 30h
    _elim(conn, base - 1 * 3600, cat="Ucok")           # Ucok recent
    assert _sw(conn, base).check_per_cat() == []        # disabled -> nothing


def test_per_cat_fires_for_silent_cat(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 30 * 3600, cat="Ella")          # Ella 30h ago
    _elim(conn, base - 1 * 3600, cat="Ucok")           # Ucok 1h ago (system clearly working)
    msgs = _sw(conn, base, per_cat_enabled=True, per_cat_hours=24).check_per_cat()
    assert any("Ella" in m for m in msgs)
    assert not any("Ucok" in m for m in msgs)
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement:**

```python
    def check_per_cat(self):
        if not self.per_cat_enabled:
            return []
        now = self.now()
        latest = {}   # cat name -> most recent eliminated enter_ts (epoch)
        for s in store.sessions(self.conn):
            if not s["eliminated"] or not s["cat"]:
                continue
            t = datetime.fromisoformat(s["enter_ts"]).timestamp()
            latest[s["cat"]] = max(latest.get(s["cat"], 0), t)
        if not latest:
            return []
        most_recent_any = max(latest.values())
        out = []
        for cat, t in latest.items():
            hours = (now - t) / 3600.0
            # only flag if the SYSTEM is clearly working (someone went recently) but
            # THIS cat is silent — avoids firing during a global quiet/outage period.
            if hours >= self.per_cat_hours and (now - most_recent_any) < self.per_cat_hours:
                out.append(f"🚨 DEAD-MAN: {cat} hasn't used the box in {hours:.0f}h "
                           f"(others have) — check on {cat}")
        return out
```

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit**

```bash
git add mw/deadman.py tests/test_deadman.py
git commit -m "feat(deadman): best-effort per-cat silence check (default off)"
```

---

### Task 4: `run_once` — fail-loud + de-dup latch

**Files:**
- Modify: `mw/deadman.py`
- Test: `tests/test_deadman.py` (append)

**Interfaces:**
- Produces: `.run_once() -> int` — runs all checks, fires `notify` for each alert that isn't latch-suppressed, returns the count fired. Wraps everything: if a check raises, it fires a "switch error" alert (fail-loud) and continues. Latch via `state_path` JSON `{key: last_alarmed_iso}`; an alert key re-fires only if `>= realarm_hours` since last fire.

- [ ] **Step 1: Write failing tests**

```python
def test_run_once_fires_and_latches(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 13 * 3600)
    sent = []
    sw = DeadManSwitch(conn, notify=sent.append, now_fn=lambda: base, no_go_hours=12,
                       state_path=str(tmp_path / "st.json"),
                       state_probe=lambda: {"last_ok_ts": base - 5})  # daemon healthy
    assert sw.run_once() == 1                       # no-go fires
    assert sw.run_once() == 0                       # latched within realarm window
    assert any("no litter box use" in m.lower() for m in sent)


def test_run_once_realarms_after_window(tmp_path):
    conn = _db(tmp_path)
    base = time.mktime(time.strptime("2026-06-22 14:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 13 * 3600)
    sent = []
    st = str(tmp_path / "st.json")
    DeadManSwitch(conn, sent.append, now_fn=lambda: base, no_go_hours=12, realarm_hours=3,
                  state_path=st, state_probe=lambda: {"last_ok_ts": base-5}).run_once()
    later = base + 4 * 3600                          # 4h later, still bad
    n = DeadManSwitch(conn, sent.append, now_fn=lambda: later, no_go_hours=12,
                      realarm_hours=3, state_path=st,
                      state_probe=lambda: {"last_ok_ts": later-5}).run_once()
    assert n == 1                                    # re-alarmed after the window


def test_run_once_fails_loud_on_exception(tmp_path):
    conn = _db(tmp_path)
    sent = []
    sw = DeadManSwitch(conn, notify=sent.append, now_fn=lambda: 10_000.0,
                       state_path=str(tmp_path / "st.json"),
                       state_probe=lambda: {"last_ok_ts": 10_000.0 - 5})
    sw.check_no_go = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # force failure
    sw.run_once()
    assert any("dead-man" in m.lower() and ("error" in m.lower() or "boom" in m.lower())
               for m in sent)                        # screamed instead of dying silently
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement:**

```python
    def _load_state(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, state):
        try:
            with open(self.state_path, "w") as f:
                json.dump(state, f)
        except Exception as e:
            print(f"[deadman] state save failed: {e}", file=sys.stderr)

    def _fire(self, key, msg, state):
        last = state.get(key)
        if last is not None:
            age_h = (self.now() - datetime.fromisoformat(last).timestamp()) / 3600.0
            if age_h < self.realarm_hours:
                return 0                              # latched — re-fire later
        self.notify(msg)
        state[key] = datetime.fromtimestamp(self.now()).isoformat(timespec="seconds")
        return 1

    def run_once(self):
        state = self._load_state()
        fired = 0
        # each check is independently fail-loud: a crash in one still alarms + continues
        for key, fn in (("no_go", lambda: [self.check_no_go()]),
                        ("liveness", lambda: [self.check_liveness()]),
                        ("per_cat", self.check_per_cat)):
            try:
                for msg in fn():
                    if msg:
                        fired += self._fire(f"{key}", msg, state)
            except Exception as e:
                fired += self._fire(f"{key}_error",
                                    f"🚨 DEAD-MAN: '{key}' check ERRORED ({e}) — "
                                    f"investigate, monitoring integrity unknown", state)
        self._save_state(state)
        return fired
```

- [ ] **Step 4: Run, verify pass** (full file: `python3 -m pytest tests/test_deadman.py -q`).
- [ ] **Step 5: Commit**

```bash
git add mw/deadman.py tests/test_deadman.py
git commit -m "feat(deadman): run_once with fail-loud wrapping + re-alarm latch"
```

---

### Task 5: `deadmand.py` entry, config, launchd job

**Files:**
- Create: `deadmand.py` (repo root)
- Modify: `config.json` (gitignored — add a `deadman` block) and `config.example.json` (committed template)
- Create: `~/Library/LaunchAgents/com.meowant.deadman.plist`

**Interfaces:**
- Consumes: `mw.config`, `mw.alerts.make_notify`, `mw.store`, `mw.deadman.DeadManSwitch`.
- Produces: a runnable `python3 deadmand.py` that does ONE check pass and exits 0.

- [ ] **Step 1: Create `deadmand.py`:**

```python
#!/usr/bin/env python3
"""Run ONE dead-man's-switch pass and exit. Scheduled by launchd (StartInterval),
independent of meowantd so it can't share its failure mode."""
from mw import config, store
from mw.alerts import make_notify
from mw.deadman import DeadManSwitch


def main():
    cfg = config.load("config.json")
    g = lambda k, d=None: config.get(cfg, k, d)
    conn = store.connect(g("deadman.db_path", "meowant.db"))
    sw = DeadManSwitch(
        conn, notify=make_notify(lambda k: config.get(cfg, k)),
        no_go_hours=g("deadman.no_go_hours", 12),
        quiet_start=g("quiet_start", "22:00"), quiet_end=g("quiet_end", "08:00"),
        per_cat_enabled=g("deadman.per_cat_enabled", False),
        per_cat_hours=g("deadman.per_cat_hours", 24),
        liveness_stale_s=g("deadman.liveness_stale_s", 180),
        realarm_hours=g("deadman.realarm_hours", 3),
        state_path=g("deadman.state_path", "deadman_state.json"))
    n = sw.run_once()
    print(f"[deadman] pass complete, {n} alert(s) fired")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test the entry**

Run: `cd ~/repos/meowant && python3 deadmand.py`
Expected: prints `[deadman] pass complete, N alert(s) fired` and exits 0 (N depends on live state; a healthy system with a recent visit prints 0).

- [ ] **Step 3: Add the `deadman` config block** (gitignored `config.json`):

```bash
cd ~/repos/meowant && python3 - <<'PY'
import json
c = json.load(open("config.json"))
d = c.setdefault("deadman", {})
d.setdefault("no_go_hours", 12)
d.setdefault("per_cat_enabled", False)   # default off until the 2nd box is consolidated
d.setdefault("per_cat_hours", 24)
d.setdefault("liveness_stale_s", 180)
d.setdefault("realarm_hours", 3)
d.setdefault("state_path", "deadman_state.json")
json.dump(c, open("config.json", "w"), indent=2)
print("deadman config:", json.dumps(d))
PY
echo "deadman_state.json" >> .gitignore
```

Also add the same `deadman` block (with placeholder/default values, no secrets) to the committed `config.example.json` so the template documents it.

- [ ] **Step 4: Create + load the launchd job**

Create `~/Library/LaunchAgents/com.meowant.deadman.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.meowant.deadman</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/opt/python@3.10/libexec/bin/python3</string>
    <string>/Users/ariapramesi/repos/meowant/deadmand.py</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/ariapramesi/repos/meowant</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/ariapramesi/.local/bin</string></dict>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>/Users/ariapramesi/repos/meowant/deadman.log</string>
  <key>StandardOutPath</key><string>/Users/ariapramesi/repos/meowant/deadman.log</string>
</dict>
</plist>
```

Run: `launchctl load ~/Library/LaunchAgents/com.meowant.deadman.plist`
Then force one run: `launchctl start com.meowant.deadman`
Expected: it appears in `launchctl list | grep deadman`, and a pass runs every 30 min independent of meowantd. (Note `StartInterval` cron-style runs are independent of the meowantd `kickstart -k` restart issue — separate job, can't be wedged by it.)

- [ ] **Step 5: Live validation — confirm it actually screams**

Temporarily prove delivery end-to-end: run with a tiny threshold so a real alert fires to Telegram, then restore.

```bash
cd ~/repos/meowant && python3 - <<'PY'
from mw import config, store
from mw.alerts import make_notify
from mw.deadman import DeadManSwitch
cfg = config.load("config.json")
sw = DeadManSwitch(store.connect("meowant.db"),
                   notify=make_notify(lambda k: config.get(cfg, k)),
                   now_fn=__import__("time").time, no_go_hours=0,   # force no-go
                   state_path="/tmp/dm_validate.json",
                   state_probe=lambda: {"last_ok_ts": __import__("time").time()})
print("fired:", sw.run_once(), "(check Telegram for a 🚨 DEAD-MAN message)")
PY
```

Expected: `fired: 1` and a `🚨 DEAD-MAN` message arrives on Telegram. (Uses a throwaway state file and `no_go_hours=0` only for this test; the real config is untouched.)

- [ ] **Step 6: Commit**

```bash
git add deadmand.py config.example.json .gitignore docs/superpowers/plans/2026-06-22-deadman-switch.md
git commit -m "feat(deadman): standalone entry + launchd job (independent watchdog)"
```

## Self-Review

- **Spec coverage:** independent process ✔ (own launchd, run-once-exit); reads meowant.db directly ✔; global no-go raw-signal ✔ (Task 1, via `last_elimination_ts` which is dp102-derived, upstream of the labeler); per-cat best-effort ✔ (Task 3, default-off per the 2nd-box caveat); daemon liveness ✔ (Task 2); Telegram delivery ✔ (`make_notify`); fail-loud ✔ (Task 4); quiet hours ✔ (Task 1); second-box caveat ✔ (per-cat default-off + noted). Life-critical-channel separation: deferred — v1 reuses the existing Telegram chat (no chatty auto-fix layer exists yet); revisit when Component 2/3 land.
- **Placeholder scan:** none — every step has full code/commands.
- **Type consistency:** `check_no_go()/check_liveness()` return `str|None`; `check_per_cat()` returns `list[str]`; `run_once()` returns `int`; `_fire(key,msg,state)` consistent across Task 4. `state_probe` is `()->dict|None` in Tasks 2 and 5.
