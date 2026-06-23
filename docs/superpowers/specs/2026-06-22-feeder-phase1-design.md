# Cat Feeder Integration — Phase 1 (Tuya local) — Design

**Date:** 2026-06-22
**Status:** design (pending user review)

## Goal

Integrate the PLAF103 WiFi auto-feeder into the meowant daemon so the cats' **food
intake is monitored and controllable** while the owner travels — specifically:
verify scheduled drops actually happen, alert on feeder failure, and allow a manual
feed from anywhere. This is the **input** half of the health loop; the litterbox
already covers the **output** half (elimination is downstream of eating).

**Prime invariant (consistent with the rest of meowant):** a feeding problem must
become LOUD — a missed/failed drop or an unreachable feeder escalates, never
silently passes.

## What the feeder can and cannot sense (the constraint that shapes everything)

DPS discovered on the live device (Tuya category `cwwsq`, id `ebb89ffc060bf03766sphf`).
The cloud API exposes codes; **local enumeration revealed more dps than the cloud
`functions` list**, including a food-level sensor. Local raw dp numbers (from a LAN
`status()` read) cross-referenced to cloud codes:

All dp numbers below are **confirmed** by a live test feed (`set_value(3,1)` →
dispensed, observed):

| local dp | meaning | confirmed behavior |
|----------|---------|--------------------|
| `3` | **manual_feed** (write-only command) | `set_value(3, N)` dispenses N portions; absent from status reads |
| `4` | **feed_state** enum | cycles `standby`→`feeding`→`standby`→`feed_end` (cloud enum omits `feed_end`) |
| `118` | **feed record** (Raw base64) | the reliable dispense signal — appears on each feed; see decode below |
| `108` | **food_level** enum | hopper IR level; read `full` (persistent) |
| `1` | meal_plan (Raw base64) | `AA==` = no schedule set |
| `11`, `14` | **UNUSED** | both stayed `0` through a confirmed dispense — NOT a portion counter; discarded |
| `18`,`103`,`109`,`112`,`113`,`116` | voice/misc | not used |

