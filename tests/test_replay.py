"""I2 — replay cycle_log.tsv and assert at least one visit ends up eliminated."""
import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mw import store
from mw.events import detect_events
from mw.tracker import VisitTracker

TSV_PATH = Path(__file__).parent.parent / "cycle_log.tsv"


def _ts(date_str):
    """Parse 'YYYY-MM-DD HH:MM:SS' into a UTC epoch float."""
    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _strip_dp(key):
    """'dp24' -> '24', 'dp102' -> '102'."""
    return key[2:] if key.startswith("dp") else key


def test_replay_finds_elimination(tmp_path):
    if not TSV_PATH.exists():
        pytest.skip(f"cycle_log.tsv not found at {TSV_PATH}")

    conn = store.connect(str(tmp_path / "replay.db"))
    store.init_db(conn)
    tracker = VisitTracker(conn)

    # Build successive full-state snapshots from the tsv change log.
    # dp102 elimination records legitimately appear with old=='' on their first
    # occurrence; detect_events naturally no-ops on repeated key=value pairs where
    # prev already holds the same value, so we process all rows including old==''.
    prev = {}

    with open(TSV_PATH, newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        for row in reader:
            # Skip comment lines and short/malformed rows
            if not row or row[0].startswith("#"):
                continue
            if len(row) < 5:
                continue

            ts_str, raw_key, _label, old_val, new_val = row[0], row[1], row[2], row[3], row[4]

            # Skip rows where the value did not change (old and new identical)
            if old_val == new_val:
                continue

            try:
                ts = _ts(ts_str)
            except ValueError:
                continue

            key = _strip_dp(raw_key)

            # Coerce value types to match what tinytuya returns
            value = new_val
            try:
                value = int(new_val)
            except (ValueError, TypeError):
                try:
                    if new_val.lower() == "true":
                        value = True
                    elif new_val.lower() == "false":
                        value = False
                except AttributeError:
                    pass

            incoming = {key: value}
            evs = detect_events(prev, incoming, ts)
            prev = {**prev, **incoming}

            for ev in evs:
                store.insert_event(conn, ev)
                tracker.handle(ev)

    rows = store.recent_visits(conn, 100)
    assert len(rows) > 0, "No visits recorded from replay"
    eliminated = [r for r in rows if r["eliminated"] == 1]
    assert len(eliminated) >= 1, (
        f"Expected at least one eliminated visit; got {len(eliminated)} of {len(rows)} visits"
    )
