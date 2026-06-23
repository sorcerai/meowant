from datetime import datetime
from mw import store, weekly


def _conn():
    conn = store.connect(":memory:")
    store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Ella", "Garfield"])
    return conn


def _add_void(conn, cat, enter_epoch, dur, weight, *, eliminated=1):
    """Insert one visit row directly (bypasses the live pipeline)."""
    cid = store.cat_id_by_name(conn, cat) if cat else None
    iso = datetime.fromtimestamp(enter_epoch).isoformat(timespec="seconds")
    leave = datetime.fromtimestamp(enter_epoch + dur).isoformat(timespec="seconds")
    with store._lock:
        conn.execute(
            "INSERT INTO visits(enter_ts, leave_ts, duration_s, cat_id, confidence, "
            "eliminated, use_record) VALUES(?,?,?,?,?,?,?)",
            (iso, leave, dur, cid, 1.0 if cid else None, eliminated, weight))
        conn.commit()


def test_collect_facts_counts_and_gaps():
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    h = 3600.0
    # Ucok: 3 voids in the last week, ~4h apart
    _add_void(conn, "Ucok", now - 12 * h, 55, 50)
    _add_void(conn, "Ucok", now - 8 * h, 60, 55)
    _add_void(conn, "Ucok", now - 4 * h, 58, 52)
    facts = weekly.collect_facts(conn, now)
    u = facts["per_cat"]["Ucok"]
    assert u["voids"] == 3
    assert u["gap_h"]["n"] == 2
    assert abs(u["gap_h"]["mean"] - 4.0) < 0.01
    assert facts["period"]["days"] == 7


def test_collect_facts_garfield_pokes_excluded():
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    h = 3600.0
    _add_void(conn, "Garfield", now - 5 * h, 6, 3)     # poke: dur<=40 -> excluded
    _add_void(conn, "Garfield", now - 4 * h, 90, 88)   # real void
    facts = weekly.collect_facts(conn, now)
    assert facts["per_cat"]["Garfield"]["voids"] == 1   # only the real one


def test_collect_facts_attribution_and_flicker():
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    h = 3600.0
    _add_void(conn, "Ucok", now - 4 * h, 55, 50)            # attributed
    _add_void(conn, None, now - 3 * h, 5, None, eliminated=0)  # flicker fragment
    facts = weekly.collect_facts(conn, now)
    s = facts["system"]
    assert s["total_visits"] == 2 and s["attributed"] == 1 and s["unattributed"] == 1
    assert abs(s["attribution_pct"] - 50.0) < 0.01
    assert s["flicker_fragments"] == 1


def test_collect_facts_window_boundaries():
    """A void ~1h before now is in-window; one ~8d before now is not."""
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    h = 3600.0
    _add_void(conn, "Ucok", now - 1 * h, 55, 50)       # in this week
    _add_void(conn, "Ucok", now - 8 * 24 * h, 60, 55)  # 8 days ago -> excluded
    facts = weekly.collect_facts(conn, now)
    assert facts["per_cat"]["Ucok"]["voids"] == 1


def _facts(per_cat, system=None):
    base_sys = {"total_visits": 0, "attributed": 0, "unattributed": 0,
                "attribution_pct": 100.0, "prev_attribution_pct": None,
                "flicker_fragments": 0}
    if system:
        base_sys.update(system)
    return {"period": {"start": "x", "end": "y", "days": 7},
            "per_cat": per_cat, "system": base_sys}


def _cat(voids, gap_mean, gap_se, gap_n, prev_gap_mean=None, prev_gap_se=0.0,
         prev_gap_n=0, weight_mean=None, weight_se=0.0, weight_n=0,
         prev_weight_mean=None, prev_weight_se=0.0, prev_weight_n=0):
    return {"voids": voids, "per_day": round(voids / 7.0, 2),
            "gap_h": {"mean": gap_mean, "min": None, "max": None, "se": gap_se, "n": gap_n},
            "weight": {"mean": weight_mean, "se": weight_se, "n": weight_n},
            "circadian": {"night": 0, "morn": 0, "aft": 0, "eve": 0},
            "prev": {"voids": 0, "gap_mean_h": prev_gap_mean, "gap_se": prev_gap_se,
                     "gap_n": prev_gap_n, "weight_mean": prev_weight_mean,
                     "weight_se": prev_weight_se, "weight_n": prev_weight_n}}


