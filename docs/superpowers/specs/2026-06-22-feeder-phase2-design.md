# Cat Feeder — Phase 2 (Bowl Camera + Vision) — Design

**Date:** 2026-06-22
**Status:** design (pending user review)

## Goal

Add the **consumption** signal the feeder hardware can't provide: a camera on the
food bowl, with full/empty vision, so the system can (1) **refill** when the bowl runs
empty (alert, or autonomously dispense), and (2) **observe** how fast the bowl empties
after each dispense (a passive food-preference trend). Together with Phase 1 (dispense
+ hopper + offline) this closes the feeding loop: food in (Phase 1) → food eaten
(Phase 2).

## Scope decisions (owner-confirmed)

- **No "not-eating = sick" alarm.** In this household a full/slowly-emptying bowl means
  *food boredom*, not illness — and the cats still eat it, reluctantly. A loud binary
  "not eating" alert would be noisy (fires on boredom) and rarely true-fire (the bowl
  always depletes eventually). Boredom is a *trend to glance at*, not an alarm. So
  Phase 2 **logs consumption for observability** but raises **no health alarm** on a
  full bowl.
- **Empty action is configurable** (`bowl.auto_feed`): `false` (default) = alert only,
  you tap `/feed`; `true` = auto-dispense via the Phase-1 `FeederDevice.feed`,
  hard-rate-limited, plus a notice. Start safe, flip to autonomous once the classifier
  is proven.
- **Hybrid detection:** cheap image-diff vs a pinned empty-bowl reference runs every
  poll; an empty/ambiguous read **escalates to `agy`** for confirmation before acting.
- **New hardware, design now / build when mounted:** a Wyze Cam OG → cryze→MediaMTX as
  `meowcam5`, pointed at the bowl. The detection *logic* is unit-testable now with fake
  frames; ROI/threshold/reference **calibration** waits on the mounted camera.

## What the camera adds (vs Phase 1)

Phase 1 senses DISPENSING (dp 118) + hopper (dp 108) + offline. It cannot see the
bowl. Phase 2's camera senses the **bowl** — the only way to know food was actually
*consumed*, not just delivered. The mealtime dispense sound makes the cats gather at
the bowl, so the camera also gets a natural multi-cat observation window.

## Architecture

A new `BowlWatch` service, parallel to the litterbox watchers, reusing the shared
`catfilter` and the `agy` vision backend.

```
meowcam5 (Wyze OG -> cryze -> MediaMTX rtsp)
   │  (timer poll ~20min — independent of cat presence)
   ▼
grab frame ──► catfilter.is_clear? ──no──► skip (a cat is at the bowl; don't judge)
                    │ yes
                    ▼
            image-diff vs empty_ref (bowl ROI) ──► fullness: full | some | empty/ambiguous
                    │ empty/ambiguous
                    ▼
            agy confirm ("is the bowl empty?") ──► confirmed empty?
                    │  (2 consecutive confirmed reads = debounce)
                    ▼
   ┌──────────── EMPTY ────────────┐         ┌──── full -> empty transition ────┐
   │ alert "bowl empty Xh"          │         │ log consumption: secs since the   │
   │ if auto_feed: FeederDevice.feed │         │ last Phase-1 feed_event           │
   │   (rate-limited max/day) + note │         │ (digest trend, NO alarm)          │
   └────────────────────────────────┘         └───────────────────────────────────┘
```

### Components

**`mw/bowl_watch.py`**
- `bowl_fullness(frame, empty_ref, roi) -> str` — pure: image-diff of the bowl ROI
  vs the empty reference → `"full" | "some" | "empty"` by threshold band. Unit-tested
  with synthetic arrays/frames (no camera). Mirrors `scatter_detector`'s diff approach.
