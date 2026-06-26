# Cat Mission Control — Phase 2 (Interactivity) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Checkbox (`- [ ]`) steps.

**Goal:** Make the dashboard a real control center — working **Feed** buttons and a **tap-a-cat detail view** (the two interactions whose absence read as "nothing's clickable"). Settings (⚙) stays Phase 3.

**Architecture:** Backend adds a `feed` action to `/command` (reaching the feeder devices, which the daemon already builds) and a `/cat/<name>` read endpoint (per-cat timeline + weekly + photos). Frontend enables the Feed buttons and adds a Cat Detail overlay opened by tapping a cat card. Same stack (Flask + Svelte/Vite/TS/Tailwind, Memphis tokens), built to `static/`.

**Tech Stack:** Python/Flask/SQLite (pytest); Svelte 5 + Vite + TS + Tailwind (Vitest/svelte-check).

## Global Constraints

- Branch `dashboard-phase2` (off main). Suite green at 353. Frontend: `cd frontend && npm run build` + `npx svelte-check --threshold error` (0 errors).
- store.py conventions: `conn` first, `with _lock:`, `strftime('%s', col)` time math.
- Memphis tokens (verbatim): paper `#fdf3e0`, ink/border `#111` 2.5px, hard offset shadow `3px 3px 0 <accent>`, ok `#00b8a9`, watch `#ffd32a`, alert `#ff4757`, sys `#3742fa`, uncertain badge `#efe2b3`/`#6b5d2f`.
- Frontend is Svelte 5; components use legacy `export let` props + `$:` reactivity (match existing files). Events use `onclick={...}`. The `state` store is imported `as sysState` to avoid the `$state` rune.
- Feeder facts: `meowantd` builds `feeder_devs = {label: FeederDevice}` and `feeder_monitors = {label: FeederMonitor}` (labels: "downstairs", "upstairs"). `FeederDevice.feed(portions)->bool`; `FeederMonitor.note_manual_feed()` marks an expected manual dispense so it logs as `source='manual'`. `create_app(daemon, conn, bus=None)` currently does NOT receive feeders — add a param.
- config.json gitignored.

---

### Task 1: `feed` command (backend)

**Files:** Modify `mw/api.py` (`create_app` signature + `/command`), `meowantd.py` (pass feeders to `create_app`). Test: `tests/test_api_feed.py`.

**Interfaces:**
- `create_app(daemon, conn, bus=None, feeders=None)` — `feeders` is `{label: FeederDevice}` (None → feed action returns 503-style error).
- New `/command` action `feed`: body `{action:"feed", feeder:"downstairs"|"upstairs", portions:int(1..10)}` → calls `feeders[feeder].feed(portions)`; on success returns `{ok:true}`, on unknown feeder `{ok:false,error}` 400, on device failure `{ok:false,error}` 500.

- [ ] **Step 1: Failing test** — `tests/test_api_feed.py`:

```python
from mw import store, api

class _Feeder:
    def __init__(self): self.fed = []
    def feed(self, n): self.fed.append(n); return True

class _Dev:
    state = {"dps": {}}; last_ok_ts = None; device = None; smartclean = None

def _client(tmp_path, feeders):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    return api.create_app(_Dev(), conn, feeders=feeders).test_client()

def test_feed_dispatches_to_named_feeder(tmp_path):
    f = _Feeder()
    c = _client(tmp_path, {"downstairs": f})
    r = c.post("/command", json={"action": "feed", "feeder": "downstairs", "portions": 2})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert f.fed == [2]

def test_feed_unknown_feeder_400(tmp_path):
    c = _client(tmp_path, {"downstairs": _Feeder()})
    r = c.post("/command", json={"action": "feed", "feeder": "nope", "portions": 1})
    assert r.status_code == 400 and r.get_json()["ok"] is False

def test_feed_clamps_and_validates_portions(tmp_path):
    f = _Feeder(); c = _client(tmp_path, {"downstairs": f})
    r = c.post("/command", json={"action": "feed", "feeder": "downstairs", "portions": 99})
    assert r.status_code == 200 and f.fed == [10]   # clamped to max 10
```

