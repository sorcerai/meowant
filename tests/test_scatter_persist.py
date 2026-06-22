"""Persistence for per-visit scatter scores + the per-cat blame tally."""
from mw import store


def _db():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def test_migration_adds_scatter_columns():
    conn = _db()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(visits)")}
    assert {"scatter_severity", "scatter_pct", "scatter_area"} <= cols


def test_set_and_get_visit_scatter():
    conn = _db()
    vid = store.open_visit(conn, 1000.0)
    store.set_visit_scatter(conn, vid, 2, 4.72, 12844)
    row = store.get_visit(conn, vid)
    assert row["scatter_severity"] == 2
    assert row["scatter_pct"] == 4.72
    assert row["scatter_area"] == 12844


def test_get_visit_missing_is_none():
    assert store.get_visit(_db(), 999) is None


def test_per_cat_scatter_blame_tally():
    conn = _db()
    store.seed_cats(conn, ["Garfield", "Ella"])
    g = store.cat_id_by_name(conn, "Garfield")
    e = store.cat_id_by_name(conn, "Ella")

    # Garfield: two visits, both messy. Ella: two visits, both clean.
    for sev, pct in [(3, 6.0), (2, 4.0)]:
        v = store.open_visit(conn, 1000.0)
        store.set_visit_identity(conn, v, g, 1.0)
        store.set_visit_scatter(conn, v, sev, pct, 9000)
    for _ in range(2):
        v = store.open_visit(conn, 1000.0)
        store.set_visit_identity(conn, v, e, 1.0)
        store.set_visit_scatter(conn, v, 0, 0.02, 0)

    # An unattributed scored visit (cat_id NULL) must not appear in the tally.
    v = store.open_visit(conn, 1000.0)
    store.set_visit_scatter(conn, v, 3, 7.0, 11000)

    tally = store.per_cat_scatter(conn)
    by_name = {r["name"]: r for r in tally}
    assert by_name["Garfield"]["scored"] == 2
    assert by_name["Garfield"]["messes"] == 2
    assert by_name["Ella"]["messes"] == 0
    # Worst-first: Garfield (2 messes) ahead of Ella (0).
    assert tally[0]["name"] == "Garfield"
    # Only attributed cats appear (the NULL-cat visit is excluded).
    assert set(by_name) == {"Garfield", "Ella"}
