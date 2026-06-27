# IR Detection + Passive Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `is_ir` frame-mode detection and stand up a passive cat-frame collector, so the system can distinguish color/IR frames AND harvest a week+ of labeled-later training data while the owner is away.

**Architecture:** A pure `is_grayscale()` image check (cv2, no ML) populates `captures.is_ir` on every capture + backfills the existing week. A `Harvester` thread runs the existing `TorchvisionCatFilter` over the warm-reader frames on an interval, saving cat-positive, de-duplicated frames to an external drive with a retention cap. Both are config-gated and reuse existing infra (`warmreader`, `catfilter`, `store`).

**Tech Stack:** Python 3.10, cv2/numpy, existing `mw/` modules, pytest.

## Global Constraints

- Frames are dual-mode color/IR; `captures.is_ir` is `1` (IR/grayscale), `0` (color), or `NULL` (unreadable). Copied from spec.
- North star: never silently mask a sick cat — this plan only *collects/flags*, it does not gate alerts, so it cannot reduce safety.
- No new hardware. Collector is OFF by default (`capture.harvest_enabled=false`) until an external drive path is configured.
- Daemon reload ONLY via `launchctl kickstart -k gui/$(id -u)/com.meowant.daemon`.
- Follow existing patterns: cv2 numpy arrays (BGR), `store._iso(ts)` timestamps, injectable deps for tests (see `mw/capture.py`, `mw/warmreader.py`).
- `gallery/` and the harvest dir are gitignored — never commit captured photos.

---

### Task 1: `is_grayscale()` IR detector

**Files:**
- Modify: `mw/imgutil.py`
- Test: `tests/test_imgutil_ir.py` (create)

**Interfaces:**
- Produces: `imgutil.is_grayscale(image_path, sat_thresh=10.0) -> bool | None` — True if the frame is effectively grayscale (IR night mode), False if color, None if unreadable.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_imgutil_ir.py
import cv2, numpy as np
from mw import imgutil

def _write(tmp, name, img):
    p = str(tmp / name); cv2.imwrite(p, img); return p

def test_grayscale_frame_is_ir(tmp_path):
    gray = np.random.randint(0, 255, (120, 160), np.uint8)
    img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)   # 3 equal channels = IR look
    assert imgutil.is_grayscale(_write(tmp_path, "ir.jpg", img)) is True

def test_color_frame_is_not_ir(tmp_path):
    img = np.zeros((120, 160, 3), np.uint8)
    img[:, :, 0] = 200  # strong blue channel only -> saturated color
    img[:, :, 2] = 30
    assert imgutil.is_grayscale(_write(tmp_path, "color.jpg", img)) is False

def test_unreadable_returns_none(tmp_path):
    assert imgutil.is_grayscale(str(tmp_path / "nope.jpg")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_imgutil_ir.py -q`
Expected: FAIL with `AttributeError: module 'mw.imgutil' has no attribute 'is_grayscale'`

- [ ] **Step 3: Implement**

```python
# append to mw/imgutil.py
import cv2
import numpy as np


def is_grayscale(image_path, sat_thresh=10.0):
    """True if the frame is effectively grayscale (IR night mode), False if it
    carries real color, None if unreadable. JPEG color-noise keeps channels from
    being bit-identical, so we threshold the mean per-pixel channel spread rather
    than test exact equality."""
    img = cv2.imread(image_path)
    if img is None:
        return None
    b, g, r = (c.astype(np.int16) for c in cv2.split(img))
    spread = np.maximum(np.maximum(np.abs(r - g), np.abs(g - b)), np.abs(r - b))
    return float(spread.mean()) < sat_thresh
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_imgutil_ir.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add mw/imgutil.py tests/test_imgutil_ir.py
git commit -m "feat(imgutil): is_grayscale IR-frame detector"
```

---

### Task 2: Populate `is_ir` (live capture + backfill existing week)

**Files:**
- Modify: `meowantd.py` (the capture `on_capture` lambda, ~line 130)
- Create: `scripts/backfill_is_ir.py`
- Test: `tests/test_backfill_is_ir.py` (create)

**Interfaces:**
- Consumes: `imgutil.is_grayscale` (Task 1), `store.insert_capture(conn, ts, visit_id, camera, path, is_ir=None)`.
- Produces: `scripts/backfill_is_ir.py:backfill(conn) -> int` (rows updated).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backfill_is_ir.py
import cv2, numpy as np
from mw import store
import scripts.backfill_is_ir as bf

def _cap(tmp, name, color):
    img = np.zeros((60, 80, 3), np.uint8)
    if color: img[:, :, 0] = 200; img[:, :, 2] = 20
    else: img[:] = 90
    p = str(tmp / name); cv2.imwrite(p, img); return p

def test_backfill_sets_is_ir(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    v = store.open_visit(conn, 1000.0)
    ir = _cap(tmp_path, "ir.jpg", color=False)
    col = _cap(tmp_path, "col.jpg", color=True)
    store.insert_capture(conn, 1000.0, v, "meowcam1", ir, is_ir=None)
    store.insert_capture(conn, 1001.0, v, "meowcam1", col, is_ir=None)
    n = bf.backfill(conn)
    rows = {r["path"]: r["is_ir"] for r in conn.execute("SELECT path,is_ir FROM captures").fetchall()}
    assert n == 2
    assert rows[ir] == 1 and rows[col] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backfill_is_ir.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.backfill_is_ir'`

- [ ] **Step 3: Implement the backfill script**

```python
# scripts/backfill_is_ir.py
"""Backfill captures.is_ir for rows where it's NULL (the existing week of data
predates is_ir detection). Idempotent: only touches NULL rows."""
import os
import sys
from mw import store
from mw.imgutil import is_grayscale


def backfill(conn):
    with store._lock:
        rows = [(r["id"], r["path"]) for r in
                conn.execute("SELECT id, path FROM captures WHERE is_ir IS NULL").fetchall()]
    n = 0
    for cid, path in rows:
        if not os.path.exists(path):
            continue
        g = is_grayscale(path)
        if g is None:
            continue
        with store._lock:
            conn.execute("UPDATE captures SET is_ir=? WHERE id=?", (1 if g else 0, cid))
            conn.commit()
        n += 1
    return n


if __name__ == "__main__":
    conn = store.connect(sys.argv[1] if len(sys.argv) > 1 else "meowant.db")
    print(f"[backfill_is_ir] updated {backfill(conn)} rows")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backfill_is_ir.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Wire is_ir into live capture**

In `meowantd.py`, change the capture `on_capture` lambda (currently passes `None`):

```python
# was: on_capture=lambda name, path, ts, vid: store.insert_capture(conn, ts, vid, name, path, None))
on_capture=lambda name, path, ts, vid: store.insert_capture(
    conn, ts, vid, name, path, _is_ir_flag(path)))
