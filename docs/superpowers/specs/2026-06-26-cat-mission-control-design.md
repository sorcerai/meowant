# Cat Mission Control — Design Spec

**Date:** 2026-06-26
**Status:** Draft for review

## Goal

A phone-first, cat-centric "mission control" dashboard for the meowant system: open it and instantly know **which cat is healthy and which needs attention**, with the box / feeders / bowls as supporting context, and full control over the system. Replaces the existing minimal device-only panel (`static/index.html`).

North star (inherited): never silently miss — or falsely cry wolf about — a sick-cat signal. The dashboard makes the signals the daemon already computes legible at a glance.

## Users & form factor

- **Primary:** the owner, on a **phone** (same device that receives Telegram alerts — tap alert → open dashboard → check the cat). Responsive up to desktop, but designed thumb-first / single narrow column.
- Single-user, LAN-local (daemon on `:8765`); no auth in scope (same trust boundary as the current panel).

## Aesthetic — "Memphis Pop" (approved)

90s-playful, high-legibility. Design tokens (centralized, Tailwind theme + CSS vars):
- **Surface:** cream paper `#fdf3e0`; cards white `#fff`.
- **Ink:** near-black `#111` for text + borders (thick **2.5px** borders are the signature).
- **Offset shadow:** `3px 3px 0 <accent>` (hard, no blur) — the core "pop" motif.
- **Accents / status:** teal `#00b8a9` = OK/healthy; yellow `#ffd32a` = watch; red `#ff4757` = alert/attention; blue `#3742fa` = system/neutral; green `#2ecc71` = live/positive trend.
- **Type:** bold geometric sans (Trebuchet-like / a Google font e.g. *Poppins*/*Fredoka* for headers), heavy weights for names + numbers.
- Sparse confetti shapes (dot, triangle, squiggle) as accents — restrained, not noisy.
- Motion: subtle (live-dot pulse, gentle card entrance, status-change flash). Respect `prefers-reduced-motion`.

## Architecture

```
frontend/                     # Svelte + Vite + TypeScript + Tailwind (SOURCE)
  src/
    lib/
      theme.ts                # Memphis tokens (shared)
      api.ts                  # typed fetch wrappers + SSE client
      stores.ts               # Svelte stores: state, cats, system, alerts
    components/               # CatCard, AlertBanner, SystemStrip, ControlBar, ...
    routes/                   # MissionControl, CatDetail, Settings (client-side routing)
    App.svelte
  vite.config.ts              # build.outDir = ../static ; dev proxy -> :8765
static/                       # BUILD OUTPUT (Flask serves this, unchanged path)
mw/api.py                     # Flask: existing + new read/write endpoints
```

- **Build:** `npm run build` (in `frontend/`) emits to `static/`. The existing `mw/api.py` `@app.get("/")` + `static_url_path="/static"` serve it with **no Flask changes to the serving path**. Production = single origin (`:8765`), no CORS needed.
- **Dev:** `npm run dev` (Vite dev server) proxies `/state`, `/cats`, `/events`, `/command`, etc. to `http://localhost:8765`. Permissive CORS added **only** for the dev origin, gated behind a config flag (off in production).
- **No change to how the daemon launches** — it already serves `static/`. The build artifact is committed or built at deploy; `frontend/node_modules` is gitignored.

`★ Why a framework here:` the app has three screens, live state, client routing, and will keep growing (settings, history, photos). Svelte compiles to a tiny bundle, scoped styles keep the Memphis tokens disciplined, and TypeScript types the API contract end-to-end. The cost — a `node_modules` + build step in an otherwise zero-toolchain repo — is accepted deliberately.

## Screens

### 1. Mission Control (main — approved layout)
- **Header:** "MEOWANT / MISSION CONTROL", live dot (green = fresh, grey = stale via `/state.stale`), connection state.
- **Alert banner:** the single most-pressing signal, when present (e.g. "⚠ GARFIELD — 9h since litter"); hidden when all-clear. Sources: per-cat no-go/watch, attribution-degraded notice, box UNUSABLE, bin full, feeder missed, stream down.
- **Cat cards (one per cat):** name + status pill (OK/watch/alert, color-coded; shadow color encodes status). Four metrics on the face: **Litter** (count today + recency), **Ate** (time + duration + which bowl), **Output** (weight/volume trend — the UTI/blockage early-warning), **Scatter**. Tap → Cat Detail.
- **System strip:** Box (status), Bin (% + "~N cleans left" from learned capacity), Feeders (next drop / hopper), Bowls (fullness ●●). Tap → relevant detail/settings.
- **Control bar:** Clean now · Feed ↑ · Feed ↓ · Settings.

### 2. Cat Detail (tap a cat)
- Header with status + the cat's thresholds.
- **Timeline:** recent visits (litter) + bowl sessions (eating), newest first, with elimination/output/scatter per visit.
- **Weekly trend:** from `weekly_reports.facts_json` per-cat (frequency, output, attribution %), with the gatekeeper severity (nominal/watch/drift).
- **Photos:** recent confident captures + gallery refs for that cat (from `gallery/<cat>/` + `captures`).

### 3. Settings (full control center)
Two tiers by implementation cost:
- **Tier A — device commands (already have command paths):** Clean now, Auto-clean toggle, Clean delay, Sleep, Quiet hours, Feed (per feeder/bowl). Wired through `/command`.
- **Tier B — persisted config (new infra needed):** Feeder mealtimes/portions and per-cat health thresholds currently live in `config.json` / hardcoded `health_watch.THRESHOLDS`. Making them UI-editable requires a **config-write endpoint + persistence + daemon reload signal**, and moving thresholds into `config`/DB. This is flagged as its own work item and will be **phased after** the monitoring + Tier-A build.

## API (in `mw/api.py`)

**Existing (reused):** `GET /state`, `GET /visits`, `POST /command` (clean/autoclean/delay/sleep/quiet), `GET /events` (SSE).

**New — read (JSON):**
- `GET /cats` → array of per-cat rollups for the cards: `{name, status: "ok"|"watch"|"alert", litter_count_today, last_litter_ts, last_ate, output_trend, scatter_level, threshold_h}`. Derived from `store.sessions`, `store.bowl_sessions`, `use_record`, scatter columns, and the `health_watch` threshold logic (extract the per-cat status computation into a shared, testable helper so UI and watcher agree).
- `GET /cat/<name>` → detail: recent visits + bowl sessions + weekly per-cat facts + photo paths.
- `GET /feeders` → per feeder: `{label, online, hopper, last_feed, next_meal, today_count}`.
- `GET /bowls` → per bowl: `{location, state, last_consumption_secs, auto_feeds_today}` (fullness now live post-sx1).
- `GET /weekly` → latest `weekly_reports` row (facts/findings/narrative).
- `GET /boxhealth` → `{bin_full_since, capacity, cleans_since_empty, est_cleans_left, faults, auto_clean}`.

**New — write (extend `POST /command`):** `feed` (`{feeder|location, portions}`), and (Tier B, later) `schedule`, `threshold`.

**Contract:** all endpoints return JSON with explicit shapes; the Svelte `api.ts` mirrors them as TypeScript types (single source of contract truth in the spec → types).

## Data flow

- On load: parallel fetch `/state`, `/cats`, `/feeders`, `/bowls`, `/boxhealth`.
- Live: subscribe to `/events` (SSE) → update box state, cleans, bin, and push transient alerts.
- Periodic: re-fetch `/cats` + `/bowls` + `/boxhealth` every ~30s (cat rollups aren't event-pushed). SSE event arrival also triggers an opportunistic `/cats` refresh.
- **Stale handling:** `/state.stale` (daemon quiet > 2 polls) → grey live-dot + "data may be stale" badge; never present stale data as current.

## Error handling / degradation

- Any failed endpoint → that card shows a "—/unavailable" state, never a blank or a crash; the rest of the dashboard renders.
- SSE disconnect → auto-reconnect with backoff; fall back to periodic polling meanwhile; show connection state in the header.
- Write commands → optimistic disabled-state on the button + confirmation toast; on failure, revert + show the error (reuse `/command`'s `{ok, error}` shape).
- Empty data (no cats seeded, no weekly report yet) → friendly empty states, not errors.

## Testing

- **API:** pytest per new endpoint in the existing style (`tmp_path` SQLite, seed rows, assert JSON shape + values). The extracted per-cat status helper gets unit tests so UI status == watcher status.
- **Front-end:** keep logic in `lib/` (pure functions: formatting, status→color, "N cleans left") and unit-test those with Vitest. Components stay thin. A smoke test that the built `static/index.html` serves and boots.
- **No** heavy E2E in scope.

## Out of scope (YAGNI)

- Auth / multi-user / remote (non-LAN) access.
- Historical charts beyond the weekly rollup the daemon already computes.
- Push notifications (Telegram already covers that).
- Editing the vision gallery from the UI (separate concern).
- Tier-B persisted-config editing in the first phase (flagged for a later phase).

## Phasing (informs the implementation plan)

1. **Foundation + Mission Control (read):** Vite/Svelte scaffold, theme, `/cats` + `/boxhealth` + `/bowls` + `/feeders` endpoints + the status helper, the main screen, SSE + stale handling.
2. **Controls (Tier A):** control bar + `feed` command, confirmations.
3. **Cat Detail + Weekly:** `/cat/<name>`, timeline, weekly trend, photos.
4. **Settings Tier A** then **Tier B** (persisted config infra — its own phase).
