"""Phase 3a: serve gallery photos over HTTP so cat-detail shows real pictures
(it returned filesystem paths the browser couldn't fetch). Traversal-safe."""
from mw import store
from mw.device import FakeDevice
from mw.daemon import Daemon
from mw.smartclean import SmartClean
from mw.api import create_app


def _app(tmp_path, gallery_dir):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Garfield", "Ella"])
    dev = FakeDevice([{"24": "standby"}])
    d = Daemon(dev, conn, SmartClean(), now_fn=lambda: 1.0); d.tick()
    return create_app(d, conn, gallery_dir=str(gallery_dir)).test_client(), conn


def test_gallery_serves_file(tmp_path):
    gdir = tmp_path / "gallery" / "ucok"; gdir.mkdir(parents=True)
    (gdir / "a.jpg").write_bytes(b"\xff\xd8\xffDATA")
    c, _ = _app(tmp_path, tmp_path / "gallery")
    r = c.get("/gallery/ucok/a.jpg")
    assert r.status_code == 200
    assert r.data == b"\xff\xd8\xffDATA"


def test_gallery_missing_is_404(tmp_path):
    (tmp_path / "gallery").mkdir()
    c, _ = _app(tmp_path, tmp_path / "gallery")
    assert c.get("/gallery/ucok/nope.jpg").status_code == 404


def test_gallery_rejects_traversal(tmp_path):
    (tmp_path / "gallery").mkdir()
    (tmp_path / "secret.txt").write_text("SECRET")    # sibling of gallery, outside it
    c, _ = _app(tmp_path, tmp_path / "gallery")
    r = c.get("/gallery/..%2fsecret.txt")             # encoded traversal attempt
    assert r.status_code in (400, 403, 404)
    assert b"SECRET" not in r.data


def test_cat_detail_photos_are_urls_not_fs_paths(tmp_path):
    gdir = tmp_path / "gallery" / "ucok"; gdir.mkdir(parents=True)
    (gdir / "a.jpg").write_text("x")
    c, _ = _app(tmp_path, tmp_path / "gallery")
    photos = c.get("/cat/Ucok").get_json()["photos"]
    assert photos == ["/gallery/ucok/a.jpg"], photos