```

Add near the top of `main()` (after imports), reusing Task 1:

```python
from mw.imgutil import is_grayscale
def _is_ir_flag(path):
    g = is_grayscale(path)
    return None if g is None else (1 if g else 0)
```

- [ ] **Step 6: Verify wiring + run backfill on the live DB**

```bash
python -c "import ast; ast.parse(open('meowantd.py').read()); print('ok')"
python -m pytest -q
python scripts/backfill_is_ir.py meowant.db   # flags the existing ~1490 captures
launchctl kickstart -k gui/$(id -u)/com.meowant.daemon
```
Expected: tests pass; backfill prints a non-zero updated count; `SELECT COUNT(*) FROM captures WHERE is_ir=1` is now > 0.

- [ ] **Step 7: Commit**

```bash
git add meowantd.py scripts/backfill_is_ir.py tests/test_backfill_is_ir.py
git commit -m "feat(capture): populate is_ir on capture + backfill existing frames"
```

---

### Task 3: `Harvester` — passive cat-frame collector core

**Files:**
- Create: `mw/harvester.py`
- Test: `tests/test_harvester.py` (create)

**Interfaces:**
- Consumes: a `frame_source` with `.frame_path(cam_name)` (the `WarmReaderPool` from `mw/warmreader.py`); a `catfilter` with `.has_cat(path) -> bool` (`mw/catfilter.py`).
- Produces: `harvester.Harvester(cams, frame_source, catfilter, out_dir, *, interval_s=5.0, retention=20000, now_fn, sleep)` with `harvest_once() -> int` (frames saved this pass) and `run()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harvester.py
import os
from mw.harvester import Harvester

class _FakeSource:
    def __init__(self, d): self.d = d
    def frame_path(self, name): return os.path.join(self.d, f"{name}.jpg")

class _FakeFilter:
    def __init__(self, verdicts): self.verdicts = verdicts   # path-substr -> bool
    def has_cat(self, path):
        return any(v for k, v in self.verdicts.items() if k in path)

def _touch(p, content=b"x"):
    with open(p, "wb") as f: f.write(content)

def test_saves_only_cat_frames(tmp_path):
    src_d = tmp_path / "warm"; src_d.mkdir()
    out = tmp_path / "harvest"
    _touch(str(src_d / "meowcam1.jpg"), b"CAT")
    _touch(str(src_d / "meowcam2.jpg"), b"EMPTY")
    cams = [{"name": "meowcam1"}, {"name": "meowcam2"}]
    h = Harvester(cams, _FakeSource(str(src_d)),
                  _FakeFilter({"meowcam1": True, "meowcam2": False}),
                  str(out), now_fn=lambda: 100.0, sleep=lambda s: None)
    saved = h.harvest_once()
    files = os.listdir(out)
    assert saved == 1 and len(files) == 1 and "meowcam1" in files[0]

