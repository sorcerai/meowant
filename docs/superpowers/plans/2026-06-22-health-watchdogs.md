# Health Watchdogs: no-go alarm + dead-man's switch Plan

> **For agentic workers:** Implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** The two "tell me when something is WRONG" travel safeguards:
- **No-go alarm (`d9e`):** push an alert when the box goes unused for ≥ N hours (default
  12h) — the sick-cat / blocked-bladder detector.
- **Dead-man's switch (`3vs`):** (a) ping an external heartbeat URL every few minutes so a
  dead daemon/host is caught off-box; (b) a daily "✅ alive" digest via Telegram so a
  healthy silence is positively confirmed, not just assumed.

**Architecture:** Two new daemon threads (the `CaptureHealth` pattern). `HealthWatch`
owns the no-go alarm + the daily digest (both run on one ~30-min loop). `Heartbeat` owns
the external URL ping (separate, faster cadence, only starts if a URL is configured). All
alerts go through the injected `notify` (Telegram in prod). Latches re-arm so alarms
fire once per episode.

**Tech Stack:** Python 3.10 stdlib, sqlite3, pytest. Matches `mw/capture_health.py` style.

## Global Constraints

- No new dependencies. External ping is a plain `urllib` GET.
- Restart behavior: in-process latches reset on restart (one alarm per restart while the
  condition holds is acceptable for an alarm — do NOT add persistence here).
- The no-go threshold (12h) comfortably exceeds the overnight quiet window (~10h), so a
  normal night won't false-alarm. Keep it a flat hour threshold (no per-cat — that's the
  deferred Phase-4 `meowant-6mo`).
- `Heartbeat` thread starts ONLY if `health.heartbeat_url` is set (inert otherwise, so
  nothing breaks before the user creates a healthchecks.io check).
- Config defaults: `health.no_go_hours=12`, `health.check_interval_s=1800`,
  `health.digest_hour=9`, `health.heartbeat_url=""`, `health.heartbeat_interval_s=900`.

---

### Task 1: store + report support

**Files:** Modify `mw/store.py`, `mw/report.py`; tests in `tests/test_store.py`,
`tests/test_report.py` (append).

**Interfaces:**
- `store.last_elimination_ts(conn) -> str | None` — enter_ts (local-ISO) of the most
  recent eliminated visit, or None.
- `report.digest(conn, now=None) -> str` — one short "alive + today" summary built on
  sessions: total uses today, last use time, per-cat today counts.

- [ ] **Step 1: failing tests**

```python
# tests/test_store.py (append)
def test_last_elimination_ts(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    assert store.last_elimination_ts(conn) is None        # empty DB
    v1 = store.open_visit(conn, 1000.0); store.mark_elimination(conn, v1, 55)
    store.close_visit(conn, v1, 1060.0, 60)
    v2 = store.open_visit(conn, 5000.0)                    # later but NOT eliminated
    store.close_visit(conn, v2, 5005.0, 5)
    ts = store.last_elimination_ts(conn)
    assert ts == store._iso(1000.0)                        # the eliminated one
```

```python
# tests/test_report.py (append)
def test_digest_summarizes_today(tmp_path):
    import time
    from datetime import date
    conn = _db(tmp_path)
    now = time.time()
    v = store.open_visit(conn, now); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, now + 60, 60)
    store.set_visit_identity(conn, v, store.cat_id_by_name(conn, "Ucok"), 1.0)
    txt = report.digest(conn, now=now + 120)
    assert "Ucok" in txt and ("1" in txt)
```

(`_db` helper already exists at the top of test_report.py.)

- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement.**

```python
# store.py (near recent_visits)
def last_elimination_ts(conn):
    """enter_ts of the most recent eliminated visit, or None — drives the no-go alarm."""
    with _lock:
        row = conn.execute(
            "SELECT enter_ts FROM visits WHERE eliminated=1 "
            "ORDER BY enter_ts DESC LIMIT 1").fetchone()
        return row["enter_ts"] if row else None
```

```python
# report.py (append)
def digest(conn, now=None):
    """One-line-ish 'alive + today' summary for the daily heartbeat digest."""
    from datetime import date, datetime
    now = now if now is not None else datetime.now().timestamp()
    today = date.fromtimestamp(now).isoformat()
    sess = store.sessions(conn)
    today_elim = [s for s in sess if s["eliminated"] and s["enter_ts"].startswith(today)]
    if not today_elim:
        return f"✅ Meowant alive [{today}] — no box uses yet today."
    from collections import Counter
    by_cat = Counter((s["cat"] or "unattributed") for s in today_elim)
    last = max(s["enter_ts"] for s in today_elim)[11:16]
    parts = ", ".join(f"{c} {n}" for c, n in by_cat.most_common())
    return (f"✅ Meowant alive [{today}] — {len(today_elim)} box uses today "
            f"(last {last}). {parts}")
```

