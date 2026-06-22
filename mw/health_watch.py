"""Absence/liveness watchdogs (the 'tell me when something is WRONG' half).

HealthWatch: a no-go alarm (box unused >N hours -> a cat may be sick/blocked) plus a
once-daily 'alive' digest. Both run on one slow loop. Heartbeat (separate) pings an
external URL so a dead daemon/host is caught off-box."""
import sys
import time
from datetime import date, datetime

from mw import store, report


class HealthWatch:
    def __init__(self, conn, notify, now_fn=time.time,
                 no_go_hours=12, digest_hour=9, interval=1800):
        self.conn = conn
        self.notify = notify
        self.now = now_fn
        self.no_go_hours = no_go_hours
        self.digest_hour = digest_hour
        self.interval = interval
        self._alarmed = False          # no-go latch
        self._digest_day = None        # last local date a digest was sent

    def _check_no_go(self):
        ts = store.last_elimination_ts(self.conn)
        if ts is None:
            return                     # no data yet — nothing to alarm on
        hours = (self.now() - datetime.fromisoformat(ts).timestamp()) / 3600.0
        if hours >= self.no_go_hours and not self._alarmed:
            self.notify(f"⚠️ No litter box use in {hours:.0f}h (since {ts[5:16].replace('T',' ')}) "
                        f"— check on the cats")
            self._alarmed = True
        elif hours < self.no_go_hours:
            self._alarmed = False      # a fresh use re-arms the alarm

    def _check_digest(self):
        lt = time.localtime(self.now())
        today = date(lt.tm_year, lt.tm_mon, lt.tm_mday).isoformat()
        if today != self._digest_day and lt.tm_hour >= self.digest_hour:
            self.notify(report.digest(self.conn, now=self.now()))
            self._digest_day = today

    def run_once(self):
        self._check_no_go()
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
