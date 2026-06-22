# Session-Merge Read-Model Implementation Plan

> **For agentic workers:** Implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a non-destructive `store.sessions()` read-model that collapses IR-flicker
visit fragments into one logical session, while keeping deliberate re-entries (gaming
blips) and genuinely separate trips as distinct sessions. Raw `visits` rows are never
mutated.

**Architecture:** Pure read-time grouping over the immutable `visits` table. Walk visits
oldest→newest; absorb an adjacent fragment into the running session only when an
elimination *anchor* (dp102) justifies it. No new event types, no bus changes, no schema
change. This is the council verdict: gap size alone cannot separate flicker from gaming
(measured gap distribution is unimodal ~6s), so the merge is anchored on **dp102 +
duration**, not gap, and runs at read-time where the async vision `cat_id` is available.

**Tech Stack:** Python 3.10, sqlite3 (existing `store.py` conventions — module-global
`_lock`, `sqlite3.Row`), pytest with `tmp_path`.

## Global Constraints

- Behavior-preserving for everything else: do NOT alter raw `visits`/`captures`/`events`
  rows or any existing `store.py` function. `sessions()` is read-only.
- Match `store.py` style: terse, comment-the-WHY, acquire `_lock` around the single SELECT,
  return list-of-dicts (like `recent_visits`).
- No new dependencies. Use `datetime.fromisoformat`.
- Timestamps in the DB are local-ISO strings with no tz (see `store._iso`). One legacy row
  may carry a tz offset — parse defensively (`.replace(tzinfo=None)` after parse).
- The merge must NEVER combine two eliminating fragments (two real pees = two trips) and
  must NEVER combine two non-eliminating fragments (preserves gaming blips as distinct).

---

### Task 1: `store.sessions()` read-model + tests

**Files:**
- Modify: `mw/store.py` (add `sessions()` near `recent_visits`, ~line 105)
- Test: `tests/test_sessions.py` (create)

**Interfaces:**
- Consumes: existing `store.connect`, `store.init_db`, `store.open_visit`,
  `store.close_visit`, `store.mark_elimination`, `store.set_visit_identity`,
  `store.seed_cats`, `store.cat_id_by_name`.
- Produces: `sessions(conn, gap_s=30) -> list[dict]`, newest-first, each dict:
  ```
  {
    "visit_ids": [int, ...],   # raw rows folded into this session, in time order
    "enter_ts": str,           # first fragment's enter_ts (iso)
    "leave_ts": str | None,    # last fragment's leave_ts (None if the session is still open)
    "duration_s": int,         # wall-clock span: last_leave - first_enter (0 if open)
    "cat_id": int | None,      # first resolved cat_id among fragments
    "cat": str | None,         # cat name for cat_id (None if unresolved)
    "eliminated": int,         # 1 if any fragment eliminated, else 0
    "use_record": int | None,  # first non-null use_record among fragments
    "scatter_severity": int | None,  # max severity across fragments (None if none scored)
    "scatter_pct": float | None,     # the pct that goes with the max severity
    "n_fragments": int,        # len(visit_ids)
  }
  ```

**Merge rule (the heart — implement exactly):** walking oldest→newest, fragment `v` is
absorbed into the running session `s` iff ALL hold:
1. `s["leave_ts"]` is not None and `v["enter_ts"]` is not None (cannot merge across an open span).
2. gap `= (parse(v.enter_ts) - parse(s.leave_ts)).total_seconds()`, clamped `max(0, gap)`, and `gap < gap_s`.
3. cat-compatible: `s.cat_id == v.cat_id` OR `s.cat_id is None` OR `v.cat_id is None`
   (never merge across a resolved cat conflict).
4. elimination XOR: `bool(s["eliminated"]) != bool(v["eliminated"])` — exactly one side
   carries the dp102 anchor. (Two eliminations → separate trips. Zero eliminations →
   separate gaming blips.)

When absorbed: append `v.id` to `visit_ids`; set `leave_ts = v.leave_ts`; recompute
`duration_s` as the span from the first fragment's enter to the new leave; OR-in
`eliminated`; fill `cat_id`/`use_record` if still None; track max `scatter_severity` (and
its `scatter_pct`). Otherwise start a new session from `v`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sessions.py
from mw import store


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield"])
    return conn


