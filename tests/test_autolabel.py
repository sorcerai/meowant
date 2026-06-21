"""Auto-labeler: cross-frame agreement gate, worker apply+provenance, validation."""
from mw import store
from mw.labeler import decide, NONE, ERROR
from mw.autolabel import AutoLabeler, discover_refs, validate

CATS = {"Ucok", "Garfield", "Ella"}


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return conn


def _pf(file, cat, conf=0.9):
    return {"file": file, "cat": cat, "confidence": conf}


class FakeLabeler:
    """Maps a path substring -> cat name (or NONE)."""
    def __init__(self, table):
        self.table = table
    def predict_visit(self, frame_paths, refs):
        out = []
        for p in frame_paths:
            cat = NONE
            for key, val in self.table.items():
                if key in p:
                    cat = val
            out.append(_pf(p, cat))
        return out


# ---- decide() gate (pure) ---------------------------------------------------

def test_decide_unanimous_labels_named_frames_only():
    d = decide([_pf("a", "Garfield"), _pf("b", NONE), _pf("c", "Garfield")], CATS)
    assert d["status"] == "labeled" and d["cat"] == "Garfield"
    assert [a[0] for a in d["apply"]] == ["a", "c"]   # empty 'b' not labeled


def test_decide_all_empty_is_empty():
    d = decide([_pf("a", NONE), _pf("b", NONE)], CATS)
    assert d["status"] == "empty" and d["apply"] == []


def test_decide_conflict_blocks_whole_visit():
    d = decide([_pf("a", "Garfield"), _pf("b", "Ucok")], CATS)
    assert d["status"] == "conflict" and d["apply"] == []
    assert d["cats"] == ["Garfield", "Ucok"]


def test_decide_established_agreement_applies_established():
    d = decide([_pf("a", "Garfield"), _pf("b", NONE)], CATS, established="Garfield")
    assert d["status"] == "labeled" and d["cat"] == "Garfield"
    assert [a[0] for a in d["apply"]] == ["a"]


def test_decide_established_disagreement_is_conflict_never_applied():
    # human established Ella for the visit; model says Ucok on a leftover frame
    d = decide([_pf("a", "Ucok"), _pf("b", "Ucok")], CATS, established="Ella")
    assert d["status"] == "conflict" and d["apply"] == []   # never overrides the human


def test_decide_majority_tolerates_minority_misread():
    # 3 Ella + 1 Garfield (a stray agy misread) -> Ella wins, only Ella frames applied
    d = decide([_pf("a", "Ella"), _pf("b", "Ella"), _pf("c", "Ella"), _pf("d", "Garfield")], CATS)
    assert d["status"] == "labeled" and d["cat"] == "Ella"
    assert sorted(a[0] for a in d["apply"]) == ["a", "b", "c"]   # not the outlier 'd'


def test_decide_near_tie_is_conflict():
    # 2 vs 2 -> no majority -> review (likely a real two-cat visit)
    d = decide([_pf("a", "Ella"), _pf("b", "Ella"), _pf("c", "Garfield"), _pf("d", "Garfield")], CATS)
    assert d["status"] == "conflict" and d["apply"] == []


def test_decide_ignores_unknown_cat_names():
    d = decide([_pf("a", "Dog"), _pf("b", "Garfield")], CATS)
    assert d["status"] == "labeled" and d["cat"] == "Garfield"
    assert [a[0] for a in d["apply"]] == ["b"]


# ---- worker apply + provenance ---------------------------------------------