**dp 118 feed-record format** (10 bytes, confirmed against the test feed):
`[year_hi, year_lo, month, day, hour, min, sec, portions, type, flag]` — e.g.
`07 EA 06 16 16 23 28 01 02 00` = `2026-06-22 22:35:40`, `portions=1`. Because dp 118
**persists the last feed** (it's a record, not a transient edge), a slow poll reads it
reliably: when the decoded timestamp advances, a new feed happened (with its portion
count). **This is what makes adherence monitoring reliable without fast/windowed
polling** — the original concern (catching a ~10s `feeding` edge) is moot.

**Cloud control is NOT available** for this device: `manual_feed` via the cloud API
returns `1109 param is illegal`. Local control is the only working path — which
vindicates the local-control decision below.

**For the EATING signal, the feeder still reports DISPENSING, not CONSUMPTION** — no
bowl weight, no bowl camera. Phase 1 monitors *that food was delivered* (dp 118) and
*that the hopper still has food* (dp 108), not *that a cat ate*. The "are they eating"
signal remains **Phase 2** (a bowl camera, `meowcam5`, full/empty vision) — out of
scope here.

**New in Phase 1 from discovery — hopper-level alert (dp 108):** the feeder senses
whether its food *storage* is full/empty. Phase 1 adds a **"feeder out of food"**
alert (hopper not `full`) — a genuine "cats will go unfed" signal, distinct from the
Phase-2 bowl-consumption signal.

## Key decisions (owner-confirmed)

1. **Local control**, not cloud. The feeder is mains-powered and always on WiFi, so
   `tinytuya` local (TCP 6668 + `local_key`, mirroring `mw/device.py`) is viable and
   keeps manual control working during an internet outage (owner reaches the Mac on
   the LAN via Tailscale). One-time build cost: discover the feeder's LAN IP +
   protocol version via a `tinytuya` scan. **The schedule runs on the feeder hardware
   itself**, so scheduled drops happen even if the Mac, daemon, and internet are all
   down — our system sets nothing in the critical path; it monitors and overrides.
2. **Schedule is app-set, we monitor.** The owner sets the feeding schedule once in
   the SmartLife app (on-device, reliable). Our system knows the mealtimes and alerts
   if a scheduled drop fails. System-managed scheduling (encoding `meal_plan` + a
   `/schedule` command) is a later stretch, not Phase 1.

## Architecture

A new feeder subsystem parallel to the litterbox device path, surfaced through the
existing notify + Telegram + digest channels.

```
FeederDevice (local tinytuya I/O)  ──┐
                                      ├─► FeederMonitor (poll loop: detect drop,
config: mealtimes + thresholds  ─────┘      verify adherence, alert) ─► notify
                                                │
                                                ├─► store.feed_events (log)
                                                └─► /feed, /feedstatus, digest
```

### Components

**`mw/feeder.py`**
- `FeederDevice(cfg)` — thin local wrapper, mirroring `mw/device.py`'s `TuyaDevice`,
  with a named dp-constant block (`DP_FEED_STATE="4"`, `DP_FEED_REPORT="11"`,
  `DP_MANUAL_FEED="3"`, `DP_MEAL_PLAN="1"`, `DP_FOOD_LEVEL="108"` — the "(confirm)"
  ones verified at build):
  - `status() -> dict` → `{"feed_state", "food_level", "last_feed", "online"}`
    (best-effort; on a local I/O error returns `{"online": False}` rather than
    raising). `food_level` is the dp-108 enum (e.g. `"full"`); `last_feed` is the
    decoded dp-118 record `{"ts": <epoch>, "portions": <int>}` or `None` if no feed
    recorded yet. A pure `decode_feed_record(b64) -> {"ts","portions"}|None` helper is
    unit-tested directly (the 10-byte format above).
  - `feed(portions) -> bool` → `set_value(3, portions)` (local dp 3); True on confirmed
    send. (Cloud control is unavailable — `1109`.)
  - Holds the feeder's own `device_id`/`local_key`/`address`/`version` (separate from
    the SC10's), read from a `feeder` config block. Discovered: `address=192.168.2.84`,
    `version=3.4`.
- `FeederMonitor(device, conn, notify, mealtimes, now_fn=time.time, ...)` — the
  watchdog, testable with a fake `FeederDevice`:
  - Polls `status()` each cycle. Detects a **dispense** when `last_feed["ts"]` is newer
    than the last logged feed's ts (dp 118 persists, so this is reliable regardless of
    poll timing) → `store.log_feed_event(portions, source, ts)`. Source is `manual` if
    a `/feed` was issued within a short expectation window (`note_manual_feed()` sets
    it), else `scheduled`. dp 118 is the single logging point — no double-count.
  - **Adherence check:** for each scheduled mealtime, once the mealtime + a grace
    window has passed with no logged dispense in `[mealtime, mealtime+window]`, fire a
    missed-drop alert (once per mealtime per day; re-arms next day). Reliable now that
    dp 118 gives authoritative feed timestamps. (Moot until `mealtimes` is configured —
    `meal_plan` is currently empty.)
  - **Unreachable check:** if `status()` reports `online: False` for longer than
    `offline_minutes`, fire an unreachable alert (latched, re-arms on recovery).
  - **Hopper-empty check:** if `food_level` is not `full` (e.g. `empty`/`low`), fire a
    "feeder low/out of food" alert (latched, re-arms when it reads `full` again).
  - Latch + re-arm + **fail-loud-on-delivery** (`if notify(msg) is not False:`),
    matching `mw/health_watch.py`, `mw/deadman.py`, `mw/invariant_canary.py`. Each
    independent check has its own latch (a missed drop, an outage, and a low hopper can
    coexist) — one latch per concern, like `mw/deadman.py`'s per-key latches.

**`mw/store.py`** — new `feed_events` table (added to `SCHEMA`, not `_MIGRATIONS`):
`id, ts, portions, source` (`source` = `scheduled` | `manual`). Functions:
`log_feed_event(conn, portions, source, ts=None)`, `feed_events_today(conn, day=None)
-> (meals, portions)`, `recent_feed_events(conn, limit=20)`. Same `with _lock:` idiom.

**`meowantd.py`** — construct `FeederDevice` + `FeederMonitor` and start the monitor
as a daemon thread, gated on `feeder.enabled` AND a `feeder.device_id` being present
(absent ⇒ feeder simply not wired, like the camera-absent path).

