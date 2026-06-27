"""Passive cat-frame collector: independent of litterbox events, harvests
cat-positive frames from the warm-reader stream to an external drive for the
later re-ID training/gallery. Cat/no-cat gated (skip empty frames), de-duplicated
(skip unchanged frames), retention-capped (bound disk)."""
import hashlib
import os
import shutil
import sys
import tempfile
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
        files = [f for f in files if os.path.isfile(f) and not f.endswith(".tmp")]
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
                # Dedup is a byte-hash of consecutive frames: it catches a stalled warm reader
                # (identical file) but NOT re-encoded identical scenes (H.264 I-frame refresh /
                # IR AGC re-quantize a static scene to different bytes). That's acceptable here:
                # the catfilter gates to cat-present frames only, retention caps disk, and the
                # poll interval bounds the save rate. Perceptual dedup is out of scope for a
                # collector.
                #
                # Fix 2: copy to a temp file first, then digest the temp copy so the stored
                # hash always matches the archived bytes (avoids TOCTOU if ffmpeg rewrites
                # the warm-frame file between the digest and the copy).
                fd, tmp = tempfile.mkstemp(dir=self.out_dir, suffix=".tmp")
                os.close(fd)
                try:
                    shutil.copyfile(path, tmp)
                    digest = self._digest(tmp)
                    if self._last_hash.get(name) == digest:
                        os.remove(tmp)
                        continue                       # unchanged since last save
                    # Fix 6: millisecond-resolution timestamp prevents collisions at fast
                    # poll rates (multiple cams, sub-second interval).
                    dst = os.path.join(self.out_dir, f"{int(self.now() * 1000)}_{name}.jpg")
                    os.replace(tmp, dst)               # atomic move into place
                    self._last_hash[name] = digest
                    saved += 1
                except Exception:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                    raise
            except Exception as e:
                print(f"[harvester] {name} failed: {e}", file=sys.stderr)
        if saved:
            self._enforce_retention()
        return saved

    def run(self):
        while not self._stop:
            try:
                self.harvest_once()
                # Fix 4: sleep inside the try so a bad interval value can't kill the thread.
                self._sleep(self.interval_s)
            except Exception as e:           # the thread must never die
                print(f"[harvester] loop error: {e}", file=sys.stderr)
                self._sleep(self.interval_s)  # back off so a sustained error doesn't busy-spin

    def stop(self):
        self._stop = True
