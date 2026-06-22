from mw import store
from mw.elim_notify import EliminationNotifier


class _Labeler:                       # stand-in for AutoLabeler.label_visit
    def __init__(self, conn, cat=None):
        self.conn, self.cat = conn, cat
    def label_visit(self, vid, dry_run=False, sample=None):
        if self.cat:
            store.set_visit_identity(self.conn, vid,
                                     store.cat_id_by_name(self.conn, self.cat), 1.0)
        return None


def _setup(tmp_path, cat=None):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    sent = []
    n = EliminationNotifier(conn, _Labeler(conn, cat), notify=sent.append,
                            now_fn=lambda: 10_000.0, settle_s=15)
    return conn, n, sent


def test_named_alert_when_identified(tmp_path):
    conn, n, sent = _setup(tmp_path, cat="Ucok")
    v = store.open_visit(conn, 9_000.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 9_900.0, 900)      # closed well before now-settle
    n.run_once()
    assert len(sent) == 1 and "Ucok" in sent[0] and "box" in sent[0].lower()
    assert store.get_visit(conn, v)["notified"] == 1


def test_anonymous_alert_when_unidentified(tmp_path):
    conn, n, sent = _setup(tmp_path, cat=None)     # labeler resolves no cat
    v = store.open_visit(conn, 9_000.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 9_900.0, 900)
    n.run_once()
    assert len(sent) == 1 and "couldn't ID" in sent[0]
    assert store.get_visit(conn, v)["notified"] == 1


def test_only_alerts_once(tmp_path):
    conn, n, sent = _setup(tmp_path, cat="Ucok")
    v = store.open_visit(conn, 9_000.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 9_900.0, 900)
    n.run_once(); n.run_once()
    assert len(sent) == 1                           # second pass is a no-op


def test_recent_visit_waits_for_settle(tmp_path):
    conn, n, sent = _setup(tmp_path, cat="Ucok")
    v = store.open_visit(conn, 9_990.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 9_995.0, 5)          # closed 5s before now < settle 15s
    n.run_once()
    assert sent == []                               # too fresh, not yet alerted


def test_unidentified_triggers_ask_who(tmp_path):
    conn, _, sent = _setup(tmp_path, cat=None)
    asked = []
    # rebuild notifier with ask_who
    from mw.elim_notify import EliminationNotifier
    n = EliminationNotifier(conn, _Labeler(conn, None), notify=sent.append,
                            now_fn=lambda: 10_000.0, settle_s=15,
                            ask_who=lambda vid, paths, when: asked.append(vid))
    v = store.open_visit(conn, 9_000.0); store.mark_elimination(conn, v, 55)
    store.insert_capture(conn, 9_100.0, v, "cam", "/g/x.jpg")
    store.close_visit(conn, v, 9_900.0, 900)
    n.run_once()
    assert asked == [v]                      # prompt fired
    assert sent == []                        # no dead-end text
    assert store.get_visit(conn, v)["notified"] == 1


def test_frameless_visit_recovers_window_photos(tmp_path):
    # eliminated visit with NO captures of its own, but sibling frames exist in the
    # window -> ask_who fires with the recovered paths (IR-flicker recovery)
    conn, _, _ = _setup(tmp_path, cat=None)
    asked = {}
    from mw.elim_notify import EliminationNotifier
    n = EliminationNotifier(conn, _Labeler(conn, None), notify=lambda m: None,
                            now_fn=lambda: 10_000.0, settle_s=15,
                            ask_who=lambda vid, paths, when: asked.update(vid=vid, paths=paths))
    # a sibling fragment dropped frames just before
    store.insert_capture(conn, 9_880.0, 1, "cam", "/g/sib.jpg")
    v = store.open_visit(conn, 9_900.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 9_905.0, 5)   # v itself has zero captures
    n.run_once()
    assert asked.get("vid") == v and "/g/sib.jpg" in asked.get("paths", [])


def test_frameless_with_no_window_photos_falls_back_to_text(tmp_path):
    conn, _, sent = _setup(tmp_path, cat=None)
    from mw.elim_notify import EliminationNotifier
    called = []
    n = EliminationNotifier(conn, _Labeler(conn, None), notify=sent.append,
                            now_fn=lambda: 10_000.0, settle_s=15,
                            ask_who=lambda vid, paths, when: called.append(vid))
    v = store.open_visit(conn, 9_900.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 9_905.0, 5)   # no captures anywhere
    n.run_once()
    assert called == []                       # ask_who NOT used (no photos)
    assert any("couldn't ID" in m for m in sent)   # plain text instead
