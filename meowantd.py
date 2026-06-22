#!/usr/bin/env python3
"""Run the Meowant SC10 daemon: owns the device, serves the API on :8765."""
import os
import threading
import time

from mw import config, store
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


def main():
    cfg = config.load("config.json")
    conn = store.connect("meowant.db")
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])

    threading.Thread(target=_run_pruner, args=(conn, "."),
                     daemon=True).start()

    device = TuyaDevice(cfg)
    sc = SmartClean(
        idle_seconds=config.get(cfg, "smartclean.idle_seconds", 90),
        max_wait_seconds=config.get(cfg, "smartclean.max_wait_seconds", 480),
        enabled=config.get(cfg, "smartclean.enabled", True))
    daemon = Daemon(device, conn, sc)

    bus = EventBus()
    daemon.on_event = bus.publish
    alerts = Alerts(bus, notify=make_notify(lambda k: config.get(cfg, k)))
    threading.Thread(target=alerts.run, daemon=True).start()

    cams = config.get(cfg, "cameras", [])
    if cams:
        from mw.capture import CaptureService
        from mw.capture_health import CaptureHealth
        cap = CaptureService(
            bus, cams, "gallery/captures",
            frames=config.get(cfg, "capture.frames", 1),
            interval_s=config.get(cfg, "capture.interval_s", 1.5),
            max_frames=config.get(cfg, "capture.max_frames", 12),  # bound disk + agy cost
            # Capture CONTINUOUSLY while the cat is present (not a fixed burst),
            # so brief visitors like Ucok are actually photographed. Reads the
            # daemon's already-maintained dp24 state — no extra device polling.
            presence_fn=lambda: daemon.state.get("24") == "cat_get_in",
            visit_resolver=lambda: store.latest_open_visit_id(conn),
            on_capture=lambda name, path, ts, vid: store.insert_capture(
                conn, ts, vid, name, path, None))
        threading.Thread(target=cap.run, daemon=True).start()
        print(f"capture-service: {len(cams)} camera(s), parallel grab while present "
              f"(≤{cap.max_frames} rounds @ {cap.interval_s}s)")

        # Make capture failures loud: probe the (flaky, on-demand) RTSP sources
        # and flag eliminations that landed zero frames.
        health = CaptureHealth(conn, cams,
                               notify=make_notify(lambda k: config.get(cfg, k)),
                               settle_seconds=config.get(cfg, "capture.settle_seconds", 120))
        threading.Thread(
            target=health.run,
            kwargs={"interval": config.get(cfg, "capture.health_interval_s", 300)},
            daemon=True).start()
        print(f"capture-health: stream probe + missed-capture guard "
              f"(every {config.get(cfg, 'capture.health_interval_s', 300)}s)")

        # Auto-labeler (the 'teacher'): name the cat per visit so the gallery
        # builds itself. agy/antigravity backend (82% vs haiku's 45%); the
        # cross-frame agreement gate only auto-applies UNANIMOUS calls and
        # defers ambiguous visits to human review — the trust channel.
        from mw.autolabel import AutoLabeler, discover_refs
        from mw.labeler import AgyLabeler
        from mw.catfilter import TorchvisionCatFilter
        catfilter = TorchvisionCatFilter()  # shared: cat/no-cat for labels + floor-clear for scatter
        _cats = list(store.gallery_counts(conn).keys())
        autolabeler = AutoLabeler(conn, AgyLabeler(), discover_refs("gallery", _cats), _cats,
                                  catfilter=catfilter)  # drop empties before agy
        threading.Thread(
            target=autolabeler.run,
            kwargs={"interval": config.get(cfg, "autolabel.interval_s", 900)},
            daemon=True).start()
        print("auto-labeler: agy backend + cross-frame gate "
              f"(every {config.get(cfg, 'autolabel.interval_s', 900)}s)")

        from mw.elim_notify import EliminationNotifier
        elim_notifier = EliminationNotifier(
            conn, autolabeler, notify=make_notify(lambda k: config.get(cfg, k)))
        threading.Thread(target=elim_notifier.run, daemon=True).start()
        print("elim-notifier: named 'who used the box' alerts (label-on-leave)")

        # Litter-scatter detector: per-visit floor delta on meowcam3 (pin a clean
        # reference at cat-enter, score post-leave frames) -> 'time to sweep' alert.
        from mw.scatter_detector import ScatterDetector

        def _start_scatter(cam, out_dir, zone_label, threshold, roi=None):
            scat = ScatterDetector(
                bus, conn, cam["url"], out_dir,
                notify=make_notify(lambda k: config.get(cfg, k)),
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

    # Poll interval: 2s (was 3s) to better catch brief visits that fall between
    # polls (e.g. Ucok's in-and-out). Configurable via poll_interval_s.
    t = threading.Thread(target=daemon.run,
                         kwargs={"interval": config.get(cfg, "poll_interval_s", 2.0)},
                         daemon=True)
    t.start()

    from mw.health_watch import HealthWatch, Heartbeat
    hw = HealthWatch(
        conn, make_notify(lambda k: config.get(cfg, k)),
        no_go_hours=config.get(cfg, "health.no_go_hours", 12),
        digest_hour=config.get(cfg, "health.digest_hour", 9),
        interval=config.get(cfg, "health.check_interval_s", 1800))
    threading.Thread(target=hw.run, daemon=True).start()
    print("health-watch: no-go alarm + daily digest")

    hb_url = config.get(cfg, "health.heartbeat_url", "")
    if hb_url:
        hb = Heartbeat(hb_url, interval=config.get(cfg, "health.heartbeat_interval_s", 900))
        threading.Thread(target=hb.run, daemon=True).start()
        print("heartbeat: external dead-man's-switch ping")

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
        bot = TelegramBot(tg_token, tg_chat, {
            "/cats": lambda: report.cat_report(conn),
            "/status": lambda: report.status_report(conn, daemon.state),
            "/health": lambda: report.health_report(conn),
            "/start": lambda: "🐈 Meowant SC10 bot. Commands: /cats /status /health",
        }, label_cb=_label_cb)
        threading.Thread(target=bot.run, daemon=True).start()
        print("telegram-bot: inbound commands (/cats /status /health), owner-allowlisted")
        # Wire the photo-prompt into the notifier (only when both cameras AND Telegram are configured)
        if 'elim_notifier' in locals():
            elim_notifier.ask_who = lambda vid, paths, when: send_label_request(
                tg_token, tg_chat, vid, paths, _valid_cats, when)

    app = create_app(daemon, conn, bus=bus)
    print("meowantd → http://0.0.0.0:8765  (smart-clean idle="
          f"{sc.idle}s, enabled={sc.enabled})")
    app.run(host="0.0.0.0", port=8765, threaded=True)


if __name__ == "__main__":
    main()