- `BowlWatch(grab, catfilter, confirm_empty, feeder, conn, notify, now_fn=time.time, *,
  empty_ref, roi, poll_interval_s=1200, empty_alert_hours=3, auto_feed=False,
  auto_feed_portions=1, auto_feed_max_per_day=4)`:
  - `grab() -> frame|None` — single-frame grab from meowcam5 (ffmpeg, like `capture`);
    None on failure → skip this cycle (no false empty).
  - `catfilter.is_clear(frame) -> bool` — reuse the shared filter; skip non-clear
    frames (a cat at the bowl).
  - `confirm_empty(frame) -> bool` — `agy`-backed in prod (a thin vision call with a
    bowl-specific prompt); injectable fake in tests.
  - `feeder` — the Phase-1 `FeederDevice` (for `auto_feed`); may be None (alert-only).
  - `poll_once()`: grab → cat-free gate → `bowl_fullness` → if empty/ambiguous,
    `confirm_empty` → debounce (2 consecutive confirmed-empty) → on confirmed empty:
    log the empty transition, alert (latched), and if `auto_feed` dispense (rate-limit
    via today's auto-feed count in `bowl_events`) + notice. On a full→empty transition,
    log consumption (now − last `feed_event` ts). Latched fail-loud, re-arm on refill.
  - `run()`: `while True: try poll_once() except -> stderr; sleep(poll_interval_s)`.

**`mw/store.py`** — new `bowl_events` table (in `SCHEMA`): `id, ts, state, source`
(`state` = `full|some|empty`; `source` = `vision|auto_feed`). Functions:
`log_bowl_event(conn, state, source="vision", ts=None)`, `last_bowl_state(conn)`,
`recent_bowl_events(conn, limit=20)`, `auto_feeds_today(conn)` (count of
`source="auto_feed"` rows today, for the rate-limit).

**`meowantd.py`** — add `meowcam5` to the `cameras` config; construct `BowlWatch`
(gated on `bowl.enabled` + meowcam5 present), pass the Phase-1 `FeederDevice` when
`auto_feed`, start it on a daemon thread.

**Telegram / report** — `/bowl` command (current state + last empty + last feed);
a digest line ("bowl: <state>; emptied ~Xh after last feed").

**Config** (`config.json`, gitignored) — `cameras += {name: meowcam5, url: ...}`; a
`bowl` block: `{enabled, roi, empty_ref_path, poll_interval_s, empty_alert_hours,
diff_thresholds, auto_feed, auto_feed_portions, auto_feed_max_per_day}`.

## Reliability & safety

- **Debounce (2 consecutive confirmed-empty)** + **agy confirm** before any action —
  makes a false-empty (glare, angle) very unlikely, which matters most when `auto_feed`
  is on (a false empty would over-feed).
- **Cat-free gate** prevents judging the bowl while a cat is eating from it.
- **Auto-feed rate-limit** (`auto_feed_max_per_day`) caps the damage of any residual
  false-empty; once hit, it stops auto-feeding and alerts instead (fail to LOUD, not to
  over-feed).
- **Fail-loud latches** (alert once per empty episode, re-arm on refill), matching every
  other watchdog (`health_watch`, `deadman`, `invariant_canary`, `feeder`).
- `grab()`/`confirm_empty` failures → skip the cycle (never a false empty); `run()`
  never dies.
- **Independence:** a BowlWatch failure cannot mask the litterbox elimination signal
  (separate thread, camera, tables). And the on-device feeder schedule still feeds the
  cats regardless.

## Error handling

Every failure degrades toward "no action / alert," never toward a silent over-feed or a
missed empty: a failed grab or agy call skips the cycle; an auto-feed that fails
(`FeederDevice.feed` returns False) alerts; the rate-limit converts excess auto-feeds
into alerts.

## Testing

- `bowl_fullness`: synthetic frames (uniform arrays / fixtures) at empty/some/full →
  correct band; ROI respected.
- `BowlWatch` with fakes (scripted `grab`, `catfilter`, `confirm_empty`, fake feeder):
  - cat-present frame → skipped, no judgment.
  - empty confirmed twice → alert once, latches; refill → re-arm.
  - single empty read → no action (debounce).
  - `auto_feed=True` → calls `feeder.feed`, logs `auto_feed`, respects the daily
    rate-limit (then alerts instead).
  - `auto_feed=False` → alert only, never calls `feeder.feed`.
  - full→empty transition → logs consumption (secs since last feed_event).
  - fail-loud: a notify returning False does not latch.
- `store.bowl_events`: log/query/`auto_feeds_today` round-trip.
- `report`: digest bowl line; `/bowl` text.
- meowantd wiring presence test.
- **Live calibration (build-time, manual, needs the camera):** capture the empty-bowl
  reference, tune `roi` + `diff_thresholds`, validate the `agy` prompt against real
  empty/some/full frames.

## Build-time prerequisites (not code)

1. Mount the Wyze OG as `meowcam5` aimed at the bowl; relocate the feeder off the
   counter so the bowl is reachable by the cats AND in frame.
2. Get `meowcam5` streaming via cryze→MediaMTX; add its rtsp URL to config.
3. Capture a clean empty-bowl reference frame; tune ROI + diff thresholds; validate the
   `agy` empty/full prompt.

## Out of scope (later)

- Per-cat eating attribution (who ate / who's at the bowl) — possible via `agy` on the
  gather, but its own effort.
- A not-eating *health* alarm (cut — boredom-dominated here).
- Wet-food / multi-bowl support.
- System-managed feeder scheduling (still a Phase-1 follow-up).
