# Meowant SC10 — Cat Identification & Smart-Care System

**Date:** 2026-06-20
**Status:** Design (pending review)
**Repo:** `~/repos/meowant`

## 1. Goal

Turn the Meowant SC10 from a dumb auto-scooper into a per-cat smart-care system that:

- **A. Smart auto-clean** — scoop after a cat has truly left, defeating the
  "orange cat re-enters and resets the timer" loop.
- **B. Usage tracking + alerts** — log every visit/elimination; notify on
  bin-full, chute-full, and per-cat events.
- **C. Per-cat identification** — attribute each visit to one of 3 cats using a
  camera, since the box has **no weight sensor**.
- **D. Health/anomaly watch** — flag "cat hasn't gone in 24h", frequency spikes
  (possible UTI), and other deviations from each cat's baseline.

## 2. Constraints (discovered, not assumed)

1. **No cat scale.** The SC10 detects *presence* only (`dp24 → cat_get_in`,
   IR/proximity). It cannot weigh or identify the cat. → identification needs a
   camera.
2. **Nocturnal usage.** Observed visits cluster at ~04:50–05:10 and ~08:30. The
   camera must see in the dark → **IR night vision**, which is **grayscale**.
   Color-based ID fails at night → ID must use **shape/size/markings**, i.e. a
   learned model, not a color rule.
3. **Single local socket.** The device accepts ~one local connection on TCP
   6668 (Tuya v3.5). Multiple consumers (TUI, smart-clean, capture trigger,
   dashboard) cannot each open their own connection. → one process must own the
   device and fan out events/commands.
4. **Heterogeneous hardware.** Mac Studio (always on, never sleeps) is the
   single host for everything including inference (model footprint ~1.3–1.6 GB
   on the Metal/MPS backend — well under budget). Windows "gpubox" (GPU,
   SSH-accessible) is an **optional** fallback for a heavier model, not a
   dependency. Jetson Nano, many Raspberry Pis, Hikvision + Wyze Cam OG cameras
   also available. Camera choice deferred; **multiple cameras** on the unit are
   in scope for multi-view ID.
5. **Verified control.** `set_value(24,"cleaning")` triggers a scoop; the device
   safety-interlocks scooping while a cat is present (hardware, always wins).

## 3. Architecture

A single daemon owns the device and publishes events; all other components are
clients. This resolves the single-socket constraint and the
"can't run TUI + monitor together" problem we already hit.

```
                         ┌──────────────── meowantd (daemon) ────────────────┐
   SC10  ◀──TCP 6668──▶  │  owns the ONE Tuya socket                          │
   (dp24, dp7, dp21…)    │  • polls state, detects transitions               │
                         │  • runs smart auto-clean rule                      │
                         │  • writes events + state to store                  │
                         │  • exposes local HTTP API + SSE event stream       │
                         └───▲───────────────▲───────────────▲───────────────┘
                             │ events/cmds   │ events        │ events/state
                   ┌─────────┴───┐   ┌────────┴────────┐   ┌──┴───────────────┐
                   │  TUI / web  │   │ capture-service │   │  alerts-service  │
                   │ (clients)   │   │ (RTSP grab on   │   │ (bin/chute/cat/  │
                   └─────────────┘   │  cat_enter)     │   │  health → notify)│
                                     └────────┬────────┘   └──────────────────┘
                                              │ POST frame
                                     ┌────────▼─────────────────┐
                                     │ inference-service        │
                                     │ (Mac Studio, MPS)        │
                                     │  permanent process;      │
                                     │  lazy-load model per     │
                                     │  visit, unload after     │
                                     │  ~60s idle (frees GPU)   │
                                     │  YOLO crop → embed →match │
                                     └──────────────────────────┘
   (gpubox = optional fallback for a heavier model; not in the default path)
```

All of the above run on the **Mac Studio** (single always-on host). Cameras
stream RTSP to it. The gpubox is only engaged if a future heavier model is
wanted.

### Components