- [ ] **Step 2: Run → FAIL** (`python -m pytest tests/test_api_feed.py -q`).

- [ ] **Step 3: Implement** — `mw/api.py`: change signature to `def create_app(daemon, conn, bus=None, feeders=None):`. In `/command`, add before the `else`:

```python
        elif action == "feed":
            label = body.get("feeder")
            if not feeders or label not in feeders:
                return jsonify({"ok": False, "error": f"unknown feeder {label}"}), 400
            try:
                portions = max(1, min(10, int(body.get("portions", 1))))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "portions must be an integer"}), 400
            try:
                ok = feeders[label].feed(portions)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
            if not ok:
                return jsonify({"ok": False, "error": "feeder unreachable"}), 500
```

`meowantd.py:414`: `app = create_app(daemon, conn, bus=bus, feeders=feeder_devs)`.

- [ ] **Step 4: Run → PASS**, then full suite green.

- [ ] **Step 5: Commit** — `feat(api): /command feed action dispatches to a named feeder`

---

### Task 2: `/cat/<name>` detail endpoint (backend)

**Files:** Modify `mw/api.py`; Test: `tests/test_api_cat_detail.py`.

**Interfaces:** `GET /cat/<name>` → `{name, status:<from cat_status>, timeline:[{kind:"litter"|"ate", ts, ...}], weekly:<per-cat facts or null>, photos:[paths]}`. 404 if name not a known cat.

- [ ] **Step 1: Failing test** — `tests/test_api_cat_detail.py`:

```python
from mw import store, api

class _Dev:
    state = {"dps": {}}; last_ok_ts = None; device = None; smartclean = None

def _client(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return api.create_app(_Dev(), conn).test_client(), conn

def test_cat_detail_known_cat(tmp_path):
    c, conn = _client(tmp_path)
    r = c.get("/cat/Ucok")
    assert r.status_code == 200
    d = r.get_json()
    assert d["name"] == "Ucok"
    assert "timeline" in d and "weekly" in d and "photos" in d
    assert isinstance(d["timeline"], list)

def test_cat_detail_unknown_404(tmp_path):
    c, conn = _client(tmp_path)
    assert c.get("/cat/Nobody").status_code == 404
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — add to `mw/api.py` inside `create_app`:

```python
    @app.get("/cat/<name>")
    def cat_detail(name):
        from mw import cat_status as _cs
        rows = {r["name"]: r for r in _cs.cat_status(conn)}
        if name not in rows:
            return jsonify({"error": f"unknown cat {name}"}), 404
        # timeline: this cat's recent litter visits + eating sessions, newest-first
        visits = [v for v in store.recent_visits(conn, 60)
                  if v.get("cat") == name or v.get("cat_id") == store.cat_id_by_name(conn, name)]
        litter = [{"kind": "litter", "ts": v["enter_ts"], "duration_s": v.get("duration_s"),
                   "eliminated": bool(v.get("eliminated")), "confidence": v.get("confidence")}
                  for v in visits][:20]
        ate = [{"kind": "ate", "ts": s["ts"], "location": s["location"], "duration_s": s["duration_s"]}
               for s in store.recent_bowl_sessions(conn, 60) if s["cat"] == name][:20]
        timeline = sorted(litter + ate, key=lambda x: x["ts"], reverse=True)[:30]
        import json as _json, os as _os, glob as _glob
        rep = store.latest_weekly_report(conn)
        weekly = (_json.loads(rep["facts_json"]).get("per_cat", {}).get(name)
                  if rep else None)
        photos = sorted(_glob.glob(f"gallery/{name.lower()}/*.jp*"))[:6]
        return jsonify({**rows[name], "timeline": timeline, "weekly": weekly, "photos": photos})
