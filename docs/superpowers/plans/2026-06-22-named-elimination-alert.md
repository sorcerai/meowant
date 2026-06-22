# Named Elimination Alert (label-on-leave) Implementation Plan

> **For agentic workers:** Implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the instant, unnamed `🐈 A cat used the litter box` alert with a
slightly-delayed **named** alert — `🐈 Ucok used the box [HH:MM]`, or an honest
`🐈 A cat used the box (couldn't ID — likely in-box) [HH:MM]` when vision can't
resolve the cat (hooded-box occlusion).

**Architecture:** A poll-based notifier thread (same shape as `CaptureHealth`) finds
recently-closed eliminated visits that haven't been notified, runs the existing
auto-labeler on just that visit (so the name resolves in seconds, not the 15-min
sweep), reads the attributed cat, sends ONE named alert, and marks the visit notified.
Poll-based (not a CAT_LEAVE event handler) because dp102 can land AFTER CAT_LEAVE via
the 1800s grace window — a pure leave trigger would miss late eliminations. A
`visits.notified` column makes "already alerted" survive restarts.

**Tech Stack:** Python 3.10, sqlite3 (`store.py` conventions), pytest with `tmp_path`.

## Global Constraints

- The immediate `ELIMINATION` alert must be REMOVED from `mw/alerts.py` `_MESSAGES`
  (no duplicate unnamed ping). Other immediate alerts (BIN_FULL, CHUTE_FULL, FAULT)
  stay.
- Reuse the existing labeler — do NOT write new vision code. Add a public
  `AutoLabeler.label_visit(vid)` that wraps the existing `_process_visit` with the
  same untouched-rows filter `run_once` uses, and refactor `run_once` to call it.
- Idempotent + restart-safe: a visit is alerted at most once (the `notified` flag).
- No new dependencies. Match store.py style (`_lock`, list-of-dicts, comment-the-WHY).
- Notifier alerts via the injected `notify` callable (Telegram in prod).

---

### Task 1: store schema + queries for notification tracking

**Files:**
- Modify: `mw/store.py`
- Test: `tests/test_store.py` (append)

**Interfaces:**
- Produces:
  - migration adds `visits.notified INTEGER DEFAULT 0`
  - `pending_elimination_notifications(conn, before_iso) -> list[dict]` — eliminated,
    closed (`leave_ts IS NOT NULL`), `notified=0`, and `leave_ts <= before_iso` (settle
    window so captures are in). Oldest-first.
  - `mark_notified(conn, visit_id) -> None`

- [ ] **Step 1: Write failing tests**

```python
# append to tests/test_store.py
def test_pending_and_mark_notified(tmp_path):
    import time
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    now = time.time()
    # eliminated + closed + old enough -> pending
    v1 = store.open_visit(conn, now - 100); store.mark_elimination(conn, v1, 55)
    store.close_visit(conn, v1, now - 90, 10)
    # eliminated but too recent (inside settle) -> not pending under a tight `before`
    v2 = store.open_visit(conn, now - 5); store.mark_elimination(conn, v2, 60)
    store.close_visit(conn, v2, now - 4, 1)
    # not eliminated -> never pending
    v3 = store.open_visit(conn, now - 100); store.close_visit(conn, v3, now - 95, 5)

    before = store._iso(now - 30)
    pend = store.pending_elimination_notifications(conn, before)
    assert [p["id"] for p in pend] == [v1]      # only v1: elim, closed, settled

    store.mark_notified(conn, v1)
    assert store.pending_elimination_notifications(conn, before) == []   # v1 cleared
```

- [ ] **Step 2: Run, verify fail** — `pytest tests/test_store.py::test_pending_and_mark_notified -q` → AttributeError.

- [ ] **Step 3: Implement** — add to `_MIGRATIONS`: `("visits", "notified", "INTEGER DEFAULT 0")`. Then:

```python
def pending_elimination_notifications(conn, before_iso):
    """Eliminated, closed visits not yet alerted and old enough that their capture
    frames have settled (leave_ts <= before_iso). Oldest-first so alerts are ordered."""
    with _lock:
        cur = conn.execute(
            "SELECT * FROM visits WHERE eliminated=1 AND leave_ts IS NOT NULL "
            "AND COALESCE(notified,0)=0 AND leave_ts <= ? ORDER BY id", (before_iso,))
        return [dict(r) for r in cur.fetchall()]


def mark_notified(conn, visit_id):
    with _lock:
        conn.execute("UPDATE visits SET notified=1 WHERE id=?", (visit_id,))
        conn.commit()
```

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** (controller decides; default conservative — do not push).

---

### Task 2: public `AutoLabeler.label_visit(vid)`

