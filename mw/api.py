"""Read/command HTTP API over the daemon's current state + store."""
import json
import queue
import os
import time

from flask import Flask, Response, jsonify, request, send_from_directory

from mw import decode, store

POLL_INTERVAL = 3.0

_static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


def _decode_state(dps):
    g = lambda k: dps.get(str(k))
    return {
        "status": g(24),
        "auto_clean": bool(g(4)),
        "delay_clean_time": g(5),
        "uses_today": g(7),
        "sleep_active": bool(g(10)),
        "quiet_start": decode.hhmm(g(11)),
        "quiet_end": decode.hhmm(g(12)),
        "bin_full": bool((g(21) or 0) & 1),
        "faults": decode.decode_bits(g(22), ["E1", "E2", "E3", "E4", "E5"]),
        "phase": g(107),
        "raw": dps,
        "named": decode.named(dps),
    }


def _default_reload():
    """Restart the daemon out-of-band so the HTTP response (which runs INSIDE the
    daemon) flushes first — sleep, then kickstart kills+restarts this process."""
    import subprocess
    uid = os.getuid()
    subprocess.Popen(
        ["sh", "-c", f"sleep 1; launchctl kickstart -k gui/{uid}/com.meowant.daemon"])


def create_app(daemon, conn, bus=None, feeders=None, monitors=None,
               gallery_dir="gallery", config_path="config.json", reload_fn=None):
    app = Flask(__name__, static_folder=_static_dir, static_url_path="/static")
    gallery_abs = os.path.abspath(gallery_dir)
    reload_fn = reload_fn or _default_reload

    @app.get("/config")
    def get_config():
        from mw import config_write
        try:
            with open(config_path) as f:
                cfg = json.load(f)
        except Exception as e:
            return jsonify({"error": f"config unreadable: {e}"}), 500
        return jsonify(config_write.read_safe(cfg))

    @app.post("/config")
    def post_config():
        from mw import config_write
        edits = request.get_json(silent=True) or {}
        try:
            applied = config_write.apply_edits(config_path, edits)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:
            return jsonify({"ok": False, "error": f"write failed: {e}"}), 500
        # Write succeeded — the config IS saved. If the detached restart fails to
        # launch, don't 500 a successful save; warn so the owner can restart.
        try:
            reload_fn()   # detached restart; response flushes before the process dies
        except Exception as e:
            return jsonify({"ok": True, "applied": applied,
                            "warning": f"saved, but reload failed ({e}) — restart the daemon"})
        return jsonify({"ok": True, "applied": applied})

    @app.get("/")
    def index():
        return send_from_directory(_static_dir, "index.html")

    @app.get("/gallery/<path:p>")
    def gallery(p):
        # Serve cat photos from gallery/. send_from_directory uses safe_join,
        # which rejects path-traversal (../) — a 404 rather than escaping the dir.
        return send_from_directory(gallery_abs, p)

    @app.get("/state")
    def state():
        st = _decode_state(daemon.state)
        # dp7 (firmware counter) is unreliable — count from OUR tracking instead
        st["uses_today"] = store.eliminations_today(conn)
        st["uses_today_dp7"] = st["raw"].get("7")  # keep the box's claim for reference
        last_ok = daemon.last_ok_ts
        st["last_ok_ts"] = last_ok
        st["stale"] = (last_ok is None
                       or time.time() - last_ok > 2 * POLL_INTERVAL)
        return jsonify(st)

    @app.get("/visits")
    def visits():
        limit = int(request.args.get("limit", 20))
        return jsonify(store.recent_visits(conn, limit))

    @app.get("/cats")
    def cats():
        from mw import cat_status as _cat_status
        rows = _cat_status.cat_status(conn)
        # Fetch once and index by cat name — newest-first order means first hit per name is latest
        sessions = store.recent_bowl_sessions(conn, limit=50)
        latest_by_cat = {}
        for s in sessions:
            if s["cat"] and s["cat"] not in latest_by_cat:
                latest_by_cat[s["cat"]] = s
        for r in rows:
            mine = latest_by_cat.get(r["name"])
            r["last_ate"] = (
                {"ts": mine["ts"], "location": mine["location"], "duration_s": mine["duration_s"]}
                if mine else None
            )
        return jsonify(rows)

    @app.post("/command")
    def command():
        body = request.get_json(force=True) or {}
        action = body.get("action")
        if action == "clean":
            try:
                daemon.device.clean()
                daemon.smartclean.notify_cleaned()  # avoid post-manual double scoop
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        elif action == "autoclean":
            try:
                daemon.device.set_value(4, bool(body.get("value")))
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        elif action == "delay":
            raw = body.get("value")
            if raw is None:
                return jsonify({"ok": False, "error": "missing value"}), 400
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "value must be an integer"}), 400
            try:
                daemon.device.set_value(5, max(1, min(60, value)))
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        elif action == "sleep":
            try:
                daemon.device.set_value(10, bool(body.get("value")))
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        elif action == "quiet":
            val = body.get("value") or {}
            try:
                def _mins(s):
                    h, m = str(s).split(":")
                    return int(h) * 60 + int(m)
                start, end = _mins(val["start"]), _mins(val["end"])
            except (KeyError, ValueError, AttributeError, TypeError):
                return jsonify({"ok": False, "error": "value must be {start:'HH:MM', end:'HH:MM'}"}), 400
            try:
                daemon.device.set_value(11, start)
                daemon.device.set_value(12, end)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        elif action == "feed":
            label = body.get("feeder")
            if not feeders or label not in feeders:
                return jsonify({"ok": False, "error": f"unknown feeder {label}"}), 400
            try:
                portions = max(1, min(10, int(body.get("portions", 1))))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "portions must be an integer"}), 400
            try:
                ok = feeders[label].feed(portions)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
            if not ok:
                return jsonify({"ok": False, "error": "feeder unreachable"}), 500
            if monitors and label in monitors:
                monitors[label].note_manual_feed()
        else:
            return jsonify({"ok": False, "error": f"unknown action {action}"}), 400
        return jsonify({"ok": True})

    @app.get("/boxhealth")
    def boxhealth():
        full_since = store.bin_full_since(conn)
        cap = store.bin_fill_capacity(conn)
        last_clear = store.last_bin_clear_ts(conn)
        cleans = store.cleans_since(conn, last_clear) if last_clear is not None else None
        left = max(0, cap - cleans) if (cap is not None and cleans is not None) else None
        st = _decode_state(daemon.state)
        return jsonify({
            "bin_full_since": full_since,
            "capacity": cap,
            "cleans_since_empty": cleans,
            "est_cleans_left": left,
            "auto_clean": st["auto_clean"],
            "faults": st["faults"],
        })

    @app.get("/bowls")
    def bowls():
        # TODO Phase 2: read locations from config
        out = []
        for loc in ("downstairs", "upstairs"):
            out.append({
                "location": loc,
                "state": store.last_bowl_state(conn, location=loc),
                "last_consumption_secs": store.last_consumption_secs(conn, location=loc),
                "auto_feeds_today": store.auto_feeds_today(conn, location=loc),
            })
        return jsonify(out)

    @app.get("/feeders")
    def feeders_route():
        # TODO Phase 2: read feeder labels from config
        out = []
        for label in ("downstairs", "upstairs"):
            meals, _ = store.feed_events_today(conn, feeder=label)
            lf = store.last_feed_event_ts(conn, feeder=label)  # epoch float or None
            out.append({
                "label": label,
                # emit ISO like every other timestamp on the API (was epoch float)
                "last_feed_ts": store._iso(lf) if lf is not None else None,
                "today_count": meals,
            })
        return jsonify(out)

    @app.get("/cat/<name>")
    def cat_detail(name):
        import glob as _glob
        from mw import cat_status as _cs

        rows = {r["name"]: r for r in _cs.cat_status(conn)}
        if name not in rows:
            return jsonify({"error": f"unknown cat {name}"}), 404

        cat_id = store.cat_id_by_name(conn, name)

        # Litter visits for this cat (filter by cat_id, which is what recent_visits rows carry).
        # Guard against cat_id=None: that would match ALL unattributed visits (NULL == NULL in Python).
        if cat_id is None:
            litter = []
        else:
            litter = [
                {
                    "kind": "litter",
                    "ts": v["enter_ts"],
                    "duration_s": v.get("duration_s"),
                    "eliminated": bool(v.get("eliminated")),
                    "confidence": v.get("confidence"),
                }
                for v in store.recent_visits(conn, 60)
                if v.get("cat_id") == cat_id
            ][:20]

        ate = [
            {"kind": "ate", "ts": s["ts"], "location": s["location"], "duration_s": s["duration_s"]}
            for s in store.recent_bowl_sessions(conn, 60)
            if s["cat"] == name
        ][:20]

        timeline = sorted(litter + ate, key=lambda x: x["ts"] or "", reverse=True)[:30]

        rep = store.latest_weekly_report(conn)
        weekly = (
            json.loads(rep["facts_json"]).get("per_cat", {}).get(name)
            if rep else None
        )

        # Browser-fetchable URLs (served by the /gallery route), not FS paths.
        files = sorted(_glob.glob(os.path.join(gallery_abs, name.lower(), "*.jp*")))[:6]
        photos = [f"/gallery/{name.lower()}/{os.path.basename(f)}" for f in files]

        return jsonify({**rows[name], "timeline": timeline, "weekly": weekly, "photos": photos})

    if bus is not None:
        @app.get("/events")
        def events_stream():
            def gen():
                # Subscribe INSIDE the generator so the matching unsubscribe in
                # `finally` is always reached — if subscribe ran in the view and the
                # client died before the body iterated, the queue would orphan and
                # accumulate on every publish for the life of the daemon.
                q = bus.subscribe()
                try:
                    # Flush headers immediately so the client's EventSource fires
                    # `onopen` (shows "live") without waiting for the first event;
                    # the keepalive comment also keeps idle proxies from dropping it.
                    yield ": connected\n\n"
                    while True:
                        try:
                            ev = q.get(timeout=20)
                        except queue.Empty:
                            yield ": keepalive\n\n"
                            continue
                        yield ("data: " + json.dumps(
                            {"kind": ev.kind, "ts": ev.ts, "detail": ev.detail}) + "\n\n")
                finally:
                    bus.unsubscribe(q)

            return Response(gen(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache",
                                     "X-Accel-Buffering": "no"})

    return app