- [ ] **Step 4: run, verify pass.**  **Step 5: commit.**

---

### Task 2: `HealthWatch` (no-go alarm + daily digest)

**Files:** Create `mw/health_watch.py`; test `tests/test_health_watch.py`.

**Interfaces:**
- `HealthWatch(conn, notify, now_fn=time.time, no_go_hours=12, digest_hour=9,
  interval=1800)` with `run_once()` and `run()`.
  - No-go: if `last_elimination_ts` is older than `no_go_hours`, alert ONCE (latch);
    re-arm when a fresh elimination brings it back under threshold.
  - Digest: once per local day, at/after `digest_hour`, send `report.digest`.

- [ ] **Step 1: failing tests** (`tests/test_health_watch.py`):

```python
from mw import store
from mw.health_watch import HealthWatch


def _conn(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    return conn


def _elim(conn, ts):
    v = store.open_visit(conn, ts); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, ts + 60, 60)
    return v


def test_no_go_alarm_fires_once_then_latches(tmp_path):
    conn = _conn(tmp_path)
    _elim(conn, 1000.0)                       # last use at t=1000
    sent = []
    now = {"t": 1000.0 + 13 * 3600}           # 13h later (> 12h)
    hw = HealthWatch(conn, sent.append, now_fn=lambda: now["t"],
                     no_go_hours=12, digest_hour=99)   # digest_hour=99 disables digest
    hw.run_once(); hw.run_once()              # two passes
    nogo = [m for m in sent if "No litter box use" in m]
    assert len(nogo) == 1                     # latched: only one alarm


def test_no_go_rearms_after_new_use(tmp_path):
    conn = _conn(tmp_path)
    _elim(conn, 1000.0)
    sent = []
    now = {"t": 1000.0 + 13 * 3600}
    hw = HealthWatch(conn, sent.append, now_fn=lambda: now["t"],
                     no_go_hours=12, digest_hour=99)
    hw.run_once()                             # alarm 1
    _elim(conn, now["t"])                     # a fresh use clears it
    hw.run_once()                             # under threshold -> re-arm, no alarm
    now["t"] += 13 * 3600                     # quiet again > 12h
    hw.run_once()                             # alarm 2
    assert len([m for m in sent if "No litter box use" in m]) == 2


def test_no_alarm_when_recent(tmp_path):
    conn = _conn(tmp_path)
    _elim(conn, 1000.0)
    sent = []
    hw = HealthWatch(conn, sent.append, now_fn=lambda: 1000.0 + 3600,  # 1h
                     no_go_hours=12, digest_hour=99)
    hw.run_once()
    assert [m for m in sent if "No litter box use" in m] == []


def test_no_alarm_on_empty_db(tmp_path):
    conn = _conn(tmp_path)
    sent = []
    hw = HealthWatch(conn, sent.append, now_fn=lambda: 1_000_000.0,
                     no_go_hours=12, digest_hour=99)
    hw.run_once()
    assert sent == []                         # no data -> no alarm


def test_daily_digest_fires_once_per_day(tmp_path):
    import time as _t
    conn = _conn(tmp_path)
    # pick a now at 10:00 local on some day, with a use today
    base = _t.mktime(_t.strptime("2026-06-22 10:00", "%Y-%m-%d %H:%M"))
    _elim(conn, base - 3600)
    sent = []
    hw = HealthWatch(conn, sent.append, now_fn=lambda: base,
                     no_go_hours=999, digest_hour=9)    # no_go disabled
    hw.run_once(); hw.run_once()
    digests = [m for m in sent if "alive" in m.lower()]
    assert len(digests) == 1                  # once per day even on repeated passes
```

- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement `mw/health_watch.py`:**

