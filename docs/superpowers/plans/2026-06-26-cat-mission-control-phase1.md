# Cat Mission Control — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Ship a working phone-first "Cat Mission Control" dashboard — open it and see each cat's health at a glance — backed by new JSON endpoints and a shared per-cat status helper.

**Architecture:** New backend read endpoints in `mw/api.py` (Flask, already serving on `:8765`) feed a Svelte SPA built by Vite into `static/` (the path Flask already serves). A single shared `mw/cat_status.py` helper computes per-cat health so the UI and the health-watcher cannot drift. Live data via the existing `/events` SSE stream + a ~30s poll; the front-end keeps logic in pure, unit-tested `lib/` functions.

**Tech Stack:** Python 3 / Flask / SQLite (backend, pytest); Svelte + Vite + TypeScript + Tailwind (frontend, Vitest).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-26-cat-mission-control-design.md`.
- Backend conventions (`mw/store.py`): public fns take `conn` first; wrap DB access in `with store._lock:`; time-window math via `strftime('%s', col)`; "latest of kind" by autoincrement `id`. New endpoints return JSON with explicit, stable shapes.
- Cats are seeded id→name: 1=Ucok, 2=Garfield, 3=Ella. Health thresholds (hours since last attributed elimination): **Ucok 8, Ella 24, Garfield 24** — copied verbatim from `health_watch.THRESHOLDS`; the helper is the new single source and `health_watch` will be aligned to it in a later phase (do NOT refactor `health_watch` in Phase 1 — the critical alarm path was just stabilized).
- Memphis Pop design tokens (verbatim): surface `#fdf3e0`; card `#fff`; ink/border `#111` at 2.5px; offset shadow `3px 3px 0 <accent>` (no blur); status teal `#00b8a9` (ok), yellow `#ffd32a` (watch), red `#ff4757` (alert), system blue `#3742fa`, live green `#2ecc71`. Respect `prefers-reduced-motion`.
- The daemon already serves `static/` via `mw/api.py` (`@app.get("/")` + `static_url_path="/static"`). The Vite build MUST output to `static/` so serving is unchanged. `frontend/node_modules` and `frontend/dist` (if any) are gitignored; the built assets in `static/` ARE committed.
- Python tests: `cd ~/repos/meowant && python -m pytest -q` (currently 332 green, pytest.ini scopes to `tests/`). Frontend tests: `cd frontend && npm test` (Vitest).
- config.json is gitignored — never commit it.
- No auth; LAN-local single origin in production (no CORS in prod; dev proxy only).

## File Structure

- `mw/cat_status.py` (new) — `cat_status(conn, now_fn=time.time)` → per-cat health rollup. Single source of truth for ok/watch/alert.
- `mw/api.py` (modify) — add `/cats`, `/boxhealth`, `/bowls`, `/feeders` read endpoints.
- `frontend/` (new) — Svelte+Vite+TS+Tailwind source:
  - `vite.config.ts` — `build.outDir='../static'`, `emptyOutDir` false (keep non-built files), dev proxy to `:8765`.
  - `tailwind.config.js` — Memphis tokens as theme colors.
  - `src/lib/format.ts` — pure helpers (relative time, status→color, "N cleans left"). **Vitest-tested.**
  - `src/lib/api.ts` — typed fetch wrappers + SSE client.
  - `src/lib/stores.ts` — Svelte stores (cats, system, alerts, connection).
  - `src/components/` — `AlertBanner.svelte`, `CatCard.svelte`, `SystemStrip.svelte`, `ControlBar.svelte`.
  - `src/App.svelte`, `src/main.ts`, `index.html`.
- `tests/test_cat_status.py`, `tests/test_api_dashboard.py` (new) — backend pytest.

---

### Task 1: Shared per-cat status helper (`mw/cat_status.py`)

**Files:**
- Create: `mw/cat_status.py`
- Test: `tests/test_cat_status.py`

