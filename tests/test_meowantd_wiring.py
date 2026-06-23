# Verify the capture wiring helper writes a captures row tied to the open visit.
from mw import store
from mw.bus import EventBus
from mw.events import Event, CAT_ENTER
from mw.capture import CaptureService

def test_remediator_is_wired_into_capture_health():
    import inspect
    import meowantd
    src = inspect.getsource(meowantd)
    assert "Remediator(" in src                         # remediator constructed
    assert "remediator=remediator" in src               # passed to CaptureHealth
    assert "/incidents" in src                          # command registered


def test_invariant_canary_is_wired():
    import inspect
    import meowantd
    src = inspect.getsource(meowantd)
    assert "InvariantCanary(" in src           # canary constructed
    assert "canary.enabled" in src             # config-gated


def test_on_capture_writes_row_for_open_visit(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    vid = store.open_visit(conn, 1000.0)
    bus = EventBus()
    def grabber(url, path, timeout=15):
        open(path, "w").close(); return path
    def on_capture(name, path, ts, vid):
        store.insert_capture(conn, ts, vid, name, path, None)
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=grabber,
                        visit_resolver=lambda: store.latest_open_visit_id(conn),
                        on_capture=on_capture)
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    rows = store.captures_for_visit(conn, vid)
    assert len(rows) == 1 and rows[0]["camera"] == "front"
