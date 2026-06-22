#!/usr/bin/env python3
"""
meowant.py — local control for the Meowant SC10 automated litter box.

The SC10 is a Tuya v3.5 (AES-GCM) device. All control here is LOCAL over the
LAN (TCP 6668) using a local_key pulled once from the Tuya IoT cloud — no app,
no cloud round-trip, works even if the internet is down.

Usage:
    python3 meowant.py status            # decoded dashboard
    python3 meowant.py raw               # raw DPS dict
    python3 meowant.py watch             # live-stream DPS changes (decode hidden DPs)
    python3 meowant.py clean             # trigger a manual scoop cycle
    python3 meowant.py autoclean on|off  # toggle auto-clean
    python3 meowant.py quiet 22:00 08:00 # set sleep/quiet window
    python3 meowant.py cats              # per-cat report (flicker-collapsed sessions)
    python3 meowant.py refresh-key       # re-pull local_key from Tuya cloud
"""
import json, os, sys, time
from mw.decode import (
    DOCUMENTED, VENDOR, STATUS_VALUES, NOTIFY_BITS, PHASE_VALUES,
    hhmm, decode_bits, decode_dp102,
)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")


def load_config():
    if not os.path.exists(CONFIG):
        sys.exit(f"Missing {CONFIG} — copy config.example.json and fill it in.")
    with open(CONFIG) as f:
        return json.load(f)


def device(cfg):
    import tinytuya
    d = tinytuya.Device(dev_id=cfg["device_id"], address=cfg["address"],
                        local_key=cfg["local_key"], version=float(cfg["version"]))
    d.set_socketPersistent(True)
    return d


def cmd_status(cfg):
    dps = device(cfg).status().get("dps", {})
    g = lambda k: dps.get(str(k))
    print("╔══════════════════ MEOWANT SC10 ══════════════════╗")
    print(f"  status              : {g(24)}")
    print(f"  auto-clean          : {'ON' if g(4) else 'OFF'}")
    print(f"  clean delay         : {g(5)} min after cat leaves")
    print(f"  uses today          : {g(7)}")
    print(f"  sleep active now    : {'yes' if g(10) else 'no'}")
    print(f"  quiet hours         : {hhmm(g(11))} -> {hhmm(g(12))}")
    print(f"  notifications       : {', '.join(decode_bits(g(21), NOTIFY_BITS))}")
    print(f"  faults              : {', '.join(decode_bits(g(22), ['E1','E2','E3','E4','E5']))}")
    print("  -- vendor extras (inferred) --")
    for k, name in VENDOR.items():
        print(f"  dp{k:<3} {name:<20}: {g(k)}")
    print("╚══════════════════════════════════════════════════╝")


def cmd_raw(cfg):
    print(json.dumps(device(cfg).status().get("dps", {}), indent=2, ensure_ascii=False))


def cmd_watch(cfg):
    """Stream DPS changes — the way to decode the undocumented vendor DPs:
    run this, then use the box (or wave a hand at the sensor) and watch which dp flips."""
    d = device(cfg)
    d.set_socketPersistent(True)
    print("Watching for DPS changes (Ctrl-C to stop)…")
    prev = {}
    last_poll = 0
    while True:
        data = d.receive()
        now = time.time()
        # heartbeat poll every 15s so we still see slow changes
        if now - last_poll > 15:
            d.heartbeat()
            data = d.status()
            last_poll = now
        if data and "dps" in data:
            label = lambda k: DOCUMENTED.get(int(k)) or VENDOR.get(int(k), f"dp{k}")
            for k, v in data["dps"].items():
                if prev.get(k) != v:
                    ts = time.strftime("%H:%M:%S")
                    print(f"  [{ts}] dp{k} ({label(k)}): {prev.get(k)!r} -> {v!r}")
                    prev[k] = v