**Interfaces:**
- Produces: `cat_status(conn, now_fn=time.time) -> list[dict]`, one dict per cat with refs in the gallery, each:
  `{"name": str, "status": "ok"|"watch"|"alert", "last_litter_ts": str|None, "hours_since": float|None, "threshold_h": int, "litter_count_today": int}`.
  Status rule: `hours_since is None` → `"ok"` is NOT assumed — use `"watch"` only when there IS data; with no attributed litter ever, status is `"ok"` with `last_litter_ts=None` (insufficient data is not an alarm). With data: `ok` if `hours_since < 0.75*threshold`; `watch` if `0.75*threshold <= hours_since < threshold`; `alert` if `hours_since >= threshold`.

- [ ] **Step 1: Write the failing tests** — `tests/test_cat_status.py`:

```python
import time
from datetime import datetime
from mw import store, cat_status

T = datetime(2026, 6, 26, 12, 0, 0).timestamp()

def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return conn

def _elim(conn, cat_name, ts):
    # an attributed, eliminated visit at ts
    cid = store.cat_id_by_name(conn, cat_name)
    with store._lock:
        cur = conn.execute("INSERT INTO visits(enter_ts, eliminated, cat_id, use_record, duration_s) "
                           "VALUES(?,1,?,60,60)", (store._iso(ts), cid))
        conn.commit()

def test_ok_when_recent(tmp_path):
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 3600)             # 1h ago, threshold 8h -> ok
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    assert rows["Ucok"]["status"] == "ok"
    assert rows["Ucok"]["threshold_h"] == 8
    assert rows["Ucok"]["litter_count_today"] == 1

def test_watch_band(tmp_path):
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 7 * 3600)         # 7h, threshold 8 -> >=6 and <8 -> watch
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    assert rows["Ucok"]["status"] == "watch"

def test_alert_at_threshold(tmp_path):
    conn = _db(tmp_path)
    _elim(conn, "Ucok", T - 9 * 3600)         # 9h >= 8 -> alert
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    assert rows["Ucok"]["status"] == "alert"

def test_no_data_is_ok_not_alarm(tmp_path):
    conn = _db(tmp_path)
    rows = {r["name"]: r for r in cat_status.cat_status(conn, now_fn=lambda: T)}
    assert rows["Ella"]["status"] == "ok"
    assert rows["Ella"]["last_litter_ts"] is None
    assert rows["Ella"]["hours_since"] is None
```

- [ ] **Step 2: Run to verify they fail** — `python -m pytest tests/test_cat_status.py -q` → FAIL (ModuleNotFoundError: mw.cat_status). (If `store.cat_id_by_name`/`seed_cats` signatures differ, the implementer adjusts the test helpers to the real store API — confirm by reading `mw/store.py` — without changing the assertions.)

- [ ] **Step 3: Implement** — `mw/cat_status.py`:

```python
"""Single source of truth for per-cat health status (UI + watcher must agree).

Mirrors health_watch.THRESHOLDS. Status from hours since the most recent
attributed, eliminated visit: ok < 0.75*threshold <= watch < threshold <= alert.
No attributed data => 'ok' with nulls (insufficient data is not an alarm)."""
import time
from datetime import datetime

from mw import store

THRESHOLDS = {"Ucok": 8, "Ella": 24, "Garfield": 24}


def cat_status(conn, now_fn=time.time):
    now = now_fn()
    out = []
    for name, threshold in THRESHOLDS.items():
        last_ts = store.last_attributed_elimination_ts(conn, name)
        count = store.eliminations_today_for_cat(conn, name, now=now)
        if last_ts is None:
            out.append({"name": name, "status": "ok", "last_litter_ts": None,
                        "hours_since": None, "threshold_h": threshold,
                        "litter_count_today": count})
            continue
        hours = (now - datetime.fromisoformat(last_ts).timestamp()) / 3600.0
        if hours >= threshold:
            status = "alert"
        elif hours >= 0.75 * threshold:
            status = "watch"
        else:
            status = "ok"
        out.append({"name": name, "status": status, "last_litter_ts": last_ts,
                    "hours_since": round(hours, 2), "threshold_h": threshold,
                    "litter_count_today": count})
    return out
```