def test_assess_insufficient_data():
    f = _facts({"Ucok": _cat(voids=3, gap_mean=3.0, gap_se=0.1, gap_n=2)})
    out = [x for x in weekly.assess(f) if x["cat"] == "Ucok"]
    assert out == [{"cat": "Ucok", "metric": "frequency",
                    "severity": "insufficient_data", "value": 3,
                    "margin": None, "delta": None,
                    "evidence": "N=3 voids this week — too few to judge drift"}]


def test_assess_nominal_within_noise():
    f = _facts({"Ucok": _cat(voids=20, gap_mean=3.2, gap_se=0.2, gap_n=19,
                             prev_gap_mean=3.0, prev_gap_se=0.2, prev_gap_n=18)})
    freq = [x for x in weekly.assess(f) if x["cat"] == "Ucok" and x["metric"] == "frequency"][0]
    assert freq["severity"] == "nominal"   # 0.2 delta < 2*sqrt(.2^2+.2^2)=0.566


def test_assess_watch_on_significant_delta():
    f = _facts({"Ucok": _cat(voids=20, gap_mean=6.0, gap_se=0.2, gap_n=19,
                             prev_gap_mean=3.0, prev_gap_se=0.2, prev_gap_n=18)})
    freq = [x for x in weekly.assess(f) if x["cat"] == "Ucok" and x["metric"] == "frequency"][0]
    assert freq["severity"] == "watch" and freq["delta"] == 3.0


def test_assess_drift_on_persistence():
    f = _facts({"Ucok": _cat(voids=20, gap_mean=6.0, gap_se=0.2, gap_n=19,
                             prev_gap_mean=3.0, prev_gap_se=0.2, prev_gap_n=18)})
    prev = [{"cat": "Ucok", "metric": "frequency", "severity": "watch", "delta": 2.5}]
    freq = [x for x in weekly.assess(f, prev_findings=prev)
            if x["cat"] == "Ucok" and x["metric"] == "frequency"][0]
    assert freq["severity"] == "drift"   # same cat+metric+sign, was watch -> escalates


def test_assess_attribution_drop():
    f = _facts({}, system={"attribution_pct": 40.0, "prev_attribution_pct": 70.0})
    attr = [x for x in weekly.assess(f) if x["metric"] == "attribution"][0]
    assert attr["severity"] == "watch" and attr["delta"] == -30.0


def test_assess_weight_drift_watch():
    f = _facts({"Ucok": _cat(voids=20, gap_mean=3.0, gap_se=0.2, gap_n=19,
                             prev_gap_mean=3.0, prev_gap_se=0.2, prev_gap_n=18,
                             weight_mean=60.0, weight_se=1.0, weight_n=19,
                             prev_weight_mean=50.0, prev_weight_se=1.0,
                             prev_weight_n=18)})
    w = [x for x in weekly.assess(f) if x["cat"] == "Ucok" and x["metric"] == "weight"][0]
    assert w["severity"] == "watch" and w["delta"] == 10.0


def test_assess_weight_nominal_no_prev():
    f = _facts({"Ucok": _cat(voids=20, gap_mean=3.0, gap_se=0.2, gap_n=19,
                             prev_gap_mean=3.0, prev_gap_se=0.2, prev_gap_n=18,
                             weight_mean=60.0, weight_se=1.0, weight_n=19,
                             prev_weight_mean=None, prev_weight_se=0.0,
                             prev_weight_n=0)})
    w = [x for x in weekly.assess(f) if x["cat"] == "Ucok" and x["metric"] == "weight"][0]
    assert w["severity"] == "nominal"   # no prev weight -> establishing baseline


def test_assess_zero_variance_floor_rounding_ignored():
    # se=0 both sides; tiny change vs prev mean 3.0 -> below 25% floor -> nominal
    f = _facts({"Ucok": _cat(voids=20, gap_mean=3.0001, gap_se=0.0, gap_n=19,
                             prev_gap_mean=3.0, prev_gap_se=0.0, prev_gap_n=18)})
    freq = [x for x in weekly.assess(f) if x["cat"] == "Ucok" and x["metric"] == "frequency"][0]
    assert freq["severity"] == "nominal"