def test_run_once_applies_auto_labels_with_provenance(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    g1 = store.insert_capture(conn, 1000.0, vid, "cam1", "/g/1000_cam1_0.jpg")
    e1 = store.insert_capture(conn, 1000.0, vid, "cam2", "/g/1000_cam2_0.jpg")  # empty
    g2 = store.insert_capture(conn, 1009.0, vid, "cam1", "/g/1009_cam1_1.jpg")
    lab = FakeLabeler({"cam1": "Garfield"})   # cam1 frames = Garfield, cam2 = none
    al = AutoLabeler(conn, lab, refs={}, valid_cats=CATS)
    res = al.run_once()
    assert res[0]["status"] == "labeled" and res[0]["applied"] == 2
    rows = {r["id"]: r for r in store.captures_for_visit(conn, vid)}
    gid = store.cat_id_by_name(conn, "Garfield")
    assert rows[g1]["label"] == gid and rows[g1]["label_source"] == "auto"
    assert rows[g2]["label"] == gid and rows[g2]["pred"] == gid
    assert rows[e1]["label"] is None   # empty frame left for the (future) detector


def test_run_once_syncs_visit_cat_id(tmp_path):
    # 6v5: the labeler wrote captures.label but never visits.cat_id, so any
    # visit-level attribution (scatter blame, health baselines) was wrong.
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    store.insert_capture(conn, 1000.0, vid, "cam1", "/g/garf_1.jpg")
    store.insert_capture(conn, 1001.0, vid, "cam2", "/g/garf_2.jpg")
    al = AutoLabeler(conn, FakeLabeler({"garf": "Garfield"}), refs={}, valid_cats=CATS)
    al.run_once()
    assert store.gallery_counts(conn)["Garfield"] == 2
    row = conn.execute("SELECT cat_id FROM visits WHERE id=?", (vid,)).fetchone()
    assert row["cat_id"] == store.cat_id_by_name(conn, "Garfield")


class _NoCatFilter:
    """Rejects every frame — simulates the COCO detector missing a cat in
    dawn / IR low-light frames."""
    def has_cat(self, path):
        return False


def test_eliminated_visit_bypasses_catfilter(tmp_path):
    # Live bug: dawn eliminations had frames captured but the cat/no-cat filter
    # scored them all <0.4 -> auto-none -> never labeled. An elimination is
    # ground truth a cat was present, so the filter must not veto it.
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    store.mark_elimination(conn, vid, use_record=69)  # ground truth: a cat was here
    store.insert_capture(conn, 1000.0, vid, "cam1", "/g/garf_1.jpg")
    store.insert_capture(conn, 1001.0, vid, "cam2", "/g/garf_2.jpg")
    al = AutoLabeler(conn, FakeLabeler({"garf": "Garfield"}), refs={},
                     valid_cats=CATS, catfilter=_NoCatFilter())
    al.run_once()
    # filter said 'no cat' on every frame, but the elimination forces them to the model
    assert store.gallery_counts(conn)["Garfield"] == 2
    row = conn.execute("SELECT cat_id FROM visits WHERE id=?", (vid,)).fetchone()
    assert row["cat_id"] == store.cat_id_by_name(conn, "Garfield")


def test_noneliminated_visit_still_filters(tmp_path):
    # control: a non-eliminated visit with a rejecting filter is still vetoed
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    store.insert_capture(conn, 1000.0, vid, "cam1", "/g/garf_1.jpg")
    al = AutoLabeler(conn, FakeLabeler({"garf": "Garfield"}), refs={},
                     valid_cats=CATS, catfilter=_NoCatFilter())
    al.run_once()
    assert store.gallery_counts(conn)["Garfield"] == 0
    assert all(r["label_source"] == "auto-none" for r in store.captures_for_visit(conn, vid))


def test_run_once_conflict_writes_nothing(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    store.insert_capture(conn, 1000.0, vid, "cam1", "/g/garf.jpg")
    store.insert_capture(conn, 1000.0, vid, "cam2", "/g/ucok.jpg")
    lab = FakeLabeler({"garf": "Garfield", "ucok": "Ucok"})
    al = AutoLabeler(conn, lab, refs={}, valid_cats=CATS)
    res = al.run_once()
    assert res[0]["status"] == "conflict"
    assert all(r["label"] is None for r in store.captures_for_visit(conn, vid))


def test_examined_frames_are_not_reprocessed(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    store.insert_capture(conn, 1000.0, vid, "cam1", "/g/garf.jpg")    # Garfield
    store.insert_capture(conn, 1000.0, vid, "cam2", "/g/empty.jpg")   # none
    calls = []

    class CountingLabeler(FakeLabeler):
        def predict_visit(self, fp, refs):
            calls.append(list(fp))
            return super().predict_visit(fp, refs)

    al = AutoLabeler(conn, CountingLabeler({"garf": "Garfield"}), refs={}, valid_cats=CATS)
    al.run_once()
    al.run_once()                       # nothing untouched remains
    assert len(calls) == 1              # expensive model invoked only once
    assert store.unlabeled_visit_ids(conn) == []


def test_conflict_frames_go_to_review_queue_not_auto_requeue(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    store.insert_capture(conn, 1000.0, vid, "cam1", "/g/garf.jpg")
    store.insert_capture(conn, 1000.0, vid, "cam2", "/g/ucok.jpg")
    al = AutoLabeler(conn, FakeLabeler({"garf": "Garfield", "ucok": "Ucok"}),
                     refs={}, valid_cats=CATS)
    al.run_once()
    assert len(store.review_queue(conn)) == 2       # both flagged for a human
    assert store.unlabeled_visit_ids(conn) == []    # NOT re-fed to the model


class ErrLabeler:
    def predict_visit(self, frame_paths, refs):
        return [{"file": p, "cat": ERROR, "confidence": 0.0} for p in frame_paths]


def test_backend_error_skips_visit_and_keeps_it_queued(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    store.insert_capture(conn, 1000.0, vid, "cam1", "/g/a.jpg")
    al = AutoLabeler(conn, ErrLabeler(), refs={}, valid_cats=CATS)
    assert al.run_once()[0]["status"] == "error"
    row = store.captures_for_visit(conn, vid)[0]
    assert row["label"] is None and row["label_source"] is None   # untouched
    assert store.unlabeled_visit_ids(conn) == [vid]               # queued for retry


def test_late_frame_does_not_reexamine_conflict_frames(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    a = store.insert_capture(conn, 1000.0, vid, "cam1", "/g/garf.jpg")
    b = store.insert_capture(conn, 1000.0, vid, "cam2", "/g/ucok.jpg")
    seen = []

    class Spy(FakeLabeler):
        def predict_visit(self, fp, refs):
            seen.extend(fp)
            return super().predict_visit(fp, refs)

    al = AutoLabeler(conn, Spy({"garf": "Garfield", "ucok": "Ucok"}),
                     refs={}, valid_cats=CATS)
    al.run_once()                                  # conflict -> a,b -> auto-conflict
    assert {r["id"] for r in store.review_queue(conn)} == {a, b}
    store.insert_capture(conn, 1009.0, vid, "cam1", "/g/garf2.jpg")  # late sibling frame
    seen.clear()
    al.run_once()
    assert seen == ["/g/garf2.jpg"]                # only the NEW frame re-examined
    assert {r["id"] for r in store.review_queue(conn)} == {a, b}     # a,b still pending human


def test_process_visit_does_not_override_human_visit_cat(tmp_path):
    # The Ella→Ucok overnight bug: clear frames human-labeled Ella, a leftover
    # dig frame the model misreads as Ucok must go to review, not get applied.
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    h = store.insert_capture(conn, 1000.0, vid, "cam1", "/g/ella_clear.jpg")
    store.set_capture_label(conn, h, store.cat_id_by_name(conn, "Ella"), source="human")
    u = store.insert_capture(conn, 1001.0, vid, "cam2", "/g/ucok_lookalike.jpg")
    al = AutoLabeler(conn, FakeLabeler({"ucok_lookalike": "Ucok"}),
                     refs={}, valid_cats=CATS)
    al.run_once()
    rows = {r["id"]: r for r in store.captures_for_visit(conn, vid)}
    assert rows[u]["label"] is None                       # NOT mislabeled Ucok
    assert rows[u]["label_source"] == "auto-conflict"     # routed to human review
    assert len(store.review_queue(conn)) == 1


def test_empty_valid_cats_is_noop(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    store.insert_capture(conn, 1000.0, vid, "cam1", "/g/a.jpg")
    al = AutoLabeler(conn, FakeLabeler({"a": "Garfield"}), refs={}, valid_cats=set())
    assert al.run_once() == []
    assert store.captures_for_visit(conn, vid)[0]["label_source"] is None


def test_apply_auto_label_does_not_clobber_human(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    cid = store.insert_capture(conn, 1000.0, vid, "cam1", "/g/a.jpg")
    gid = store.cat_id_by_name(conn, "Garfield")
    uid = store.cat_id_by_name(conn, "Ucok")
    store.set_capture_label(conn, cid, gid, source="human")    # human there first
    assert store.apply_auto_label(conn, cid, uid, 0.9) is False  # auto must not win
    row = store.captures_for_visit(conn, vid)[0]
    assert row["label"] == gid and row["label_source"] == "human"


class FakeFilter:
    """has_cat by path: a path containing 'empty' is judged no-cat."""
    def has_cat(self, path):
        return "empty" not in path


def test_catfilter_drops_empties_before_labeler(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    g = store.insert_capture(conn, 1000.0, vid, "cam1", "/g/ella_clear.jpg")
    e = store.insert_capture(conn, 1000.0, vid, "cam2", "/g/empty_box.jpg")
    seen = []

    class Spy(FakeLabeler):
        def predict_visit(self, fp, refs):
            seen.extend(fp)
            return super().predict_visit(fp, refs)

    al = AutoLabeler(conn, Spy({"ella_clear": "Ella"}), refs={}, valid_cats=CATS,
                     catfilter=FakeFilter())
    al.run_once()
    assert seen == ["/g/ella_clear.jpg"]            # empty never hit the labeler
    rows = {r["id"]: r for r in store.captures_for_visit(conn, vid)}
    assert rows[e]["label"] is None and rows[e]["label_source"] == "auto-none"
    assert rows[g]["label"] == store.cat_id_by_name(conn, "Ella")
    assert rows[g]["label_source"] == "auto"


def test_dry_run_changes_nothing(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    store.insert_capture(conn, 1000.0, vid, "cam1", "/g/ella.jpg")
    al = AutoLabeler(conn, FakeLabeler({"ella": "Ella"}), refs={}, valid_cats=CATS)
    al.run_once(dry_run=True)
    assert all(r["label"] is None for r in store.captures_for_visit(conn, vid))


# ---- trust-channel accuracy + validation -----------------------------------

def test_labeler_accuracy_counts_corrections(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    a = store.insert_capture(conn, 1000.0, vid, "cam1", "/g/a.jpg")
    b = store.insert_capture(conn, 1000.0, vid, "cam2", "/g/b.jpg")
    gid = store.cat_id_by_name(conn, "Garfield")
    uid = store.cat_id_by_name(conn, "Ucok")
    store.set_capture_label(conn, a, gid, source="auto")        # auto, stands
    store.set_capture_label(conn, b, uid, source="corrected")   # auto was wrong, human fixed
    acc = store.labeler_accuracy(conn)
    assert acc["auto"] == 1 and acc["corrected"] == 1
    assert abs(acc["auto_accuracy"] - 0.5) < 1e-9


def test_validate_scores_against_human_labels(tmp_path):
    conn = _db(tmp_path)
    vid = store.open_visit(conn, 1000.0)
    a = store.insert_capture(conn, 1000.0, vid, "cam1", "/g/garf.jpg")
    b = store.insert_capture(conn, 1000.0, vid, "cam2", "/g/ella.jpg")
    store.set_capture_label(conn, a, store.cat_id_by_name(conn, "Garfield"), source="human")
    store.set_capture_label(conn, b, store.cat_id_by_name(conn, "Ella"), source="human")
    # labeler gets 'garf' right, calls 'ella' frame Ucok (wrong)
    lab = FakeLabeler({"garf": "Garfield", "ella": "Ucok"})
    rep = validate(conn, lab, refs={}, valid_cats=CATS)
    assert rep["total"] == 2 and rep["correct"] == 1
    assert rep["wrong"] == [("/g/ella.jpg", "Ella", "Ucok")]


def test_discover_refs_returns_all_seeds_per_cat(tmp_path):
    (tmp_path / "ucok").mkdir()
    (tmp_path / "ucok" / "seed-01.jpeg").write_text("x")
    (tmp_path / "ucok" / "seed-02.jpeg").write_text("x")
    refs = discover_refs(str(tmp_path), ["Ucok", "Garfield"])
    assert len(refs["Ucok"]) == 2 and "Garfield" not in refs   # no garfield dir
