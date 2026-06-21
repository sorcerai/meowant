"""C1 — verify store functions are safe to call from multiple threads."""
import threading

from mw import store
from mw.events import Event, CAT_ENTER


def test_cross_thread_insert_and_open_visit(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)

    errors = []

    def worker():
        try:
            store.insert_event(conn, Event(CAT_ENTER, 1000.0, {"from": "standby"}))
            store.open_visit(conn, 1000.0)
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert errors == [], f"Thread raised: {errors}"
    rows = store.recent_visits(conn, 10)
    assert len(rows) == 1
