# Cat Mission Control — Phase 3 Design

**Status:** approved 2026-06-26. Owner said "just do it, change later" — no spec-review gate.

**Goal:** Make the dashboard a control surface, not just a monitor: edit daemon
config (feeder mealtimes, quiet hours, smart-clean, per-cat thresholds) from the
⚙ panel, and show real cat photos in cat-detail.

## Decisions (owner-confirmed)
- **Apply model:** save writes `config.json`, then the daemon auto-reloads
  (`launchctl kickstart -k`). Safe now that alarm/offset latches persist (the
  Telegram fix). The API runs *inside* the daemon, so the endpoint must
  **respond 200 first, then fire a detached reload** or it kills its own response.
- **Editable set (v1):** feeder mealtimes, quiet hours, smart-clean
  (enabled/idle_seconds), per-cat litter thresholds.
- **Security:** dashboard is reached over **Tailscale** — the Tailnet is the
  trust boundary, so NO app-level auth (matches existing `/command`). Secrets
  (device_id, local_key, cloud keys) are NEVER serialized to the frontend or
  accepted from it, in any code path.

## Scope: two independent sub-projects
### Phase 3a — Gallery photos (small, ship first)
- Photos live at `gallery/<catname>/*.jpg`; `cat_detail` returns raw FS paths the
  browser can't fetch; Flask serves only `/static`.
- Add `GET /gallery/<path:p>` → `send_from_directory("gallery", p)` (traversal-safe).
- `cat_detail` returns `/gallery/<cat>/<file>` URLs; frontend `<img>` renders them.

### Phase 3b — Settings + config-write (the meaty one)
**Config migrations (two settings aren't in config yet):**
- Quiet hours → add `health.quiet_start`/`health.quiet_end`; wire `health_watch`
  (replaces hardcoded `is_night` 22–8) and `deadman` (already takes the params).
- Per-cat thresholds → migrate `cat_status.THRESHOLDS` into `config.json`
  (`thresholds: {...}`). `cat_status` becomes the loader (config value, code dict
  as fallback default); `health_watch`/`deadman` keep importing from `cat_status`
  (preserves the 29e single-source — it just reads config now).

**`mw/config_write.py` (new) — safety core:**
- `SAFE_FIELDS` allowlist: only the four editable groups are writable.
- `read_safe(cfg)` → editable subset ONLY (no secrets out).
- `apply_edits(path, edits)` → validate (mealtimes `HH:MM`+sorted; hours in sane
  ranges; thresholds positive ints; reject any non-allowlisted key), then **merge
  into existing on-disk config** (preserve secrets + unknown keys) and write
  **atomically** (temp + `os.replace`). Invalid input → reject, nothing written.

**API (`mw/api.py`):**
- `GET /config` → `read_safe`.
- `POST /config` → validate+write, return `200 {ok, applied}` FIRST, then detached
  reload (`sh -c 'sleep 1; launchctl kickstart -k gui/<uid>/com.meowant.daemon'`).
- Reload command injected as a dependency so tests don't actually restart anything.

**Frontend:** ⚙ opens a full-screen Memphis Settings panel (matches CatDetail),
four sections loaded from `GET /config`, client-side validation, Save → POST →
"Saved — reloading…" with a brief reconnect while the daemon restarts.

## Safety & testing
- `config_write` units: secret-preservation, atomic write, every validator
  (good+bad), reject-out-of-allowlist.
- Migration units: quiet hours + thresholds read from config, code defaults as fallback.
- API: `GET /config` excludes secrets; `POST /config` writes + invokes (mocked)
  reload; `/gallery` serves + rejects traversal.

## Out of scope (v1)
Editing cameras/feeder devices/secrets; multi-user; audit log; undo.