**Files:**
- Modify: `mw/autolabel.py`
- Test: `tests/test_autolabel.py` (append; if absent, create with the same import style)

**Interfaces:**
- Produces: `AutoLabeler.label_visit(self, vid, dry_run=False) -> dict | None` — labels a
  single visit's untouched frames (the same filter `run_once` applies) and returns the
  `_process_visit` summary, or `None` if the visit has no untouched frames.

- [ ] **Step 1: Write failing test** — construct an AutoLabeler with a stub labeler that
  returns a fixed cat for the frames, insert a visit with 2 captures, assert
  `label_visit(vid)["cat"]` is the stub's cat and `store.get_visit(...)["cat_id"]` is set.

```python
# tests/test_autolabel.py (append or create)
from mw import store
from mw.autolabel import AutoLabeler

class _StubLabeler:
    def __init__(self, cat): self.cat = cat
    def predict_visit(self, paths, refs):
        return [{"cat": self.cat, "confidence": 0.99} for _ in paths]

def test_label_visit_attributes_single_visit(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    vid = store.open_visit(conn, 1000.0); store.mark_elimination(conn, vid, 55)
    store.insert_capture(conn, 1000.0, vid, "cam", "/g/a.jpg")
    store.insert_capture(conn, 1001.0, vid, "cam", "/g/b.jpg")
    al = AutoLabeler(conn, _StubLabeler("Ucok"), {}, ["Ucok", "Garfield", "Ella"])
    res = al.label_visit(vid)
    assert res["cat"] == "Ucok"
    assert store.cat_name_by_id(conn, store.get_visit(conn, vid)["cat_id"]) == "Ucok"
```

NOTE: confirm the `AutoLabeler.__init__` signature before writing the test — match the
real positional args (`conn, labeler, refs, valid_cats`, optional `catfilter=`). The stub
labeler only needs `predict_visit`. An eliminated visit bypasses the catfilter (see
`_process_visit`), so no catfilter stub is needed here.

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** — extract the per-vid body of `run_once` into `label_visit`:

```python
def label_visit(self, vid, dry_run=False):
    """Label ONE visit's still-untouched frames now (used by the elimination
    notifier for label-on-leave, so the cat name resolves in seconds rather than
    waiting for the next full sweep). Returns the summary, or None if nothing to do."""
    if not self.valid_cats:
        return None
    groups = store.captures_by_visit(self.conn, [vid])
    rows = [r for r in groups.get(vid, [])
            if r["label"] is None and r["label_source"] is None]
    if not rows:
        return None
    return self._process_visit(vid, rows, dry_run)
```

Then refactor `run_once` to use it:

```python
def run_once(self, dry_run=False):
    if not self.valid_cats:
        return []
    results = []
    for vid in store.unlabeled_visit_ids(self.conn):
        res = self.label_visit(vid, dry_run)
        if res is not None:
            results.append(res)
    return results
```

- [ ] **Step 4: Run, verify pass** (existing autolabel tests must stay green too).
- [ ] **Step 5: Commit.**

---

### Task 3: `EliminationNotifier` + wiring + drop the unnamed alert

**Files:**
- Create: `mw/elim_notify.py`
- Modify: `mw/alerts.py` (remove `ELIMINATION` from `_MESSAGES`), `meowantd.py` (wire),
  `tests/test_alerts.py` (the mapping test must change)
- Test: `tests/test_elim_notify.py` (create)

**Interfaces:**
- Consumes: `store.pending_elimination_notifications`, `store.mark_notified`,
  `store.get_visit`, `store.cat_name_by_id`, `AutoLabeler.label_visit`.
- Produces: `EliminationNotifier(conn, labeler, notify, now_fn=time.time,
  settle_s=15, interval=30)` with `run_once()` and `run()`.

- [ ] **Step 1: Write failing tests** (`tests/test_elim_notify.py`):

