#!/usr/bin/env python3
"""Pre-trip readiness checker — run the day before leaving.

Read-only by default: it inspects the daemon, cameras, bridge, config and DB
and prints a report. Nothing is armed/changed unless --send-test is passed.

Structured as small `check_*` functions returning a list of (name, ok, detail)
tuples, each independently unit-testable with injected fakes (see
tests/test_pretrip.py). main() assembles the checks into a report and picks
the exit code.

`ok` is tri-state:
  True  -- pass (green)
  False -- CRITICAL failure -- counts toward "NOT READY (N critical failures)"
  None  -- informational / non-blocking note (yellow, doesn't fail the run)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mw import client as mw_client
from mw import config as mw_config
from mw import store as mw_store

PASS, FAIL, WARN = True, False, None
_ICONS = {True: "✅", False: "❌", None: "⚠️ "}


# ---------------------------------------------------------------------------
# 1. daemon process
# ---------------------------------------------------------------------------

def _default_launchctl_list():
    return subprocess.run(["launchctl", "list"], capture_output=True,
                          text=True, timeout=10).stdout


def check_daemon_running(run_launchctl=None):
    run_launchctl = run_launchctl or _default_launchctl_list
    try:
        out = run_launchctl()
    except Exception as e:
        return [("daemon running", FAIL, f"launchctl list failed: {e}")]
    ok = "com.meowant.daemon" in out
    detail = ("com.meowant.daemon is loaded" if ok else
              "com.meowant.daemon NOT in launchctl list — daemon is not running")
    return [("daemon running", ok, detail)]


# ---------------------------------------------------------------------------
# 2. daemon API (/state)
# ---------------------------------------------------------------------------

def check_daemon_api(cfg, fetch_state=None):
    fetch_state = fetch_state or mw_client.get_state
    try:
        st = fetch_state()
    except Exception as e:
        return [("daemon API", FAIL, f"GET /state failed: {e}")]

    results = []
    faults = st.get("faults") or ["none"]
    if faults == ["none"]:
        results.append(("daemon API: faults", PASS, "no active faults"))
    else:
        results.append(("daemon API: faults", FAIL,
                         f"active fault(s): {', '.join(faults)}"))

    bin_full = bool(st.get("bin_full"))
    results.append(("daemon API: bin_full", not bin_full,
                     "waste bin OK" if not bin_full
                     else "waste bin is FULL — empty it before leaving"))

    threshold = mw_config.get(cfg, "litter.low_threshold", 110)
    status = st.get("status")
    load = (st.get("named") or {}).get("contents_load")
    # dp101 reads litter+cat weight together — only trust it in standby
    # (same reading discipline as mw.litter_watch.sample_once).
    if status != "standby" or load is None:
        results.append(("daemon API: litter level", WARN,
                         f"box not in standby (status={status!r}) — "
                         f"litter level unreadable this sample"))
    elif load < threshold:
        results.append(("daemon API: litter level", FAIL,
                         f"litter level {load} is below threshold {threshold} "
                         f"— refill before leaving"))
    elif load < threshold + 20:
        results.append(("daemon API: litter level", WARN,
                         f"litter level {load} is within 20 of threshold "
                         f"{threshold} — consider topping up"))
    else:
        results.append(("daemon API: litter level", PASS,
                         f"litter level {load} (threshold {threshold})"))
    return results


# ---------------------------------------------------------------------------
# 3. cameras (warm-frame freshness)
# ---------------------------------------------------------------------------

def litter_cams(cfg):
    """Cameras used for litterbox capture — everything not assigned to a bowl.
    Mirrors meowantd.litterbox_cameras() without importing the daemon module."""
    cams = mw_config.get(cfg, "cameras", []) or []
    bowl_cams = {b["camera"] for b in (mw_config.get(cfg, "bowls", []) or [])
                 if b.get("camera")}
    return [c["name"] for c in cams if c.get("name") not in bowl_cams]


def check_cameras(cfg, warm_dir="warm_frames", max_age_s=120,
                   now_fn=time.time, mtime_fn=None):
    mtime_fn = mtime_fn or os.path.getmtime
    ignore = set(mw_config.get(cfg, "capture.blackout_ignore_cams",
                                ["meowcam4"]) or [])
    results = []
    for cam in litter_cams(cfg):
        path = os.path.join(warm_dir, f"{cam}.jpg")
        try:
            age = now_fn() - mtime_fn(path)
            fresh = age < max_age_s
            detail = f"{age:.0f}s old"
        except OSError:
            fresh = False
            detail = "warm frame missing"
        if cam in ignore:
            # Reported for visibility, but never blocks the trip — these cams
            # are already excluded from the bridge-blackout vote elsewhere.
            results.append((f"camera {cam} (blackout-ignored)", WARN, detail))
        else:
            results.append((f"camera {cam}", fresh, detail))
    return results


# ---------------------------------------------------------------------------
# 4. bridge (Proxmox stream bridge: disk + mediamtx)
# ---------------------------------------------------------------------------

def default_bridge_ssh(host, user, cmd, timeout=15):
    """SSH one command to the bridge host via key auth (BatchMode) — the same
    path bridge-watch and the manual runbook use (aria@bridge, keys already
    trusted from this Mac). Raises on transport/exit failure."""
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
         f"{user}@{host}", cmd],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"ssh exited {r.returncode}")
    return r.stdout


def default_http_get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode()


def check_bridge(cfg, ssh_runner=None):
    """Both probes go over SSH: the MediaMTX API (9997) is bound to localhost
    on the bridge, so the paths check must run ON the bridge via curl — the
    same route mw.bridge_watch uses."""
    host = mw_config.get(cfg, "bridge.host", "192.168.2.79")
    user = mw_config.get(cfg, "bridge.ssh_user", "aria")
    ssh_runner = ssh_runner or default_bridge_ssh

    results = []
    try:
        out = ssh_runner(host, user, "df -P / | tail -1")
        pct = int(out.split()[4].rstrip("%"))
        results.append(("bridge: disk", pct < 80,
                         f"{pct}% used (threshold 80%)"))
    except Exception as e:
        results.append(("bridge: disk", FAIL, f"ssh probe failed: {e}"))

    try:
        out = ssh_runner(host, user,
                         "curl -s --max-time 5 http://127.0.0.1:9997/v3/paths/list")
        data = json.loads(out)
        items = data.get("items", [])
        ready = sum(1 for it in items if it.get("ready"))
        results.append(("bridge: mediamtx paths", ready > 0,
                         f"{ready}/{len(items)} paths ready"))
    except Exception as e:
        results.append(("bridge: mediamtx paths", FAIL,
                         f"mediamtx probe failed: {e}"))
    return results


# ---------------------------------------------------------------------------
# 5. sitters
# ---------------------------------------------------------------------------

def check_sitters(cfg):
    ids = mw_config.get(cfg, "alerts.telegram_chat_ids", []) or []
    if isinstance(ids, str):
        ids = [ids]
    n = len(ids)
    if n < 2:
        return [("sitters configured", FAIL,
                  f"only {n} telegram_chat_ids configured — "
                  f"add a sitter chat id before leaving")]
    return [("sitters configured", PASS, f"{n} telegram_chat_ids configured")]


# ---------------------------------------------------------------------------
# 6. watchers armed
# ---------------------------------------------------------------------------

def check_watchers_armed(cfg):
    results = []
    for label, key in [("jam watch", "jam_watch.enabled"),
                        ("litter watch", "litter.watch_enabled"),
                        ("feed-plan sync", "feed_plan_sync.enabled")]:
        val = mw_config.get(cfg, key, True)
        ok = val is not False
        results.append((f"watcher: {label}", ok,
                         "armed" if ok else "disabled in config"))

    if mw_config.get(cfg, "bridge", None) is not None:
        val = mw_config.get(cfg, "bridge.enabled", True)
        ok = val is not False
        results.append(("watcher: bridge", ok,
                         "armed" if ok else "disabled in config"))

    for f in (mw_config.get(cfg, "feeders", []) or []):
        if not f.get("enabled", True):
            continue
        label = f.get("label", "default")
        ok = f.get("deadman_enabled", True) is not False
        results.append((f"watcher: feeder deadman ({label})", ok,
                         "armed" if ok else "disabled in config"))

    results.append(("watcher: capture health", PASS,
                     "implied by the camera checks above"))
    return results


# ---------------------------------------------------------------------------
# 7. feeder schedules
# ---------------------------------------------------------------------------

def check_feeder_schedules(cfg):
    results = []
    for f in (mw_config.get(cfg, "feeders", []) or []):
        if not f.get("enabled", True):
            continue
        label = f.get("label", "default")
        mealtimes = f.get("mealtimes") or []
        ok = len(mealtimes) > 0
        results.append((f"feeder schedule ({label})", ok,
                         f"{len(mealtimes)} mealtimes" if ok
                         else "no mealtimes configured"))
    if not results:
        results.append(("feeder schedules", WARN, "no enabled feeders configured"))
    return results


# ---------------------------------------------------------------------------
# 8. heartbeat
# ---------------------------------------------------------------------------

def check_heartbeat(cfg):
    url = mw_config.get(cfg, "health.heartbeat_url")
    ok = bool(url)
    return [("heartbeat configured", ok, url or "health.heartbeat_url not set")]


# ---------------------------------------------------------------------------
# 9. recent attribution alive
# ---------------------------------------------------------------------------

def check_recent_attribution(conn, window_hours=48, now_fn=time.time):
    cutoff = mw_store._iso(now_fn() - window_hours * 3600)
    with mw_store._lock:
        row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN cat_id IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM visits WHERE enter_ts >= ?", (cutoff,)).fetchone()
    total, attributed = row[0], row[1] or 0
    if total == 0:
        return [("recent attribution", WARN,
                  f"no visits in the last {window_hours}h to validate attribution")]
    ok = attributed > 0
    return [("recent attribution", ok,
              f"{attributed}/{total} visits in last {window_hours}h have a cat_id")]


# ---------------------------------------------------------------------------
# 10. disk on Mac
# ---------------------------------------------------------------------------

def check_disk_space(path=None, threshold_pct=90, disk_usage_fn=None):
    path = path or REPO_ROOT
    disk_usage_fn = disk_usage_fn or shutil.disk_usage
    usage = disk_usage_fn(path)
    pct = 100 * usage.used / usage.total
    return [("disk space (repo volume)", pct < threshold_pct,
              f"{pct:.1f}% used (threshold {threshold_pct}%)")]


# ---------------------------------------------------------------------------
# optional: real test alert
# ---------------------------------------------------------------------------

def send_test_alerts(cfg, telegram_notify=None):
    from mw.alerts import telegram_notify as _real_telegram_notify
    telegram_notify = telegram_notify or _real_telegram_notify

    token = mw_config.get(cfg, "alerts.telegram_bot_token")
    primary = mw_config.get(cfg, "alerts.telegram_chat_id")
    extra = mw_config.get(cfg, "alerts.telegram_chat_ids", []) or []
    if isinstance(extra, str):
        extra = [extra]
    seen, recipients = set(), []
    for cid in ([primary] if primary else []) + list(extra):
        if cid and cid not in seen:
            seen.add(cid)
            recipients.append(cid)

    if not token or not recipients:
        return [("send-test", FAIL,
                  "no alerts.telegram_bot_token or recipients configured")]

    msg = "✅ Meowant pre-trip test message — if you got this, alerts reach you."
    results = []
    for cid in recipients:
        ok = telegram_notify(msg, token, cid)
        results.append((f"send-test -> {cid}", ok,
                         "delivered" if ok else "delivery failed"))
    return results


# ---------------------------------------------------------------------------
# report assembly
# ---------------------------------------------------------------------------

def run_all_checks(cfg, conn):
    return [
        ("1. Daemon process", check_daemon_running()),
        ("2. Daemon API", check_daemon_api(cfg)),
        ("3. Cameras", check_cameras(cfg)),
        ("4. Bridge", check_bridge(cfg)),
        ("5. Sitters", check_sitters(cfg)),
        ("6. Watchers armed", check_watchers_armed(cfg)),
        ("7. Feeder schedules", check_feeder_schedules(cfg)),
        ("8. Heartbeat", check_heartbeat(cfg)),
        ("9. Recent attribution", check_recent_attribution(conn)),
        ("10. Disk space (Mac)", check_disk_space()),
    ]


def _print_section(results):
    for name, ok, detail in results:
        print(f"  {_ICONS[ok]} {name:<40} {detail}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Meowant pre-trip readiness checker")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--db", default="meowant.db")
    ap.add_argument("--send-test", action="store_true",
                     help="send a real Telegram test message to every "
                          "configured recipient (default: off, no side effects)")
    args = ap.parse_args(argv)

    cfg = mw_config.load(args.config)
    conn = mw_store.connect(args.db)

    print("Meowant pre-trip readiness check\n")
    critical = 0
    for title, results in run_all_checks(cfg, conn):
        print(title)
        _print_section(results)
        critical += sum(1 for _, ok, _ in results if ok is False)
        print()

    if args.send_test:
        print("Send-test alerts")
        _print_section(send_test_alerts(cfg))
        print()

    if critical:
        print(f"NOT READY ({critical} critical failure{'s' if critical != 1 else ''})")
        return 1
    print("READY FOR TRIP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
