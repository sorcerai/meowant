# Cat Feeder — Phase 2 (Bowl Camera + Vision) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bowl full/empty vision (a Wyze OG `meowcam5`) so the system refills when the bowl is empty (alert, or autonomous dispense via the Phase-1 feeder) and logs how fast the bowl empties after each dispense (a food-boredom trend).

**Architecture:** `mw/bowl.py` is a pure cv2 ROI-diff (current frame vs a pinned empty-bowl reference → full/some/empty), mirroring `mw/scatter.py`. `mw/bowl_watch.py`'s `BowlWatch` polls meowcam5 on a timer, gates on cat-free frames (`catfilter.is_clear`), classifies fullness, confirms an empty read with `agy` (hybrid), debounces (2 consecutive), then alerts and/or auto-feeds (rate-limited) and logs consumption. Surfaced via the digest + a `/bowl` Telegram command.

**Tech Stack:** Python 3, `cv2`/`numpy` (already deps via scatter), `agy` CLI (already used by the labeler), `tinytuya` (Phase-1 feeder), pytest with `tmp_path` + synthetic cv2 images + fakes.

## Global Constraints

- Source of truth: `docs/superpowers/specs/2026-06-22-feeder-phase2-design.md`.
- **Detection is reference-relative**: diff the bowl ROI of the current frame vs a pinned EMPTY-bowl reference. High changed-% = food present; low = empty. Mirror `mw/scatter.py` exactly (cv2 grayscale + GaussianBlur + absdiff + threshold + `(mask>0).sum()/size`).
- **Hybrid + debounce + safety:** only a diff-`empty` read triggers `agy` confirmation; act only after **2 consecutive confirmed-empty** reads; **cat-free gate** (`catfilter.is_clear`) before judging; **auto-feed rate-limited** (`auto_feed_max_per_day`, then alert instead). These make a false-empty (→ over-feed) very unlikely.
- **No not-eating health alarm** (cut — boredom-dominated). Consumption is logged for the digest trend only.
- **Fail toward no-action / alert, never silent over-feed or missed empty:** failed grab/agy → skip the cycle; failed auto-feed → alert; rate-limit → alert. Latch alerts per empty episode, re-arm on refill, and latch only on confirmed delivery (`notify(msg) is not False`) — matching `health_watch`/`deadman`/`invariant_canary`/`feeder`.
- `store.py` access via `with _lock:`; timestamps via `_iso(epoch)`. `bowl_events` is a NEW table → add to `SCHEMA`.
- Reuse Phase-1 `store.last_feed_event_ts` for consumption timing and the Phase-1 `FeederDevice.feed` for auto-feed. Reuse the shared `catfilter` and the `agy` backend.
- Do NOT commit secrets (`config.json` gitignored). The build merges WIRED BUT DORMANT until `bowl.enabled` + a calibrated `empty_ref` exist (the camera isn't mounted yet) — exactly like the feeder's `mealtimes: []`. Detection LOGIC is fully unit-tested with synthetic frames; ROI/threshold/agy-prompt CALIBRATION is a manual build-time step against the real mounted camera.

---

### Task 1: `bowl_events` table + store functions

**Files:**
- Modify: `mw/store.py` (table in `SCHEMA`; functions near `feed_events`)
- Test: `tests/test_store.py` (add)

**Interfaces:**
- Consumes: `store._lock`, `store._iso`, `date`/`datetime`.
- Produces:
  - `store.log_bowl_event(conn, state, source="vision", secs_since_feed=None, ts=None) -> None`.
  - `store.last_bowl_state(conn) -> str|None` — most recent `source="vision"` state.
  - `store.auto_feeds_today(conn) -> int` — count of `source="auto_feed"` rows for today.
  - `store.last_consumption_secs(conn) -> int|None` — `secs_since_feed` of the most recent vision `empty` event with a non-null value.
  - `store.recent_bowl_events(conn, limit=20) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_store.py`:

```python
def test_bowl_events_log_and_query(tmp_path):
    from mw import store
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    T = 1_000_000.0
    store.log_bowl_event(conn, "full", "vision", ts=T)
    store.log_bowl_event(conn, "empty", "vision", secs_since_feed=7200, ts=T + 100)
    assert store.last_bowl_state(conn) == "empty"
    assert store.last_consumption_secs(conn) == 7200
    rows = store.recent_bowl_events(conn)
    assert len(rows) == 2 and rows[0]["state"] == "empty"


def test_auto_feeds_today_counts_only_today_autofeeds(tmp_path):
    from mw import store
    from datetime import datetime
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    now = datetime.now().timestamp()
    store.log_bowl_event(conn, "empty", "auto_feed", ts=now - 50)
    store.log_bowl_event(conn, "empty", "auto_feed", ts=now - 20)
    store.log_bowl_event(conn, "empty", "vision", ts=now - 10)   # not an auto_feed
    store.log_bowl_event(conn, "empty", "auto_feed", ts=1_000.0)  # old (1970) — not today
    assert store.auto_feeds_today(conn) == 2


def test_last_bowl_state_ignores_autofeed_rows(tmp_path):
    from mw import store
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    T = 1_000_000.0
    store.log_bowl_event(conn, "full", "vision", ts=T)
    store.log_bowl_event(conn, "empty", "auto_feed", ts=T + 50)   # bookkeeping, not a vision read
    assert store.last_bowl_state(conn) == "full"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_store.py -k bowl -v`
Expected: FAIL — `AttributeError: module 'mw.store' has no attribute 'log_bowl_event'`.

- [ ] **Step 3: Add the table to SCHEMA**

In `mw/store.py`, inside the `SCHEMA` string (after `feed_events(...)`), add:

```sql
CREATE TABLE IF NOT EXISTS bowl_events(
  id INTEGER PRIMARY KEY, ts TEXT, state TEXT,
  source TEXT,            -- 'vision' | 'auto_feed'
  secs_since_feed INTEGER);
```

- [ ] **Step 4: Add the store functions**

In `mw/store.py`, after the `feed_events` functions, add:

```python
def log_bowl_event(conn, state, source="vision", secs_since_feed=None, ts=None):
    """Record a bowl observation ('vision') or an auto-feed bookkeeping row."""
    stamp = _iso(ts) if ts is not None else datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn.execute(
            "INSERT INTO bowl_events(ts, state, source, secs_since_feed) "
            "VALUES(?,?,?,?)", (stamp, state, source, secs_since_feed))
        conn.commit()


def last_bowl_state(conn):
    """Most recent vision-observed bowl state (ignores auto_feed bookkeeping rows)."""
    with _lock:
        row = conn.execute(
            "SELECT state FROM bowl_events WHERE source='vision' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        return row["state"] if row else None


def auto_feeds_today(conn):
    """Count of auto-feed dispenses today — the BowlWatch rate-limit primitive."""
    today = date.today().isoformat()
    with _lock:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM bowl_events "
            "WHERE source='auto_feed' AND ts LIKE ?", (today + "%",)).fetchone()
        return row["n"]


def last_consumption_secs(conn):
    """secs_since_feed of the most recent vision 'empty' event that has one."""
    with _lock:
        row = conn.execute(
            "SELECT secs_since_feed FROM bowl_events "
            "WHERE source='vision' AND state='empty' AND secs_since_feed IS NOT NULL "
            "ORDER BY id DESC LIMIT 1").fetchone()
        return row["secs_since_feed"] if row else None


def recent_bowl_events(conn, limit=20):
    with _lock:
        rows = conn.execute("SELECT * FROM bowl_events ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
        return [dict(r) for r in rows]
```

(`date`/`datetime` are already imported in `store.py`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_store.py -k bowl -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
cd ~/repos/meowant
git add mw/store.py tests/test_store.py
git commit -m "feat(bowl): bowl_events table + store log/query (Phase 2)"
```

---

### Task 2: `mw/bowl.py` — fullness vision (cv2 ROI diff)

**Files:**
- Create: `mw/bowl.py`
- Test: `tests/test_bowl.py` (create)

**Interfaces:**
- Consumes: `cv2`, `numpy`.
- Produces:
  - `bowl.FULL`, `bowl.SOME`, `bowl.EMPTY` constants; `bowl.DEFAULT_ROI`.
  - `bowl.changed_pct(frame_path, empty_ref_path, roi=DEFAULT_ROI, delta=22) -> float|None` — % of the bowl ROI differing from the empty reference; None if either image is unreadable.
  - `bowl.fullness(frame_path, empty_ref_path, roi=DEFAULT_ROI, delta=22, empty_max=5.0, full_min=20.0) -> str|None` — `full|some|empty`, or None if unreadable.

- [ ] **Step 1: Write the failing test**

Create `tests/test_bowl.py`:

```python
"""Bowl fullness via ROI diff-from-empty (mirrors scatter)."""
import cv2
import numpy as np

from mw import bowl


def _img(tmp_path, name, fill, patch=None):
    """A 200x200 gray frame; optionally a bright square patch in the ROI center."""
    a = np.full((200, 200, 3), fill, dtype=np.uint8)
    if patch:
        a[80:120, 80:120] = patch          # center, inside DEFAULT_ROI
    p = str(tmp_path / name)
    cv2.imwrite(p, a)
    return p


def test_empty_matches_reference(tmp_path):
    ref = _img(tmp_path, "ref.jpg", 100)
    cur = _img(tmp_path, "cur.jpg", 100)               # identical -> empty
    assert bowl.fullness(cur, ref) == bowl.EMPTY
    assert bowl.changed_pct(cur, ref) is not None and bowl.changed_pct(cur, ref) <= 5.0


def test_full_differs_a_lot_from_reference(tmp_path):
    ref = _img(tmp_path, "ref.jpg", 100)
    cur = _img(tmp_path, "cur.jpg", 100, patch=255)    # big bright food patch -> full
    assert bowl.fullness(cur, ref) == bowl.FULL


def test_unreadable_returns_none(tmp_path):
    ref = _img(tmp_path, "ref.jpg", 100)
    assert bowl.fullness("/nonexistent.jpg", ref) is None
    assert bowl.changed_pct("/nonexistent.jpg", ref) is None


def test_some_is_between_bands(tmp_path):
    ref = _img(tmp_path, "ref.jpg", 100)
    # a small patch -> mid changed-% -> 'some' (tune bands so this lands between)
    cur = _img(tmp_path, "cur.jpg", 100, patch=255)
    # shrink the patch by using explicit bands that put this frame in 'some'
    pct = bowl.changed_pct(cur, ref)
    state = bowl.fullness(cur, ref, empty_max=pct - 1, full_min=pct + 1)
    assert state == bowl.SOME
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_bowl.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mw.bowl'`.

- [ ] **Step 3: Implement `mw/bowl.py`**

Create `mw/bowl.py`:

```python
"""Bowl fullness via ROI diff-from-empty (mirrors mw/scatter.py).

A bowl with kibble differs from the pinned empty-bowl reference inside the bowl
ROI; an empty bowl matches it. So changed-% vs the EMPTY reference is a fullness
proxy: high = food present, low = empty. Reference-relative, so fixed background
and matched lighting cancel; calibrate empty_max / full_min / roi at build
against real empty/some/full frames.
"""
import cv2
import numpy as np

FULL, SOME, EMPTY = "full", "some", "empty"
DEFAULT_ROI = (0.30, 0.30, 0.70, 0.70)   # placeholder — calibrate to the bowl


def _roi(img, roi):
    h, w = img.shape[:2]
    x0, y0, x1, y1 = roi
    return img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def changed_pct(frame_path, empty_ref_path, roi=DEFAULT_ROI, delta=22):
    """Percent of the bowl ROI differing from the empty reference, or None."""
    cur = cv2.imread(frame_path)
    ref = cv2.imread(empty_ref_path)
    if cur is None or ref is None:
        return None
    cg = cv2.GaussianBlur(_roi(cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY), roi), (5, 5), 0)
    rg = cv2.GaussianBlur(_roi(cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), roi), (5, 5), 0)
    if cg.shape != rg.shape:
        rg = cv2.resize(rg, (cg.shape[1], cg.shape[0]))
    d = cv2.absdiff(cg, rg)
    _, m = cv2.threshold(d, delta, 255, cv2.THRESH_BINARY)
    return 100.0 * float((m > 0).sum()) / m.size


def fullness(frame_path, empty_ref_path, roi=DEFAULT_ROI, delta=22,
             empty_max=5.0, full_min=20.0):
    """Classify bowl state from diff-vs-empty: 'full'|'some'|'empty', or None."""
    pct = changed_pct(frame_path, empty_ref_path, roi, delta)
    if pct is None:
        return None
    if pct <= empty_max:
        return EMPTY
    if pct >= full_min:
        return FULL
    return SOME
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_bowl.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/meowant
git add mw/bowl.py tests/test_bowl.py
git commit -m "feat(bowl): fullness vision via cv2 ROI diff-from-empty"
```

---

### Task 3: `BowlWatch` service + agy confirm

**Files:**
- Create: `mw/bowl_watch.py`
- Test: `tests/test_bowl_watch.py` (create)

**Interfaces:**
- Consumes: `mw.bowl` (Task 2); `store.log_bowl_event`, `store.last_bowl_state`, `store.auto_feeds_today`, `store.last_feed_event_ts` (Tasks 1 + Phase 1); `catfilter.is_clear`; the Phase-1 `FeederDevice.feed`.
- Produces:
  - `bowl_watch.agy_bowl_empty(frame_path, timeout=240) -> bool|None` — `agy --print` bowl-empty classifier; True empty / False has-food / None on error.
  - `bowl_watch.BowlWatch(grab, catfilter, conn, notify, feeder=None, confirm_empty=agy_bowl_empty, now_fn=time.time, *, empty_ref, roi=bowl.DEFAULT_ROI, empty_max=5.0, full_min=20.0, delta=22, poll_interval_s=1200, auto_feed=False, auto_feed_portions=1, auto_feed_max_per_day=4)` with `.poll_once()` and `.run()`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_bowl_watch.py`:

```python
"""BowlWatch: cat-free gate + diff + agy-confirm + debounce -> alert/auto-feed."""
from mw import store, bowl
from mw.bowl_watch import BowlWatch

T = 1_000_000.0


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


class _Cat:
    def __init__(self, clear=True):
        self.clear = clear

    def is_clear(self, path):
        return self.clear


class _Feeder:
    def __init__(self, ok=True):
        self.ok = ok
        self.fed = []

    def feed(self, n):
        self.fed.append(n)
        return self.ok


def _watch(tmp_path, conn, **kw):
    # grab returns a fixed path; fullness is stubbed via monkeypatch in each test
    kw.setdefault("empty_ref", "ref.jpg")
    return BowlWatch(grab=lambda: "frame.jpg", catfilter=_Cat(), conn=conn,
                     notify=kw.pop("notify"), now_fn=lambda: T, **kw)


def test_cat_present_frame_is_skipped(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    msgs = []
    w = BowlWatch(grab=lambda: "f.jpg", catfilter=_Cat(clear=False), conn=conn,
                  notify=msgs.append, now_fn=lambda: T, empty_ref="ref.jpg",
                  confirm_empty=lambda p: True)
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once()
    assert msgs == []                              # cat at bowl -> no judgment
    assert store.recent_bowl_events(conn) == []


def test_two_confirmed_empties_alert_once_then_latch(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    msgs = []
    w = _watch(tmp_path, conn, notify=msgs.append, confirm_empty=lambda p: True)
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once()                                  # streak 1 -> no alert (debounce)
    assert msgs == []
    w.poll_once()                                  # streak 2 -> alert
    w.poll_once()                                  # still empty -> latched, no repeat
    assert len(msgs) == 1 and "empty" in msgs[0].lower()


def test_single_empty_then_food_does_not_alert(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    msgs = []
    w = _watch(tmp_path, conn, notify=msgs.append, confirm_empty=lambda p: True)
    states = iter([bowl.EMPTY, bowl.FULL])
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: next(states))
    w.poll_once()                                  # empty streak 1
    w.poll_once()                                  # full -> resets streak
    assert msgs == []


def test_agy_says_food_blocks_empty_action(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    msgs = []
    w = _watch(tmp_path, conn, notify=msgs.append, confirm_empty=lambda p: False)  # agy: has food
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once(); w.poll_once()
    assert msgs == []                              # diff said empty, agy overruled


def test_auto_feed_dispenses_and_rate_limits(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    msgs = []
    feeder = _Feeder(ok=True)
    w = BowlWatch(grab=lambda: "f.jpg", catfilter=_Cat(), conn=conn,
                  notify=msgs.append, feeder=feeder, now_fn=lambda: T,
                  empty_ref="ref.jpg", confirm_empty=lambda p: True,
                  auto_feed=True, auto_feed_portions=2, auto_feed_max_per_day=1)
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once(); w.poll_once()                   # confirmed empty -> auto-feed once
    assert feeder.fed == [2]
    assert store.auto_feeds_today(conn) == 1
    # next empty episode after a refill: rate limit (1/day) reached -> alert, no feed
    w._empty_alerted = False; w._prev_state = bowl.FULL   # simulate a refill+empty again
    w.poll_once()
    assert feeder.fed == [2]                        # not fed again
    assert any("limit" in m.lower() for m in msgs)


def test_consumption_logged_on_full_to_empty(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    store.log_feed_event(conn, 2, "scheduled", ts=T - 7200)   # fed 2h before
    msgs = []
    w = _watch(tmp_path, conn, notify=msgs.append, confirm_empty=lambda p: True)
    w._prev_state = bowl.FULL
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once(); w.poll_once()
    assert store.last_consumption_secs(conn) == 7200          # ~2h to empty


def test_failed_delivery_does_not_latch(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    sent = []

    def _notify(m):
        sent.append(m)
        return False

    w = _watch(tmp_path, conn, notify=_notify, confirm_empty=lambda p: True)
    monkeypatch.setattr(bowl, "fullness", lambda *a, **k: bowl.EMPTY)
    w.poll_once(); w.poll_once()                    # streak 2 -> tries to alert (fails)
    w.poll_once()                                   # still empty -> retries (not latched)
    assert len(sent) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_bowl_watch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mw.bowl_watch'`.

- [ ] **Step 3: Implement `mw/bowl_watch.py`**

Create `mw/bowl_watch.py`:

```python
"""BowlWatch: poll the bowl camera, judge full/empty, refill / observe.

Pipeline per poll: grab a frame -> skip if a cat is at the bowl (catfilter) ->
cv2 diff-vs-empty (mw.bowl) -> if it reads empty, CONFIRM with agy -> require 2
consecutive confirmed-empty (debounce) -> alert and/or auto-feed (rate-limited).
On a full->empty transition, log time-since-last-feed (consumption trend).

Every failure degrades toward no-action/alert, never a silent over-feed or
missed empty. Latches per empty episode, re-arms on refill, and latches only on
confirmed delivery (matching the other watchdogs).
"""
import subprocess
import sys
import time

from mw import bowl, store


def agy_bowl_empty(frame_path, timeout=240):
    """True if agy says the bowl is empty, False if it has food, None on error.
    One `agy --print` call (same backend as the labeler)."""
    prompt = (f"Look at the cat food bowl in this image: {frame_path}. "
              f"Is the bowl EMPTY (no food) or does it have FOOD in it? "
              f"Answer with one word: EMPTY or FOOD.")
    try:
        out = subprocess.run(["agy", "--print", prompt], capture_output=True,
                             text=True, timeout=timeout, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError) as e:
        print(f"[bowl/agy] {frame_path} failed ({e})", file=sys.stderr)
        return None
    t = out.stdout.lower()
    ie, ifd = t.find("empty"), t.find("food")
    if ie == -1 and ifd == -1:
        return None
    if ie == -1:
        return False
    if ifd == -1:
        return True
    return ie < ifd            # whichever word agy says first wins


class BowlWatch:
    def __init__(self, grab, catfilter, conn, notify, feeder=None,
                 confirm_empty=agy_bowl_empty, now_fn=time.time, *,
                 empty_ref, roi=bowl.DEFAULT_ROI, empty_max=5.0, full_min=20.0,
                 delta=22, poll_interval_s=1200, auto_feed=False,
                 auto_feed_portions=1, auto_feed_max_per_day=4):
        self.grab = grab
        self.catfilter = catfilter
        self.conn = conn
        self.notify = notify
        self.feeder = feeder
        self.confirm_empty = confirm_empty
        self.now = now_fn
        self.empty_ref = empty_ref
        self.roi = roi
        self.empty_max = empty_max
        self.full_min = full_min
        self.delta = delta
        self.poll_interval_s = poll_interval_s
        self.auto_feed = auto_feed
        self.auto_feed_portions = auto_feed_portions
        self.auto_feed_max_per_day = auto_feed_max_per_day
        self._prev_state = store.last_bowl_state(conn)   # resume across restarts
        self._empty_streak = 0
        self._empty_alerted = False

    def poll_once(self):
        path = self.grab()
        if not path:
            return                       # grab failed -> skip (no false empty)
        if not self.catfilter.is_clear(path):
            return                       # a cat is at the bowl -> don't judge
        state = bowl.fullness(path, self.empty_ref, self.roi, self.delta,
                              self.empty_max, self.full_min)
        if state is None:
            return                       # unreadable -> skip
        if state != bowl.EMPTY:
            self._empty_streak = 0
            self._empty_alerted = False  # has food -> re-arm
            self._prev_state = state
            return
        conf = self.confirm_empty(path)
        if conf is None:
            return                       # agy inconclusive -> skip
        if not conf:                     # agy: actually has food -> not empty
            self._empty_streak = 0
            self._empty_alerted = False
            self._prev_state = bowl.SOME
            return
        self._empty_streak += 1
        if self._empty_streak >= 2:      # debounce: 2 consecutive confirmed-empty
            self._on_empty()

    def _on_empty(self):
        now = self.now()
        if self._prev_state != bowl.EMPTY:          # transition: log consumption once
            lf = store.last_feed_event_ts(self.conn)
            secs = int(now - lf) if lf is not None else None
            store.log_bowl_event(self.conn, bowl.EMPTY, "vision",
                                 secs_since_feed=secs, ts=now)
            self._prev_state = bowl.EMPTY
        if self._empty_alerted:
            return                                  # one action per empty episode
        if self.auto_feed and self.feeder is not None:
            if store.auto_feeds_today(self.conn) >= self.auto_feed_max_per_day:
                if self.notify(f"🔔 Bowl empty — auto-feed daily limit "
                               f"({self.auto_feed_max_per_day}) reached; refill "
                               f"manually.") is not False:
                    self._empty_alerted = True
            elif self.feeder.feed(self.auto_feed_portions):
                store.log_bowl_event(self.conn, bowl.EMPTY, "auto_feed", ts=now)
                if self.notify(f"🍽️ Bowl was empty — auto-fed "
                               f"{self.auto_feed_portions} portion(s).") is not False:
                    self._empty_alerted = True
            else:
                self.notify("⚠️ Bowl empty + auto-feed FAILED (feeder unreachable?).")
        else:
            if self.notify("🔔 Bowl empty — /feed to refill?") is not False:
                self._empty_alerted = True

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:                  # never let the bowl thread die
                print(f"[bowl-watch] error: {e}", file=sys.stderr)
            time.sleep(self.poll_interval_s)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_bowl_watch.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/meowant
git add mw/bowl_watch.py tests/test_bowl_watch.py
git commit -m "feat(bowl): BowlWatch — cat-free gate + agy-confirm + debounce + refill/auto-feed"
```

---

### Task 4: digest bowl line + `/bowl` text helper

**Files:**
- Modify: `mw/report.py` (extend `digest`; add `bowl_status_text`)
- Test: `tests/test_report.py` (add)

**Interfaces:**
- Consumes: `store.last_bowl_state`, `store.last_consumption_secs`, `store.recent_bowl_events` (Task 1).
- Produces: `report.bowl_status_text(conn) -> str` — `/bowl` reply (current state + last consumption).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_report.py`:

```python
def test_digest_includes_bowl_when_data_present(tmp_path):
    from mw import store, report
    from datetime import datetime
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    now = datetime.now().timestamp()
    store.log_bowl_event(conn, "empty", "vision", secs_since_feed=7200, ts=now - 10)
    out = report.digest(conn)
    assert "bowl" in out.lower()


def test_digest_silent_on_bowl_when_no_data(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    out = report.digest(conn)
    assert "bowl" not in out.lower()        # no bowl data -> no bowl line


def test_bowl_status_text(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.log_bowl_event(conn, "empty", "vision", secs_since_feed=3600, ts=1_000_000.0)
    txt = report.bowl_status_text(conn)
    assert "empty" in txt.lower()


def test_bowl_status_text_no_data(tmp_path):
    from mw import store, report
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    assert "no bowl" in report.bowl_status_text(conn).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_report.py -k bowl -v`
Expected: FAIL — `AttributeError: ... 'bowl_status_text'` and the digest assertion.

- [ ] **Step 3: Extend `digest` and add `bowl_status_text`**

In `mw/report.py`, in `digest`, append a bowl suffix to BOTH return statements exactly as the feeds suffix is appended — change the no-elim branch's return to end with `+ _feeds_suffix(conn, today) + _bowl_suffix(conn)` and the main branch's return likewise (append `+ _bowl_suffix(conn)` after the existing `_feeds_suffix(conn, today)`).

Then add after `_feeds_suffix`:

```python
def _hours(secs):
    return f"{secs / 3600.0:.1f}h"


def _bowl_suffix(conn):
    state = store.last_bowl_state(conn)
    if not state:
        return ""
    secs = store.last_consumption_secs(conn)
    tail = f", emptied ~{_hours(secs)} after a feed" if secs is not None else ""
    return f" 🥣 bowl {state}{tail}."


def bowl_status_text(conn):
    """Reply for /bowl: current state + last consumption time."""
    state = store.last_bowl_state(conn)
    if not state:
        return "🥣 No bowl data yet (camera not calibrated/enabled)."
    secs = store.last_consumption_secs(conn)
    tail = f"; last emptied ~{_hours(secs)} after a feed" if secs is not None else ""
    return f"🥣 Bowl: {state}{tail}."
```

(`store` is already imported in `report.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/repos/meowant && python -m pytest tests/test_report.py -k bowl -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/meowant
git add mw/report.py tests/test_report.py
git commit -m "feat(bowl): digest bowl line + /bowl text helper"
```

---

### Task 5: meowantd wiring + `/bowl` command + config

**Files:**
- Modify: `meowantd.py` (construct `BowlWatch` thread; `/bowl` command)
- Test: `tests/test_meowantd_wiring.py` (add)

**Interfaces:**
- Consumes: `bowl_watch.BowlWatch` (Task 3); `report.bowl_status_text` (Task 4); the shared `catfilter` already built in meowantd; the Phase-1 `feeder_dev` (when present); `mw.capture.ffmpeg_grab`.
- Produces: a `BowlWatch` daemon thread when `bowl.enabled` + a `meowcam5` camera + an `empty_ref` are configured; a `/bowl` Telegram command.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_meowantd_wiring.py`:

```python
def test_bowl_watch_is_wired():
    import inspect, meowantd
    src = inspect.getsource(meowantd)
    assert "BowlWatch(" in src
    assert "bowl.enabled" in src
    assert "/bowl" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/meowant && python -m pytest tests/test_meowantd_wiring.py::test_bowl_watch_is_wired -v`
Expected: FAIL — assertion error (strings absent).

- [ ] **Step 3: Wire meowantd**

PLACEMENT (avoids a scope tangle): the bowl block goes **right after the Phase-1 feeder block** (currently ends ~line 201 where `feeder_monitor` is built) and **before** the `tg_token = config.get(...)` line (~line 203). Rationale: `BowlWatch` needs `catfilter` (defined at ~line 101 inside `if cams:`) AND `feeder_dev` (defined at ~line 190 inside the feeder block, which is AFTER the `if cams:` block). Both are in function scope at this point — `catfilter` is bound whenever `cams` is truthy, and requiring a `meowcam5` camera guarantees `cams` is truthy; `feeder_dev` is bound whenever `feeder_monitor is not None`.

First, ensure `feeder_dev` is always bound: at the top of the feeder block (the line `feeder_monitor = None` at ~187), also add `feeder_dev = None` so the reference below is always valid. Change:

```python
    feeder_monitor = None
```

to:

```python
    feeder_monitor = None
    feeder_dev = None
```

Then add the bowl block after the feeder block:

```python
    # Bowl camera (Phase 2): full/empty vision -> refill alert / auto-feed.
    # Dormant until bowl.enabled + a meowcam5 + a calibrated empty_ref on disk.
    cams = config.get(cfg, "cameras", [])
    m5 = next((c for c in cams if c["name"] == "meowcam5"), None)
    bowl_ref = config.get(cfg, "bowl.empty_ref_path", "")
    if config.get(cfg, "bowl.enabled", False) and m5 and bowl_ref and os.path.exists(bowl_ref):
        from mw.bowl_watch import BowlWatch
        from mw.bowl import DEFAULT_ROI
        from mw.capture import ffmpeg_grab
        os.makedirs("gallery/bowl", exist_ok=True)

        def _bowl_grab(url=m5["url"]):
            try:
                return ffmpeg_grab(url, "gallery/bowl/latest.jpg")
            except Exception:
                return None

        auto = config.get(cfg, "bowl.auto_feed", False)
        bw = BowlWatch(
            _bowl_grab, catfilter, conn,
            make_notify(lambda k: config.get(cfg, k)),
            feeder=(feeder_dev if auto and feeder_monitor is not None else None),
            empty_ref=bowl_ref,
            roi=tuple(config.get(cfg, "bowl.roi", list(DEFAULT_ROI))),
            empty_max=config.get(cfg, "bowl.empty_max", 5.0),
            full_min=config.get(cfg, "bowl.full_min", 20.0),
            poll_interval_s=config.get(cfg, "bowl.poll_interval_s", 1200),
            auto_feed=auto,
            auto_feed_portions=config.get(cfg, "bowl.auto_feed_portions", 1),
            auto_feed_max_per_day=config.get(cfg, "bowl.auto_feed_max_per_day", 4))
        threading.Thread(target=bw.run, daemon=True).start()
        print("bowl-watch: full/empty vision + refill/auto-feed")
```

(`catfilter` is referenced here; it is only reachable when `cams` is truthy, which the `m5` guard guarantees. `import os` is already at the top of `meowantd.py`.)

Then add `/bowl` to the Telegram command dict (near `/feedstatus`), unconditionally — it returns a friendly "no data" string when the bowl isn't active:

```python
            "/bowl": lambda: report.bowl_status_text(conn),
```

and append ` /bowl` to the `/start` help text.

- [ ] **Step 4: Run tests + full suite**

Run: `cd ~/repos/meowant && python -m pytest -q`
Expected: PASS — all prior tests plus the new wiring test.

- [ ] **Step 5: Commit**

```bash
cd ~/repos/meowant
git add meowantd.py tests/test_meowantd_wiring.py
git commit -m "feat(bowl): wire BowlWatch into meowantd + /bowl command (Phase 2 complete)"
```

---

## Self-Review

**1. Spec coverage** (against `2026-06-22-feeder-phase2-design.md`):
- meowcam5 + cat-free gate + hybrid diff/agy + debounce → Tasks 2-3 ✓
- empty → refill alert + configurable auto-feed (rate-limited) → Task 3 ✓
- consumption logging (time-to-empty) + digest trend → Tasks 1, 3, 4 ✓
- no not-eating alarm → not present ✓
- `/bowl` + wiring, dormant until enabled+calibrated → Task 5 ✓
- fail-loud latch on confirmed delivery → Task 3 ✓

**2. Placeholder scan:** No TBD/TODO. ROI/threshold defaults are placeholders explicitly flagged for build-time calibration (the design's stated approach), not plan placeholders — every code step is complete and runnable.

**3. Type consistency:** `bowl.fullness/changed_pct` signatures match Task 3 usage; `bowl.FULL/SOME/EMPTY` constants used consistently; `log_bowl_event(conn, state, source=, secs_since_feed=, ts=)` and `auto_feeds_today`/`last_bowl_state`/`last_consumption_secs` used identically across Tasks 1/3/4; the `notify(msg) is not False` latch idiom is uniform; `confirm_empty -> bool|None` handled (None=skip) in `poll_once`.

**Executor notes:**
- Task 5 placement is explicit: the bowl block goes after the Phase-1 feeder block and before the `tg_token` line, with `feeder_dev = None` added beside `feeder_monitor = None`. `feeder_dev` is referenced only when `feeder_monitor is not None`, and `catfilter` only when a `meowcam5` exists (⇒ `cams` truthy ⇒ `catfilter` bound). No `dir()`/`__import__` hacks.
- The bowl thread only starts when `bowl.enabled` AND `meowcam5` configured AND `empty_ref_path` exists on disk — so it ships dormant until you mount the camera and capture the reference.
- `bowl.fullness` interpreting diff-from-empty: a cat in frame would read "full" (huge diff), but the cat-free gate runs first, so that can't trigger a false full/empty.
