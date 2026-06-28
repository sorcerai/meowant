from mw import store
from mw.events import Event, BIN_FULL, BIN_CLEAR, CLEAN_DONE, FAULT, FAULT_CLEAR

T = 1_000_000.0

def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn); return conn

def _ev(conn, kind, ts):
    store.insert_event(conn, Event(kind, ts))

def _fault(conn, ts, bitmap):
    store.insert_event(conn, Event(FAULT, ts, {"bitmap": bitmap}))

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

def test_active_fault_none_with_no_events(tmp_path):
    assert store.active_fault(_db(tmp_path)) is None

def test_active_fault_returns_onset_and_bitmap(tmp_path):
    conn = _db(tmp_path)
    _fault(conn, T, 1)                                   # faulted, never cleared
    assert store.active_fault(conn) == (store._iso(T), 1)

def test_active_fault_none_after_clear(tmp_path):
    conn = _db(tmp_path)
    _fault(conn, T, 1); _ev(conn, FAULT_CLEAR, T + 50)   # cleared
    assert store.active_fault(conn) is None

def test_active_fault_onset_is_earliest_since_clear_not_latest(tmp_path):
    # Bitmap shifts while still stuck (E1 -> E1+E2): onset must stay the FIRST fault
    # after the last clear so the UNUSABLE clock measures true stuck-duration; the
    # bitmap reported is the LATEST so the message names the current codes.
    conn = _db(tmp_path)
    _fault(conn, T - 1000, 1); _ev(conn, FAULT_CLEAR, T - 900)   # an old, cleared fault
    _fault(conn, T, 1)                                            # onset of current stretch
    _fault(conn, T + 4 * 3600, 3)                                # codes change, still stuck
    assert store.active_fault(conn) == (store._iso(T), 3)

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


def _cycle(conn, t, n_cleans):
    """Emit one fill cycle: bin_clear, n clean_done, bin_full. Returns next ts."""
    _ev(conn, BIN_CLEAR, t)
    for i in range(n_cleans):
        _ev(conn, CLEAN_DONE, t + i + 1)
    _ev(conn, BIN_FULL, t + n_cleans + 1)
    return t + n_cleans + 10


def test_bin_fill_capacity_resists_single_fluke(tmp_path):
    """fu2: a single fluke short cycle must not poison learned capacity forever.
    With many normal cycles (9 cleans) and one fluke (3), capacity should reflect
    the normal behavior (low percentile), not the global MIN (which would be 3 and
    nag every cycle with approaching_margin=2)."""
    conn = _db(tmp_path)
    t = T
    t = _cycle(conn, t, 3)                       # the fluke
    for _ in range(9):
        t = _cycle(conn, t, 9)                   # nine normal cycles
    cap = store.bin_fill_capacity(conn)
    assert cap == 9, f"single fluke poisoned capacity (got {cap}, want 9)"


def test_bin_fill_capacity_recent_window_ages_out_old_fluke(tmp_path):
    """fu2: capacity tracks recent behavior — an old fluke beyond the window
    ages out rather than sticking forever."""
    conn = _db(tmp_path)
    t = T
    t = _cycle(conn, t, 2)                       # ancient fluke
    for _ in range(13):
        t = _cycle(conn, t, 8)                   # 13 recent normal cycles (> window)
    cap = store.bin_fill_capacity(conn)
    assert cap == 8, f"old fluke not aged out (got {cap}, want 8)"
