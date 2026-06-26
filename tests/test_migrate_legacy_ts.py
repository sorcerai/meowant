"""meowant-cft: legacy true-UTC '+00:00' rows mis-window under both raw-string
AND strftime comparison (the offset never cancels against a naive-local bound).
The real fix is format uniformity — normalize the closed set of legacy rows to
naive-local. After migration a legacy instant lands in/out of a window
correctly."""
from datetime import datetime

from mw import store
from scripts.migrate_legacy_timestamps import normalize_legacy_timestamps

LEGACY = "2026-06-21T00:21:10.372317+00:00"   # real legacy format (true UTC, frac)


def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok"])
    return conn


def test_normalizes_offset_to_naive_local(tmp_path):
    conn = _db(tmp_path)
    with store._lock:
        conn.execute("INSERT INTO events(ts, kind, detail) VALUES(?,?,?)", (LEGACY, "x", "{}"))
        conn.execute("INSERT INTO visits(enter_ts, leave_ts, eliminated) VALUES(?,?,1)", (LEGACY, LEGACY))
        conn.commit()
    changed = normalize_legacy_timestamps(conn)
    assert changed == 3
    expected = store._iso(datetime.fromisoformat(LEGACY).timestamp())
    assert "+" not in expected and not expected.endswith("Z")
    for tbl, col in [("events", "ts"), ("visits", "enter_ts"), ("visits", "leave_ts")]:
        v = conn.execute(f"SELECT {col} AS v FROM {tbl} LIMIT 1").fetchone()["v"]
        assert v == expected, f"{tbl}.{col} not normalized"


def test_idempotent(tmp_path):
    conn = _db(tmp_path)
    with store._lock:
        conn.execute("INSERT INTO events(ts, kind, detail) VALUES(?,?,?)", (LEGACY, "x", "{}"))
        conn.commit()
    assert normalize_legacy_timestamps(conn) == 1
    assert normalize_legacy_timestamps(conn) == 0   # nothing left to change


def test_legacy_row_windows_correctly_after_migration(tmp_path):
    """Legacy instant is 00:21:10 UTC June 21. A window whose bound is one minute
    AFTER that instant must EXCLUDE the row. Red before migration (tz offset
    wrongly includes it under both raw-string and strftime), green after."""
    conn = _db(tmp_path)
    with store._lock:
        cur = conn.execute("INSERT INTO visits(enter_ts, eliminated, cat_id, use_record, duration_s) "
                           "VALUES(?,1,1,60,60)", (LEGACY,))
        vid = cur.lastrowid
        conn.execute("INSERT INTO captures(ts, visit_id, camera, path) VALUES(?,?,?,?)",
                     (LEGACY, vid, "cam", "/x.jpg"))   # capture -> counts as 'framed'
        conn.commit()
    true_epoch = datetime.fromisoformat(LEGACY).timestamp()
    bound = store._iso(true_epoch + 60)                 # 1 min after -> correct: EXCLUDED
    before = store._iso(true_epoch + 60 + 24 * 3600)

    framed_before, _, _ = store.elimination_attribution_stats(conn, bound, before)
    assert framed_before == 1, "expected the pre-migration tz bug to wrongly include the legacy row"

    normalize_legacy_timestamps(conn)
    framed_after, _, _ = store.elimination_attribution_stats(conn, bound, before)
    assert framed_after == 0, "after migration the legacy row's true instant is before the bound"
