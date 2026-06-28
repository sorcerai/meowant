from mw import store, api


class _Dev:
    state = {"dps": {}}; last_ok_ts = None; device = None; smartclean = None


def _client(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    return api.create_app(_Dev(), conn).test_client(), conn


def test_cat_detail_known_cat(tmp_path):
    c, conn = _client(tmp_path)
    r = c.get("/cat/Ucok")
    assert r.status_code == 200
    d = r.get_json()
    assert d["name"] == "Ucok"
    assert "timeline" in d and "weekly" in d and "photos" in d
    assert isinstance(d["timeline"], list)


def test_cat_detail_unknown_404(tmp_path):
    c, conn = _client(tmp_path)
    assert c.get("/cat/Nobody").status_code == 404


def _labeled_capture(conn, gdir, cat_id, n, visit_id, source):
    """Create a capture file under <gdir>/captures and a labeled DB row pointing at it."""
    rel = f"captures/g{n}.jpg"
    (gdir / "captures").mkdir(parents=True, exist_ok=True)
    (gdir / rel).write_bytes(b"\xff\xd8\xff\xe0")          # minimal jpeg header
    cid = store.insert_capture(conn, 1000 + n, visit_id, "meowcam1", f"gallery/{rel}")
    conn.execute("UPDATE captures SET label=?, label_source=? WHERE id=?", (cat_id, source, cid))
    conn.commit()


def test_cat_detail_photos_come_from_labeled_captures_not_folder(tmp_path):
    """Reference photos must be the cat's human-labeled captures (what the matcher
    embeds), NOT the orphaned gallery/<name>/ folder of empty-box full frames."""
    gdir = tmp_path / "gallery"
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    gid = store.cat_id_by_name(conn, "Garfield")
    _labeled_capture(conn, gdir, gid, 0, 10, "human")
    _labeled_capture(conn, gdir, gid, 1, 11, "human-propagated")
    # a junk empty-box file in the orphaned folder — must NOT be chosen
    (gdir / "garfield").mkdir(parents=True, exist_ok=True)
    (gdir / "garfield" / "ref-emptybox.jpg").write_bytes(b"\xff\xd8\xff\xe0")

    c = api.create_app(_Dev(), conn, gallery_dir=str(gdir)).test_client()
    d = c.get("/cat/Garfield").get_json()
    assert d["photos"], "expected reference photos"
    assert all("/gallery/captures/" in p for p in d["photos"])        # from labeled captures
    assert not any("ref-emptybox" in p for p in d["photos"])          # not the orphaned folder


def test_cat_detail_photos_one_per_visit(tmp_path):
    """Two captures from the SAME visit collapse to one photo (variety across visits)."""
    gdir = tmp_path / "gallery"
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    gid = store.cat_id_by_name(conn, "Garfield")
    _labeled_capture(conn, gdir, gid, 0, 10, "human")
    _labeled_capture(conn, gdir, gid, 1, 10, "human")                 # same visit 10
    _labeled_capture(conn, gdir, gid, 2, 11, "human")                 # visit 11
    c = api.create_app(_Dev(), conn, gallery_dir=str(gdir)).test_client()
    d = c.get("/cat/Garfield").get_json()
    assert len(d["photos"]) == 2                                      # one per distinct visit


def test_cat_detail_photos_fallback_to_folder_when_no_labels(tmp_path):
    """A cat with no labeled captures still shows its hand-placed folder refs."""
    gdir = tmp_path / "gallery"
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    (gdir / "ucok").mkdir(parents=True, exist_ok=True)
    (gdir / "ucok" / "refphoto-01.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    c = api.create_app(_Dev(), conn, gallery_dir=str(gdir)).test_client()
    d = c.get("/cat/Ucok").get_json()
    assert any("ucok/refphoto-01.jpg" in p for p in d["photos"])
