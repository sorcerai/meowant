#!/usr/bin/env python3
"""Run the Meowant SC10 daemon: owns the device, serves the API on :8765."""
import threading

from mw import config, store
from mw.alerts import Alerts, make_notify
from mw.bus import EventBus
from mw.daemon import Daemon
from mw.device import TuyaDevice
from mw.smartclean import SmartClean
from mw.api import create_app


def main():
    cfg = config.load("config.json")
    conn = store.connect("meowant.db")
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])

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

        # Litter-scatter detector: per-visit floor delta on meowcam3 (pin a clean
        # reference at cat-enter, score post-leave frames) -> 'time to sweep' alert.
        m3 = next((c for c in cams if c["name"] == "meowcam3"), None)
        if m3 and config.get(cfg, "scatter.enabled", True):
            from mw.scatter_detector import ScatterDetector
            scat = ScatterDetector(
                bus, conn, m3["url"], "gallery/scatter",
                notify=make_notify(lambda k: config.get(cfg, k)),
                presence_fn=lambda: daemon.state.get("24") == "cat_get_in",
                visit_resolver=lambda: store.latest_open_visit_id(conn),
                clear_fn=catfilter.is_clear,   # reject frames with a cat/dog/person on the floor
                threshold=config.get(cfg, "scatter.severity_threshold", 1),
                min_duration_s=config.get(cfg, "scatter.min_duration_s", 20),
                post_leave_delay_s=config.get(cfg, "scatter.post_leave_delay_s", 12))
            threading.Thread(target=scat.run, daemon=True).start()
            print("scatter-detector: meowcam3 floor delta + 'time to sweep' alert")

    # Poll interval: 2s (was 3s) to better catch brief visits that fall between
    # polls (e.g. Ucok's in-and-out). Configurable via poll_interval_s.
    t = threading.Thread(target=daemon.run,
                         kwargs={"interval": config.get(cfg, "poll_interval_s", 2.0)},
                         daemon=True)
    t.start()

    app = create_app(daemon, conn, bus=bus)
    print("meowantd → http://0.0.0.0:8765  (smart-clean idle="
          f"{sc.idle}s, enabled={sc.enabled})")
    app.run(host="0.0.0.0", port=8765, threaded=True)


if __name__ == "__main__":
    main()
