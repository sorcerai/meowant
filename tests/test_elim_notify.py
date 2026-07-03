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
                            ask_who=lambda vid, paths, when, waste='': asked.append(vid))
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
                            ask_who=lambda vid, paths, when, waste='': asked.update(vid=vid, paths=paths))
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
                            ask_who=lambda vid, paths, when, waste='': called.append(vid))
    v = store.open_visit(conn, 9_900.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 9_905.0, 5)   # no captures anywhere
    n.run_once()
    assert called == []                       # ask_who NOT used (no photos)
    assert any("couldn't ID" in m for m in sent)   # plain text instead


def test_alert_marks_pee_vs_poop(tmp_path):
    conn, _, _ = _setup(tmp_path, cat="Ucok")
    from mw.elim_notify import EliminationNotifier
    n = EliminationNotifier(conn, _Labeler(conn, "Ucok"), notify=lambda m: None,
                            now_fn=lambda: 10_000.0, pee_threshold=80, poop_threshold=130)
    pee = n._alert_text({"cat_id": store.cat_id_by_name(conn, "Ucok"), "use_record": 65})
    poop = n._alert_text({"cat_id": store.cat_id_by_name(conn, "Ucok"), "use_record": 140})
    uncertain = n._alert_text({"cat_id": store.cat_id_by_name(conn, "Ucok"), "use_record": 100})
    unknown = n._alert_text({"cat_id": store.cat_id_by_name(conn, "Ucok"), "use_record": None})
    assert "pee" in pee and "💧" in pee
    assert "poop" in poop and "💩" in poop
    assert "uncertain" in uncertain and "❓" in uncertain
    assert "pee" not in unknown and "poop" not in unknown   # no marker when unknown


def test_disabled_silences_alert_but_marks_notified(tmp_path):
    conn, _, sent = _setup(tmp_path, cat="Ucok")
    from mw.elim_notify import EliminationNotifier
    n = EliminationNotifier(conn, _Labeler(conn, "Ucok"), notify=sent.append,
                            now_fn=lambda: 10_000.0, enabled=False)
    v = store.open_visit(conn, 9_000.0); store.mark_elimination(conn, v, 55)
    store.close_visit(conn, v, 9_900.0, 900)
    n.run_once()
    assert sent == []                               # silenced
    assert store.get_visit(conn, v)["notified"] == 1 # but still marked to avoid backlog


# ---- globe-tipped era: local matcher first, honest hidden-cat text ---------

class _StubMatcher:
    """Names `cat_id` on every frame; None = abstain (no crop / closed globe)."""
    def __init__(self, cat_id, conf=0.9):
        self.cat_id, self.conf = cat_id, conf
    def predict(self, path):
        return (self.cat_id, self.conf if self.cat_id else 0.0)


class _StubFilter:
    def __init__(self, visible):
        self.visible = visible
    def has_cat(self, path):
        return self.visible


def _visit_with_frames(conn, tmp_path, n=4):
    v = store.open_visit(conn, 9_000.0); store.mark_elimination(conn, v, 55)
    for i in range(n):
        p = str(tmp_path / f"f{i}.jpg")
        open(p, "wb").write(b"jpg")
        store.insert_capture(conn, 9_100.0 + i, v, "cam", p)
    store.close_visit(conn, v, 9_900.0, 900)
    return v


def test_matcher_fast_path_names_before_labeler(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    ucok = store.cat_id_by_name(conn, "Ucok")
    sent = []
    n = EliminationNotifier(conn, _Labeler(conn, None),   # agy finds nothing
                            notify=sent.append, now_fn=lambda: 10_000.0,
                            matcher=_StubMatcher(ucok), catfilter=_StubFilter(True))
    v = _visit_with_frames(conn, tmp_path)
    n.run_once()
    assert len(sent) == 1 and "Ucok" in sent[0]           # matcher named it locally
    assert store.get_visit(conn, v)["cat_id"] == ucok     # identity persisted


def test_hidden_cat_text_when_nothing_visible(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    sent, asked = [], []
    n = EliminationNotifier(conn, _Labeler(conn, None), notify=sent.append,
                            now_fn=lambda: 10_000.0,
                            matcher=_StubMatcher(None), catfilter=_StubFilter(False),
                            ask_who=lambda vid, paths, when, waste='': asked.append(vid))
    _visit_with_frames(conn, tmp_path)
    n.run_once()
    assert asked == []                                    # no useless closed-ball photos
    assert len(sent) == 1 and "hidden" in sent[0].lower() # honest message instead
    assert "couldn't ID" not in sent[0]


def test_visible_unknown_still_asks_who(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    sent, asked = [], []
    n = EliminationNotifier(conn, _Labeler(conn, None), notify=sent.append,
                            now_fn=lambda: 10_000.0,
                            matcher=_StubMatcher(None), catfilter=_StubFilter(True),
                            ask_who=lambda vid, paths, when, waste='': asked.append(vid))
    v = _visit_with_frames(conn, tmp_path)
    n.run_once()
    assert asked == [v]                                   # photos are useful: ask
    assert sent == []


def test_matcher_never_overrides_human(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Ella"])
    ucok = store.cat_id_by_name(conn, "Ucok")
    ella = store.cat_id_by_name(conn, "Ella")
    sent = []
    n = EliminationNotifier(conn, _Labeler(conn, None), notify=sent.append,
                            now_fn=lambda: 10_000.0,
                            matcher=_StubMatcher(ucok), catfilter=_StubFilter(True))
    v = _visit_with_frames(conn, tmp_path)
    cid = store.captures_for_visit(conn, v)[0]["id"]
    conn.execute("UPDATE captures SET label=?, label_source='human' WHERE id=?", (ella, cid))
    conn.commit()
    store.set_visit_identity(conn, v, ella, 1.0)
    n.run_once()
    assert store.get_visit(conn, v)["cat_id"] == ella     # human label stands