def _visit(conn, enter, dur, *, cat=None, elim=False, use_record=None,
           conn_cats=None):
    """Open+close a visit; optionally attribute a cat and mark elimination."""
    vid = store.open_visit(conn, enter)
    store.close_visit(conn, vid, enter + dur, dur)
    if elim:
        store.mark_elimination(conn, vid, use_record)
    if cat is not None:
        store.set_visit_identity(conn, vid, store.cat_id_by_name(conn, cat), 1.0)
    return vid


def test_flicker_tail_collapses_into_pee(tmp_path):
    # Ucok: a 70s real pee (dp102) + a 2s flicker tail 4s later -> ONE session
    conn = _db(tmp_path)
    v1 = _visit(conn, 1000.0, 70, cat="Ucok", elim=True, use_record=55)
    v2 = _visit(conn, 1074.0, 2)              # 1070 leave -> 1074 enter = 4s gap, no elim
    sess = store.sessions(conn, gap_s=30)
    assert len(sess) == 1
    s = sess[0]
    assert s["visit_ids"] == [v1, v2]
    assert s["eliminated"] == 1
    assert s["cat"] == "Ucok"
    assert s["n_fragments"] == 2
    assert s["duration_s"] == 76            # 1076 - 1000


def test_gaming_blips_stay_separate(tmp_path):
    # Garfield: two 2s blips, no elimination on either -> stay TWO sessions
    conn = _db(tmp_path)
    _visit(conn, 1000.0, 2, cat="Garfield")
    _visit(conn, 1007.0, 2, cat="Garfield")   # 5s gap, both elim=0
    sess = store.sessions(conn, gap_s=30)
    assert len(sess) == 2


def test_two_real_pees_stay_separate(tmp_path):
    # Two eliminating visits close in time are two trips, never merged
    conn = _db(tmp_path)
    _visit(conn, 1000.0, 60, cat="Ucok", elim=True)
    _visit(conn, 1065.0, 60, cat="Ucok", elim=True)   # 5s gap, both elim=1
    sess = store.sessions(conn, gap_s=30)
    assert len(sess) == 2


def test_gap_beyond_window_not_merged(tmp_path):
    # A non-elim fragment far after the pee (> gap_s) is its own session
    conn = _db(tmp_path)
    _visit(conn, 1000.0, 60, cat="Ucok", elim=True)
    _visit(conn, 1100.0, 5)                    # 40s gap > 30 -> separate
    sess = store.sessions(conn, gap_s=30)
    assert len(sess) == 2


def test_resolved_cat_conflict_not_merged(tmp_path):
    # Pee is Ucok, the adjacent fragment resolved to Garfield -> do not merge
    conn = _db(tmp_path)
    _visit(conn, 1000.0, 60, cat="Ucok", elim=True)
    _visit(conn, 1065.0, 3, cat="Garfield")    # 5s gap, but a different resolved cat
    sess = store.sessions(conn, gap_s=30)
    assert len(sess) == 2


def test_unresolved_tail_merges(tmp_path):
    # The flicker tail has no cat_id yet (vision pending) -> still merges into the pee
    conn = _db(tmp_path)
    v1 = _visit(conn, 1000.0, 60, cat="Ucok", elim=True)
    v2 = _visit(conn, 1065.0, 2)               # cat_id NULL
    sess = store.sessions(conn, gap_s=30)
    assert len(sess) == 1 and sess[0]["visit_ids"] == [v1, v2]


def test_newest_first_order(tmp_path):
    conn = _db(tmp_path)
    _visit(conn, 1000.0, 60, cat="Ucok", elim=True)
    _visit(conn, 5000.0, 60, cat="Garfield", elim=True)
    sess = store.sessions(conn, gap_s=30)
    assert sess[0]["enter_ts"] > sess[1]["enter_ts"]   # newest first
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd ~/repos/meowant && python3 -m pytest tests/test_sessions.py -q`
Expected: FAIL — `AttributeError: module 'mw.store' has no attribute 'sessions'`

- [ ] **Step 3: Implement `store.sessions()`**

Add near `recent_visits` in `mw/store.py`. Reuse the module-global `_lock` only for the
SELECT; the folding is pure Python on the fetched rows.

```python
def _parse_ts(s):
    # DB stamps are naive local-ISO (see _iso); one legacy row may carry a tz — drop it.
    return datetime.fromisoformat(s).replace(tzinfo=None)