def cmd_monitor(cfg, logfile="cycle_log.tsv"):
    """Long-run passive logger: append every DPS change (with timestamp) to a TSV.
    Leave it running to decode vendor DPs from real cat usage. Ctrl-C to stop."""
    d = device(cfg)
    d.set_socketPersistent(True)
    d.set_socketTimeout(5)   # don't hang forever on a dead socket — error so we reconnect
    path = os.path.join(HERE, logfile)
    label = lambda k: DOCUMENTED.get(int(k)) or VENDOR.get(int(k), f"dp{k}")
    prev = (d.status() or {}).get("dps", {})
    print(f"Monitoring → {path} (Ctrl-C to stop). Baseline captured ({len(prev)} dps).")
    HEARTBEAT = 300  # write a "# alive" line every 5 min so a freeze is detectable
    with open(path, "a") as f:
        f.write(f"# session {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.flush()
        last_hb = 0
        while True:
            try:
                cur = (d.status() or {}).get("dps", {})
            except Exception:
                cur = {}
                try:  # dead socket → rebuild so the next poll reconnects
                    d = device(cfg)
                    d.set_socketPersistent(True)
                    d.set_socketTimeout(5)
                except Exception:
                    pass
            for k, v in cur.items():
                if prev.get(k) != v:
                    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}\tdp{k}\t{label(k)}\t{prev.get(k,'')}\t{v}"
                    print("  " + line)
                    f.write(line + "\n"); f.flush()
                    prev[k] = v
            # heartbeat ONLY on a successful poll → no heartbeat = stuck/offline (detectable)
            now_t = time.time()
            if cur and now_t - last_hb >= HEARTBEAT:
                hb = f"# alive {time.strftime('%Y-%m-%d %H:%M:%S')} dp24={cur.get('24')} ({len(cur)} dps)"
                print("  " + hb); f.write(hb + "\n"); f.flush()
                last_hb = now_t
            time.sleep(2)


def cmd_report(cfg, logfile="cycle_log.tsv"):
    """Summarize cat usage from the monitor/TUI log: visits, cleans, busiest hours."""
    from collections import Counter
    path = os.path.join(HERE, logfile)
    if not os.path.exists(path):
        sys.exit(f"No log at {path} — run `monitor` or the TUI first.")
    rows = []
    for ln in open(path):
        if ln.startswith("#") or not ln.strip():
            continue
        p = ln.rstrip("\n").split("\t")
        if len(p) >= 5:
            rows.append(p[:5])  # ts, dp, code, old, new

    entries, cleans, by_hour = [], 0, Counter()
    weights102, weights101 = [], []
    for ts, dp, code, old, new in rows:
        real = old != "" and old != new  # ignore partial-update artifacts
        if dp == "dp24" and real:
            if new == "cat_get_in":
                entries.append(ts); by_hour[ts[11:13]] += 1
            elif new == "cleaning":
                cleans += 1
        if dp == "dp102" and new:
            v = decode_dp102(new)
            if v: weights102.append((ts, v))
        if dp == "dp101" and real:
            weights101.append(int(new))

    print("╔════════════ SC10 USAGE REPORT ════════════╗")
    if rows:
        print(f"  window     : {rows[0][0]} → {rows[-1][0]}")
    print(f"  cat entries: {len(entries)}")
    print(f"  clean runs : {cleans}")
    if by_hour:
        busy = ", ".join(f"{h}:00 ({n})" for h, n in by_hour.most_common(3))
        print(f"  busiest hrs: {busy}")
    if weights102:
        vals = [v for _, v in weights102]
        print(f"  use records: {vals}  (1 per substantive visit; raw uint16, unit unknown)")
    if weights101:
        print(f"  contents   : load {min(weights101)}–{max(weights101)} raw (litter+waste, during clean)")
    print("╚═══════════════════════════════════════════╝")