```python
from mw import store
from mw.elim_notify import EliminationNotifier


class _Labeler:                       # stand-in for AutoLabeler.label_visit
    def __init__(self, conn, cat=None):
        self.conn, self.cat = conn, cat
    def label_visit(self, vid, dry_run=False):
        if self.cat:
            store.set_visit_identity(self.conn, vid,
                                     store.cat_id_by_name(self.conn, self.cat), 1.0)
        return None


def _setup(tmp_path, cat=None):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    sent = []
    n = EliminationNotifier(conn, _Labeler(conn, cat), notify=sent.append,
                            now_fn=lambda: 10_000.0, settle_s=15)
    return conn, n, sent


def test_named_alert_when_identified(tmp_path):
    conn, n, sent = _setup(tmp_path, cat="Ucok")
    v = store.open_visit(conn, 9_000.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 9_900.0, 900)      # closed well before now-settle
    n.run_once()
    assert len(sent) == 1 and "Ucok" in sent[0] and "box" in sent[0].lower()
    assert store.get_visit(conn, v)["notified"] == 1


def test_anonymous_alert_when_unidentified(tmp_path):
    conn, n, sent = _setup(tmp_path, cat=None)     # labeler resolves no cat
    v = store.open_visit(conn, 9_000.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 9_900.0, 900)
    n.run_once()
    assert len(sent) == 1 and "couldn't ID" in sent[0]
    assert store.get_visit(conn, v)["notified"] == 1


def test_only_alerts_once(tmp_path):
    conn, n, sent = _setup(tmp_path, cat="Ucok")
    v = store.open_visit(conn, 9_000.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 9_900.0, 900)
    n.run_once(); n.run_once()
    assert len(sent) == 1                           # second pass is a no-op


def test_recent_visit_waits_for_settle(tmp_path):
    conn, n, sent = _setup(tmp_path, cat="Ucok")
    v = store.open_visit(conn, 9_990.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 9_995.0, 5)          # closed 5s before now < settle 15s
    n.run_once()
    assert sent == []                               # too fresh, not yet alerted
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement `mw/elim_notify.py`:**

```python
"""Named elimination alerts (label-on-leave).

Polls for recently-closed eliminated visits not yet alerted, labels each one NOW
(so the cat name resolves in seconds, not the 15-min sweep), and sends a single
named alert. Poll-based rather than a CAT_LEAVE handler because dp102 can arrive
after CAT_LEAVE via the grace window — a leave-only trigger would miss those."""
import sys
import time

from mw import store


class EliminationNotifier:
    def __init__(self, conn, labeler, notify, now_fn=time.time,
                 settle_s=15, interval=30):
        self.conn = conn
        self.labeler = labeler            # has .label_visit(vid)
        self.notify = notify
        self.now = now_fn
        self.settle_s = settle_s          # wait this long after close (frames settle)
        self.interval = interval

    def _alert_text(self, visit):
        cat = store.cat_name_by_id(self.conn, visit["cat_id"]) if visit["cat_id"] else None
        when = time.strftime("%H:%M", time.localtime(self.now()))
        if cat:
            return f"🐈 {cat} used the box [{when}]"
        return f"🐈 A cat used the box (couldn't ID — likely in-box) [{when}]"

    def run_once(self):
        before = store._iso(self.now() - self.settle_s)
        for v in store.pending_elimination_notifications(self.conn, before):
            try:
                self.labeler.label_visit(v["id"])      # resolve the cat now
            except Exception as e:
                print(f"[elim-notify] label {v['id']} failed: {e}", file=sys.stderr)
            fresh = store.get_visit(self.conn, v["id"]) or v   # re-read post-label cat_id
            self.notify(self._alert_text(fresh))
            store.mark_notified(self.conn, v["id"])

    def run(self):
        while True:
            try:
                self.run_once()
            except Exception as e:
                print(f"[elim-notify] error: {e}", file=sys.stderr)
            time.sleep(self.interval)
```

- [ ] **Step 4: Remove the unnamed immediate alert.** In `mw/alerts.py` delete the
  `ELIMINATION: ...` entry from `_MESSAGES` (and drop the now-unused `ELIMINATION`
  import if nothing else uses it). Update `tests/test_alerts.py`
  `test_alert_message_mapping`: change the ELIMINATION assertion to
  `assert alert_message(Event(ELIMINATION, 1.0, {})) is None` (it is no longer an
  immediate alert).

- [ ] **Step 5: Wire into `meowantd.py`.** The autolabeler is created inside the
  `if cams:` block as `autolabeler`. After it, start the notifier (it reuses that same
  labeler instance):

```python
from mw.elim_notify import EliminationNotifier
elim_notifier = EliminationNotifier(
    conn, autolabeler, notify=make_notify(lambda k: config.get(cfg, k)))
threading.Thread(target=elim_notifier.run, daemon=True).start()
print("elim-notifier: named 'who used the box' alerts (label-on-leave)")
```

- [ ] **Step 6: Run the full suite** — `pytest -q`, all green (including the changed
  alerts mapping test).
- [ ] **Step 7: Commit.**

## Self-Review

- Coverage: named alert ✔, anonymous fallback ✔, once-only ✔, settle window ✔,
  single-visit label ✔, schema/queries ✔, unnamed-alert removed ✔.
- No placeholders: full code + tests above.
- Types: `label_visit(vid)`, `pending_elimination_notifications(conn, before_iso)`,
  `EliminationNotifier(conn, labeler, notify, ...)` consistent across tasks.
