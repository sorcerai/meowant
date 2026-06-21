# meowant

Local control for the **Meowant SC10** automated cat litter box.

The SC10 is a [Tuya](https://www.tuya.com/) v3.5 (AES-GCM) device. This talks to
it **directly over the LAN** (TCP 6668) using a `local_key` — no phone app, no
cloud round-trip, works even with the internet down.

## Setup

```bash
pip install -r requirements.txt
cp config.example.json config.json   # then fill in local_key (config.json is gitignored)
```

`config.json` already holds the discovered device + key for this household. To
recover the key on a fresh machine, fill in the `cloud` block and run
`python3 meowant.py refresh-key` (see *Recovering the key* below).

## Dashboards

```bash
python3 meowantd.py        # daemon (owns the device socket)
python3 tui.py             # terminal dashboard (Textual) — requires meowantd running
```

**TUI keys:** `c` clean · `a` auto-clean · `s` sleep · `[` / `]` delay −/+ · `r` refresh · `q` quit.

The web dashboard is served by meowantd at `http://<host>:8765/`. It auto-refreshes every 3s and has buttons for every control plus a raw-DPS view.

## CLI

```bash
python3 meowant.py status              # decoded dashboard
python3 meowant.py raw                 # raw DPS dict
python3 meowant.py watch               # live-stream DPS changes
python3 meowant.py clean               # trigger a manual scoop cycle
python3 meowant.py autoclean on|off    # toggle auto-clean
python3 meowant.py quiet 22:00 08:00   # set quiet/sleep window
python3 meowant.py refresh-key         # re-pull local_key from Tuya cloud
```

## Device facts

| | |
|---|---|
| Model | Meowant SC10 ("MW-SC10", firmware "GEN 5+改易佰") |
| IP | `192.168.1.X` (your device's LAN IP) |
| MAC | `XX:XX:XX:XX:XX:XX` (Tuya Smart OUI) |
| Device ID | `<your device id>` |
| Protocol | Tuya v3.5 (AES-GCM), only open port TCP 6668 |
| Cloud region | US (`tuyaus`) |

## Data points (DPS)

Tuya category `msp` (pet smart device) plus SC10 vendor extras.

| dp | code | type | meaning |
|----|------|------|---------|
| 4  | `auto_clean` | bool | auto-clean enabled |
| 5  | `delay_clean_time` | int (min, 1–60) | delay after cat leaves before scooping |
| 7  | `excretion_times_day` | int | daily use counter |
| 10 | `sleep` | bool | quiet mode currently active |
| 11 | `sleep_start_time` | int | quiet window start (minutes since midnight) |
| 12 | `sleep_end_time` | int | quiet window end (minutes since midnight) |
| 21 | `notification` | bitmap | bit0 = `garbage_box_full`, then E1–E5 |
| 22 | `fault` | bitmap | E1–E5 error flags |
| 23 | `factory_reset` | bool | factory reset trigger |
| 24 | `status` | enum | `standby` \| `cleaning` |
| 101 | vendor | — | **undocumented**; only appears while `dp24=cleaning` → likely cycle/progress. |
| 103–111 | vendor | mixed | **undocumented**; `dp107` seen as `"enter"` (likely cat IR presence). Use `watch` to confirm. |

Times are stored as minutes-since-midnight (e.g. `1320` = 22:00, `480` = 08:00).

## Recovering the key

The `local_key` is per-device and lives in the Tuya cloud account the app is
paired to. To re-pull it:

1. Create a free Cloud Project at [iot.tuya.com](https://iot.tuya.com)
   (Data Center: **Western America**).
2. Copy the **Access ID** and **Access Secret** into the `cloud` block of `config.json`.
3. Project → **Devices → Link App Account → Add App Account**, then scan the QR
   from the phone app's **Me → scan** screen.
4. `python3 meowant.py refresh-key`

## How it was found

Discovered by sweeping the LAN, fingerprinting the Tuya Smart MAC OUI, decoding
the device's v3.5 UDP discovery broadcast (AES-GCM under Tuya's global UDP key),
and pulling the `local_key` via the Tuya IoT cloud API.

## Home Assistant

The same `device_id` / `local_key` / `version` work with the
[LocalTuya](https://github.com/rospogrigio/localtuya) integration for fully
local HA control.