def cmd_catreport(cfg, gap_s=30):
    """Per-cat report from the DB, built on SESSIONS (IR-flicker fragments collapsed
    via store.sessions) so the counts are real trips, not sensor-split rows."""
    from collections import Counter
    from datetime import date
    from mw import store
    db = os.path.join(HERE, "meowant.db")
    if not os.path.exists(db):
        sys.exit(f"No DB at {db} — start the daemon first.")
    conn = store.connect(db)
    sess = store.sessions(conn, gap_s=gap_s)
    frames = store.gallery_counts(conn)            # labeled captures per cat
    today = date.today().isoformat()

    by_cat = {}
    for s in sess:
        by_cat.setdefault(s["cat"], []).append(s)

    raw_total = conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
    print("╔══════════════ PER-CAT REPORT ══════════════╗")
    print(f"  {raw_total} raw visit rows → {len(sess)} sessions "
          f"({raw_total - len(sess)} flicker fragments collapsed)")
    for cat in sorted(k for k in by_cat if k):      # named cats first
        rows = by_cat[cat]
        elim = [s for s in rows if s["eliminated"]]
        today_e = sum(1 for s in elim if s["enter_ts"].startswith(today))
        durs = [s["duration_s"] for s in elim if s["duration_s"]]
        hours = Counter(s["enter_ts"][11:13] for s in elim)
        scored = [s for s in rows if s["scatter_severity"] is not None]
        messy = [s for s in scored if s["scatter_severity"] >= 1]
        print(f"\n  ── {cat} ──")
        print(f"    sessions      : {len(rows)}  (eliminations: {len(elim)}, today: {today_e})")
        print(f"    last seen     : {rows[0]['enter_ts']}")
        if durs:
            print(f"    avg elim dur  : {sum(durs)//len(durs)}s")
        if hours:
            busy = ", ".join(f"{h}:xx ({n})" for h, n in hours.most_common(3))
            print(f"    busiest hrs   : {busy}")
        print(f"    gallery frames: {frames.get(cat, 0)}")
        if scored:
            avg = sum(s["scatter_pct"] for s in scored) / len(scored)
            print(f"    scatter       : {len(messy)}/{len(scored)} messy, avg {avg:.2f}% of apron")
    pending = by_cat.get(None, [])
    if pending:
        p_elim = sum(1 for s in pending if s["eliminated"])
        print(f"\n  ── (unattributed: occlusion / vision pending) ──")
        print(f"    sessions      : {len(pending)}  (eliminations: {p_elim})")
    print("╚════════════════════════════════════════════╝")


def cmd_clean(cfg):
    d = device(cfg)
    # dp24 status enum standby|cleaning — setting "cleaning" triggers a scoop.
    print("Triggering manual clean cycle…")
    print(d.set_value(24, "cleaning"))


def cmd_autoclean(cfg, state):
    d = device(cfg)
    on = state.lower() in ("on", "true", "1", "yes")
    print(f"Setting auto-clean {'ON' if on else 'OFF'}…")
    print(d.set_value(4, on))


def cmd_quiet(cfg, start, end):
    def to_min(s):
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    d = device(cfg)
    print(f"Setting quiet hours {start} -> {end}…")
    print(d.set_value(11, to_min(start)))
    print(d.set_value(12, to_min(end)))


def cmd_refresh_key(cfg):
    import tinytuya
    c = cfg["cloud"]
    cloud = tinytuya.Cloud(apiRegion=c["region"], apiKey=c["api_id"], apiSecret=c["api_secret"])
    devs = cloud.getdevices()
    if not isinstance(devs, list):
        sys.exit(f"Cloud error: {devs}")
    for dev in devs:
        if dev.get("id") == cfg["device_id"]:
            cfg["local_key"] = dev["key"]
            with open(CONFIG, "w") as f:
                json.dump(cfg, f, indent=2)
            print(f"Updated local_key for {dev.get('name')} -> {dev['key']}")
            return
    sys.exit("Device not found on cloud account.")


def main():
    cfg = load_config()
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "status":        cmd_status(cfg)
    elif cmd == "raw":         cmd_raw(cfg)
    elif cmd == "watch":       cmd_watch(cfg)
    elif cmd == "monitor":     cmd_monitor(cfg)
    elif cmd == "report":      cmd_report(cfg)
    elif cmd == "cats":        cmd_catreport(cfg)
    elif cmd == "clean":       cmd_clean(cfg)
    elif cmd == "autoclean":   cmd_autoclean(cfg, args[1])
    elif cmd == "quiet":       cmd_quiet(cfg, args[1], args[2])
    elif cmd == "refresh-key": cmd_refresh_key(cfg)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