def test_dedup_skips_unchanged_frame(tmp_path):
    src_d = tmp_path / "warm"; src_d.mkdir(); out = tmp_path / "harvest"
    _touch(str(src_d / "meowcam1.jpg"), b"SAME")
    cams = [{"name": "meowcam1"}]
    t = [100.0]
    h = Harvester(cams, _FakeSource(str(src_d)), _FakeFilter({"meowcam1": True}),
                  str(out), now_fn=lambda: t[0], sleep=lambda s: None)
    assert h.harvest_once() == 1
    t[0] = 105.0
    assert h.harvest_once() == 0          # identical bytes -> skipped
    _touch(str(src_d / "meowcam1.jpg"), b"DIFFERENT")
    t[0] = 110.0
    assert h.harvest_once() == 1          # changed -> saved

def test_retention_caps_total_files(tmp_path):
    src_d = tmp_path / "warm"; src_d.mkdir(); out = tmp_path / "harvest"; out.mkdir()
    for i in range(5): _touch(str(out / f"old_{i}.jpg"))
    _touch(str(src_d / "meowcam1.jpg"), b"NEW")
    cams = [{"name": "meowcam1"}]
    h = Harvester(cams, _FakeSource(str(src_d)), _FakeFilter({"meowcam1": True}),
                  str(out), retention=3, now_fn=lambda: 200.0, sleep=lambda s: None)
    h.harvest_once()
    assert len(os.listdir(out)) <= 3     # retention enforced
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_harvester.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mw.harvester'`

- [ ] **Step 3: Implement**

```python
# mw/harvester.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_harvester.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add mw/harvester.py tests/test_harvester.py
git commit -m "feat(harvester): passive cat-frame collector (cat-gated, dedup, retention)"
```

---

### Task 4: Wire the harvester into meowantd (config-gated)

**Files:**
- Modify: `meowantd.py` (the `if litter_cams:` block, after the warm-reader pool is created)

**Interfaces:**
- Consumes: `Harvester` (Task 3), the `warm_pool` (`WarmReaderPool`), `TorchvisionCatFilter` (already imported as `catfilter` in `meowantd.py`), config keys.

- [ ] **Step 1: Add the wiring (config-gated, OFF by default)**

In `meowantd.py`, after `warm_pool` is created and its thread started (inside the `elif warm:` branch), add:

```python
harvest_dir = config.get(cfg, "capture.harvest_dir", "")
if harvest_dir and config.get(cfg, "capture.harvest_enabled", False) and warm_pool is not None:
    from mw.harvester import Harvester
    harvester = Harvester(
        litter_cams, warm_pool, catfilter, harvest_dir,
        interval_s=config.get(cfg, "capture.harvest_interval_s", 5.0),
        retention=config.get(cfg, "capture.harvest_retention", 20000))
    threading.Thread(target=harvester.run, daemon=True).start()
    _CLEANUPS.append(harvester.stop)
    print(f"harvester: passive cat-frame collection -> {harvest_dir}")
```

Note: `catfilter` is the `TorchvisionCatFilter` already constructed in `meowantd.py` (search for `catfilter =`); reuse that instance so the model loads once. If the harvester branch runs before `catfilter` is defined, move this block to just after `catfilter` is constructed.

- [ ] **Step 2: Verify syntax + suite**

Run:
```bash
python -c "import ast; ast.parse(open('meowantd.py').read()); print('ok')"
python -m pytest -q
```
Expected: parses ok; full suite passes (harvester OFF by default → no behavior change).

- [ ] **Step 3: Live enable test (once external drive mounted)**

```bash
# in config.json: set capture.harvest_dir to the external drive path,
# capture.harvest_enabled true, then reload:
launchctl kickstart -k gui/$(id -u)/com.meowant.daemon
# after a cat visit, confirm cat frames appear:
ls "<harvest_dir>" | head
```
Expected: cat-positive frames accumulate; empty frames are skipped; file count stays ≤ retention.

- [ ] **Step 4: Commit**

```bash
git add meowantd.py
git commit -m "feat(meowantd): wire passive harvester (config-gated, off by default)"
```

---

## Self-Review

**Spec coverage:** `is_ir` fix (Tasks 1–2) ✓; passive collector + external drive + dedup + retention + cat-gating (Tasks 3–4) ✓; reuse warmreader/catfilter ✓; off-by-default until drive configured ✓. Cluster-and-propagate labeling, the DINOv2 matcher, co-presence, and LoRA are explicitly OUT of this plan (post-trip phases, separate plans).

**Placeholder scan:** none — every code step is complete and runnable.

**Type consistency:** `is_grayscale(path) -> bool|None` used consistently (Tasks 1,2); `_is_ir_flag` maps it to `1/0/None` matching `insert_capture(is_ir=)`'s INTEGER column; `Harvester` ctor signature identical across Task 3 impl and Task 4 wiring; `frame_source.frame_path(name)` matches `WarmReaderPool.frame_path`; `catfilter.has_cat(path)` matches `TorchvisionCatFilter`.

**Note:** the live steps (2.6, 4.3) require the daemon and, for 4.3, a mounted external drive; they are integration checks, not unit tests.
