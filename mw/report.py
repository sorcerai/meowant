"""Plain-text status/report builders, shared by the CLI (`meowant.py`) and the
Telegram command bot. Pure functions over the DB/state — no I/O, easy to test."""
from collections import Counter
from datetime import date, datetime

from mw import store


def _parse(ts):
    return datetime.fromisoformat(ts).replace(tzinfo=None)


def cat_report(conn, gap_s=30):
    """Per-cat usage built on flicker-collapsed sessions (store.sessions)."""
    sess = store.sessions(conn, gap_s=gap_s)
    frames = store.gallery_counts(conn)
    today = date.today().isoformat()
    raw_total = conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0]

    by_cat = {}
    for s in sess:
        by_cat.setdefault(s["cat"], []).append(s)

    lines = [f"🐈 PER-CAT REPORT — {len(sess)} sessions "
             f"({raw_total - len(sess)} flicker fragments collapsed)"]
    for cat in sorted(k for k in by_cat if k):
        rows = by_cat[cat]
        elim = [s for s in rows if s["eliminated"]]
        today_e = sum(1 for s in elim if s["enter_ts"].startswith(today))
        durs = [s["duration_s"] for s in elim if s["duration_s"]]
        hours = Counter(s["enter_ts"][11:13] for s in elim)
        scored = [s for s in rows if s["scatter_severity"] is not None]
        messy = [s for s in scored if s["scatter_severity"] >= 1]
        lines.append(f"\n• {cat}: {len(rows)} sessions "
                     f"({len(elim)} elim, {today_e} today)")
        if rows:
            lines.append(f"   last seen {rows[0]['enter_ts'][5:16].replace('T', ' ')}")
        if durs:
            lines.append(f"   avg elim {sum(durs)//len(durs)}s")
        if hours:
            lines.append("   busiest " +
                         ", ".join(f"{h}:xx({n})" for h, n in hours.most_common(3)))
        lines.append(f"   gallery {frames.get(cat, 0)} frames")
        if scored:
            avg = sum(s["scatter_pct"] for s in scored) / len(scored)
            lines.append(f"   scatter {len(messy)}/{len(scored)} messy, avg {avg:.1f}%")
    pending = by_cat.get(None, [])
    if pending:
        p_elim = sum(1 for s in pending if s["eliminated"])
        lines.append(f"\n• unattributed (occlusion): {len(pending)} sessions "
                     f"({p_elim} elim)")
    return "\n".join(lines)


def health_report(conn, now=None):
    """Time since the last elimination overall and per cat — the 'is everyone
    still going' pull view (companion to the no-go alarm). `now` is epoch secs
    (injectable for tests); defaults to wall clock."""
    now = now if now is not None else datetime.now().timestamp()
    sess = store.sessions(conn)
    elim = [s for s in sess if s["eliminated"]]
    if not elim:
        return "🩺 HEALTH: no eliminations recorded yet."

    def _ago(ts):
        h = (now - _parse(ts).timestamp()) / 3600.0
        return f"{h:.1f}h ago" if h < 48 else f"{h/24:.1f}d ago"

    lines = [f"🩺 HEALTH — last box use {_ago(elim[0]['enter_ts'])}"]
    latest = {}
    for s in elim:                      # sessions are newest-first; keep the first per cat
        latest.setdefault(s["cat"], s["enter_ts"])
    for cat in sorted(k for k in latest if k):
        lines.append(f"• {cat}: last {_ago(latest[cat])}")
    return "\n".join(lines)


def status_report(conn, state):
    """Current device/daemon snapshot. `state` is the daemon's raw dp dict."""
    dp24 = state.get("24", "?")
    bin_full = bool(int(state.get("21", 0) or 0) & 1)
    fault = int(state.get("22", 0) or 0)
    uses = store.eliminations_today(conn)
    parts = [f"📟 STATUS — box: {dp24}",
             f"• uses today: {uses}",
             f"• bin: {'FULL ⚠️' if bin_full else 'ok'}",
             f"• fault: {'E'+str(fault)+' ⚠️' if fault else 'none'}"]
    return "\n".join(parts)


def digest(conn, now=None):
    """One-line-ish 'alive + today' summary for the daily heartbeat digest."""
    from datetime import date, datetime
    now = now if now is not None else datetime.now().timestamp()
    today = date.fromtimestamp(now).isoformat()
    sess = store.sessions(conn)
    today_elim = [s for s in sess if s["eliminated"] and s["enter_ts"].startswith(today)]
    if not today_elim:
        return f"✅ Meowant alive [{today}] — no box uses yet today."
    from collections import Counter
    by_cat = Counter((s["cat"] or "unattributed") for s in today_elim)
    last = max(s["enter_ts"] for s in today_elim)[11:16]
    parts = ", ".join(f"{c} {n}" for c, n in by_cat.most_common())
    return (f"✅ Meowant alive [{today}] — {len(today_elim)} box uses today "
            f"(last {last}). {parts}")
