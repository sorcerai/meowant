"""Unit tests for scripts/pretrip.py — every check function is driven with
injected fakes (fake state dict, fake config dict, tmp warm dir with
controlled mtimes, fake ssh/http runners, a tmp sqlite db via mw.store).
No real daemon, camera, SSH, or network access."""
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
for _p in (REPO_ROOT, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pretrip
from mw import store

T = time.mktime((2026, 7, 15, 12, 0, 0, 0, 0, -1))


# ---------------------------------------------------------------------------
# 1. daemon running
# ---------------------------------------------------------------------------

def test_daemon_running_pass():
    results = pretrip.check_daemon_running(
        run_launchctl=lambda: "123\t0\tcom.meowant.daemon\n")
    assert results == [("daemon running", True, "com.meowant.daemon is loaded")]


def test_daemon_running_fail_not_listed():
    name, ok, detail = pretrip.check_daemon_running(
        run_launchctl=lambda: "123\t0\tcom.other.thing\n")[0]
    assert ok is False
    assert "NOT in launchctl list" in detail


def test_daemon_running_fail_on_exception():
    def boom():
        raise RuntimeError("launchctl not found")
    name, ok, detail = pretrip.check_daemon_running(run_launchctl=boom)[0]
    assert ok is False
    assert "launchctl not found" in detail


# ---------------------------------------------------------------------------
# 2. daemon API
# ---------------------------------------------------------------------------

def _state(faults=("none",), bin_full=False, status="standby", contents_load=200):
    return {
        "faults": list(faults),
        "bin_full": bin_full,
        "status": status,
        "named": {"contents_load": contents_load},
    }


def test_daemon_api_all_pass():
    cfg = {"litter": {"low_threshold": 110}}
    results = pretrip.check_daemon_api(cfg, fetch_state=lambda: _state())
    by_name = {n: (ok, d) for n, ok, d in results}
    assert by_name["daemon API: faults"][0] is True
    assert by_name["daemon API: bin_full"][0] is True
    assert by_name["daemon API: litter level"][0] is True


def test_daemon_api_active_fault_fails():
    cfg = {}
    st = _state(faults=("E1",))
    results = pretrip.check_daemon_api(cfg, fetch_state=lambda: st)
    faults = dict((n, (ok, d)) for n, ok, d in results)["daemon API: faults"]
    assert faults[0] is False
    assert "E1" in faults[1]


def test_daemon_api_bin_full_fails():
    cfg = {}
    st = _state(bin_full=True)
    results = pretrip.check_daemon_api(cfg, fetch_state=lambda: st)
    bf = dict((n, (ok, d)) for n, ok, d in results)["daemon API: bin_full"]
    assert bf[0] is False
    assert "FULL" in bf[1]


def test_daemon_api_litter_below_threshold_fails():
    cfg = {"litter": {"low_threshold": 110}}
    st = _state(contents_load=90)
    results = pretrip.check_daemon_api(cfg, fetch_state=lambda: st)
    lvl = dict((n, (ok, d)) for n, ok, d in results)["daemon API: litter level"]
    assert lvl[0] is False


def test_daemon_api_litter_near_threshold_warns():
    cfg = {"litter": {"low_threshold": 110}}
    st = _state(contents_load=120)   # within 20 of 110
    results = pretrip.check_daemon_api(cfg, fetch_state=lambda: st)
    lvl = dict((n, (ok, d)) for n, ok, d in results)["daemon API: litter level"]
    assert lvl[0] is None


def test_daemon_api_not_standby_warns_unreadable():
    cfg = {"litter": {"low_threshold": 110}}
    st = _state(status="cleaning", contents_load=5)
    results = pretrip.check_daemon_api(cfg, fetch_state=lambda: st)
    lvl = dict((n, (ok, d)) for n, ok, d in results)["daemon API: litter level"]
    assert lvl[0] is None
    assert "unreadable" in lvl[1]


def test_daemon_api_fetch_failure():
    def boom():
        raise ConnectionError("refused")
    results = pretrip.check_daemon_api({}, fetch_state=boom)
    assert len(results) == 1
    assert results[0][1] is False
    assert "refused" in results[0][2]


# ---------------------------------------------------------------------------
# 3. cameras
# ---------------------------------------------------------------------------

def _cam_cfg(blackout_ignore=None):
    cfg = {
        "cameras": [{"name": "meowcam1"}, {"name": "meowcam2"},
                    {"name": "meowcam3"}, {"name": "meowcam4"},
                    {"name": "meowcam5"}, {"name": "meowcam6"}],
        "bowls": [{"camera": "meowcam6"}, {"camera": "meowcam5"}],
    }
    if blackout_ignore is not None:
        cfg["capture"] = {"blackout_ignore_cams": blackout_ignore}
    return cfg


def test_litter_cams_excludes_bowl_cams():
    assert pretrip.litter_cams(_cam_cfg()) == [
        "meowcam1", "meowcam2", "meowcam3", "meowcam4"]


def test_cameras_all_fresh(tmp_path):
    warm_dir = tmp_path / "warm_frames"
    warm_dir.mkdir()
    for cam in ("meowcam1", "meowcam2", "meowcam3", "meowcam4"):
        p = warm_dir / f"{cam}.jpg"
        p.write_bytes(b"x")
        os.utime(p, (T - 10, T - 10))   # 10s old
    cfg = _cam_cfg(blackout_ignore=[])
    results = pretrip.check_cameras(cfg, warm_dir=str(warm_dir), now_fn=lambda: T)
    assert all(ok is True for _, ok, _ in results)
    assert len(results) == 4


def test_cameras_stale_fails(tmp_path):
    warm_dir = tmp_path / "warm_frames"
    warm_dir.mkdir()
    for cam in ("meowcam1", "meowcam2", "meowcam3", "meowcam4"):
        p = warm_dir / f"{cam}.jpg"
        p.write_bytes(b"x")
        os.utime(p, (T - 10, T - 10))
    # meowcam2 is stale: last written 300s ago (> 120s max age)
    os.utime(warm_dir / "meowcam2.jpg", (T - 300, T - 300))
    cfg = _cam_cfg(blackout_ignore=[])
    results = pretrip.check_cameras(cfg, warm_dir=str(warm_dir), now_fn=lambda: T)
    by_name = {n: ok for n, ok, _ in results}
    assert by_name["camera meowcam2"] is False
    assert by_name["camera meowcam1"] is True


def test_cameras_missing_file_fails(tmp_path):
    warm_dir = tmp_path / "warm_frames"
    warm_dir.mkdir()   # no files written at all
    cfg = _cam_cfg(blackout_ignore=[])
    results = pretrip.check_cameras(cfg, warm_dir=str(warm_dir), now_fn=lambda: T)
    assert all(ok is False for _, ok, _ in results)
    assert all("missing" in d for _, _, d in results)


def test_cameras_blackout_ignored_reported_non_critical(tmp_path):
    warm_dir = tmp_path / "warm_frames"
    warm_dir.mkdir()
    # meowcam4 is stale AND blackout-ignored -> must not count as critical
    for cam in ("meowcam1", "meowcam2", "meowcam3"):
        p = warm_dir / f"{cam}.jpg"
        p.write_bytes(b"x")
        os.utime(p, (T - 10, T - 10))
    cfg = _cam_cfg(blackout_ignore=["meowcam4"])   # meowcam4.jpg never written
    results = pretrip.check_cameras(cfg, warm_dir=str(warm_dir), now_fn=lambda: T)
    ignored = dict((n, ok) for n, ok, _ in results)["camera meowcam4 (blackout-ignored)"]
    assert ignored is None   # non-critical, reported separately


# ---------------------------------------------------------------------------
# 4. bridge
# ---------------------------------------------------------------------------

def _bridge_cfg(password="secret"):
    cfg = {"bridge": {"ssh_host": "192.168.2.79", "ssh_user": "root"}}
    if password is not None:
        cfg["bridge"]["ssh_password"] = password  # legacy key: ignored by check
    return cfg


def _bridge_ssh_fake(disk_line="/dev/root 1 1 1 20% /\n",
                     up_cams="meowcam1\nmeowcam2\n"):
    """Scripted ssh runner: answers the df probe and the on-bridge ffprobe
    stream sweep (which echoes one cam name per decodable stream)."""
    def runner(host, user, cmd):
        if cmd.startswith("df"):
            return disk_line
        if "ffprobe" in cmd:
            return up_cams
        raise AssertionError(f"unexpected cmd: {cmd}")
    return runner


def test_bridge_all_pass():
    cfg = _bridge_cfg()
    runner = _bridge_ssh_fake(
        disk_line="/dev/root       30000000 6000000  23000000      20% /\n")
    results = pretrip.check_bridge(cfg, ssh_runner=runner)
    by_name = {n: (ok, d) for n, ok, d in results}
    assert by_name["bridge: disk"][0] is True
    assert "20%" in by_name["bridge: disk"][1]
    assert by_name["bridge: streams"][0] is True
    assert "2/3" in by_name["bridge: streams"][1]


def test_bridge_disk_over_threshold_fails():
    cfg = _bridge_cfg()
    runner = _bridge_ssh_fake(
        disk_line="/dev/root       30000000 27000000  3000000  90% /\n",
        up_cams="")
    results = pretrip.check_bridge(cfg, ssh_runner=runner)
    disk = dict((n, ok) for n, ok, _ in results)["bridge: disk"]
    assert disk is False


def test_bridge_no_decoding_streams_fails():
    cfg = _bridge_cfg()
    runner = _bridge_ssh_fake(up_cams="")   # sweep ran, nothing decoded
    results = pretrip.check_bridge(cfg, ssh_runner=runner)
    mtx = dict((n, (ok, d)) for n, ok, d in results)["bridge: streams"]
    assert mtx[0] is False
    assert "0/3" in mtx[1]


def test_bridge_probe_avoids_disabled_mediamtx_api():
    """mediamtx.yml on the bridge has `api: false` — :9997 refuses every
    connection, which silently blinded the old curl-based probe. The check
    must ffprobe the streams themselves and never touch the API port."""
    cfg = _bridge_cfg()
    cmds = []
    def runner(host, user, cmd):
        cmds.append(cmd)
        return "/dev/root 1 1 1 10% /\n" if cmd.startswith("df") else "meowcam1\n"
    pretrip.check_bridge(cfg, ssh_runner=runner)
    assert any("ffprobe" in c for c in cmds)
    assert not any("9997" in c for c in cmds)


def test_bridge_ssh_failure_reported_critical():
    cfg = _bridge_cfg()
    def runner(host, user, cmd):
        raise RuntimeError("connection refused")
    results = pretrip.check_bridge(cfg, ssh_runner=runner)
    by = dict((n, (ok, d)) for n, ok, d in results)
    assert by["bridge: disk"][0] is False
    assert "connection refused" in by["bridge: disk"][1]
    assert by["bridge: streams"][0] is False   # sweep also rides ssh


def test_bridge_probe_failure_reported_critical():
    cfg = _bridge_cfg()
    def runner(host, user, cmd):
        if cmd.startswith("df"):
            return "/dev/root 1 1 1 10% /\n"
        raise TimeoutError("bridge unreachable")
    results = pretrip.check_bridge(cfg, ssh_runner=runner)
    mtx = dict((n, (ok, d)) for n, ok, d in results)["bridge: streams"]
    assert mtx[0] is False
    assert "bridge unreachable" in mtx[1]


def test_bridge_uses_key_auth_user_from_config():
    cfg = _bridge_cfg()
    cfg["bridge"]["ssh_user"] = "aria"
    seen = []
    def runner(host, user, cmd):
        seen.append((host, user))
        return "/dev/root 1 1 1 10% /\n" if cmd.startswith("df") else ""
    pretrip.check_bridge(cfg, ssh_runner=runner)
    assert all(u == "aria" for _h, u in seen)


# ---------------------------------------------------------------------------
# 5. sitters
# ---------------------------------------------------------------------------

def test_sitters_zero_ids_fails():
    ok = pretrip.check_sitters({"alerts": {"telegram_chat_ids": []}})[0][1]
    assert ok is False


def test_sitters_one_id_fails_owner_only():
    name, ok, detail = pretrip.check_sitters(
        {"alerts": {"telegram_chat_ids": ["744579489"]}})[0]
    assert ok is False
    assert "add a sitter" in detail


def test_sitters_two_ids_passes():
    ok = pretrip.check_sitters(
        {"alerts": {"telegram_chat_ids": ["744579489", "555555"]}})[0][1]
    assert ok is True


def test_sitters_missing_key_fails():
    ok = pretrip.check_sitters({})[0][1]
    assert ok is False


# ---------------------------------------------------------------------------
# 6. watchers armed
# ---------------------------------------------------------------------------

def test_watchers_default_all_armed():
    cfg = {"feeders": [{"enabled": True, "label": "downstairs"}]}
    results = pretrip.check_watchers_armed(cfg)
    assert all(ok is True for _, ok, _ in results)


def test_watchers_explicit_disable_fails():
    cfg = {
        "jam_watch": {"enabled": False},
        "litter": {"watch_enabled": False},
        "feed_plan_sync": {"enabled": False},
        "bridge": {"enabled": False},
        "feeders": [{"enabled": True, "label": "downstairs", "deadman_enabled": False}],
    }
    results = pretrip.check_watchers_armed(cfg)
    by_name = {n: ok for n, ok, _ in results}
    assert by_name["watcher: jam watch"] is False
    assert by_name["watcher: litter watch"] is False
    assert by_name["watcher: feed-plan sync"] is False
    assert by_name["watcher: bridge"] is False
    assert by_name["watcher: feeder deadman (downstairs)"] is False


def test_watchers_bridge_check_skipped_when_key_absent():
    cfg = {"feeders": []}
    results = pretrip.check_watchers_armed(cfg)
    names = [n for n, _, _ in results]
    assert "watcher: bridge" not in names


def test_watchers_disabled_feeder_not_reported():
    cfg = {"feeders": [{"enabled": False, "label": "unused", "deadman_enabled": False}]}
    results = pretrip.check_watchers_armed(cfg)
    names = [n for n, _, _ in results]
    assert not any("unused" in n for n in names)


# ---------------------------------------------------------------------------
# 7. feeder schedules
# ---------------------------------------------------------------------------

def test_feeder_schedules_pass():
    cfg = {"feeders": [{"enabled": True, "label": "downstairs",
                        "mealtimes": ["08:00", "17:00"]}]}
    ok = pretrip.check_feeder_schedules(cfg)[0][1]
    assert ok is True


def test_feeder_schedules_empty_fails():
    cfg = {"feeders": [{"enabled": True, "label": "upstairs", "mealtimes": []}]}
    ok = pretrip.check_feeder_schedules(cfg)[0][1]
    assert ok is False


def test_feeder_schedules_ignores_disabled_feeders():
    cfg = {"feeders": [{"enabled": False, "label": "off", "mealtimes": []}]}
    results = pretrip.check_feeder_schedules(cfg)
    assert results[0][0] == "feeder schedules"
    assert results[0][1] is None   # no enabled feeders -> informational


# ---------------------------------------------------------------------------
# 8. heartbeat
# ---------------------------------------------------------------------------

def test_heartbeat_present_passes():
    ok = pretrip.check_heartbeat({"health": {"heartbeat_url": "https://hc-ping.com/x"}})[0][1]
    assert ok is True


def test_heartbeat_missing_fails():
    ok = pretrip.check_heartbeat({})[0][1]
    assert ok is False


# ---------------------------------------------------------------------------
# 9. recent attribution
# ---------------------------------------------------------------------------

def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


def _insert_visit(conn, ts, cat_id):
    with store._lock:
        conn.execute(
            "INSERT INTO visits(enter_ts, cat_id) VALUES(?, ?)",
            (store._iso(ts), cat_id))
        conn.commit()


def test_attribution_no_visits_at_all_warns(tmp_path):
    conn = _db(tmp_path)
    name, ok, detail = pretrip.check_recent_attribution(conn, now_fn=lambda: T)[0]
    assert ok is None
    assert "no visits" in detail


def test_attribution_all_unattributed_fails(tmp_path):
    conn = _db(tmp_path)
    _insert_visit(conn, T - 3600, None)
    _insert_visit(conn, T - 7200, None)
    name, ok, detail = pretrip.check_recent_attribution(conn, now_fn=lambda: T)[0]
    assert ok is False
    assert "0/2" in detail


def test_attribution_at_least_one_attributed_passes(tmp_path):
    conn = _db(tmp_path)
    _insert_visit(conn, T - 3600, None)
    _insert_visit(conn, T - 7200, 1)
    name, ok, detail = pretrip.check_recent_attribution(conn, now_fn=lambda: T)[0]
    assert ok is True
    assert "1/2" in detail


def test_attribution_respects_window(tmp_path):
    conn = _db(tmp_path)
    # Outside the 48h window entirely -> must not count, must warn "no visits"
    _insert_visit(conn, T - 72 * 3600, 1)
    name, ok, detail = pretrip.check_recent_attribution(
        conn, window_hours=48, now_fn=lambda: T)[0]
    assert ok is None
    assert "no visits" in detail


# ---------------------------------------------------------------------------
# 10. disk on Mac
# ---------------------------------------------------------------------------

def _usage(total, used):
    class U:
        pass
    u = U()
    u.total, u.used, u.free = total, used, total - used
    return u


def test_disk_space_under_threshold_passes():
    ok = pretrip.check_disk_space(
        disk_usage_fn=lambda p: _usage(100, 50))[0][1]
    assert ok is True


def test_disk_space_over_threshold_fails():
    ok = pretrip.check_disk_space(
        disk_usage_fn=lambda p: _usage(100, 95))[0][1]
    assert ok is False


# ---------------------------------------------------------------------------
# send-test (optional flag)
# ---------------------------------------------------------------------------

def test_send_test_alerts_reports_per_recipient():
    cfg = {"alerts": {"telegram_bot_token": "tok", "telegram_chat_id": "1",
                       "telegram_chat_ids": ["2", "3"]}}
    calls = []
    def fake_notify(msg, token, chat_id):
        calls.append(chat_id)
        return chat_id != "3"   # simulate one recipient failing
    results = pretrip.send_test_alerts(cfg, telegram_notify=fake_notify)
    assert calls == ["1", "2", "3"]
    by_name = {n: ok for n, ok, _ in results}
    assert by_name["send-test -> 1"] is True
    assert by_name["send-test -> 3"] is False


def test_send_test_alerts_no_recipients_fails():
    results = pretrip.send_test_alerts({})
    assert results[0][1] is False
