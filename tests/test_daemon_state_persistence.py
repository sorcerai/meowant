"""Telegram repeat-message fix: dedup state (the getUpdates offset + the
watchers' alarm latches) must survive a daemon restart. Previously all of it was
in-memory and reset to defaults on every restart, so a restart re-answered the
last command batch and re-fired every still-active alarm — duplicate messages."""
from mw import store
from mw.telegram_bot import TelegramBot
from mw.health_watch import HealthWatch
from mw.box_health import BoxHealthWatch
from mw.events import Event, BIN_FULL

T = 1_000_000.0


def _conn(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return conn


# ---- store kv ----
def test_daemon_state_roundtrip_and_default(tmp_path):
    conn = _conn(tmp_path)
    assert store.get_daemon_state(conn, "missing", 42) == 42
    store.set_daemon_state(conn, "k", {"a": 1, "b": [2, 3]})
    assert store.get_daemon_state(conn, "k") == {"a": 1, "b": [2, 3]}
    store.set_daemon_state(conn, "k", 99)        # upsert overwrites
    assert store.get_daemon_state(conn, "k") == 99


# ---- inbound: telegram offset ----
class _FakeServer:
    def __init__(self, updates): self.updates = updates
    def get(self, token, offset, timeout):
        return [u for u in self.updates if u["update_id"] >= offset]


def _cats_bot(conn, server, sent):
    return TelegramBot(
        "tok", 123, {"/cats": lambda: "report"},
        getter=server.get, sender=lambda tok, cid, txt: sent.append(txt),
        load_offset=lambda: store.get_daemon_state(conn, "telegram.offset", 0),
        save_offset=lambda o: store.set_daemon_state(conn, "telegram.offset", o))


def test_offset_persists_across_restart_no_duplicate_reply(tmp_path):
    conn = _conn(tmp_path)
    server = _FakeServer([{"update_id": 10,
                           "message": {"chat": {"id": 123}, "text": "/cats"}}])
    sent = []
    _cats_bot(conn, server, sent).poll_once()          # first run answers
    assert sent == ["report"]
    _cats_bot(conn, server, sent).poll_once()          # restart: must NOT re-answer
    assert sent == ["report"], "restart re-answered an already-handled command"


def test_without_persistence_restart_duplicates(tmp_path):
    """Documents the bug: an in-memory cursor re-answers the same command on restart."""
    server = _FakeServer([{"update_id": 10,
                           "message": {"chat": {"id": 123}, "text": "/cats"}}])
    sent = []
    mk = lambda: TelegramBot("tok", 123, {"/cats": lambda: "r"},
                             getter=server.get, sender=lambda t, c, x: sent.append(x))
    mk().poll_once(); mk().poll_once()
    assert sent == ["r", "r"]


# ---- outbound: health_watch latch ----
def _elim(conn, ts, cat="Ucok", duration=60, use_record=60):
    v = store.open_visit(conn, ts); store.mark_elimination(conn, v, use_record)
    store.close_visit(conn, v, ts + duration, duration)
    if cat:
        store.set_visit_identity(conn, v, store.cat_id_by_name(conn, cat), 1.0)
    return v


def test_health_watch_latch_persists_across_restart(tmp_path):
    conn = _conn(tmp_path)
    _elim(conn, 1000.0, cat="Ella")                    # Ella last used 25h ago
    now = 1000.0 + 25 * 3600
    _elim(conn, now - 3600, cat="Garfield")            # someone went recently (no silence)
    sent = []
    HealthWatch(conn, sent.append, now_fn=lambda: now, digest_hour=99).run_once()
    assert len([m for m in sent if "No litter box use" in m]) == 1
    HealthWatch(conn, sent.append, now_fn=lambda: now, digest_hour=99).run_once()  # restart
    assert len([m for m in sent if "No litter box use" in m]) == 1, "restart re-fired no-go alarm"


# ---- outbound: box_health latch ----
def test_box_health_nag_latch_persists_across_restart(tmp_path):
    conn = _conn(tmp_path)
    store.insert_event(conn, Event(BIN_FULL, T))       # bin full, never cleared
    now = T + 100
    sent = []
    BoxHealthWatch(conn, sent.append, now_fn=lambda: now, renag_hours=3).run_once()
    assert len(sent) == 1                              # nagged once
    BoxHealthWatch(conn, sent.append, now_fn=lambda: now, renag_hours=3).run_once()  # restart
    assert len(sent) == 1, "restart re-nagged within the re-nag window"