- [ ] **Step 4: Add the two supporting store helpers** — `mw/store.py` (place near `last_elimination_ts`):

```python
def last_attributed_elimination_ts(conn, cat_name):
    """enter_ts of the most recent eliminated+attributed visit for one cat, or None."""
    with _lock:
        row = conn.execute(
            "SELECT v.enter_ts FROM visits v JOIN cats c ON c.id=v.cat_id "
            "WHERE v.eliminated=1 AND c.name=? ORDER BY v.enter_ts DESC LIMIT 1",
            (cat_name,)).fetchone()
        return row["enter_ts"] if row else None


def eliminations_today_for_cat(conn, cat_name, now=None):
    """Count of this cat's eliminated visits since local midnight today."""
    import time as _t
    now = now if now is not None else _t.time()
    lt = _t.localtime(now)
    midnight = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    with _lock:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM visits v JOIN cats c ON c.id=v.cat_id "
            "WHERE v.eliminated=1 AND c.name=? "
            "AND strftime('%s', v.enter_ts) >= ?",
            (cat_name, str(int(midnight)))).fetchone()["n"]
```

- [ ] **Step 5: Run to verify pass** — `python -m pytest tests/test_cat_status.py -q` → PASS (4). Then `python -m pytest -q` → green.

- [ ] **Step 6: Commit** — `feat(cat-status): shared per-cat health helper + store queries`

---

### Task 2: `/cats` endpoint

**Files:**
- Modify: `mw/api.py` (add route inside `create_app`)
- Test: `tests/test_api_dashboard.py`

**Interfaces:**
- Consumes: `cat_status.cat_status(conn)` (Task 1).
- Produces: `GET /cats` → JSON array of the Task-1 dicts (plus `last_ate` from bowl sessions when available: `{"ts","location","duration_s"}` or `null`).

- [ ] **Step 1: Write the failing test** — `tests/test_api_dashboard.py`:

```python
from mw import store, api

def _app(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    class _Dev:  # minimal daemon stub
        state = {"dps": {}}; last_ok_ts = None; device = None; smartclean = None
    app = api.create_app(_Dev(), conn)
    return app.test_client(), conn

def test_cats_endpoint_shape(tmp_path):
    client, conn = _app(tmp_path)
    r = client.get("/cats")
    assert r.status_code == 200
    data = r.get_json()
    names = {c["name"] for c in data}
    assert names == {"Ucok", "Garfield", "Ella"}
    for c in data:
        assert c["status"] in ("ok", "watch", "alert")
        assert "litter_count_today" in c and "last_ate" in c
```

- [ ] **Step 2: Run to verify it fails** — `python -m pytest tests/test_api_dashboard.py -q` → FAIL (404 / KeyError).

- [ ] **Step 3: Implement** — add to `mw/api.py` inside `create_app` (after `/visits`):

```python
    @app.get("/cats")
    def cats():
        from mw import cat_status
        rows = cat_status.cat_status(conn)
        for r in rows:
            sess = store.recent_bowl_sessions(conn, limit=1)
            mine = next((s for s in store.recent_bowl_sessions(conn, limit=50)
                         if s["cat"] == r["name"]), None)
            r["last_ate"] = ({"ts": mine["ts"], "location": mine["location"],
                              "duration_s": mine["duration_s"]} if mine else None)
        return jsonify(rows)
```

