"""Absence/liveness watchdogs (the 'tell me when something is WRONG' half).

HealthWatch: a no-go alarm (box unused >N hours -> a cat may be sick/blocked) plus a
once-daily 'alive' digest. Both run on one slow loop. Heartbeat (separate) pings an
external URL so a dead daemon/host is caught off-box."""
import sys
import time
from datetime import date, datetime

from mw import store, report


class HealthWatch:
    def __init__(self, conn, notify, run_llm=None, now_fn=time.time,
                 no_go_hours=12, digest_hour=9, interval=1800,
                 attribution_renag_hours=3):
        self.conn = conn
        self.notify = notify
        self.run_llm = run_llm
        self.now = now_fn
        self.no_go_hours = no_go_hours
        self.digest_hour = digest_hour
        self.interval = interval
        self._attribution_renag_s = attribution_renag_hours * 3600
        self._alarmed = {}             # dict: cat -> bool or epoch
        self._digest_day = None        # last local date a digest was sent

    def _check_no_go(self):
        now = self.now()

        # Degraded-attribution guard: runs FIRST so the agy-down state (box being
        # used but all recent eliminations unattributed, last attributed visit >8h
        # ago) doesn't get mislabeled as "camera down" by the silence guard below.
        # When >=2 unattributed eliminations exist in 24h, per-cat no-go alarms are
        # unreliable and are suppressed; ONE honest notice is sent instead, re-nagged
        # every attribution_renag_hours so a multi-hour outage keeps pinging.
        window_iso = store._iso(now - 24 * 3600)
        uncertain = store.uncertain_eliminations_since(self.conn, window_iso)
        if store.attribution_unreliable(self.conn, window_iso):
            last_nag = self._alarmed.get("_attribution_nag", 0)
            if now - last_nag >= self._attribution_renag_s:
                self.notify(f"⚠️ Attribution degraded — {uncertain} box use(s) in 24h "
                            f"couldn't be confidently matched to a cat; per-cat no-go "
                            f"alarms paused. Check the labeler.")
                self._alarmed["_attribution_nag"] = now
            return
        else:
            self._alarmed["_attribution_nag"] = 0   # re-arm for next episode

        latest = {}
        for s in store.sessions(self.conn):
            if not s["eliminated"] or not s["cat"]:
                continue
            if s["cat"] == "Garfield":
                if s["use_record"] is None or s["duration_s"] <= 40:
                    continue
            t = datetime.fromisoformat(s["enter_ts"]).timestamp()
            latest[s["cat"]] = max(latest.get(s["cat"], 0), t)

        if not latest:
            return                     # no data yet

        most_recent_any = max(latest.values())
        if (now - most_recent_any) / 3600.0 >= 8:
            return  # System-wide silence; likely camera down, suppress per-cat alarms

        THRESHOLDS = {"Ucok": 8, "Ella": 24, "Garfield": 24}
        lt = time.localtime(now)
        is_night = (lt.tm_hour >= 22 or lt.tm_hour < 8)

        for cat, limit in THRESHOLDS.items():
            t = latest.get(cat)
            if not t:
                continue
            hours = (now - t) / 3600.0
            
            if cat == "Ucok" and hours >= limit and not is_night:
                self._alarmed[cat] = False
                continue

            if hours >= limit and not self._alarmed.get(cat, False):
                since = datetime.fromtimestamp(t).isoformat()[5:16].replace('T', ' ')
                self.notify(f"⚠️ {cat}: No litter box use in {hours:.0f}h (since {since}) — check on {cat}")
                self._alarmed[cat] = True
            elif hours < limit:
                self._alarmed[cat] = False

    def _check_digest(self):
        lt = time.localtime(self.now())
        today = date(lt.tm_year, lt.tm_mon, lt.tm_mday).isoformat()
        if today != self._digest_day and lt.tm_hour >= self.digest_hour:
            text = report.digest(self.conn, now=self.now())
            if self.run_llm:
                prompt = f"You are Meowant, a friendly smart litter box assistant. Here is the raw daily digest:\n{text}\nWrite a short, friendly 1-2 sentence daily summary for the owner. Don't invent facts. No markdown."
                try:
                    out = self.run_llm(prompt)
                    if out:
                        text = f"🌅 {out.strip()}\n\n(Raw Data: {text})"
                except Exception as e:
                    print(f"[health-watch] llm daily digest failed: {e}", file=sys.stderr)
            self.notify(text)
            self._digest_day = today

    def _check_frequency_spike(self):
        now = self.now()
        four_hours_ago = now - 4 * 3600
        
        # Get baseline from latest weekly report, if available
        import json
        rep = store.latest_weekly_report(self.conn)
        facts = json.loads(rep["facts_json"]) if rep else {}
        per_cat_facts = facts.get("per_cat", {})

        # Group recent sessions by cat
        recent = {}
        for s in store.sessions(self.conn):
            if not s["eliminated"] or not s["cat"]:
                continue
            t = datetime.fromisoformat(s["enter_ts"]).timestamp()
            if t < four_hours_ago:
                break # sessions are newest-first
            recent.setdefault(s["cat"], []).append(s)

        for cat, sessions in recent.items():
            if len(sessions) < 4:
                self._alarmed.pop(f"{cat}_spike", None)
                continue
            
            # Dual-signal: check if volume is significantly lower than baseline
            cat_baseline = per_cat_facts.get(cat, {})
            baseline_vol = cat_baseline.get("weight", {}).get("mean")
            if not baseline_vol:
                baseline_vol = 60.0 # fallback mean weight

            # Calculate average volume in this window. Ignore None weights.
            weights = [s["use_record"] for s in sessions if s["use_record"] is not None]
            avg_vol = sum(weights)/len(weights) if weights else 0.0
            
            # If frequency >= 4 in 4h AND volume < 50% of baseline, fire alert
            if avg_vol < (0.5 * baseline_vol):
                if not self._alarmed.get(f"{cat}_spike", False):
                    msg = f"🚨 {cat}: {len(sessions)} visits in 4h with very low output (avg {avg_vol:.1f}g, baseline {baseline_vol:.1f}g). Potential UTI/blockage!"
                    if self.run_llm:
                        prompt = f"You are a cat health monitoring AI. We detected an acute anomaly: {msg}. Write a brief, urgent but empathetic 1-2 sentence alert for the owner explaining that frequent unproductive visits indicate potential straining/blockage. No markdown."
                        try:
                            out = self.run_llm(prompt)
                            if out:
                                msg = "🚨 " + out.strip()
                        except Exception as e:
                            print(f"[health-watch] llm spike alert failed: {e}", file=sys.stderr)
                    self.notify(msg)
                    self._alarmed[f"{cat}_spike"] = True
            else:
                self._alarmed.pop(f"{cat}_spike", None)

    def run_once(self):
        self._check_no_go()
        self._check_frequency_spike()
        self._check_digest()

    def run(self):
        while True:
            try:
                self.run_once()
            except Exception as e:
                print(f"[health-watch] error: {e}", file=sys.stderr)
            time.sleep(self.interval)


def _http_ping(url):
    import urllib.request
    urllib.request.urlopen(url, timeout=10)


class Heartbeat:
    """Ping an external healthcheck URL (e.g. healthchecks.io) every interval. If the
    pings STOP — daemon crash-loop, Mac asleep/off/offline — that service alerts the
    user. The only check that survives the daemon/host itself dying."""
    def __init__(self, ping_url, getter=_http_ping, now_fn=time.time, interval=900):
        self.ping_url = ping_url
        self._get = getter
        self.now = now_fn
        self.interval = interval

    def run_once(self):
        try:
            self._get(self.ping_url)
        except Exception as e:
            print(f"[heartbeat] ping failed: {e}", file=sys.stderr)

    def run(self):
        while True:
            self.run_once()
            time.sleep(self.interval)
