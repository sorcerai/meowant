import sys
import time
import subprocess
from collections import Counter
from mw import store

class BowlAgyLabeler:
    CATS = ("Ucok", "Garfield", "Ella")
    def __init__(self, timeout=240):
        self.timeout = timeout

    def _prompt(self, frame_path, refs):
        ref_lines = ""
        for name, paths in refs.items():
            ps = paths if isinstance(paths, (list, tuple)) else [paths]
            ref_lines += f"{name} reference(s): {', '.join(ps)}\n"
        return (
            "Identify which of three specific cats is eating from the food bowl in this photo.\n"
            "Ucok = brown/gray mackerel tabby, short hair, large, heavy stripes.\n"
            "Garfield = ORANGE mackerel tabby, often wearing a collar.\n"
            "Ella = long-haired tortie, fluffy coat, ear tufts.\n"
            + (ref_lines if ref_lines else "")
            + f"Look at this photo (read the file): {frame_path}\n"
            "Reply with ONLY one word: Ucok, Garfield, Ella, or none "
            "(none = no cat is eating / empty)."
        )

    def classify(self, frame_path, refs):
        try:
            out = subprocess.run(["agy", "--print", self._prompt(frame_path, refs)],
                                 capture_output=True, text=True,
                                 timeout=self.timeout, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError) as e:
            print(f"[bowl_tracker] agy failed on {frame_path} ({e})", file=sys.stderr)
            return None
        text = out.stdout.lower()
        hits = {tok: text.find(tok.lower())
                for tok in (*self.CATS, "none") if text.find(tok.lower()) != -1}
        if not hits:
            return "none"
        cat = min(hits, key=hits.get)
        return cat

class BowlTracker:
    """Continuously monitors the bowl for eating cats and logs sessions."""
    def __init__(self, grab, catfilter, refs, conn, notify, location="downstairs",
                 poll_interval_s=5, now_fn=time.time):
        self.grab = grab
        self.catfilter = catfilter
        self.refs = refs
        self.conn = conn
        self.notify = notify
        self.location = location
        self.poll_interval_s = poll_interval_s
        self.now = now_fn
        self.labeler = BowlAgyLabeler()

        self._active_session = None
        self._missing_streaks = 0

    def poll_once(self):
        path = self.grab()
        if not path:
            return

        has_cat = self.catfilter.has_cat(path)
        
        if has_cat:
            self._missing_streaks = 0
            if not self._active_session:
                self._active_session = {
                    "start_ts": self.now(),
                    "frames": [],
                    "labels": []
                }
            self._active_session["frames"].append(path)
            
            # Label up to 5 frames per session to keep costs down
            if len(self._active_session["labels"]) < 5:
                cat = self.labeler.classify(path, self.refs)
                if cat and cat != "none":
                    self._active_session["labels"].append(cat)
        else:
            if self._active_session:
                self._missing_streaks += 1
                # Wait for 3 consecutive empty polls before closing (e.g. 15s grace)
                if self._missing_streaks >= 3:
                    self._close_session()

    def _close_session(self):
        s = self._active_session
        self._active_session = None
        duration_s = int(self.now() - s["start_ts"] - (self._missing_streaks * self.poll_interval_s))
        self._missing_streaks = 0
        
        if duration_s < 10:
            return # too brief to be a real meal

        cat = None
        if s["labels"]:
            votes = Counter(s["labels"])
            winner, count = votes.most_common(1)[0]
            # Require at least 50% agreement
            if count / len(s["labels"]) >= 0.5:
                cat = winner

        if cat:
            store.log_bowl_session(self.conn, self.location, cat, duration_s, ts=s["start_ts"])
            if self.notify:
                self.notify(f"🐱 {cat} ate at '{self.location}' for {duration_s}s.")

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:
                print(f"[bowl_tracker] error: {e}", file=sys.stderr)
            time.sleep(self.poll_interval_s)
