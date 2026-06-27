"""Shadow-mode evaluation of the DINOv2 GalleryMatcher.

Scores COMPLETED visits offline and logs the matcher's prediction next to the live
(agy) attribution — WITHOUT touching production attribution, alerts, or the DB's
visit rows. Fully isolated: it reads finished visits, appends a JSONL line, and a
daily job summarizes agreement + flags disagreements to the OWNER. Any failure is
logged and skipped; production is never affected. This is how we accumulate live
accuracy data during the trip before promoting the matcher to the decider.
"""
import json
import os
import sys
import time
from datetime import datetime

from mw import store, identify


def _append_jsonl(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def read_records(path):
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


class ShadowScorer:
    """Scores new eliminated visits with the matcher and appends results to a
    JSONL log. Tracks progress by last-scored visit id in a state file."""

    def __init__(self, conn, matcher, log_path, state_path, now_fn=time.time):
        self.conn = conn
        self.matcher = matcher
        self.log_path = log_path
        self.state_path = state_path
        self.now = now_fn

    def _last(self):
        try:
            with open(self.state_path) as f:
                return int(json.load(f).get("last_visit_id", 0))
        except Exception:
            return 0

    def _save(self, vid):
        try:
            with open(self.state_path, "w") as f:
                json.dump({"last_visit_id": int(vid)}, f)
        except Exception as e:
            print(f"[shadow] state save failed: {e}", file=sys.stderr)

    def score_new(self, limit=50):
        last = self._last()
        rows = store.eliminated_visits_after(self.conn, last, limit)
        scored, maxid = 0, last
        for vid, committed in rows:
            try:
                caps = store.captures_for_visit(self.conn, vid)
                preds = [self.matcher.predict(c["path"]) for c in caps]
                cat, conf = identify.fuse_views(preds)
                n = len(caps)
                nir = sum(1 for c in caps if c.get("is_ir") == 1)
                rec = {
                    "ts": datetime.fromtimestamp(self.now()).isoformat(timespec="seconds"),
                    "visit_id": vid,
                    "shadow_cat_id": cat,
                    "shadow_conf": round(float(conf), 3),
                    "committed_cat_id": committed,
                    "n_frames": n,
                    "ir_frac": round(nir / n, 2) if n else 0.0,
                    "agree": (cat is not None and cat == committed),
                }
                _append_jsonl(self.log_path, rec)
                scored += 1
            except Exception as e:
                print(f"[shadow] visit {vid} failed ({e})", file=sys.stderr)
            maxid = max(maxid, vid)
        if maxid > last:
            self._save(maxid)
        return scored


def daily_report(records, now_ts, cats_by_id, window_h=24):
    """Build the owner's daily shadow summary text from logged records."""
    cutoff = now_ts - window_h * 3600

    def _t(r):
        try:
            return datetime.fromisoformat(r["ts"]).timestamp()
        except Exception:
            return 0.0

    def name(cid):
        if cid is None:
            return "abstain"
        return cats_by_id.get(cid, f"#{cid}")

    recent = [r for r in records if _t(r) >= cutoff]
    n = len(recent)
    if n == 0:
        return "🔬 Shadow ID (24h): no completed visits scored."

    committed = [r for r in recent if r.get("shadow_cat_id") is not None]
    nc = len(committed)
    agree = [r for r in committed if r.get("agree")]
    disagree = [r for r in committed if not r.get("agree")]

    lines = ["🔬 Shadow ID (24h) — DINOv2 matcher, NOT live:",
             f"• {n} visits · committed {nc} ({nc * 100 // n}%), abstained {n - nc}"]
    if nc:
        lines.append(f"• agreed with current attribution: {len(agree)}/{nc}")
    if disagree:
        lines.append(f"• ⚠️ {len(disagree)} disagreement(s) — worth a look:")
        for r in disagree[:8]:
            lines.append(
                f"   visit {r['visit_id']}: matcher={name(r['shadow_cat_id'])} "
                f"vs current={name(r['committed_cat_id'])} "
                f"(IR {int(r.get('ir_frac', 0) * 100)}%, conf {r.get('shadow_conf', 0)})")
    return "\n".join(lines)