```python
"""Absence/liveness watchdogs (the 'tell me when something is WRONG' half).

HealthWatch: a no-go alarm (box unused >N hours -> a cat may be sick/blocked) plus a
once-daily 'alive' digest. Both run on one slow loop. Heartbeat (separate) pings an
external URL so a dead daemon/host is caught off-box."""
import sys
import time
from datetime import date, datetime

from mw import store, report


class HealthWatch:
    def __init__(self, conn, notify, now_fn=time.time,
                 no_go_hours=12, digest_hour=9, interval=1800):
        self.conn = conn
        self.notify = notify
        self.now = now_fn
        self.no_go_hours = no_go_hours
        self.digest_hour = digest_hour
        self.interval = interval
        self._alarmed = False          # no-go latch
        self._digest_day = None        # last local date a digest was sent

    def _check_no_go(self):
        ts = store.last_elimination_ts(self.conn)
        if ts is None:
            return                     # no data yet — nothing to alarm on
        hours = (self.now() - datetime.fromisoformat(ts).timestamp()) / 3600.0
        if hours >= self.no_go_hours and not self._alarmed:
            self.notify(f"⚠️ No litter box use in {hours:.0f}h (since {ts[5:16].replace('T',' ')}) "
                        f"— check on the cats")
            self._alarmed = True
        elif hours < self.no_go_hours:
            self._alarmed = False      # a fresh use re-arms the alarm

    def _check_digest(self):
        lt = time.localtime(self.now())
        today = date(lt.tm_year, lt.tm_mon, lt.tm_mday).isoformat()
        if today != self._digest_day and lt.tm_hour >= self.digest_hour:
            self.notify(report.digest(self.conn, now=self.now()))
            self._digest_day = today

    def run_once(self):
        self._check_no_go()
        self._check_digest()

    def run(self):
        while True:
            try:
                self.run_once()
            except Exception as e:
                print(f"[health-watch] error: {e}", file=sys.stderr)
            time.sleep(self.interval)
```

- [ ] **Step 4: run, verify pass.**  **Step 5: commit.**

---

### Task 3: `Heartbeat` (external dead-man's switch ping)

**Files:** add `Heartbeat` to `mw/health_watch.py`; test `tests/test_health_watch.py`.

**Interfaces:**
- `Heartbeat(ping_url, getter=<urllib GET>, now_fn=time.time, interval=900)` with
  `run_once()` (one ping) and `run()`.

- [ ] **Step 1: failing test**

```python
def test_heartbeat_pings_url():
    from mw.health_watch import Heartbeat
    hits = []
    hb = Heartbeat("https://hc-ping.com/abc", getter=lambda url: hits.append(url))
    hb.run_once()
    assert hits == ["https://hc-ping.com/abc"]


def test_heartbeat_swallows_errors():
    from mw.health_watch import Heartbeat
    def boom(url): raise OSError("network down")
    hb = Heartbeat("https://hc-ping.com/abc", getter=boom)
    hb.run_once()   # must not raise
```

- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement** (append to health_watch.py):

```python
def _http_ping(url):
    import urllib.request
    urllib.request.urlopen(url, timeout=10)


class Heartbeat:
    """Ping an external healthcheck URL (e.g. healthchecks.io) every interval. If the
    pings STOP — daemon crash-loop, Mac asleep/off/offline — that service alerts the
    user. The only check that survives the daemon/host itself dying."""
    def __init__(self, ping_url, getter=_http_ping, now_fn=time.time, interval=900):
        self.ping_url = ping_url
        self._get = getter
        self.now = now_fn
        self.interval = interval

    def run_once(self):
        try:
            self._get(self.ping_url)
        except Exception as e:
            print(f"[heartbeat] ping failed: {e}", file=sys.stderr)

    def run(self):
        while True:
            self.run_once()
            time.sleep(self.interval)
```

- [ ] **Step 4: run, verify pass.**  **Step 5: commit.**

---

### Task 4: wire into `meowantd.py`

**Files:** Modify `meowantd.py`.

- [ ] **Step 1:** after the daemon poll thread starts (near the Telegram block), add:

```python
from mw.health_watch import HealthWatch, Heartbeat
hw = HealthWatch(
    conn, make_notify(lambda k: config.get(cfg, k)),
    no_go_hours=config.get(cfg, "health.no_go_hours", 12),
    digest_hour=config.get(cfg, "health.digest_hour", 9),
    interval=config.get(cfg, "health.check_interval_s", 1800))
threading.Thread(target=hw.run, daemon=True).start()
print("health-watch: no-go alarm + daily digest")

hb_url = config.get(cfg, "health.heartbeat_url", "")
if hb_url:
    hb = Heartbeat(hb_url, interval=config.get(cfg, "health.heartbeat_interval_s", 900))
    threading.Thread(target=hb.run, daemon=True).start()
    print("heartbeat: external dead-man's-switch ping")
```

- [ ] **Step 2:** `python3 -c "import meowantd"` compiles; full suite `pytest -q` green.
- [ ] **Step 3: commit.**

## Self-Review
- Coverage: no-go fire-once/re-arm/recent/empty ✔, daily digest once-per-day ✔,
  heartbeat ping + error-swallow ✔. HTTP ping is a thin untested wrapper.
- Types: `last_elimination_ts(conn)`, `digest(conn, now=None)`,
  `HealthWatch(conn, notify, ...)`, `Heartbeat(ping_url, ...)`.
- DEPLOY: restart with `launchctl kickstart -k gui/$UID/com.meowant.daemon`. No backfill.
  Heartbeat inert until `health.heartbeat_url` is set in config.json.
