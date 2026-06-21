import socket
import threading
import time

from werkzeug.serving import make_server

from mw import store, client
from mw.device import FakeDevice
from mw.daemon import Daemon
from mw.smartclean import SmartClean
from mw.api import create_app


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_client_get_state_and_command(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    dev = FakeDevice([{"24": "standby", "4": True, "7": 1, "21": 0}])
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0)
    d.tick()
    app = create_app(d, conn)
    port = _free_port()
    srv = make_server("127.0.0.1", port, app)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{port}"
    try:
        st = client.get_state(base)
        assert st["status"] == "standby" and st["auto_clean"] is True
        r = client.send_command(base, "clean")
        assert r["ok"] is True and dev.clean_calls == 1
    finally:
        srv.shutdown()