**Telegram** (`meowantd.py` command dict + `mw/telegram_bot.py` already supports it):
- `/feed N` — owner-allowlisted; dispense N portions (`FeederDevice.feed`), reply with
  the result; log as `source="manual"`. (Allowlist is the existing security boundary.)
- `/feedstatus` — last dispense (from `feed_events`), current `feed_state`, hopper
  `food_level`, online.

**`mw/report.py`** — add a "feeds today: N meals / M portions" line to `digest`.

**Config** (`config.json`, gitignored) — a `feeder` block:
`{enabled, device_id, local_key, address, version, poll_interval_s, mealtimes:
["07:00","18:00"], miss_grace_minutes, offline_minutes, low_food_levels:
["empty","low"]}`. The `device_id` (`ebb89ffc…`), `local_key`, `address`
(`192.168.2.84`), `version` (`3.4`) live only here. `low_food_levels` lists the dp-108
enum values that count as "needs a refill" (confirmed at build).

## Schedule monitoring: source of truth (de-risked)

`meal_plan` is a proprietary base64 byte-array; reverse-engineering its format is
build-time work that may not crack cleanly. To avoid blocking Phase 1 on it:

- **Source of truth = `feeder.mealtimes` in config** (the owner enters the same times
  they set in the app — a handful of `"HH:MM"` strings). Simple, reliable,
  deterministic to test.
- **Enhancement (best-effort, optional):** decode `meal_plan` to auto-derive the
  mealtimes and/or warn if the decoded schedule diverges from config (catches "owner
  changed the app schedule but forgot config"). If the format doesn't decode cleanly
  during build, ship with config-only and file a bead.

## Error handling

- `FeederDevice` local I/O errors → `status()` returns `{"online": False}` (drives the
  unreachable alert), `feed()` returns False (so `/feed` reports the failure honestly,
  and the manual log is not written for a failed send).
- `FeederMonitor.run()` wraps `run_once()` in try/except → stderr, never dies.
- Every alert fails toward LOUD; latches only on confirmed delivery so a dead Telegram
  token can't mute a real feeding failure.
- The on-device schedule is the real feeding guarantee; our monitor failing degrades
  to "no monitoring," never to "cats unfed" (they're fed by the device clock).

## Testing

- `FeederMonitor` with a fake `FeederDevice` (scripted `status()` sequences) and
  injected `now`/`mealtimes`:
  - dispense detection on the standby→feeding edge → logs one `feed_event`.
  - missed-drop fires once after grace past a mealtime with no dispense; silent if a
    dispense landed in the window; re-arms the next day.
  - unreachable fires after `offline_minutes`; recovers/re-arms.
  - hopper-empty fires when `food_level` enters a `low_food_levels` value; silent while
    `full`; re-arms on return to `full`.
  - fail-loud: a notify returning False does not latch (retries next cycle).
- `store.feed_events`: log + `feed_events_today` + `recent_feed_events` round-trip.
- `report.digest` includes the feeds line.
- meowantd wiring presence test (matches existing `inspect.getsource` style).
- `FeederDevice` local I/O is integration-tested manually at build (real device); unit
  tests use the fake.

## Build-time discovery — ALL DONE (recorded above)

- IP/version/key: `192.168.2.84` / `3.4` / pulled → `feeder` block in gitignored config.
- dp map confirmed by a live test feed: `3`=manual_feed, `4`=feed_state, `108`=food_level,
  `118`=feed record (decoded), `11`/`14` discarded.
- dp 118 decode confirmed (`[YY YY MM DD hh mm ss portions type flag]`).
- Cloud control confirmed unavailable (`1109`) → local only.
- Remaining unknowns (handle as they arise, not blockers): `food_level` enum values
  other than `full` (the `low_food_levels` config lists candidates — adjust when first
  observed); dp 118 `type`/`flag` byte semantics (we use portions + ts only);
  outgoing `meal_plan` (dp 1) write format (system-managed scheduling is out of scope).

## Out of scope (Phase 2 / later)

- **Bowl camera (`meowcam5`) + full/empty vision** — the real "are they eating" signal
  and "bowl empty too long" alarm; the mealtime gather is its prime observation
  window. Its own spec → plan → build.
- **System-managed scheduling** (`/schedule`, encoding `meal_plan`).
- **Per-cat feed attribution** (the feeder can't attribute; only the Phase 2 camera
  could, and even then only weakly).
- Battery alerts (device is mains-powered).
