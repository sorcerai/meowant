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

DPS discovered on the live device (Tuya category `cwwsq`, id `ebb89ffc060bf03766sphf`):

| dp | type | meaning |
|----|------|---------|
| `feed_report` | Integer 0–50 | portions dispensed in the last feed |
| `feed_state` | Enum standby/feeding | current mechanical state |
| `meal_plan` | Raw (base64) | on-device feeding schedule |
| `manual_feed` | Integer 1–50 | command: dispense N portions now |
| `battery_percentage` | Integer 0–100 | (device is mains-powered; ignored) |
| `voice_times`, `factory_reset` | — | not used |

**The feeder reports DISPENSING, not CONSUMPTION.** No food-level sensor, no bowl
weight. Phase 1 therefore monitors *that food was delivered*, not *that it was
eaten*. The "are they eating" signal is **Phase 2** (a bowl camera, `meowcam5`, with
full/empty vision) — explicitly out of scope here.

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
- `FeederDevice(cfg)` — thin local wrapper, mirroring `mw/device.py`'s `TuyaDevice`:
  - `status() -> dict` → `{"feed_state", "feed_report", "online"}` (best-effort; on a
    local I/O error returns `{"online": False}` rather than raising).
  - `feed(portions) -> bool` → writes `manual_feed`; True on confirmed send.
  - `read_meal_plan() -> str|None` → raw base64 of `meal_plan` (for the optional
    decode enhancement).
  - Holds the feeder's own `device_id`/`local_key`/`address`/`version` (separate from
    the SC10's), read from a `feeder` config block.
- `FeederMonitor(device, conn, notify, mealtimes, now_fn=time.time, ...)` — the
  watchdog, testable with a fake `FeederDevice`:
  - Polls `status()` each cycle. Detects a **dispense** on a `feed_state`
    standby→feeding edge (and/or a `feed_report` change) → `store.log_feed_event`.
  - **Adherence check:** for each scheduled mealtime, once the mealtime + a grace
    window has passed with no logged dispense in `[mealtime, mealtime+window]`, fire a
    missed-drop alert (once per mealtime per day; re-arms next day).
  - **Unreachable check:** if `status()` reports `online: False` for longer than
    `offline_minutes`, fire an unreachable alert (latched, re-arms on recovery).
  - Latch + re-arm + **fail-loud-on-delivery** (`if notify(msg) is not False:`),
    matching `mw/health_watch.py`, `mw/deadman.py`, `mw/invariant_canary.py`.

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
- `/feedstatus` — last dispense (from `feed_events`), current `feed_state`, online.

**`mw/report.py`** — add a "feeds today: N meals / M portions" line to `digest`.

**Config** (`config.json`, gitignored) — a `feeder` block:
`{enabled, device_id, local_key, address, version, poll_interval_s, mealtimes:
["07:00","18:00"], miss_grace_minutes, offline_minutes}`. The `device_id`
(`ebb89ffc…`) and pulled `local_key` live only here.

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
  - fail-loud: a notify returning False does not latch (retries next cycle).
- `store.feed_events`: log + `feed_events_today` + `recent_feed_events` round-trip.
- `report.digest` includes the feeds line.
- meowantd wiring presence test (matches existing `inspect.getsource` style).
- `FeederDevice` local I/O is integration-tested manually at build (real device); unit
  tests use the fake.

## Build-time discovery tasks (not code; prerequisites)

1. `tinytuya` scan to get the feeder's LAN IP + protocol version; pull `local_key`
   (the `cmd_refresh_key` cloud path already does the key); write the `feeder` block
   into gitignored config.json.
2. Confirm local control works against the real device (read `status`, a test
   `manual_feed` of 1 portion).
3. Attempt the `meal_plan` decode (set a known schedule in the app, read the base64,
   reverse it). If it doesn't crack quickly, fall back to config mealtimes + bead.

## Out of scope (Phase 2 / later)

- **Bowl camera (`meowcam5`) + full/empty vision** — the real "are they eating" signal
  and "bowl empty too long" alarm; the mealtime gather is its prime observation
  window. Its own spec → plan → build.
- **System-managed scheduling** (`/schedule`, encoding `meal_plan`).
- **Per-cat feed attribution** (the feeder can't attribute; only the Phase 2 camera
  could, and even then only weakly).
- Battery alerts (device is mains-powered).
