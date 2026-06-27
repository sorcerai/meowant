import os

import cv2
import numpy as np

from mw import store
import scripts.backfill_is_ir as bf


def _cap(tmp, name, color):
    img = np.zeros((60, 80, 3), np.uint8)
    if color:
        img[:, :, 0] = 200
        img[:, :, 2] = 20
    else:
        img[:] = 90
    p = str(tmp / name)
    cv2.imwrite(p, img)
    return p


def _cap_at(path, color):
    """Write a grayscale or color JPEG to an absolute path."""
    img = np.zeros((60, 80, 3), np.uint8)
    if color:
        img[:, :, 0] = 200
        img[:, :, 2] = 20
    else:
        img[:] = 90
    cv2.imwrite(str(path), img)


def test_backfill_sets_is_ir(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    v = store.open_visit(conn, 1000.0)
    ir = _cap(tmp_path, "ir.jpg", color=False)
    col = _cap(tmp_path, "col.jpg", color=True)
    store.insert_capture(conn, 1000.0, v, "meowcam1", ir, is_ir=None)
    store.insert_capture(conn, 1001.0, v, "meowcam1", col, is_ir=None)
    n = bf.backfill(conn)
    rows = {r["path"]: r["is_ir"]
            for r in conn.execute("SELECT path, is_ir FROM captures").fetchall()}
    assert n == 2
    assert rows[ir] == 1 and rows[col] == 0


def test_backfill_resolves_relative_paths(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    v = store.open_visit(conn, 1000.0)
    os.makedirs(tmp_path / "sub", exist_ok=True)
    ir_rel = "sub/ir.jpg"
    col_rel = "sub/col.jpg"
    _cap_at(tmp_path / ir_rel, color=False)
    _cap_at(tmp_path / col_rel, color=True)
    store.insert_capture(conn, 1000.0, v, "c", ir_rel, is_ir=None)
    store.insert_capture(conn, 1001.0, v, "c", col_rel, is_ir=None)
    n = bf.backfill(conn, base_dir=str(tmp_path))
    rows = {r["path"]: r["is_ir"]
            for r in conn.execute("SELECT path, is_ir FROM captures").fetchall()}
    assert n == 2
    assert rows[ir_rel] == 1 and rows[col_rel] == 0