- **`meowantd`** (Mac Studio — always on). The single Tuya owner. Polls the
  device (~every 2–3s), detects DP transitions, emits semantic events
  (`cat_enter`, `cat_leave`, `clean_start`, `clean_done`, `bin_full`,
  `chute_full`, `elimination`), runs the smart-clean rule, persists everything
  to the store, and serves a local HTTP API (`/state`, `/events` SSE,
  `/command`).
- **capture-service** (host that can reach the camera + gpubox). Subscribes to
  `cat_enter` → grabs one RTSP frame → POSTs to the gpubox → records the
  attributed result against the open visit. Camera is any RTSP URL
  (Hikvision/Wyze/other) from config.
- **inference-service** (Mac Studio, Metal/MPS). Permanent process exposing
  `POST /identify` (a visit's frame bundle) → `{cat_id, confidence, per_view}`.
  Pipeline: detector crops the cat in each frame, embedding model vectorizes
  each crop, nearest-neighbor match against a per-cat gallery, then **fuse views**
  (max-confidence / vote). **Lifecycle:** the process stays up but the model is
  **lazy-loaded on the first request of a visit and unloaded after ~60s idle**
  (`torch.mps.empty_cache()`), so GPU memory (~1.3–1.6 GB) is held only during
  and just after a visit. The 60s idle window absorbs the orange cat's re-entry
  bursts without reload thrash. Optional fallback: proxy to the gpubox for a
  heavier model.
- **store** (SQLite on the Mac Studio). Tables: `events`, `visits`, `cats`,
  `captures`. SQLite chosen over the existing per-project Ghost Postgres for
  edge locality, zero network dependency, and simple backup.
- **TUI / web dashboard**. Refactored to be **daemon clients** (read `/state`,
  stream `/events`, send `/command`) — they no longer touch the socket directly.
- **alerts-service**. Subscribes to events → dispatches notifications
  (mechanism TBD: ntfy / Pushover / macOS notification). Per-rule thresholds.

## 4. Data model (SQLite)

```sql
cats(id INTEGER PK, name TEXT, notes TEXT);              -- 3 rows, hand-seeded

events(                                                  -- raw semantic event log
  id INTEGER PK, ts TEXT, kind TEXT, dp TEXT, old TEXT, new TEXT, meta JSON);

visits(                                                  -- one per cat_enter..leave
  id INTEGER PK,
  enter_ts TEXT, leave_ts TEXT, duration_s INTEGER,
  cat_id INTEGER REFERENCES cats(id),   -- NULL until identified
  confidence REAL,                       -- ID confidence (NULL if unknown)
  eliminated INTEGER,                    -- 0/1, from dp7 increment or dp102 record
  use_record INTEGER,                    -- dp102 uint16 (unit still unknown)
  contents_load_min INTEGER, contents_load_max INTEGER,  -- dp101 during the post-visit clean
  frame_path TEXT);                      -- captured image, NULL if no camera

captures(                                                -- training set / audit
  id INTEGER PK, ts TEXT, visit_id INTEGER, path TEXT,
  camera TEXT,                           -- which RTSP source (multi-view)
  label INTEGER REFERENCES cats(id),    -- NULL until labeled; becomes gallery
  pred INTEGER, pred_conf REAL,         -- per-view prediction (pre-fusion)
  is_ir INTEGER);                        -- day vs IR sample
```

A **visit** is the unit of meaning: presence span + whether it was a real
elimination + which cat + the captured frame. Smart-clean, stats, and health all
read from `visits`.

## 5. Smart auto-clean rule

Runs inside `meowantd`. Replaces the device's reset-prone timer with a rule the
firmware can't starve:

- Track time since `dp24` last became `standby` with no presence since.
- When `standby` has held continuously for **N seconds** (config, default 90s)
  **and** no `cat_get_in` in that window → send `clean`.
- The device safety-interlock still gates physical motion, so a late entry is
  harmless.
- Config: `smartclean.enabled` (bool), `smartclean.idle_seconds` (int).
- Coexistence: the device's own `auto_clean` (`dp4`) stays as the user sets it;
  the rule is additive (a backstop) by default. An optional
  `smartclean.take_over` flips `dp4` off so the rule is the sole brain.

## 6. Identification pipeline (Mac Studio / MPS)

- **Detector:** lightweight object detector (e.g. YOLOv8n) to find and crop the
  cat in the frame — works on grayscale IR.
- **Embedding:** a re-identification embedding (candidate: a pet/animal re-ID
  model, or general DINOv2/CLIP features) → fixed-length vector per crop.
- **Match:** k-NN against the per-cat gallery per view, then **fuse across
  cameras** (highest-confidence view, or majority vote) → best cat + cosine
  similarity as confidence. Below threshold → `unknown` (kept for relabeling).
  Multiple angles raise the odds of a clean head/markings shot past the hood.
- **Gallery:** built in the bootstrap phase from labeled `captures` spanning
  **day and IR** so night grayscale is represented. 3 visually distinct cats
  makes this a forgiving classification problem.
- Model choice is finalized during implementation against a labeled holdout
  (accuracy target ≥90% top-1 on night IR crops).

## 7. Rollout phases

- **Phase 0 — Daemon foundation.** Build `meowantd` (single socket owner, event
  detection, store, HTTP/SSE API, smart-clean rule). Refactor TUI to be a
  client. *Delivers:* the multi-consumer foundation + smart auto-clean (A).
- **Phase 1 — Tracking + alerts.** Visit records, usage stats, notifications for
  bin-full (`dp21`) and the chute-full flag (once identified). *Delivers:* B, D
  on confirmed signals.
- **Phase 2 — Passive capture.** capture-service grabs frames per `cat_enter`
  from each camera into `captures` (label NULL). No ID yet — this *builds the
  dataset itself*.
- **Phase 3 — Identification on.** Label captures (~15 min), stand up the
  Mac-Studio inference-service (lazy-load/unload), backfill + live-attribute
  `visits.cat_id` with multi-view fusion. *Delivers:* C.
- **Phase 4 — Per-cat health.** Per-cat baselines + anomaly alerts (no-go-24h,
  frequency spikes). *Delivers:* full D.

Each phase is independently useful; nothing waits on the camera except 2–4.

## 8. Error handling

- **Camera offline:** visit recorded with `frame_path=NULL`, `cat_id=NULL`. No
  crash; ID simply unavailable for that visit.
- **inference-service down:** frames captured and queued (label NULL);
  identified in a later batch. Live ID degrades to "unknown", backfilled when it
  returns. (Service is local on the Mac Studio, so this is rare.)
- **Daemon restart:** store is the source of truth; in-flight (open) visit is
  closed on restart with best-effort `leave_ts`.
- **Socket contention:** only `meowantd` touches the device; a startup guard
  refuses to run a second daemon (pid/lock file) and clients never open sockets.
- **IR/grayscale:** handled by training on IR samples; detector/embedding are
  grayscale-tolerant.
- **Low-confidence ID:** stored as `unknown` with the frame retained for
  relabeling — never a confident wrong guess.

## 9. Testing

- **Daemon:** unit-test transition detection + smart-clean rule by **replaying
  `cycle_log.tsv`** through a mock device (we already have real event data).
- **Capture:** test frame grab against an RTSP test source; assert a file lands
  and a `captures` row is written.
- **Inference:** accuracy on a labeled holdout (day + IR), report top-1 and the
  unknown-rate; gate Phase 3 on ≥90% night top-1.
- **Integration:** end-to-end on replayed `cat_enter` events → mock inference →
  assert `visits` rows are correct.

## 10. Open questions / deferred

- **Camera choice** (Hikvision vs Wyze OG): deferred; capture layer is
  RTSP-agnostic so it doesn't block design.
- **Chute-full flag** (likely `dp103` or `dp108`): pending the drawer-pull
  experiment to confirm which DP flips.
- **`dp102` unit** (`55/227/99`): per-substantive-visit record, unit unknown
  (not seconds, not kg). Anchor later; not load-bearing.
- **Notification transport** (ntfy / Pushover / macOS): pick in Phase 1.
- **Embedding model**: finalized in Phase 3 against the labeled holdout.
- **Camera count / placement**: how many angles and where (entrance + side
  recommended); set when the camera is chosen. Capture layer takes an N-source
  list, so adding angles is config, not code.
