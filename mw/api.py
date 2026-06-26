"""Read/command HTTP API over the daemon's current state + store."""
import json
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


def create_app(daemon, conn, bus=None):
    app = Flask(__name__, static_folder=_static_dir, static_url_path="/static")

    @app.get("/")
    def index():
        return send_from_directory(_static_dir, "index.html")

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
        else:
            return jsonify({"ok": False, "error": f"unknown action {action}"}), 400
        return jsonify({"ok": True})

    if bus is not None:
        @app.get("/events")
        def events_stream():
            q = bus.subscribe()

            def gen():
                try:
                    while True:
                        ev = q.get()
                        yield ("data: " + json.dumps(
                            {"kind": ev.kind, "ts": ev.ts, "detail": ev.detail}) + "\n\n")
                finally:
                    bus.unsubscribe(q)

            return Response(gen(), mimetype="text/event-stream")

    return app
