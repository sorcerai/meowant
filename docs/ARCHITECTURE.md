# Meowant SC10 — System Architecture

Local-first control, per-cat identification, and smart-care for a Meowant SC10
automatic litter box. No cloud dependency for the runtime path: the daemon owns
the device over Tuya's LAN protocol, turns data-point changes into semantic
visit events, photographs each visit over RTSP, and identifies which cat used
the box — all on the Mac.

> All diagrams below are [Mermaid](https://mermaid.js.org/) and render directly
> on GitHub and in VS Code.

---

## 1. System overview

How the physical world, the bridges, the daemon, and the consumers connect.

```mermaid
flowchart TB
    subgraph HW["🐱 Physical layer"]
        BOX["Meowant SC10<br/>Tuya v3.5 device<br/>192.168.2.75:6668"]
        CAM1["Wyze cam → meowcam1<br/>(box close-up, ID)"]
        CAM2["Wyze cam → meowcam2<br/>(box close-up, ID)"]
        CAM3["Wyze cam → meowcam3<br/>(floor apron, scatter + ID)"]
    end

    subgraph BRIDGE["📡 Stream bridge (Proxmox)"]
        MTX["cryze → MediaMTX<br/>rtsp://192.168.2.79:8554/&lt;path&gt;"]
    end

    subgraph MAC["🖥️ Mac Studio — meowantd"]
        DAEMON["Daemon core<br/>(poll · events · smart-clean)"]
        CAP["Capture service"]
        LAB["Auto-labeler<br/>(cat/no-cat → agy)"]
        HEALTH["Capture health<br/>watchdogs"]
        ALERTS["Alerts"]
        API["Flask API + SSE<br/>:8765"]
        DB[("SQLite<br/>meowant.db")]
    end

    subgraph OUT["📱 Consumers"]
        PHONE["ntfy push<br/>(sweep / fault / missed-capture)"]
        WEB["Web dashboard<br/>http://mac:8765"]
    end

    BOX <-->|"AES-GCM<br/>local control"| DAEMON
    CAM1 & CAM2 & CAM3 -->|RTSP publish| MTX
    MTX -->|"ffmpeg grab<br/>(tcp)"| CAP

    DAEMON --> DB
    CAP --> DB
    LAB --> DB
    DAEMON -. events .-> ALERTS
    DAEMON -. events .-> CAP
    HEALTH --> ALERTS
    ALERTS --> PHONE
    API --> WEB
    DB --- API
    DAEMON --- API
```

---

## 2. Process & threading model

`meowantd.py:main()` wires everything, then spawns five daemon threads and runs
Flask on the main thread. All threads share one SQLite connection
(`check_same_thread=False` + a module-level lock) and communicate through the
in-process `EventBus`.

```mermaid
flowchart LR
    MAIN["main()<br/>config · DB · device · bus"]

    subgraph THREADS["daemon threads (all daemon=True)"]
        T1["daemon.run<br/>poll @ 2s"]
        T2["capture.run<br/>presence-gated grab"]
        T3["capture_health.run<br/>@ 300s"]
        T4["autolabeler.run<br/>@ 900s"]
        T5["alerts.run<br/>bus subscriber"]
    end

    MAINT["Flask app.run<br/>:8765 (main thread)"]

    MAIN --> T1 & T2 & T3 & T4 & T5
    MAIN --> MAINT

    BUS{{"EventBus<br/>(pub/sub)"}}
    T1 -->|publish| BUS
    BUS --> T2
    BUS --> T5

    DBX[("meowant.db<br/>1 conn + lock")]
    T1 & T2 & T3 & T4 --- DBX
    MAINT --- DBX
```

---

## 3. Daemon poll loop & event flow

The heart of the system: `Daemon.tick()` runs every 2s. The first successful
poll only establishes a baseline (a restart must not synthesize fake edges);
thereafter, decoded DPS deltas become events that are persisted, fed to the
visit tracker, and published on the bus. Smart-clean runs on the *merged* state
(tolerating partial DPS updates).

```mermaid
sequenceDiagram
    autonumber
    participant DEV as TuyaDevice
    participant D as Daemon.tick()
    participant EV as detect_events()
    participant ST as store (SQLite)
    participant TR as VisitTracker
    participant BUS as EventBus
    participant SC as SmartClean

    loop every poll_interval (2s)
        D->>DEV: status_dps()
        DEV-->>D: dps {24:.., 101:.., 102:..}
        alt first poll
            D->>D: set baseline (no events)
            D->>SC: update(state) — may arm
        else steady state
            D->>EV: detect_events(prev, dps)
            EV-->>D: [events]
            loop each event
                D->>ST: insert_event
                D->>TR: handle(event) — open/close visit
                D->>BUS: publish(event)
            end
            D->>D: merge prev ← dps
            D->>SC: update(merged state)
            alt idle long enough & cat absent
                SC-->>D: fire
                D->>DEV: clean()
                D->>SC: notify_cleaned()
            end
        end
    end
```

---

## 4. Visit lifecycle — the dp24 state machine

`dp24` is the real, decoded state machine (richer than Tuya's spec'd
`standby|cleaning`). Visits, captures, and smart-clean all key off it. The box
detects presence only (IR) — there is **no scale**.

```mermaid
stateDiagram-v2
    [*] --> standby
    standby --> cat_get_in: cat enters (IR)
    cat_get_in --> waiting: cat leaves<br/>(delay timer starts)
    waiting --> cleaning: delay elapsed
    cleaning --> clean_done: scoop complete
    clean_done --> standby: reset

    note right of cat_get_in
        Capture service grabs frames
        CONTINUOUSLY while here.
        Visit row OPEN.
    end note
    note right of waiting
        use_record (dp102) emitted
        on substantive visits.
        pee ~40-80 / poop ~100-170.
    end note
    note right of cleaning
        Smart-clean may trigger this,
        OR the box's own delay timer.
        Never fires while cat present.
    end note
```

---

## 5. Capture + per-cat identification pipeline

When a cat enters, capture grabs frames from all cameras in parallel for the
duration of the visit. The auto-labeler then runs a two-stage
teacher→gallery loop: a cheap local cat/no-cat filter drops empty frames before
the expensive `agy` vision model names the cat, and a cross-frame agreement gate
only auto-applies confident calls — ambiguous visits defer to human review.

```mermaid
flowchart TB
    ENTER["cat_get_in event"] --> GRAB

    subgraph CAPTURE["Capture (per visit, while present)"]
        GRAB["Parallel ffmpeg grab<br/>meowcam1 / 2 / 3"]
        GRAB --> CONT{"still present<br/>& &lt; max_frames?"}
        CONT -->|yes| GRAB
        CONT -->|no| ROWS["insert_capture rows<br/>(visit_id pinned at enter)"]
    end

    ROWS --> RUN["autolabeler.run @ 900s<br/>(unlabeled visits)"]

    subgraph LABEL["Auto-labeler (teacher → gallery)"]
        RUN --> CF{"cat/no-cat filter<br/>(torchvision SSDLite COCO)"}
        CF -->|no cat| NONE["mark auto-none<br/>(skip agy)"]
        CF -->|cat| AGY["agy vision label<br/>per frame vs refs"]
        AGY --> ERR{"any ERROR?"}
        ERR -->|yes| RETRY["skip → retry next run"]
        ERR -->|no| VOTE["decide(): strong-majority vote<br/>+ established-cat authority"]
        VOTE --> APPLY["apply_auto_label<br/>(winner frames, no-clobber)"]
        VOTE --> CONFLICT["mark auto-conflict<br/>→ review queue"]
    end

    APPLY --> GALLERY[("captures.label<br/>+ label_source")]
    NONE --> GALLERY
    CONFLICT --> REVIEW["human review<br/>(trust channel)"]
    REVIEW --> GALLERY
```

---

## 6. Scatter detection pipeline (planned — `meowant-abm`)

The digging-scatter problem: a cat flings litter onto the floor at the start of
a visit. No Tuya DPS carries a digging signal, so it must be *seen*. The new
`meowcam3` floor-apron angle makes cheap reference-differencing viable, with
`agy` as the semantic tiebreaker. Per-cat blame requires a **delta** against a
clean baseline (scatter persists across visits until swept).

```mermaid
flowchart TB
    LEAVE["visit ends (cat leaves)"] --> POST["meowcam3 post-leave frame"]
    REF[("clean reference<br/>gallery/refs/<br/>meowcam3_clean_day.jpg")] --> DIFF

    subgraph DETECT["ScatterDetector"]
        POST --> DIFF["reference diff<br/>(cancels wood grain / mat)"]
        DIFF --> SCORE{"granule blobs<br/>over threshold?"}
        SCORE -->|ambiguous| AGYC["agy semantic check<br/>'litter on floor?'"]
        SCORE -->|clear| SEV["severity 0-3"]
        AGYC --> SEV
    end

    SEV --> LOG[("per-visit<br/>scatter score")]
    SEV --> ALERT{"severity ≥ threshold?"}
    ALERT -->|yes| SWEEP["ntfy: time to sweep"]

    subgraph BLAME["Per-cat culprit (needs delta)"]
        PRE["clean pre-entry baseline<br/>(apron visible between visits)"] --> DELTA["new mess =<br/>post − pre"]
        SEV --> DELTA
        DELTA --> ATTR["attribute to visit's cat<br/>(captures.label, NOT visits.cat_id*)"]
        ATTR --> TALLY[("per-cat<br/>scatter rate")]
    end

    TALLY -.A/B.-> REGIME["litter-regime table<br/>(deferred: needs baseline + fix)"]
```

\* `visits.cat_id` is not synced from the auto-labeler (bug `meowant-6v5`);
attribution reads `captures.label`, the source of truth.

---

## 7. Data model

```mermaid
erDiagram
    cats ||--o{ visits : "identified as"
    cats ||--o{ captures : "labeled as"
    visits ||--o{ captures : "has frames"
    visits ||--o{ events : "derived from"

    cats {
        int id PK
        text name
        text notes
    }
    visits {
        int id PK
        text enter_ts
        text leave_ts
        int duration_s
        int cat_id FK
        int eliminated
        int use_record "pee 40-80 poop 100-170"
        int contents_load_min
        int contents_load_max
    }
    captures {
        int id PK
        text ts
        int visit_id FK
        text camera "meowcam1 2 or 3"
        text path
        int label FK "cat id"
        int pred
        real pred_conf
        int is_ir
        text label_source "human auto none conflict"
    }
    events {
        int id PK
        text ts
        text kind
        text detail
    }
```

---

## Key design decisions

| Decision | Why |
|---|---|
| **Daemon owns the single device socket** | Tuya v3.5 allows one local connection; everything else reads through the daemon's maintained `state`, no extra polling. |
| **Event-sourced visits** | DPS deltas → events → visit rows makes the history replayable and the smart-clean rule auditable. |
| **Presence-gated continuous capture** | Brief visitors (e.g. Ucok's in-and-out) are missed by a fixed burst; capture runs while `dp24==cat_get_in`. |
| **Cheap filter before expensive model** | torchvision cat/no-cat drops empty frames before `agy` is called — same teacher→student split reused for scatter. |
| **Trust channel (agreement gate)** | Only confident cross-frame majority auto-applies; ambiguous visits defer to human review, so the gallery stays clean. |
| **Scatter by reference-diff, not absolute** | Mess persists across visits; a clean-reference delta is the only honest way to attribute *new* mess to a cat. |

---

## 8. Roadmap & build status

Tracked in [beads](https://github.com/gastownhall/beads) (`bd list`). 21 issues:
12 done, 2 in progress, 7 open/blocked. The phases below reflect what is
**actually built**, which diverges from the original plan in two places (noted).

```mermaid
flowchart TB
    subgraph P01["Phase 0-1 ✓ DONE"]
        direction LR
        A["daemon: smart-clean<br/>+ tracking + API · jqx"]
        B["alerts-service · 2kn"]
        C["SSE /events · k03"]
        D["launchd reboot-survival · cnf"]
        E["TUI/web → daemon clients · 3pl"]
    end

    subgraph P2["Phase 2 — capture · dpq ◐"]
        direction LR
        F["multi-cam RTSP capture ✓"]
        G["capture-health guards ✓ · lzq"]
        H["attribution-race fix ✓ · lyn"]
        I["continuous-while-present ✓ · 936"]
        J["meowcam3 floor cam ✓<br/>(this session)"]
    end

    subgraph P3["Phase 3 — per-cat ID ◐"]
        direction LR
        K["auto-labeler: agy teacher ✓ · uet<br/>(shipped instead of YOLO+embed)"]
        L["labeling CLI + /identify ✓ · gbn"]
        M["embedding matcher +<br/>backfill visits.cat_id ○ · oht"]
        N["fix visits.cat_id sync ○ · 6v5"]
    end

    subgraph P4["Phase 4 — health ○"]
        direction LR
        O["per-cat baselines +<br/>anomaly alerts · 6mo"]
    end

    P01 --> P2 --> P3 --> P4

    classDef done fill:#1b3a2b,stroke:#3ecf8e,color:#e8e8ea;
    classDef wip fill:#3a2f17,stroke:#f5a623,color:#e8e8ea;
    classDef open fill:#2a2330,stroke:#6c7bff,color:#e8e8ea;
    class A,B,C,D,E,F,G,H,I,J,K,L done;
    class M,N,O open;
```

### Cross-cutting "smart-care" features

These sit on top of the core pipeline and feed Phase 4 health.

```mermaid
flowchart TB
    CORE["visits + captures + events"]

    CORE --> SCAT["Litter-scatter detector ○ · abm<br/>meowcam3 reference-diff → sweep alert → per-cat delta"]
    CORE --> PP["Pee/poop classifier ○ · 7h6<br/>dp102 mass cutoff (~100): pee~40-80 / poop~100-170"]
    CORE --> CHUTE["Chute/bin-full ◐ · apk<br/>(resolved: no backing DP — alias to dp21 BIN_FULL / dp22 FAULT)"]
    CORE --> TUNE["Smart-clean tuning ○ · 014<br/>idle/max_wait from real starvation episodes"]
    CORE --> TG["Telegram transport ○ · 6lz<br/>(preferred over ntfy)"]

    SCAT --> H4
    PP --> H4
    H4["Phase 4 health watch · 6mo<br/>no-go-24h, frequency spikes, waste-type trends"]
```

### Dependency chain

```mermaid
flowchart LR
    DPQ["dpq · capture ◐"] --> OHT["oht · Phase 3 ID ○"]
    OHT --> H4["6mo · Phase 4 health ○"]
    V76["76h · drawer experiment ✓"] --> APK["apk · chute alert ◐"]
    BUG["6v5 · cat_id sync bug ○"] -.blocks clean attribution.-> OHT
    PP["7h6 · pee/poop ○"] -.feeds.-> H4
    ABM["abm · scatter ○"] -.uses meowcam3 from.-> DPQ
```

### Issue ledger

| Phase | ID | Status | What it is |
|---|---|---|---|
| 0-1 | `jqx` | ✓ | meowantd daemon: smart-clean + visit tracking + Flask API |
| 0-1 | `2kn` | ✓ | Alerts service (bin/chute/used-box/health) |
| 0-1 | `k03` | ✓ | SSE `/events` live stream |
| 0-1 | `cnf` | ✓ | launchd agent — survive reboots |
| 0-1 | `3pl` | ✓ | Refactor TUI + web to be daemon clients |
| 0-1 | `quq` | ✓ | Merge `meowantd-phase01` → main |
| 2 | `dpq` | ◐ | Multi-cam RTSP capture (cryze_v2 → MediaMTX); **3rd cam now wired** |
| 2 | `lzq` | ✓ | Capture-health: 0-frame guard + RTSP stream probe |
| 2 | `lyn` | ✓ | Fix capture→visit attribution race (pin `visit_id` at cat_enter) |
| 2 | `936` | ✓ | Capture cadence: continuous-while-present (catch brief visitors) |
| 3 | `uet` | ✓ | Auto-labeler: agy teacher + cross-frame gate + trust channel |
| 3 | `gbn` | ✓ | Labeling CLI + `/identify` + stub matcher + backfill plumbing |
| 3 | `oht` | ○ | Embedding matcher + live-attribute `visits.cat_id` (orig. YOLO plan) |
| 3 | `6v5` | ○ | **Bug:** `visits.cat_id` not synced from auto-labeler |
| 4 | `6mo` | ○ | Per-cat health/anomaly watch (baselines + alerts) |
| care | `abm` | ○ | Litter-scatter detector + sweep alert (meowcam3) |
| care | `7h6` | ○ | Confirm dp102 pee/poop cutoff + classify in events/alerts |
| care | `apk` | ◐ | Wire chute-full flag (resolve to dp21/dp22) |
| care | `014` | ○ | Tune smart-clean idle/max_wait from real data |
| care | `6lz` | ○ | Telegram notify transport |
| care | `76h` | ✓ | Drawer-pull experiment to identify chute-full flag |

> **Plan vs reality — two divergences worth knowing:**
> 1. **Phase 3 ID shipped as an agy-VLM auto-labeler (`uet`), not the planned
>    YOLO-crop → embedding → gallery-match (`oht`).** The VLM teacher reached 82%
>    on the hard brown-tabby pair and builds the gallery hands-free, so the
>    embedding matcher is now optional — its real remaining value is the
>    `visits.cat_id` backfill (coupled to bug `6v5`).
> 2. **`dpq` was blocked on "the third cam"; that's now resolved** (meowcam3 on
>    the .79 bridge), so capture is effectively complete — `dpq` can close once a
>    live visit confirms 3-cam capture.