def sessions(conn, gap_s=30):
    """Collapse IR-flicker visit fragments into logical sessions WITHOUT mutating any
    row. Two fragments merge only when a single dp102 elimination anchors them (XOR on
    `eliminated`) and they are close in time (< gap_s) and cat-compatible. This keeps a
    senior cat's weight-shift flicker as one trip, while a gaming cat's no-elimination
    blips and two genuinely separate pees stay distinct. Read-time so the async vision
    cat_id is available. Newest-first, like recent_visits."""
    with _lock:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM visits ORDER BY enter_ts ASC, id ASC").fetchall()]

    out = []          # sessions, oldest-first while folding
    for v in rows:
        s = out[-1] if out else None
        if s is not None and _can_merge(s, v, gap_s):
            _absorb(s, v)
        else:
            out.append(_new_session(conn, v))
    out.reverse()     # newest-first
    return out


def _can_merge(s, v, gap_s):
    if s["leave_ts"] is None or v["enter_ts"] is None:
        return False
    gap = max(0.0, (_parse_ts(v["enter_ts"]) - _parse_ts(s["leave_ts"])).total_seconds())
    if gap >= gap_s:
        return False
    sc, vc = s["cat_id"], v["cat_id"]
    if sc is not None and vc is not None and sc != vc:
        return False
    return bool(s["eliminated"]) != bool(v["eliminated"])   # exactly one dp102 anchor


def _new_session(conn, v):
    return {
        "visit_ids": [v["id"]],
        "enter_ts": v["enter_ts"],
        "leave_ts": v["leave_ts"],
        "duration_s": v["duration_s"] or 0,
        "cat_id": v["cat_id"],
        "cat": cat_name_by_id(conn, v["cat_id"]) if v["cat_id"] else None,
        "eliminated": int(bool(v["eliminated"])),
        "use_record": v["use_record"],
        "scatter_severity": v["scatter_severity"],
        "scatter_pct": v["scatter_pct"],
        "n_fragments": 1,
    }


def _absorb(s, v):
    s["visit_ids"].append(v["id"])
    s["leave_ts"] = v["leave_ts"]
    if s["leave_ts"] is not None:
        s["duration_s"] = int(
            (_parse_ts(s["leave_ts"]) - _parse_ts(s["enter_ts"])).total_seconds())
    s["eliminated"] = int(s["eliminated"] or bool(v["eliminated"]))
    if s["cat_id"] is None and v["cat_id"] is not None:
        s["cat_id"] = v["cat_id"]
        s["cat"] = None    # name is backfilled by the caller path that has conn; see note
    if s["use_record"] is None:
        s["use_record"] = v["use_record"]
    vs = v["scatter_severity"]
    if vs is not None and (s["scatter_severity"] is None or vs > s["scatter_severity"]):
        s["scatter_severity"] = vs
        s["scatter_pct"] = v["scatter_pct"]
    s["n_fragments"] = len(s["visit_ids"])
```

NOTE on `cat` name after absorb: simplest correct fix is to resolve the name in
`sessions()` AFTER folding (the `_absorb` path can't easily call `cat_name_by_id` without
`conn`). Replace the `_absorb` cat block with just setting `cat_id`, then in `sessions()`
before `out.reverse()` do a final pass: `for s in out: s["cat"] = cat_name_by_id(conn,
s["cat_id"]) if s["cat_id"] else None`. Implement it whichever way keeps `_absorb`
conn-free; the test only asserts the final `cat` string.

- [ ] **Step 4: Run tests, verify they pass**

Run: `cd ~/repos/meowant && python3 -m pytest tests/test_sessions.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Full suite stays green**

Run: `cd ~/repos/meowant && python3 -m pytest -q`
Expected: all prior tests + 7 new pass, 0 failures.

- [ ] **Step 6: Commit** (only if the controller authorizes — default conservative: report, do not push)

```bash
git add mw/store.py tests/test_sessions.py docs/superpowers/plans/2026-06-22-session-merge-readmodel.md
git commit -m "feat: session-merge read-model (collapse IR-flicker fragments, dp102-anchored)"
```

## Self-Review

- Spec coverage: merge rule (gap+XOR+cat) ✔ tested by flicker/gaming/two-pees/gap/conflict/unresolved.
- No placeholders: full code + full tests above.
- Type consistency: `sessions(conn, gap_s=30)`; dict keys match the Interfaces block.
