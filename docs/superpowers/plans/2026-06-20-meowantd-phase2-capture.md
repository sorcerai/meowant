# meowantd Phase 2 — multi-cam capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) tracking.

**Goal:** On each `cat_enter`, grab a frame from every configured camera and record a `captures` row — passively building the labeled dataset for Phase-3 identification. Plus the docker-wyze-bridge deployment scaffolding.

**Architecture:** A `CaptureService` subscribes to the daemon's `EventBus`; on `cat_enter` it grabs one frame per camera (RTSP via ffmpeg, injectable for tests) into `gallery/captures/`, then writes a `captures` row linked to the currently-open visit. docker-wyze-bridge exposes the 3 Wyze cams as RTSP; its live bring-up (Wyze creds, camera aiming) is a PENDING manual step — all code here is testable without live cameras.

**Tech Stack:** Python 3.10, ffmpeg (RTSP frame grab), SQLite, Flask/EventBus (existing), pytest. Docker for the bridge. Beads: meowant-dpq.

## Global Constraints
- Run from repo root `~/repos/meowant`, `python3` (3.10). Tests: `python3 -m pytest`.
- Capture/grab must NEVER block the daemon: grabs run on the capture-service thread, off the daemon poll loop. A failed grab logs to stderr and continues (one dead camera ≠ lost capture from the others).
- RTSP-agnostic: cameras come from `config.json` as `{name, url}`; the grabber is injectable so tests use a fake (no live camera).
- `gallery/` is gitignored (privacy/size); captures land in `gallery/captures/`.
- `Event` is `mw.events.Event(kind, ts, detail)`; `CAT_ENTER` is the trigger.

---

### Task 1: captures table + store helpers

**Files:** Modify `mw/store.py`, Test `tests/test_captures_store.py`

**Interfaces:**
- Add to the `SCHEMA`: a `captures` table.
- Produces: `insert_capture(conn, ts, visit_id, camera, path, is_ir=None) -> int`; `latest_open_visit_id(conn) -> int | None` (the most recent visit with NULL leave_ts); `captures_for_visit(conn, visit_id) -> list[dict]`. All lock-protected like the existing store functions.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_captures_store.py
from mw import store

