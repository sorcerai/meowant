#!/usr/bin/env python3
"""Run the Meowant SC10 daemon: owns the device, serves the API on :8765."""
import atexit
import os
import signal
import sys
import threading
import time

from mw import config, store

# Cleanup callbacks run on daemon shutdown (SIGTERM from `launchctl kickstart -k`,
# SIGINT in dev). Without this, long-lived child processes — the warm-reader
# ffmpeg — are orphaned to launchd on every reload and pile up.
_CLEANUPS = []


def _shutdown(signum=None, frame=None):
    for fn in _CLEANUPS:
        try:
            fn()
        except Exception:
            pass
    os._exit(0)
from mw.alerts import Alerts, make_notify
from mw.bus import EventBus
from mw.daemon import Daemon
from mw.device import TuyaDevice
from mw.smartclean import SmartClean
from mw.api import create_app


def _run_pruner(conn, gallery_dir, interval_s=86400, startup_delay_s=60):
    """Delete auto-none captures (examined, no cat) from disk + DB daily."""
    time.sleep(startup_delay_s)
    while True:
        paths = store.pop_empty_captures(conn)
        deleted = 0
        for p in paths:
            full = os.path.join(gallery_dir, p) if not os.path.isabs(p) else p
            try:
                os.remove(full)
                deleted += 1
            except FileNotFoundError:
                pass
        if deleted:
            print(f"[pruner] removed {deleted} empty captures")
        time.sleep(interval_s)


def _run_shadow_scorer(scorer, interval_s=600, startup_delay_s=120):
    """Score newly-completed visits with the DINOv2 matcher (shadow — logs only,
    never touches attribution). Isolated: any error is logged and the loop
    continues."""
    time.sleep(startup_delay_s)
    while True:
        try:
            n = scorer.score_new()
            if n:
                print(f"[shadow] scored {n} visit(s)")
        except Exception as e:
            print(f"[shadow] scorer error: {e}", file=sys.stderr)
        time.sleep(interval_s)


def _run_shadow_daily(log_path, cats_by_id, notify, interval_s=86400, startup_delay_s=3600):
    """Send the owner a daily shadow-matcher summary (agreement + disagreements)."""
    from mw import shadow
    time.sleep(startup_delay_s)
    while True:
        try:
            txt = shadow.daily_report(shadow.read_records(log_path), time.time(), cats_by_id)
            notify(txt)
        except Exception as e:
            print(f"[shadow] daily report error: {e}", file=sys.stderr)
        time.sleep(interval_s)


def litterbox_cameras(cameras, bowls):
    """Cameras used for litterbox ID/scatter — everything NOT assigned to a bowl.
    Bowl cams (BowlWatch) must not be captured for litterbox visits: their frames
    never show the box and each one costs a labeler call."""
    bowl_cams = {b["camera"] for b in (bowls or []) if b.get("camera")}
    return [c for c in cameras if c.get("name") not in bowl_cams]