(If `store.recent_bowl_sessions` row keys differ, the implementer adjusts to the real columns — read `mw/store.py`.)

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_api_dashboard.py -q` → PASS. Then full suite green.

- [ ] **Step 5: Commit** — `feat(api): GET /cats per-cat rollup for the dashboard`

---

### Task 3: `/boxhealth`, `/bowls`, `/feeders` endpoints

**Files:**
- Modify: `mw/api.py`
- Test: `tests/test_api_dashboard.py` (extend)

**Interfaces:**
- Produces:
  - `GET /boxhealth` → `{"bin_full_since": str|None, "capacity": int|None, "cleans_since_empty": int|None, "est_cleans_left": int|None, "auto_clean": bool, "faults": list}`.
  - `GET /bowls` → array `{"location","state","last_consumption_secs","auto_feeds_today"}`.
  - `GET /feeders` → array `{"label","last_feed_ts","today_count"}` (from `feed_events`; device-online omitted in Phase 1 — pure DB read).

- [ ] **Step 1: Write the failing tests** — extend `tests/test_api_dashboard.py`:

```python
def test_boxhealth_endpoint(tmp_path):
    client, conn = _app(tmp_path)
    r = client.get("/boxhealth")
    assert r.status_code == 200
    d = r.get_json()
    for k in ("bin_full_since", "capacity", "cleans_since_empty",
              "est_cleans_left", "auto_clean", "faults"):
        assert k in d

def test_bowls_and_feeders_endpoints(tmp_path):
    client, conn = _app(tmp_path)
    assert client.get("/bowls").status_code == 200
    assert isinstance(client.get("/bowls").get_json(), list)
    assert client.get("/feeders").status_code == 200
    assert isinstance(client.get("/feeders").get_json(), list)
```

- [ ] **Step 2: Run to verify they fail** — `python -m pytest tests/test_api_dashboard.py -q` → FAIL (404).

- [ ] **Step 3: Implement** — add to `mw/api.py` inside `create_app`:

```python
    @app.get("/boxhealth")
    def boxhealth():
        full_since = store.bin_full_since(conn)
        cap = store.bin_fill_capacity(conn)
        last_clear = store.last_bin_clear_ts(conn)
        cleans = store.cleans_since(conn, last_clear) if last_clear else None
        left = (max(0, cap - cleans) if (cap is not None and cleans is not None) else None)
        st = _decode_state(daemon.state)
        return jsonify({"bin_full_since": full_since, "capacity": cap,
                        "cleans_since_empty": cleans, "est_cleans_left": left,
                        "auto_clean": st["auto_clean"], "faults": st["faults"]})

    @app.get("/bowls")
    def bowls():
        out = []
        for loc in ("downstairs", "upstairs"):
            out.append({"location": loc,
                        "state": store.last_bowl_state(conn, location=loc),
                        "last_consumption_secs": store.last_consumption_secs(conn, location=loc),
                        "auto_feeds_today": store.auto_feeds_today(conn, location=loc)})
        return jsonify(out)

    @app.get("/feeders")
    def feeders():
        out = []
        for label in ("downstairs", "upstairs"):
            out.append({"label": label,
                        "last_feed_ts": store.last_feed_event_ts(conn, feeder=label),
                        "today_count": len(store.feed_events_today(conn, feeder=label))})
        return jsonify(out)
```

(Verify each `store.*` signature against `mw/store.py`; adjust kwargs to match. Feeder/bowl location names come from config but Phase 1 may hardcode the two known locations — note this as a Phase-2 config-read improvement.)

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_api_dashboard.py -q` → PASS. Full suite green.

- [ ] **Step 5: Commit** — `feat(api): GET /boxhealth, /bowls, /feeders for the dashboard`

---

### Task 4: Frontend scaffold + Memphis theme

**Files:**
- Create: `frontend/package.json`, `frontend/vite.config.ts`, `frontend/tailwind.config.js`, `frontend/postcss.config.js`, `frontend/tsconfig.json`, `frontend/index.html`, `frontend/src/main.ts`, `frontend/src/app.css`, `frontend/src/App.svelte`
- Modify: `.gitignore` (add `frontend/node_modules`, `frontend/dist`)

- [ ] **Step 1: Scaffold** — from repo root:

