from mw import store
from mw.events import Event, BIN_FULL, BIN_CLEAR, CLEAN_DONE

T = 1_000_000.0

def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn); return conn

def _ev(conn, kind, ts):
    store.insert_event(conn, Event(kind, ts))

def test_bin_full_since_none_when_clear(tmp_path):
    conn = _db(tmp_path)
    _ev(conn, BIN_FULL, T); _ev(conn, BIN_CLEAR, T + 100)   # cleared after full
    assert store.bin_full_since(conn) is None

def test_bin_full_since_returns_ts_when_full(tmp_path):
    conn = _db(tmp_path)
    _ev(conn, BIN_FULL, T - 100); _ev(conn, BIN_CLEAR, T - 50)  # an old cycle
    _ev(conn, BIN_FULL, T)                                       # full again, not cleared
    assert store.bin_full_since(conn) == store._iso(T)

def test_bin_full_since_none_with_no_events(tmp_path):
    assert store.bin_full_since(_db(tmp_path)) is None

def test_last_bin_clear_ts(tmp_path):
    conn = _db(tmp_path)
    assert store.last_bin_clear_ts(conn) is None
    _ev(conn, BIN_CLEAR, T); _ev(conn, BIN_CLEAR, T + 500)
    assert store.last_bin_clear_ts(conn) == store._iso(T + 500)

def test_cleans_since_counts_after_bound(tmp_path):
    conn = _db(tmp_path)
    _ev(conn, CLEAN_DONE, T - 10)                     # before the bound -> excluded
    _ev(conn, BIN_CLEAR, T)
    for i in range(3):
        _ev(conn, CLEAN_DONE, T + 60 * (i + 1))       # 3 after -> counted
    assert store.cleans_since(conn, store._iso(T)) == 3

def test_bin_fill_capacity_min_over_cycles(tmp_path):
    conn = _db(tmp_path)
    # cycle A: clear, 5 cleans, full
    _ev(conn, BIN_CLEAR, T)
    for i in range(5): _ev(conn, CLEAN_DONE, T + i + 1)
    _ev(conn, BIN_FULL, T + 10)
    # cycle B: clear, 2 cleans, full  -> min should be 2
    _ev(conn, BIN_CLEAR, T + 20)
    for i in range(2): _ev(conn, CLEAN_DONE, T + 21 + i)
    _ev(conn, BIN_FULL, T + 30)
    assert store.bin_fill_capacity(conn) == 2

def test_bin_fill_capacity_none_without_complete_cycle(tmp_path):
    conn = _db(tmp_path)
    _ev(conn, BIN_CLEAR, T)
    _ev(conn, CLEAN_DONE, T + 1)          # no bin_full yet -> no complete cycle
    assert store.bin_fill_capacity(conn) is None


def test_bin_fill_capacity_ignores_zero_clean_cycle(tmp_path):
    """Fix 4: a degenerate cycle (bin_clear immediately followed by bin_full with
    zero clean_done in between) must not pollute the min — min(0, 5) = 0 would make
    the `if cap:` guard in callers falsy, silently disabling approaching-full forever.
    The real min over non-degenerate cycles (5 here) must be returned instead."""
    conn = _db(tmp_path)
    # Degenerate cycle: bin_clear → bin_full with zero cleans
    _ev(conn, BIN_CLEAR, T)
    _ev(conn, BIN_FULL, T + 1)            # 0 cleans → degenerate cycle
    # Normal cycle: clear → 5 cleans → full
    _ev(conn, BIN_CLEAR, T + 100)
    for i in range(5): _ev(conn, CLEAN_DONE, T + 101 + i)
    _ev(conn, BIN_FULL, T + 200)
    # Zero-clean cycle must be filtered out; result must be 5 (not 0)
    assert store.bin_fill_capacity(conn) == 5