```

(Verify `recent_visits` row keys for `cat`/`cat_id` and `store.cat_id_by_name` signature against `mw/store.py`; adjust the filter to the real keys, not the assertions.)

- [ ] **Step 4: Run → PASS**, full suite green.

- [ ] **Step 5: Commit** — `feat(api): GET /cat/<name> detail (timeline + weekly + photos)`

---

### Task 3: Wire the Feed buttons (frontend)

**Files:** Modify `frontend/src/components/ControlBar.svelte`, `frontend/src/lib/api.ts`.

- [ ] **Step 1:** add to `api.ts`: `export const feed = (feeder: string, portions = 1) => fetch('/command', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'feed', feeder, portions})}).then(r => r.json())`.

- [ ] **Step 2:** In `ControlBar.svelte`: remove `disabled` from the two Feed buttons; `Feed ↑` → `feed('upstairs')`, `Feed ↓` → `feed('downstairs')` (↑ = upstairs, ↓ = downstairs); reuse the existing `showToast` for success/error; disable briefly while in-flight (mirror `handleClean`). Keep Memphis styling.

- [ ] **Step 3:** Verify `npm run build` + `npx svelte-check --threshold error` → 0 errors. Manual: click Feed, confirm a `POST /command {action:'feed'}` fires + toast.

- [ ] **Step 4: Commit** — `feat(frontend): wire Feed up/down buttons to the feed command`

---

### Task 4: Cat Detail overlay (frontend)

**Files:** Create `frontend/src/components/CatDetail.svelte`; Modify `frontend/src/App.svelte`, `frontend/src/lib/api.ts`, `frontend/src/components/CatCard.svelte`.

- [ ] **Step 1:** `api.ts`: `export type CatDetailT = Cat & {timeline:{kind:string;ts:string;duration_s?:number;location?:string;eliminated?:boolean}[]; weekly:any; photos:string[]}` and `export const getCatDetail = (name:string) => j<CatDetailT>('/cat/' + encodeURIComponent(name))`.

- [ ] **Step 2:** `CatDetail.svelte` — a Memphis modal/overlay (fixed inset, paper card, thick border) with a close button. Props `{name:string, onClose:()=>void}`. `onMount` → `getCatDetail(name)`. Renders: header (name + status/uncertain badge), a **timeline** list (litter/ate rows with relative time + duration), a **weekly** summary (if present), and **photos** (`<img src="/static/{path}">` — note: gallery paths are served under `/static`; if `photos` are repo-relative like `gallery/x.jpg`, prefix `/static/` is wrong — instead add a tiny Flask route OR serve via the existing static mount; for Phase 2 render the count + filenames if direct image serving isn't wired, and leave a TODO). Empty/loading/error states.

- [ ] **Step 3:** `CatCard.svelte`: the existing `onclick` stub → call a prop `onOpen()` (add `export let onOpen: () => void = () => {}`), wire `onclick={onOpen}`.

- [ ] **Step 4:** `App.svelte`: track `let selected: string | null = null`; pass `onOpen={() => selected = cat.name}` to each `<CatCard>`; render `{#if selected}<CatDetail name={selected} onClose={() => selected = null} />{/if}`.

- [ ] **Step 5:** `npm run build` + svelte-check 0 errors. Manual: tap a cat → detail opens with its timeline; close works.

- [ ] **Step 6: Commit** — `feat(frontend): tap-a-cat Cat Detail overlay (timeline + weekly + photos)`

---

## Self-Review
- Coverage: Feed control → T1+T3; Cat detail → T2+T4. Settings deferred to Phase 3 (per spec). 
- Photos serving: flagged in T4 Step 2 as a known gap (gallery images aren't under the built `static/` tree) — render metadata + TODO rather than broken `<img>`; a `/photo/<path>` route is a small Phase-3 follow-up.
- Types: `feed`/`getCatDetail` in api.ts; `CatDetailT extends Cat`; `onOpen` prop threaded CatCard→App.
- Constraints echoed: Svelte 5 `onclick`, `state as sysState`, Memphis tokens, store conventions, `feeders` param added to `create_app`.