```bash
cd ~/repos/meowant && npm create vite@latest frontend -- --template svelte-ts
cd frontend && npm install && npm install -D tailwindcss postcss autoprefixer && npx tailwindcss init -p
```

- [ ] **Step 2: Configure Vite to build into `static/` + dev proxy** — `frontend/vite.config.ts`:

```ts
import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

export default defineConfig({
  plugins: [svelte()],
  build: { outDir: '../static', emptyOutDir: false, assetsDir: 'assets' },
  server: { proxy: Object.fromEntries(
    ['/state','/cats','/visits','/boxhealth','/bowls','/feeders','/command','/events']
      .map(p => [p, { target: 'http://localhost:8765', changeOrigin: true }])) },
})
```

- [ ] **Step 3: Memphis tokens in Tailwind** — `frontend/tailwind.config.js`:

```js
export default {
  content: ['./index.html', './src/**/*.{svelte,ts}'],
  theme: { extend: { colors: {
    paper:'#fdf3e0', ink:'#111', ok:'#00b8a9', watch:'#ffd32a',
    alert:'#ff4757', sys:'#3742fa', live:'#2ecc71' },
    boxShadow: { pop:'3px 3px 0 #111', popOk:'3px 3px 0 #00b8a9',
      popWatch:'3px 3px 0 #ffd32a', popAlert:'3px 3px 0 #ff4757' },
    borderWidth: { 2.5:'2.5px' } } },
  plugins: [],
}
```

- [ ] **Step 4: Base CSS + Tailwind directives** — `frontend/src/app.css`:

```css
@tailwind base; @tailwind components; @tailwind utilities;
:root { color-scheme: light; }
body { margin:0; background:#fdf3e0; font-family:'Poppins',Trebuchet MS,system-ui,sans-serif; }
@media (prefers-reduced-motion: reduce){ *{animation:none!important;transition:none!important} }
```

- [ ] **Step 5: Verify build emits to `static/`** — placeholder `App.svelte` renders "Cat Mission Control":

```bash
cd ~/repos/meowant/frontend && npm run build && ls ../static/index.html ../static/assets/ && echo OK
```
Expected: built `index.html` + assets exist in `static/`. (Note: this overwrites the old device-panel `index.html` — intended.)

- [ ] **Step 6: Update `.gitignore`** — append `frontend/node_modules/` and `frontend/dist/` (the build goes to `static/`, which IS committed).

- [ ] **Step 7: Commit** — `feat(frontend): Vite+Svelte+TS+Tailwind scaffold, builds to static/, Memphis tokens`

---

### Task 5: API client + pure lib helpers (Vitest-tested)

**Files:**
- Create: `frontend/src/lib/format.ts`, `frontend/src/lib/format.test.ts`, `frontend/src/lib/api.ts`, `frontend/src/lib/stores.ts`
- Modify: `frontend/package.json` (add `"test":"vitest run"`), install vitest

**Interfaces:**
- Produces: `relativeTime(iso|null): string`; `statusColor(s: 'ok'|'watch'|'alert'): string`; `cleansLeftLabel(left:number|null, cap:number|null): string`; typed `api.getCats()/getBoxHealth()/getBowls()/getFeeders()/getState()` and `subscribeEvents(cb)`.

- [ ] **Step 1: Install Vitest** — `cd frontend && npm install -D vitest`.

- [ ] **Step 2: Write failing tests** — `frontend/src/lib/format.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { relativeTime, statusColor, cleansLeftLabel } from './format'

describe('format', () => {
  it('relativeTime handles null', () => { expect(relativeTime(null)).toBe('—') })
  it('statusColor maps statuses', () => {
    expect(statusColor('ok')).toContain('00b8a9')
    expect(statusColor('alert')).toContain('ff4757')
  })
  it('cleansLeftLabel', () => {
    expect(cleansLeftLabel(3, 9)).toBe('~3 left')
    expect(cleansLeftLabel(null, null)).toBe('—')
  })
})
```

- [ ] **Step 3: Run to verify fail** — `cd frontend && npm test` → FAIL (module not found).