def main():
    cfg = config.load("config.json")
    conn = store.connect("meowant.db")
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])

    # Apply config-driven per-cat thresholds (Settings panel writes these);
    # in-place so health_watch/deadman, which import cat_status.THRESHOLDS, see them.
    from mw import cat_status
    cat_status.load_thresholds(cfg)

    threading.Thread(target=_run_pruner, args=(conn, "."),
                     daemon=True).start()

    device = TuyaDevice(cfg)
    sc = SmartClean(
        idle_seconds=config.get(cfg, "smartclean.idle_seconds", 60),
        max_wait_seconds=config.get(cfg, "smartclean.max_wait_seconds", 240),
        enabled=config.get(cfg, "smartclean.enabled", True))
    daemon = Daemon(device, conn, sc)

    bus = EventBus()
    daemon.on_event = bus.publish
    # Two alert audiences: notify_owner = just the owner (routine/technical pings);
    # notify_all = owner + sitter(s) (important alerts only — cat health + the
    # deadman catch-all). The sitter opted into the important set, not every ping.
    _cfg_get = lambda k: config.get(cfg, k)
    notify_owner = make_notify(_cfg_get, owner_only=True)
    notify_all = make_notify(_cfg_get)
    alerts = Alerts(bus, notify=notify_owner)
    threading.Thread(target=alerts.run, daemon=True).start()

    # Shadow matcher: score completed visits with the DINOv2 gallery matcher and
    # report agreement/disagreements to the OWNER daily — WITHOUT affecting live
    # attribution or alerts. Config-gated + crash-safe (a shadow failure must
    # never destabilize the daemon). Promote to decider only after the trip.
    gallery_matcher = None   # shared DINOv2 matcher; also elim-notify's fast path
    if config.get(cfg, "identify.shadow_enabled", False):
        try:
            from mw.shadow import ShadowScorer
            from mw.identify import make_gallery_matcher
            _gal = config.get(cfg, "identify.gallery_path", "gallery.npz")
            if os.path.exists(_gal):
                _matcher = make_gallery_matcher(_gal)
                gallery_matcher = _matcher
                _slog = config.get(cfg, "identify.shadow_log", "shadow_predictions.jsonl")
                _sstate = config.get(cfg, "identify.shadow_state", "shadow_state.json")
                # Live promotion (identify.live_enabled, OFF by default): the matcher
                # WRITES visits.cat_id on a confident multi-view commit. agy-independent,
                # so a labeler-backend outage no longer blacks out attribution.
                _live = config.get(cfg, "identify.live_enabled", False)
                _scorer = ShadowScorer(
                    conn, _matcher, _slog, _sstate, live=_live,
                    min_views=config.get(cfg, "identify.live_min_views", 2),
                    threshold=config.get(cfg, "identify.live_threshold", 0.0))
                _cats_by_id = {r[0]: r[1] for r in conn.execute("SELECT id,name FROM cats")}
                threading.Thread(target=_run_shadow_scorer,
                                 args=(_scorer, config.get(cfg, "identify.shadow_interval_s", 600)),
                                 daemon=True).start()
                threading.Thread(target=_run_shadow_daily,
                                 args=(_slog, _cats_by_id, notify_owner,
                                       config.get(cfg, "identify.shadow_report_interval_s", 86400)),
                                 daemon=True).start()
                print(f"shadow-matcher: DINOv2 gallery scoring completed visits "
                      f"({'LIVE — writing visits.cat_id' if _live else 'shadow-only'}; owner daily report)")
            else:
                print(f"shadow-matcher: gallery '{_gal}' missing — disabled (run scripts/build_gallery.py)",
                      file=sys.stderr)
        except Exception as e:
            print(f"shadow-matcher: wiring failed ({e}) — continuing without it", file=sys.stderr)

    endpoint = config.get(cfg, "weekly.llm_endpoint")
    timeout = config.get(cfg, "weekly.llm_timeout_s", 120)
    custom_run = None
    if endpoint:
        import requests
        def _custom_run(prompt):
            resp = requests.post(
                endpoint,
                json={"model": "gemma", "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
                timeout=timeout
            )
            resp.raise_for_status()
            message = resp.json()["choices"][0]["message"]
            return message.get("content", "") + " " + message.get("reasoning_content", "")
        custom_run = _custom_run

    # Weekly per-cat consolidation + statistical gatekeeper (chronic-drift
    # report). Phase 2 adds shadow-first LLM classifier/hypothesis layer.
    if config.get(cfg, "weekly.enabled", False):
        from mw.weekly import WeeklyAnalyst
        
        analyst = WeeklyAnalyst(
            conn, notify_owner,
            run=custom_run,
            state_path=config.get(cfg, "weekly.state_path", "weekly_state.json"),
            interval_days=config.get(cfg, "weekly.interval_days", 7),
            min_void_n=config.get(cfg, "weekly.min_void_n", 5),
            shadow=config.get(cfg, "weekly.shadow", True))
        threading.Thread(target=analyst.run, daemon=True).start()
        print("weekly-analyst: per-cat 7d consolidation + LLM gatekeeper (shadow: %s)" % analyst.shadow)

    cams = config.get(cfg, "cameras", [])
    litter_cams = litterbox_cameras(cams, config.get(cfg, "bowls", []))
    catfilter = None   # shared SSDLite cat detector; created by first consumer
    if litter_cams:
        from mw.capture import CaptureService, ffmpeg_grab, http_grab
        from mw.capture_health import CaptureHealth
        # Grab source, in order of preference:
        #  1. snapshot sidecar  — GET <base>/<cam>.jpg (warm cached frame, no RTSP)
        #  2. local warm readers — one persistent ffmpeg per cam keeps the stream
        #     hot and writes the latest frame; capture copies it (the fix for the
        #     shared-redroid publisher choking on 6 cold RTSP opens every ~1.5s)
        #  3. plain rtsp/ffmpeg cold-open per grab (legacy fallback)
        # All switchable via config — no code deploy.
        snap_base = config.get(cfg, "capture.snapshot_base", "")
        warm = config.get(cfg, "capture.warm_readers", True)
        warm_pool = None
        if snap_base:
            grabber = http_grab
            base = snap_base.rstrip("/")
            litter_cams = [{**c, "url": f"{base}/{c['name']}.jpg"} for c in litter_cams]
            src = "http-sidecar"
        elif warm:
            from mw.warmreader import WarmReaderPool, file_grab
            warm_pool = WarmReaderPool(
                litter_cams, "warm_frames",
                fps=config.get(cfg, "capture.warm_fps", 1.0),
                # Offload decode to the media engine (Mac Studio: videotoolbox)
                # so 4 steady readers cost ~no CPU. "" disables.
                hwaccel=config.get(cfg, "capture.warm_hwaccel", "videotoolbox") or None)
            threading.Thread(target=warm_pool.run, daemon=True).start()
            _CLEANUPS.append(warm_pool.stop)   # terminate ffmpeg children on shutdown
            grabber = file_grab
            litter_cams = [{**c, "url": warm_pool.frame_path(c["name"])}
                           for c in litter_cams]
            src = "warm-readers"
            print(f"warm-readers: {len(litter_cams)} persistent ffmpeg @ "
                  f"{config.get(cfg, 'capture.warm_fps', 1.0)}fps "
                  f"(hwaccel={config.get(cfg, 'capture.warm_hwaccel', 'videotoolbox')}) "
                  f"-> warm_frames/")
        else:
            grabber = ffmpeg_grab
            src = "rtsp/ffmpeg"
        from mw.imgutil import is_grayscale

        cap = CaptureService(
            bus, litter_cams, "gallery/captures", grabber=grabber,
            frames=config.get(cfg, "capture.frames", 1),
            interval_s=config.get(cfg, "capture.interval_s", 1.5),
            max_frames=config.get(cfg, "capture.max_frames", 12),  # bound disk + agy cost
            # Bound simultaneous grabs: 5 of 6 cams share one redroid publisher,
            # so firing all of them at once caused exit-8/timeouts and could wedge
            # the stack. Cap concurrency + retry transient failures with backoff.
            max_concurrent=config.get(cfg, "capture.max_concurrent", 2),
            grab_retries=config.get(cfg, "capture.grab_retries", 1),
            retry_backoff_s=config.get(cfg, "capture.retry_backoff_s", 0.5),
            # Capture CONTINUOUSLY while the cat is present (not a fixed burst),
            # so brief visitors like Ucok are actually photographed. Reads the
            # daemon's already-maintained dp24 state — no extra device polling.
            presence_fn=lambda: daemon.state.get("24") == "cat_get_in",
            visit_resolver=lambda: store.latest_open_visit_id(conn),
            on_capture=lambda name, path, ts, vid: store.insert_capture(
                conn, ts, vid, name, path, is_grayscale(path)))
        threading.Thread(target=cap.run, daemon=True).start()
        print(f"capture-service: {len(litter_cams)} camera(s) via {src}, "
              f"≤{cap.max_concurrent} concurrent, {cap.grab_retries} retr(y/ies), "
              f"≤{cap.max_frames} rounds @ {cap.interval_s}s")

        from mw.remediation import Remediator
        remediator = Remediator(
            conn, notify_owner,
            max_per_window=config.get(cfg, "remediation.max_per_window", 3),
            window_s=config.get(cfg, "remediation.window_s", 3600))
        # Make capture failures loud AND remediated: probe streams, guard missed
        # captures, and route detections through the deterministic playbooks
        # (debounce streams, diagnose labeler stalls) -> incidents table + escalate.
        health = CaptureHealth(conn, litter_cams,
                               notify=notify_owner,
                               settle_seconds=config.get(cfg, "capture.settle_seconds", 120),
                               # Warm-frame blackout guard: only meaningful when warm
                               # readers actually started (they write warm_frames/<cam>.jpg).
                               # Gate on warm_pool, NOT the `warm` config flag — snapshot_base
                               # mode skips the reader branch even with warm=True (the default),
                               # which would otherwise stat missing files and false-alarm forever.
                               warm_dir=("warm_frames" if warm_pool is not None else None),
                               warm_stale_seconds=config.get(cfg, "capture.warm_stale_seconds", 180),
                               remediator=remediator)
        threading.Thread(
            target=health.run,
            kwargs={"interval": config.get(cfg, "capture.health_interval_s", 300)},
            daemon=True).start()
        print(f"capture-health: stream probe + missed-capture guard + warm-frame "
              f"blackout guard (every {config.get(cfg, 'capture.health_interval_s', 300)}s)")

        # Auto-labeler (the 'teacher'): name the cat per visit so the gallery
        # builds itself. agy/antigravity backend (82% vs haiku's 45%); the
        # cross-frame agreement gate only auto-applies UNANIMOUS calls and
        # defers ambiguous visits to human review — the trust channel.
        from mw.autolabel import AutoLabeler, discover_refs
        from mw.labeler import AgyLabeler, LlamaCppLabeler, FallbackLabeler
        from mw.catfilter import TorchvisionCatFilter
        catfilter = TorchvisionCatFilter()  # shared: cat/no-cat for labels + floor-clear for scatter
        _cats = list(store.gallery_counts(conn).keys())
        autolabeler = AutoLabeler(conn, FallbackLabeler(AgyLabeler(timeout=45), LlamaCppLabeler()), discover_refs("gallery", _cats), _cats,
                                  catfilter=catfilter)  # drop empties before agy
        threading.Thread(
            target=autolabeler.run,
            kwargs={"interval": config.get(cfg, "autolabel.interval_s", 900)},
            daemon=True).start()
        print("auto-labeler: agy backend + cross-frame gate "
              f"(every {config.get(cfg, 'autolabel.interval_s', 900)}s)")

        from mw.elim_notify import EliminationNotifier
        elim_notifier = EliminationNotifier(
            conn, autolabeler, notify=notify_owner,
            pee_threshold=config.get(cfg, "alerts.pee_threshold", 80),
            poop_threshold=config.get(cfg, "alerts.poop_threshold", 130),
            enabled=config.get(cfg, "alerts.notify_eliminations", True),
            # Local matcher beats a saturated agy for the fast ID; catfilter
            # distinguishes "unknown cat in frame" (ask human, photos help)
            # from "globe tipped closed" (say so, photos are a white ball).
            matcher=gallery_matcher, catfilter=catfilter,
            min_views=config.get(cfg, "identify.live_min_views", 2),
            threshold=config.get(cfg, "identify.live_threshold", 0.0))
        threading.Thread(target=elim_notifier.run, daemon=True).start()
        print("elim-notifier: named 'who used the box' alerts (label-on-leave)")

        # Invariant canary (self-heal C5): cross-check raw eliminations vs
        # attributed ones; fire if the labeler is silently dropping health events.
        if config.get(cfg, "canary.enabled", True):
            from mw.invariant_canary import InvariantCanary
            canary = InvariantCanary(
                conn, notify_owner,
                window_hours=config.get(cfg, "canary.window_hours", 48),
                grace_hours=config.get(cfg, "canary.grace_hours", 2),
                min_sample=config.get(cfg, "canary.min_sample", 4),
                min_ratio=config.get(cfg, "canary.min_ratio", 0.5),
                interval=config.get(cfg, "canary.interval_s", 3600))
            threading.Thread(target=canary.run, daemon=True).start()
            print("invariant-canary: raw-vs-attributed elimination check")



        # Litter-scatter detector: per-visit floor delta on meowcam3 (pin a clean
        # reference at cat-enter, score post-leave frames) -> 'time to sweep' alert.
        from mw.scatter_detector import ScatterDetector

        def _start_scatter(cam, out_dir, zone_label, threshold, roi=None):
            # Reuse the warm reader's hot frames instead of cold-opening RTSP per
            # grab — meowcam3/4 are litter cams, so a warm reader already exists.
            if warm_pool is not None:
                from mw.warmreader import file_grab
                s_grabber, s_url = file_grab, warm_pool.frame_path(cam["name"])
            else:
                s_grabber, s_url = ffmpeg_grab, cam["url"]
            scat = ScatterDetector(
                bus, conn, s_url, out_dir, grabber=s_grabber,
                notify=notify_owner,
                presence_fn=lambda: daemon.state.get("24") == "cat_get_in",
                visit_resolver=lambda: store.latest_open_visit_id(conn),
                clear_fn=catfilter.is_clear,   # reject frames with a cat/dog/person on the floor
                min_duration_s=config.get(cfg, "scatter.min_duration_s", 20),
                post_leave_delay_s=config.get(cfg, "scatter.post_leave_delay_s", 12),
                threshold=threshold, roi=roi, zone_label=zone_label)
            threading.Thread(target=scat.run, daemon=True).start()
            print(f"scatter-detector: {cam['name']} -> {zone_label} (threshold {threshold})")

        m3 = next((c for c in cams if c["name"] == "meowcam3"), None)
        if m3 and config.get(cfg, "scatter.enabled", True):
            _start_scatter(m3, "gallery/scatter", "the apron",
                           config.get(cfg, "scatter.severity_threshold", 1))

        # 2nd zone: meowcam4 covers Garfield's preferred fling spot. Threshold left
        # conservative (2) until its clean/messy reference is calibrated post-sweep.
        m4 = next((c for c in cams if c["name"] == "meowcam4"), None)
        if m4 and config.get(cfg, "scatter.m4_enabled", True):
            _start_scatter(m4, "gallery/scatter_m4", "Garfield's fling zone",
                           config.get(cfg, "scatter.m4_severity_threshold", 2),
                           roi=tuple(config.get(cfg, "scatter.m4_roi",
                                                 [0.22, 0.48, 0.62, 0.95])))

        # Passive cat-frame collector (off by default; needs warm readers + a
        # mounted external drive). A setup failure (e.g. drive not mounted ->
        # os.makedirs raises) must NEVER crash the daemon and take litterbox
        # monitoring down — it's optional data collection.
        harvest_dir = (config.get(cfg, "capture.harvest_dir", "") or "").strip()
        harvest_on = config.get(cfg, "capture.harvest_enabled", False)
        if harvest_dir and harvest_on and warm_pool is not None:
            try:
                from mw.harvester import Harvester
                harvester = Harvester(
                    litter_cams, warm_pool, catfilter, harvest_dir,
                    interval_s=config.get(cfg, "capture.harvest_interval_s", 5.0),
                    retention=config.get(cfg, "capture.harvest_retention", 20000))
                threading.Thread(target=harvester.run, daemon=True).start()
                # Signal the harvester to stop before warm readers on shutdown (best-effort;
                # stop() sets a flag, not a join).
                _CLEANUPS.insert(0, harvester.stop)
                print(f"harvester: passive cat-frame collection -> {harvest_dir}")
            except Exception as e:
                print(f"harvester: DISABLED — setup failed ({e}); "
                      f"litterbox monitoring continues", file=sys.stderr)
        elif harvest_dir and harvest_on and warm_pool is None:
            print("harvester: DISABLED — needs warm readers (capture.warm_readers), "
                  "but grab source is not warm-readers", file=sys.stderr)

        # Camera self-heal watchdog: a cam whose stream wedges (warm frame goes
        # 0-byte/undecodable/stale for a sustained window) gets SSH-restarted at
        # the source (service restart prudynt -> reboot). Off unless configured.
        # Crash-safe: a setup failure must not take the daemon down.
        if config.get(cfg, "cam_watchdog.enabled", False) and warm_pool is not None:
            try:
                from mw.cam_watchdog import CamWatchdog, make_ssh_restart, frame_healthy
                _wcam = config.get(cfg, "cam_watchdog.cam", "meowcam4")
                _whost = config.get(cfg, "cam_watchdog.ssh_host")
                _wpw = config.get(cfg, "cam_watchdog.ssh_password")
                if _whost and _wpw:
                    _wfp = warm_pool.frame_path(_wcam)
                    _wmax = config.get(cfg, "cam_watchdog.max_age_s", 300)
                    wdog = CamWatchdog(
                        _wcam,
                        is_healthy_fn=lambda: frame_healthy(_wfp, _wmax),
                        restart_fn=make_ssh_restart(
                            _whost, config.get(cfg, "cam_watchdog.ssh_user", "root"), _wpw),
                        notify=notify_owner,
                        fail_grace_s=config.get(cfg, "cam_watchdog.fail_grace_s", 300),
                        cooldown_s=config.get(cfg, "cam_watchdog.cooldown_s", 1800))
                    threading.Thread(target=wdog.run, daemon=True).start()
                    _CLEANUPS.append(wdog.stop)
                    print(f"cam-watchdog: {_wcam} self-heal via ssh {_whost}")
                else:
                    print("cam-watchdog: DISABLED — needs cam_watchdog.ssh_host + ssh_password",
                          file=sys.stderr)
            except Exception as e:
                print(f"cam-watchdog: DISABLED — setup failed ({e})", file=sys.stderr)

    # Poll interval: 2s (was 3s) to better catch brief visits that fall between
    # polls (e.g. Ucok's in-and-out). Configurable via poll_interval_s.
    t = threading.Thread(target=daemon.run,
                         kwargs={"interval": config.get(cfg, "poll_interval_s", 2.0)},
                         daemon=True)
    t.start()

    from mw.health_watch import HealthWatch, Heartbeat
    hw = HealthWatch(
        conn, notify_all,            # cat-health alerts -> owner + sitter (important)
        run_llm=custom_run,
        no_go_hours=config.get(cfg, "health.no_go_hours", 12),
        digest_hour=config.get(cfg, "health.digest_hour", 9),
        interval=config.get(cfg, "health.check_interval_s", 1800),
        quiet_start=config.get(cfg, "quiet_start", "22:00"),
        quiet_end=config.get(cfg, "quiet_end", "08:00"))
    threading.Thread(target=hw.run, daemon=True).start()
    print("health-watch: no-go alarm + daily digest")

    hb_url = config.get(cfg, "health.heartbeat_url", "")
    if hb_url:
        hb = Heartbeat(hb_url, interval=config.get(cfg, "health.heartbeat_interval_s", 900))
        threading.Thread(target=hb.run, daemon=True).start()
        print("heartbeat: external dead-man's-switch ping")

    from mw.box_health import BoxHealthWatch
    # notify_all: box faults/bin-full/UNUSABLE are service calls — the sitter
    # is the hands during a trip, not just the owner's FYI.
    bhw = BoxHealthWatch(
        conn, notify_all,
        interval=config.get(cfg, "box_health.check_interval_s", 900),
        renag_hours=config.get(cfg, "box_health.renag_hours", 3),
        unusable_hours=config.get(cfg, "box_health.unusable_hours", 6),
        approaching_margin=config.get(cfg, "box_health.approaching_margin", 2))
    threading.Thread(target=bhw.run, daemon=True).start()
    print("box-health: bin-full re-nag + UNUSABLE escalation + approaching-full heads-up")

    # Jam detection: box logs eliminated visits, cameras never see a cat ->
    # drum likely stuck with fault-free firmware (dp22=0), and the deadman is
    # being pacified by phantom eliminations. Camera-based, so it stays useful
    # exactly when the box's own fault reporting is blind.
    if config.get(cfg, "jam_watch.enabled", True):
        from mw.jam_watch import JamWatch
        if catfilter is None:
            from mw.catfilter import TorchvisionCatFilter
            catfilter = TorchvisionCatFilter()
        jw = JamWatch(
            conn, catfilter, notify_all,   # jammed box needs hands on site
            k=config.get(cfg, "jam_watch.k", 6),
            frames_per_visit=config.get(cfg, "jam_watch.frames_per_visit", 8),
            interval=config.get(cfg, "jam_watch.check_interval_s", 600))
        threading.Thread(target=jw.run, daemon=True).start()
        print(f"jam-watch: phantom-visit cross-check (K={jw.k}, every {jw.interval}s)")

    # Litter-level watch: dp101 load cell, standby samples only. Sitters get a
    # top-up ping instead of us discovering a bare pan on camera (Jul 1).
    if config.get(cfg, "litter.watch_enabled", True):
        from mw.litter_watch import LitterWatch
        lw = LitterWatch(
            conn, lambda: daemon.state, notify_all,
            low_threshold=config.get(cfg, "litter.low_threshold", 110),
            consecutive=config.get(cfg, "litter.consecutive_low", 3),
            rearm_margin=config.get(cfg, "litter.rearm_margin", 40),
            interval=config.get(cfg, "litter.sample_interval_s", 300),
            log_path=config.get(cfg, "litter.load_log", "litter_load.jsonl"))
        threading.Thread(target=lw.run, daemon=True).start()
        print(f"litter-watch: dp101 low-litter ping (<{lw.low_threshold}, "
              f"{lw.consecutive}x{lw.interval}s sustained)")

    # Feeder (Phase 1): local Tuya control + dispense logging + watchdogs.
    feeder_devs = {}
    feeder_monitors = {}
    
    feeders_cfg = config.get(cfg, "feeders", [])
    if config.get(cfg, "feeder"):  # back-compat
        f_cfg = config.get(cfg, "feeder")
        f_cfg["label"] = f_cfg.get("label", "downstairs")
        feeders_cfg.append(f_cfg)
        
    if feeders_cfg:
        from mw.feeder import FeederDevice, FeederMonitor
        for f_cfg in feeders_cfg:
            if not f_cfg.get("enabled", True) or not f_cfg.get("device_id"):
                continue
            lbl = f_cfg.get("label", "default")
            if not f_cfg.get("address") or not f_cfg.get("local_key"):
                print(f"feeder '{lbl}': missing address or local_key, skipping", file=sys.stderr)
                continue
            f_dev = FeederDevice(f_cfg)
            f_mon = FeederMonitor(
                f_dev, conn, notify_owner,
                mealtimes=f_cfg.get("mealtimes", []),
                poll_interval_s=f_cfg.get("poll_interval_s", 120),
                miss_grace_minutes=f_cfg.get("miss_grace_minutes", 30),
                offline_minutes=f_cfg.get("offline_minutes", 30),
                low_food_levels=f_cfg.get("low_food_levels", ["empty", "low"]))
            threading.Thread(target=f_mon.run, daemon=True).start()
            feeder_devs[lbl] = f_dev
            feeder_monitors[lbl] = f_mon
            print(f"feeder '{lbl}': local control + dispense logging + watchdogs")

    if config.get(cfg, "random_probe.enabled", False):
        from mw.random_probe import RandomProbe
        probe = RandomProbe(
            feeder_devs, feeder_monitors,
            min_hours=config.get(cfg, "random_probe.min_hours", 2.0),
            max_hours=config.get(cfg, "random_probe.max_hours", 5.0),
            start_hour=config.get(cfg, "random_probe.start_hour", 8),
            end_hour=config.get(cfg, "random_probe.end_hour", 22)
        )
        threading.Thread(target=probe.run, daemon=True).start()
        print(f"random-probe: drops 1 portion every {config.get(cfg, 'random_probe.min_hours', 2)}-{config.get(cfg, 'random_probe.max_hours', 5)}h to learn habits")

    # Bowl camera (Phase 2): full/empty vision -> refill alert / auto-feed.
    cams = config.get(cfg, "cameras", [])
    bowls_cfg = config.get(cfg, "bowls", [])
    if config.get(cfg, "bowl"): # back-compat
        b_cfg = config.get(cfg, "bowl")
        b_cfg["location"] = b_cfg.get("location", "downstairs")
        b_cfg["feeder_label"] = b_cfg.get("feeder_label", "downstairs")
        bowls_cfg.append(b_cfg)
        
    if bowls_cfg:
        from mw.bowl_watch import BowlWatch
        from mw.bowl_tracker import BowlTracker
        from mw.bowl import DEFAULT_ROI
        from mw.capture import ffmpeg_grab
        from mw.autolabel import discover_refs
        os.makedirs("gallery/bowl", exist_ok=True)
        
        _cats = list(store.gallery_counts(conn).keys())
        bowl_refs = discover_refs("gallery", _cats)

        for b_cfg in bowls_cfg:
            b_enabled = b_cfg.get("enabled", True)
            b_cam_name = b_cfg.get("camera")
            b_ref = b_cfg.get("empty_ref_path", "")
            cam_conf = next((c for c in cams if c["name"] == b_cam_name), None)
            
            if b_enabled and cam_conf and b_ref and os.path.exists(b_ref):
                loc = b_cfg.get("location", "downstairs")
                
                # Bind url locally for lambda
                def _make_grab(url, loc_name):
                    def _grab():
                        try:
                            return ffmpeg_grab(url, f"gallery/bowl/latest_{loc_name}.jpg")
                        except Exception:
                            return None
                    return _grab
                
                auto = b_cfg.get("auto_feed", False)
                f_label = b_cfg.get("feeder_label", loc)
                paired_feeder = feeder_devs.get(f_label) if auto else None
                
                bw = BowlWatch(
                    _make_grab(cam_conf["url"], loc), catfilter, conn,
                    notify_owner,
                    feeder=paired_feeder,
                    empty_ref=b_ref,
                    roi=tuple(b_cfg.get("roi", list(DEFAULT_ROI))),
                    empty_max=b_cfg.get("empty_max", 5.0),
                    full_min=b_cfg.get("full_min", 20.0),
                    poll_interval_s=b_cfg.get("poll_interval_s", 1200),
                    auto_feed=auto,
                    auto_feed_portions=b_cfg.get("auto_feed_portions", 1),
                    auto_feed_max_per_day=b_cfg.get("auto_feed_max_per_day", 4),
                    location=loc)
                paired_monitor = feeder_monitors.get(f_label)
                if paired_monitor:
                    paired_monitor.bowl_watch = bw
                threading.Thread(target=bw.run, daemon=True).start()
                print(f"bowl-watch '{loc}': full/empty vision + refill/auto-feed")

                bt = BowlTracker(
                    _make_grab(cam_conf["url"], loc + "_tracker"), catfilter, bowl_refs, conn,
                    notify_owner,
                    location=loc,
                    poll_interval_s=config.get(cfg, "bowl_tracker.poll_interval_s", 5))
                threading.Thread(target=bt.run, daemon=True).start()
                print(f"bowl-tracker '{loc}': eating session tracking")

    # Inbound Telegram commands (/cats /status /health) — allowlisted to the owner
    # chat. Only starts if Telegram creds are configured.
    tg_token = config.get(cfg, "alerts.telegram_bot_token")
    tg_chat = config.get(cfg, "alerts.telegram_chat_id")
    if tg_token and tg_chat:
        from mw.telegram_bot import TelegramBot, send_label_request
        from mw import report
        _valid_cats = [c for c in store.gallery_counts(conn).keys()]
        def _label_cb(vid, cat):
            if cat == "none":
                store.human_mark_no_cat(conn, vid)
                return f"🚫 Visit {vid}: marked no cat (not counted as a real use)"
            cid = store.cat_id_by_name(conn, cat)
            if cid and store.human_attribute_visit(conn, vid, cid):
                return f"✓ Visit {vid} labeled {cat}"
            return f"⚠️ Couldn't label visit {vid} as {cat}"
        def _do_feed(arg):
            try:
                parts = arg.split()
                if not parts:
                    return "Usage: /feed [location] <portions>"
                
                if len(parts) >= 2 and not parts[0].isdigit():
                    lbl = parts[0]
                    n_str = parts[1]
                else:
                    lbl = list(feeder_devs.keys())[0] if feeder_devs else "downstairs"
                    n_str = parts[0]

                n = int(n_str)
            except ValueError:
                return "Usage: /feed [location] <portions>"
            n = max(1, min(50, n))
            
            dev = feeder_devs.get(lbl)
            mon = feeder_monitors.get(lbl)
            if dev and dev.feed(n):
                if mon: mon.note_manual_feed()
                return f"🍽️ Dispensed {n} portion(s) to '{lbl}'."
            return f"⚠️ Feed command failed (feeder '{lbl}' unreachable or not found)."
            
        bot = TelegramBot(tg_token, tg_chat, {
            **({"/feed": (lambda arg="": _do_feed(arg)),
                "/feedstatus": (lambda: "\n\n".join(f"[{lbl}]\n{report.feed_status_text(conn, dev.status())}" for lbl, dev in feeder_devs.items()))}
               if feeder_devs else {}),
            "/cats": lambda: report.cat_report(conn),
            "/status": lambda: report.status_report(conn, daemon.state),
            "/health": lambda: report.health_report(conn),
            "/incidents": lambda: report.incidents_report(conn),
            "/bowl": lambda: report.bowl_status_text(conn),
            "/weekly": lambda: report.weekly_status_text(conn),
            "/start": lambda: "🐈 Meowant SC10 bot. Commands: /cats /status /health /incidents /feed /feedstatus /bowl /weekly",
        }, label_cb=_label_cb,
            load_offset=lambda: store.get_daemon_state(conn, "telegram.offset", 0),
            save_offset=lambda o: store.set_daemon_state(conn, "telegram.offset", o),
            # Sitter(s) get read-only query access; /feed (and any future action)
            # stays owner-only by being absent from readonly_cmds.
            extra_chat_ids=config.get(cfg, "alerts.telegram_chat_ids", []),
            readonly_cmds=["/cats", "/status", "/health", "/incidents",
                           "/bowl", "/weekly", "/feedstatus", "/start"])
        threading.Thread(target=bot.run, daemon=True).start()
        _sitters = config.get(cfg, "alerts.telegram_chat_ids", [])
        print(f"telegram-bot: inbound commands, owner-allowlisted"
              + (f" + {len(_sitters)} sitter(s) read-only" if _sitters else ""))
        # Wire the photo-prompt into the notifier (only when both cameras AND Telegram are configured)
        if 'elim_notifier' in locals():
            elim_notifier.ask_who = lambda vid, paths, when, waste="": send_label_request(
                tg_token, tg_chat, vid, paths, _valid_cats, when, waste)

    app = create_app(daemon, conn, bus=bus, feeders=feeder_devs, monitors=feeder_monitors,
                     config_path="config.json")
    # Reap child processes (warm-reader ffmpeg) on reload/exit so they don't orphan.
    atexit.register(_shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    print("meowantd → http://0.0.0.0:8765  (smart-clean idle="
          f"{sc.idle}s, enabled={sc.enabled})")
    app.run(host="0.0.0.0", port=8765, threaded=True)


if __name__ == "__main__":
    main()