def test_insert_and_fetch_capture(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    vid = store.open_visit(conn, 1000.0)
    cid = store.insert_capture(conn, 1000.5, vid, "litter-front", "/x/a.jpg", is_ir=1)
    assert isinstance(cid, int)
    rows = store.captures_for_visit(conn, vid)
    assert len(rows) == 1
    assert rows[0]["camera"] == "litter-front" and rows[0]["path"] == "/x/a.jpg"
    assert rows[0]["label"] is None and rows[0]["is_ir"] == 1

def test_latest_open_visit_id(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    assert store.latest_open_visit_id(conn) is None
    v1 = store.open_visit(conn, 100.0)
    assert store.latest_open_visit_id(conn) == v1
    store.close_visit(conn, v1, 160.0, 60)
    assert store.latest_open_visit_id(conn) is None
```

- [ ] **Step 2: Run, verify fail** — `python3 -m pytest tests/test_captures_store.py -v` → AttributeError: insert_capture

- [ ] **Step 3: Implement** — add to `mw/store.py`:

In `SCHEMA`, append:
```sql
CREATE TABLE IF NOT EXISTS captures(
  id INTEGER PRIMARY KEY, ts TEXT, visit_id INTEGER REFERENCES visits(id),
  camera TEXT, path TEXT, label INTEGER REFERENCES cats(id),
  pred INTEGER, pred_conf REAL, is_ir INTEGER);
```
Add functions (mirror the existing `_lock`/`_iso` style):
```python
def insert_capture(conn, ts, visit_id, camera, path, is_ir=None):
    with _lock:
        cur = conn.execute(
            "INSERT INTO captures(ts, visit_id, camera, path, is_ir) VALUES(?,?,?,?,?)",
            (_iso(ts), visit_id, camera, path, is_ir))
        conn.commit()
        return cur.lastrowid


def latest_open_visit_id(conn):
    with _lock:
        row = conn.execute(
            "SELECT id FROM visits WHERE leave_ts IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None


def captures_for_visit(conn, visit_id):
    with _lock:
        cur = conn.execute("SELECT * FROM captures WHERE visit_id=? ORDER BY id", (visit_id,))
        return [dict(r) for r in cur.fetchall()]
```

- [ ] **Step 4: Run, verify pass** — `python3 -m pytest tests/test_captures_store.py -v`

- [ ] **Step 5: Commit** — `git add mw/store.py tests/test_captures_store.py && git commit -m "feat: captures table + store helpers"`

---

### Task 2: CaptureService (`mw/capture.py`)

**Files:** Create `mw/capture.py`, Test `tests/test_capture.py`

**Interfaces:**
- Consumes: `EventBus`, `mw.events.CAT_ENTER`.
- Produces: `ffmpeg_grab(rtsp_url, out_path, timeout=15) -> str` (one-frame RTSP grab via ffmpeg subprocess); `CaptureService(bus, cameras, out_dir, grabber=ffmpeg_grab, on_capture=None)` where `cameras` is `[{"name":str,"url":str}]` and `on_capture(camera_name, path, ts)` is called after each successful grab. Methods `run_once()` (drain queue) and `run()` (loop). A failed grab logs to stderr and continues.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capture.py
import os
from mw.bus import EventBus
from mw.events import Event, CAT_ENTER, CAT_LEAVE
from mw.capture import CaptureService

def fake_grabber(url, path, timeout=15):
    with open(path, "w") as f:   # write a stand-in "frame"
        f.write(url)
    return path

def test_cat_enter_grabs_each_camera(tmp_path):
    bus = EventBus()
    cams = [{"name": "front", "url": "rtsp://x/front"},
            {"name": "side", "url": "rtsp://x/side"}]
    recorded = []
    cs = CaptureService(bus, cams, str(tmp_path), grabber=fake_grabber,
                        on_capture=lambda name, path, ts: recorded.append((name, path, ts)))
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    assert len(recorded) == 2
    names = sorted(r[0] for r in recorded)
    assert names == ["front", "side"]
    for _, path, _ in recorded:
        assert os.path.exists(path)

def test_non_enter_event_ignored(tmp_path):
    bus = EventBus()
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=fake_grabber, on_capture=lambda *a: (_ for _ in ()).throw(AssertionError()))
    bus.publish(Event(CAT_LEAVE, 1.0))
    cs.run_once()  # must not call on_capture

def test_failed_grab_does_not_stop_others(tmp_path):
    bus = EventBus()
    cams = [{"name": "bad", "url": "u1"}, {"name": "good", "url": "u2"}]
    def grabber(url, path, timeout=15):
        if "u1" in url:
            raise RuntimeError("camera offline")
        return fake_grabber(url, path)
    recorded = []
    cs = CaptureService(bus, cams, str(tmp_path), grabber=grabber,
                        on_capture=lambda name, path, ts: recorded.append(name))
    bus.publish(Event(CAT_ENTER, 5.0))
    cs.run_once()
    assert recorded == ["good"]  # bad failed, good still captured
```

- [ ] **Step 2: Run, verify fail** — `ModuleNotFoundError: No module named 'mw.capture'`

- [ ] **Step 3: Implement**

```python
# mw/capture.py
"""Grab a frame per camera on cat_enter; passively build the Phase-3 dataset."""
import os
import queue
import subprocess
import sys

from mw.events import CAT_ENTER


def ffmpeg_grab(rtsp_url, out_path, timeout=15):
    """Grab a single frame from an RTSP stream to out_path via ffmpeg."""
    subprocess.run(
        ["ffmpeg", "-rtsp_transport", "tcp", "-y", "-i", rtsp_url,
         "-frames:v", "1", "-q:v", "2", out_path],
        timeout=timeout, capture_output=True, check=True)
    return out_path


class CaptureService:
    def __init__(self, bus, cameras, out_dir, grabber=ffmpeg_grab, on_capture=None):
        self.bus = bus
        self.cameras = cameras
        self.out_dir = out_dir
        self.grabber = grabber
        self.on_capture = on_capture
        os.makedirs(out_dir, exist_ok=True)
        self._q = bus.subscribe()

    def _handle(self, ev):
        if ev.kind != CAT_ENTER:
            return
        ts = ev.ts
        for cam in self.cameras:
            path = os.path.join(self.out_dir, f"{int(ts)}_{cam['name']}.jpg")
            try:
                self.grabber(cam["url"], path)
            except Exception as e:
                print(f"[capture] {cam['name']} grab failed: {e}", file=sys.stderr)
                continue
            if self.on_capture:
                self.on_capture(cam["name"], path, ts)

    def run_once(self):
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                return
            self._handle(ev)

    def run(self):
        while True:
            self._handle(self._q.get())
```

- [ ] **Step 4: Run, verify pass** — `python3 -m pytest tests/test_capture.py -v`

- [ ] **Step 5: Commit** — `git add mw/capture.py tests/test_capture.py && git commit -m "feat: CaptureService grabs a frame per camera on cat_enter"`

---

### Task 3: docker-wyze-bridge scaffolding + config (infra, no test)

**Files:** Create `docker-wyze-bridge/docker-compose.yml`, `docker-wyze-bridge/.env.example`, `docker-wyze-bridge/README.md`; Modify `config.example.json`, `.gitignore`

**Interfaces:** none (infra files). This task produces deployment scaffolding only; the live `docker compose up` is a PENDING manual step (needs Wyze creds + cameras).

- [ ] **Step 1: Create the compose file**

```yaml
# docker-wyze-bridge/docker-compose.yml
services:
  wyze-bridge:
    image: mrlt8/wyze-bridge:latest
    container_name: wyze-bridge
    restart: unless-stopped
    ports:
      - "8554:8554"   # RTSP
      - "5000:5000"   # web UI (http://localhost:5000)
    environment:
      - WYZE_EMAIL=${WYZE_EMAIL}
      - WYZE_PASSWORD=${WYZE_PASSWORD}
      - API_ID=${WYZE_API_ID}
      - API_KEY=${WYZE_API_KEY}
      - NET_MODE=LAN          # prefer direct LAN to the cameras (.76/.78/+1)
      - RTSP_PROTOCOLS=tcp
```

- [ ] **Step 2: Create `.env.example`**

```bash
# docker-wyze-bridge/.env.example  — copy to .env and fill in (gitignored)
WYZE_EMAIL=you@example.com
WYZE_PASSWORD=your-wyze-password
# Create an API key at https://support.wyze.com/hc/en-us/articles/16129834216731
WYZE_API_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
WYZE_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

- [ ] **Step 3: Create `README.md`**

```markdown
# docker-wyze-bridge

Exposes the Wyze cams (192.168.1.x, 192.168.1.y, + third) as local RTSP for the
capture-service. Firmware RTSP is unreliable; this bridge is the chosen path.

## Bring-up (manual — needs Wyze account)
1. `cp .env.example .env` and fill in Wyze email/password + API ID/KEY.
2. `docker compose up -d`
3. Web UI at http://localhost:5000 — confirm all cams are streaming; note each
   camera's stream name (its Wyze nickname, lowercased, dashes for spaces).
4. RTSP URLs are `rtsp://<bridge-host>:8554/<stream-name>`.
5. Put those URLs into `config.json` under `cameras` (see config.example.json).
6. Aim the cameras: one at the SC10 entrance, one oblique/side; both must see
   the cat past the hooded globe. Night = IR (grayscale), so frame the body/markings.

## Notes
- Run on an always-on host (the Mac Studio works — docker present).
- NET_MODE=LAN keeps traffic local; the cams are at .76/.78/(+1).
```

- [ ] **Step 4: Add `cameras` to `config.example.json`** (merge into the existing JSON object):

```json
  "cameras": [
    {"name": "litter-front", "url": "rtsp://127.0.0.1:8554/litter-front"},
    {"name": "litter-side",  "url": "rtsp://127.0.0.1:8554/litter-side"}
  ]
```
(And the user adds the matching block to the real gitignored `config.json` after bring-up.)

- [ ] **Step 5: Gitignore the real env** — add to `.gitignore`:
```
docker-wyze-bridge/.env
```

- [ ] **Step 6: Commit** — `git add docker-wyze-bridge config.example.json .gitignore && git commit -m "feat: docker-wyze-bridge scaffolding + cameras config"`

---

### Task 4: Wire CaptureService into meowantd

**Files:** Modify `meowantd.py`, Test `tests/test_meowantd_wiring.py`

**Interfaces:** meowantd builds a `CaptureService` only when `config.cameras` is non-empty, wires `on_capture` to write a `captures` row linked to the latest open visit, and runs it on a daemon thread.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_meowantd_wiring.py
# Verify the capture wiring helper writes a captures row tied to the open visit.
from mw import store
from mw.bus import EventBus
from mw.events import Event, CAT_ENTER
from mw.capture import CaptureService

def test_on_capture_writes_row_for_open_visit(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    vid = store.open_visit(conn, 1000.0)
    bus = EventBus()
    def grabber(url, path, timeout=15):
        open(path, "w").close(); return path
    def on_capture(name, path, ts):
        store.insert_capture(conn, ts, store.latest_open_visit_id(conn), name, path, None)
    cs = CaptureService(bus, [{"name": "front", "url": "u"}], str(tmp_path),
                        grabber=grabber, on_capture=on_capture)
    bus.publish(Event(CAT_ENTER, 1000.0))
    cs.run_once()
    rows = store.captures_for_visit(conn, vid)
    assert len(rows) == 1 and rows[0]["camera"] == "front"
```

- [ ] **Step 2: Run, verify fail** — fails until `captures` helpers exist (they do after Task 1) — this test should actually PASS already if Tasks 1–2 are done; run it to confirm the wiring contract, then add the meowantd glue in Step 3. If it passes immediately, that confirms the contract; proceed.

- [ ] **Step 3: Wire `meowantd.py`** — after the alerts thread is started, add:

```python
    cams = config.get(cfg, "cameras", [])
    if cams:
        from mw.capture import CaptureService
        cap = CaptureService(
            bus, cams, "gallery/captures",
            on_capture=lambda name, path, ts: store.insert_capture(
                conn, ts, store.latest_open_visit_id(conn), name, path, None))
        threading.Thread(target=cap.run, daemon=True).start()
        print(f"capture-service: {len(cams)} camera(s)")
```

- [ ] **Step 4: Verify import + full suite** — `python3 -c "import meowantd"` (clean) and `python3 -m pytest tests/ -q` (all pass).

- [ ] **Step 5: Commit** — `git add meowantd.py tests/test_meowantd_wiring.py && git commit -m "feat: wire CaptureService into meowantd"`

---

## Self-Review
- captures persistence (meowant-dpq): Task 1. ✅
- capture pipeline (RTSP-agnostic, fault-tolerant): Task 2. ✅
- bridge deployment scaffolding (pending live creds): Task 3. ✅
- end-to-end wiring: Task 4. ✅
- PENDING (manual, user at home): docker compose up with Wyze creds, camera stream-name discovery, camera aiming, populating real config.json cameras. Documented in docker-wyze-bridge/README.md.
- Placeholder scan: none. Type consistency: `insert_capture(conn, ts, visit_id, camera, path, is_ir)`, `CaptureService(bus, cameras, out_dir, grabber, on_capture)`, `on_capture(name, path, ts)` consistent across tasks.
```