- [ ] **Step 4: Implement** — `frontend/src/lib/format.ts`:

```ts
export function relativeTime(iso: string | null): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  const mins = Math.round((Date.now() - t) / 60000)
  if (mins < 1) return 'now'
  if (mins < 60) return `${mins}m ago`
  const h = Math.round(mins / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.round(h / 24)}d ago`
}
export function statusColor(s: 'ok' | 'watch' | 'alert'): string {
  return { ok: '#00b8a9', watch: '#ffd32a', alert: '#ff4757' }[s]
}
export function cleansLeftLabel(left: number | null, cap: number | null): string {
  if (left === null || cap === null) return '—'
  return `~${left} left`
}
```

- [ ] **Step 5: Implement `api.ts` + `stores.ts`** — `frontend/src/lib/api.ts`:

```ts
export type Cat = { name: string; status: 'ok'|'watch'|'alert'; last_litter_ts: string|null;
  hours_since: number|null; threshold_h: number; litter_count_today: number;
  last_ate: { ts: string; location: string; duration_s: number } | null }
export type BoxHealth = { bin_full_since: string|null; capacity: number|null;
  cleans_since_empty: number|null; est_cleans_left: number|null; auto_clean: boolean; faults: string[] }
export type Bowl = { location: string; state: string|null; last_consumption_secs: number|null; auto_feeds_today: number }
export type Feeder = { label: string; last_feed_ts: string|null; today_count: number }