def test_assess_zero_variance_floor_real_change():
    # se=0 both sides; doubling (3.0 -> 6.0) exceeds 25% of 3.0 floor -> watch
    f = _facts({"Ucok": _cat(voids=20, gap_mean=6.0, gap_se=0.0, gap_n=19,
                             prev_gap_mean=3.0, prev_gap_se=0.0, prev_gap_n=18)})
    freq = [x for x in weekly.assess(f) if x["cat"] == "Ucok" and x["metric"] == "frequency"][0]
    assert freq["severity"] == "watch" and freq["delta"] == 3.0


def test_assess_attribution_nominal_no_prev():
    f = _facts({}, system={"attribution_pct": 80.0, "prev_attribution_pct": None})
    attr = [x for x in weekly.assess(f) if x["metric"] == "attribution"][0]
    assert attr["severity"] == "nominal"


def test_assess_attribution_nominal_small_drop():
    # 70 -> 65 is a 5pp drop, below the 15pp threshold -> nominal
    f = _facts({}, system={"attribution_pct": 65.0, "prev_attribution_pct": 70.0})
    attr = [x for x in weekly.assess(f) if x["metric"] == "attribution"][0]
    assert attr["severity"] == "nominal"


def test_facts_only_text_renders_cats_and_system():
    f = _facts(
        {"Ucok": _cat(voids=20, gap_mean=6.0, gap_se=0.2, gap_n=19,
                      prev_gap_mean=3.0, prev_gap_se=0.2, prev_gap_n=18),
         "Ella": _cat(voids=3, gap_mean=10.0, gap_se=0.5, gap_n=2)},
        system={"total_visits": 40, "attributed": 30, "unattributed": 10,
                "attribution_pct": 75.0, "prev_attribution_pct": 78.0,
                "flicker_fragments": 8})
    findings = weekly.assess(f)
    txt = weekly.facts_only_text(f, findings)
    lines = txt.split("\n")
    ucok_line = next(l for l in lines if "Ucok" in l)
    ella_line = next(l for l in lines if "Ella" in l)
    # Ucok gap 3.0->6.0 is a significant delta -> frequency watch -> ⚠️ on its line
    assert "⚠️" in ucok_line
    # Ella has 3 voids (<5) -> insufficient_data banner on its line -> ❓
    assert "❓" in ella_line
    assert "insufficient" in ella_line.lower() and "N=3" in ella_line
    assert "75.0%" in txt                       # attribution line
    assert "8" in txt                           # flicker count


def test_facts_only_text_attribution_flagged_on_drop():
    f = _facts({}, system={"total_visits": 10, "attributed": 4, "unattributed": 6,
                           "attribution_pct": 40.0, "prev_attribution_pct": 70.0,
                           "flicker_fragments": 2})
    txt = weekly.facts_only_text(f, weekly.assess(f))
    assert "⚠️" in txt and "unidentified" in txt.lower()


def test_weekly_analyst_not_due_is_noop(tmp_path):
    conn = _conn()
    sp = str(tmp_path / "wk.json")
    sent = []
    a = weekly.WeeklyAnalyst(conn, lambda m: sent.append(m) or True,
                             now_fn=lambda: 1000.0, state_path=sp)
    a._save_state({"last_run": 1000.0})           # just ran
    assert a.run_once(1000.0 + 3600) is False      # 1h later -> not due
    assert sent == [] and store.latest_weekly_report(conn) is None


def test_weekly_analyst_due_persists_and_notifies(tmp_path):
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    for k in range(6):                              # 6 Ucok voids this week
        _add_void(conn, "Ucok", now - (20 - 2 * k) * 3600, 55, 50)
    sp = str(tmp_path / "wk.json")
    sent = []
    a = weekly.WeeklyAnalyst(conn, lambda m: sent.append(m) or True,
                             now_fn=lambda: now, state_path=sp)
    assert a.run_once(now) is True                  # no prior state -> due
    assert len(sent) == 1 and "Ucok" in sent[0]
    rep = store.latest_weekly_report(conn)
    assert rep is not None and rep["narrative_json"] is None
    assert a._load_state().get("last_run") == now   # stamped


def test_weekly_analyst_stamps_even_if_notify_fails(tmp_path):
    conn = _conn()
    now = datetime(2026, 6, 23, 12, 0, 0).timestamp()
    sp = str(tmp_path / "wk.json")
    a = weekly.WeeklyAnalyst(conn, lambda m: False,    # delivery fails
                             now_fn=lambda: now, state_path=sp)
    assert a.run_once(now) is True
    assert a._load_state().get("last_run") == now      # week still recorded (pull-recoverable)
    assert store.latest_weekly_report(conn) is not None
