"""Passive cat-frame collector: independent of litterbox events, harvests
cat-positive frames from the warm-reader stream to an external drive for the
later re-ID training/gallery. Cat/no-cat gated (skip empty frames), de-duplicated
(skip unchanged frames), retention-capped (bound disk)."""
import hashlib
import os
import shutil
import sys
import time


class Harvester:
    def __init__(self, cams, frame_source, catfilter, out_dir, *,
                 interval_s=5.0, retention=20000, now_fn=time.time, sleep=time.sleep):
        self.cams = cams
        self.frame_source = frame_source     # .frame_path(name)
        self.catfilter = catfilter           # .has_cat(path)
        self.out_dir = out_dir
        self.interval_s = interval_s
        self.retention = max(1, retention)
        self.now = now_fn
        self._sleep = sleep
        self._last_hash = {}                 # cam -> last saved content hash
        self._stop = False
        os.makedirs(out_dir, exist_ok=True)

    def _digest(self, path):
        h = hashlib.md5()
        with open(path, "rb") as f:
            h.update(f.read())
        return h.hexdigest()

    def _enforce_retention(self):
        files = [os.path.join(self.out_dir, f) for f in os.listdir(self.out_dir)]
        files = [f for f in files if os.path.isfile(f)]
        if len(files) <= self.retention:
            return
        files.sort(key=lambda p: os.path.getmtime(p))   # oldest first
        for p in files[:len(files) - self.retention]:
            try:
                os.remove(p)
            except OSError:
                pass

    def harvest_once(self):
        saved = 0
        for cam in self.cams:
            name = cam["name"]
            path = self.frame_source.frame_path(name)
            if not os.path.exists(path):
                continue
            try:
                if not self.catfilter.has_cat(path):
                    continue
                digest = self._digest(path)
                if self._last_hash.get(name) == digest:
                    continue                 # unchanged since last save
                dst = os.path.join(self.out_dir, f"{int(self.now())}_{name}.jpg")
                shutil.copyfile(path, dst)
                self._last_hash[name] = digest
                saved += 1
            except Exception as e:
                print(f"[harvester] {name} failed: {e}", file=sys.stderr)
        if saved:
            self._enforce_retention()
        return saved

    def run(self):
        while not self._stop:
            try:
                self.harvest_once()
            except Exception as e:           # the thread must never die
                print(f"[harvester] loop error: {e}", file=sys.stderr)
            self._sleep(self.interval_s)

    def stop(self):
        self._stop = True
