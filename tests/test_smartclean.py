from mw.smartclean import SmartClean


def test_cold_start_does_not_fire_without_presence():
    sc = SmartClean(idle_seconds=1)
    assert sc.update({"24": "standby"}, 0) is False    # never saw a cat
    assert sc.update({"24": "standby"}, 100) is False


def test_fires_after_idle():
    sc = SmartClean(idle_seconds=90)  # explicitly set to 90 for test
    assert sc.update({"24": "cat_get_in"}, 0) is False   # arms
    assert sc.update({"24": "standby"}, 10) is False     # standby_since=10
    assert sc.update({"24": "standby"}, 99) is False     # 89s < 90
    assert sc.update({"24": "standby"}, 100) is True     # 90s -> fire
    assert sc.update({"24": "standby"}, 200) is False    # one-shot, disarmed


def test_reentry_resets_idle_but_not_maxwait():
    sc = SmartClean(idle_seconds=90, max_wait_seconds=300)
    sc.update({"24": "cat_get_in"}, 0)
    sc.update({"24": "standby"}, 10)
    sc.update({"24": "cat_get_in"}, 50)                  # re-arm, idle resets
    sc.update({"24": "standby"}, 60)                     # new idle clock from 60
    assert sc.update({"24": "standby"}, 149) is False    # idle 89
    assert sc.update({"24": "standby"}, 150) is True     # idle 90


def test_maxwait_cap_beats_starvation():
    """Orange-cat starvation: re-enters every 30s so idle (90) never matures,
    yet the 300s max-wait cap (since first departure @100) fires the clean."""
    sc = SmartClean(idle_seconds=90, max_wait_seconds=300)
    sc.update({"24": "cat_get_in"}, 90)                  # arm

    fired_before_400 = False
    fired_at_or_after_400 = False
    max_idle_seen = 0.0

    # First departure at t=100. Then re-enter every 30s: standby for ~29s, dip in.
    # standby ticks every 10s; cat_get_in injected at 130,160,190,... resetting idle.
    t = 100
    while t <= 410:
        if t % 30 == 0 and t > 100:
            # brief re-entry resets the idle clock (idle never reaches 90)
            assert sc.update({"24": "cat_get_in"}, t) is False
        else:
            # track how long idle has run (should always be < 90)
            if sc._standby_since is not None:
                max_idle_seen = max(max_idle_seen, t - sc._standby_since)
            res = sc.update({"24": "standby"}, t)
            if res:
                if t < 400:
                    fired_before_400 = True
                else:
                    fired_at_or_after_400 = True
                break
        t += 10

    assert max_idle_seen < 90, f"idle matured ({max_idle_seen}); test not exercising the cap"
    assert not fired_before_400, "fired before the cap was reached"
    assert fired_at_or_after_400, "max-wait cap should have fired at/after t=400"


def test_notify_cleaned_disarms():
    sc = SmartClean()
    sc.update({"24": "cat_get_in"}, 0)
    sc.update({"24": "standby"}, 10)
    sc.notify_cleaned()
    assert sc.update({"24": "standby"}, 200) is False    # disarmed until next presence


def test_partial_poll_no_dp24_is_noop():
    sc = SmartClean()  # idle=90 default
    sc.update({"24": "cat_get_in"}, 0)
    sc.update({"24": "standby"}, 10)                      # standby_since=10
    assert sc.update({"102": "x"}, 11) is False           # partial poll: no-op
    assert sc.update({"24": "standby"}, 100) is True      # 90s from 10 -> fires


def test_disabled_never_fires():
    sc = SmartClean(enabled=False)
    sc.update({"24": "cat_get_in"}, 0)
    sc.update({"24": "standby"}, 0)
    assert sc.update({"24": "standby"}, 100) is False
