#!/usr/bin/env python3
"""Run ONE dead-man's-switch pass and exit. Scheduled by launchd (StartInterval),
independent of meowantd so it can't share its failure mode."""
from mw import cat_status, config, store
from mw.alerts import make_notify
from mw.deadman import DeadManSwitch


def main():
    cfg = config.load("config.json")
    # Apply config thresholds here too: this is a SEPARATE process from meowantd,
    # so without this the deadman would use code-default thresholds while the main
    # daemon uses config-edited ones — the two surfaces would diverge (29e).
    cat_status.load_thresholds(cfg)
    g = lambda k, d=None: config.get(cfg, k, d)
    conn = store.connect(g("deadman.db_path", "meowant.db"))
    sw = DeadManSwitch(
        conn, notify=make_notify(lambda k: config.get(cfg, k)),
        no_go_hours=g("deadman.no_go_hours", 12),
        quiet_start=g("quiet_start", "22:00"), quiet_end=g("quiet_end", "08:00"),
        per_cat_enabled=g("deadman.per_cat_enabled", False),
        per_cat_hours=g("deadman.per_cat_hours", 24),
        liveness_stale_s=g("deadman.liveness_stale_s", 180),
        realarm_hours=g("deadman.realarm_hours", 3),
        state_path=g("deadman.state_path", "deadman_state.json"))
    n = sw.run_once()
    print(f"[deadman] pass complete, {n} alert(s) fired")


if __name__ == "__main__":
    main()