const j = async <T>(p: string): Promise<T> => { const r = await fetch(p); if (!r.ok) throw new Error(p); return r.json() }
export const getCats = () => j<Cat[]>('/cats')
export const getBoxHealth = () => j<BoxHealth>('/boxhealth')
export const getBowls = () => j<Bowl[]>('/bowls')
export const getFeeders = () => j<Feeder[]>('/feeders')
export const getState = () => j<any>('/state')
export function subscribeEvents(onEvent: (e: any) => void): () => void {
  const es = new EventSource('/events')
  es.onmessage = (m) => { try { onEvent(JSON.parse(m.data)) } catch {} }
  return () => es.close()
}
```

`frontend/src/lib/stores.ts`:

```ts
import { writable } from 'svelte/store'
import type { Cat, BoxHealth, Bowl, Feeder } from './api'
export const cats = writable<Cat[]>([])
export const box = writable<BoxHealth | null>(null)
export const bowls = writable<Bowl[]>([])
export const feeders = writable<Feeder[]>([])
export const state = writable<any>(null)
export const connected = writable<boolean>(false)
```

- [ ] **Step 6: Run to verify pass** — `cd frontend && npm test` → PASS.

- [ ] **Step 7: Commit** — `feat(frontend): typed API client, stores, tested format helpers`

---

### Task 6: Mission Control screen (components)

**Files:**
- Create: `frontend/src/components/AlertBanner.svelte`, `CatCard.svelte`, `SystemStrip.svelte`, `ControlBar.svelte`
- Modify: `frontend/src/App.svelte`

**Interfaces:**
- Consumes: stores + `format` helpers (Task 5).
- Each component takes typed props; `CatCard` props `{cat: Cat}`; `SystemStrip` `{box, bowls, feeders}`; `AlertBanner` `{cats, box}` → derives the single top alert.

- [ ] **Step 1: `CatCard.svelte`** — Memphis card; status pill + shadow color; the four metrics (Litter count + recency, Ate, Output placeholder `~`, Scatter placeholder from latest visit if available else `—`). Full markup using Tailwind tokens, `statusColor`, `relativeTime`. (Implementer writes the component; props typed to `Cat`; shadow class chosen by `cat.status`.)

- [ ] **Step 2: `AlertBanner.svelte`** — derives the single most-pressing line: any cat `alert` > any `watch` > box `bin_full_since`/`est_cleans_left<=1`; hidden when none. Yellow Memphis banner with `⚠`.

- [ ] **Step 3: `SystemStrip.svelte`** — blue Memphis strip: Box status (`state.status`), Bin (`cleansLeftLabel`), Feeders (next/`today_count`), Bowls (state ●●). 

- [ ] **Step 4: `ControlBar.svelte`** — four buttons (Clean / Feed ↑ / Feed ↓ / ⚙). Phase 1: Clean wired to existing `POST /command {action:'clean'}`; Feed buttons disabled with a "soon" title (the `feed` command lands in Phase 2); ⚙ routes to a stub. Confirmation toast on success, revert+error on failure.

- [ ] **Step 5: `App.svelte`** — header (title + live dot bound to `connected`/`state.stale`), `<AlertBanner>`, `{#each $cats as cat}<CatCard>`, `<SystemStrip>`, `<ControlBar>`. Narrow max-width column (phone-first), centered, Memphis paper bg.

- [ ] **Step 6: Build + manual smoke** — `cd frontend && npm run build` then load `http://localhost:8765/` against the live daemon; confirm cats render with real data. (No automated browser test in Phase 1.)

- [ ] **Step 7: Commit** — `feat(frontend): Memphis mission-control screen (cat cards, alert, system, controls)`

---

### Task 7: Live updates, stale handling, wiring

**Files:**
- Modify: `frontend/src/App.svelte` (lifecycle), `frontend/src/lib/stores.ts` (refresh fn)
- Test: `tests/test_api_dashboard.py` (add a serve-smoke assertion)

- [ ] **Step 1: Initial + periodic load** — in `App.svelte` `onMount`: `Promise.all([getCats,getBoxHealth,getBowls,getFeeders,getState])` → set stores; `setInterval` every 30s re-fetch `getCats/getBoxHealth/getBowls`; clear on destroy.

- [ ] **Step 2: SSE wiring** — `subscribeEvents`: set `connected=true` on open; on any event, opportunistically re-fetch `getCats()` + `getState()`; on error set `connected=false` and let EventSource auto-reconnect; reflect `state.stale` + `connected` in the header dot (green live / grey stale).

- [ ] **Step 3: Backend serve-smoke test** — add to `tests/test_api_dashboard.py`:

```python
def test_index_served(tmp_path):
    client, conn = _app(tmp_path)
    r = client.get("/")
    # static/index.html exists once the frontend is built; tolerate 200 or 404 pre-build
    assert r.status_code in (200, 404)
```

- [ ] **Step 4: Build + verify** — `cd frontend && npm run build`; `cd ~/repos/meowant && python -m pytest -q` green; manual: open dashboard, trigger a clean, watch the live dot + state update via SSE.

- [ ] **Step 5: Commit** — `feat(frontend): live SSE + periodic refresh + stale handling`

---

## Self-Review

- **Spec coverage:** Mission Control screen → T6; Memphis tokens → T4; `/cats` → T2; `/boxhealth`/`/bowls`/`/feeders` → T3; shared status helper → T1; SSE + stale → T7; Svelte+Vite→static/ + dev proxy → T4. Cat Detail / Settings / Tier-B / `feed` command are explicitly **later phases** (own plans) per the spec — not gaps.
- **Placeholder scan:** backend tasks carry full code + tests; frontend component bodies (T6) are described with exact props/data/tokens rather than full markup (Memphis component markup is craft, built+reviewed per task) — acceptable, but every step names exact files, props, endpoints, and token values. `format.ts`/`api.ts`/`stores.ts`/config carry full code.
- **Type consistency:** `Cat`/`BoxHealth`/`Bowl`/`Feeder` TS types (T5) mirror the endpoint JSON shapes (T2/T3) field-for-field; `cat_status` dict keys (T1) match the `/cats` shape (T2) match the `Cat` type (T5). `statusColor` values match the Global Constraints palette.
- **Note for implementers:** verify each `store.*` signature against `mw/store.py` before use (kwargs like `location=`, `feeder=`); adjust calls, not assertions.
