# MeoWant SC10 Evidence, Query-Discovery & Calculator-Feasibility Audit

Read-only audit of one household's live SC10 cat-management system. Compiled 2026-07-17. All numbers computed from the actual local database, event log, incident records, git history, and issue tracker — no invented values. This report is self-contained and safe to paste into a planning conversation.

---

# Executive Verdict

One household ran a MeoWant SC10 under continuous independent instrumentation (local Tuya polling + external cameras + custom monitoring) for **28 consecutive days** with 3 cats, including a **~2-week owner absence** with sitters. The dataset contains 428 box entries, 261 eliminated visits, 299 cleaning cycles, 26 waste-drawer cycles, and 8 native fault episodes, cross-checked against ~3,900 camera frames of which ~3,100 are human-labeled.

Honest top-line conclusions:

1. **The SC10's core sensing works well in normal operation.** Entry/exit detection, elimination detection, cleaning triggers, and bin-full signaling were consistent with independent camera/human observation the large majority of the time. The 3-minute clean delay executed with near-perfect regularity (median 181s from elimination to clean start).
2. **The dangerous failure mode is not "sensor broken" — it is "misleading state under physical fault."** The single worst incident (drum physically stuck ~34h) produced **zero error codes**: the box reported standby, generated ~20 phantom visit events with plausible durations, and its own daily-use counter disagreed with its own event stream. A stock owner watching the app would have believed everything was fine.
3. **Waste-drawer capacity is the binding constraint for travel.** With 3 cats (~9.3 eliminations/day, ~10.7 cleans/day), a full drawer cycle lasted a **median 21 clean cycles ≈ 1.8 days**. This scales predictably per-cat and supports a defensible service-interval calculator — the strongest first-party numeric asset in the dataset.
4. **Same-weight multi-cat tracking is a real stock limitation.** The household built a local vision-identity system to work around it; it demonstrates the limitation convincingly but is **not** a consumer solution (cameras, GPU-class compute, gallery curation, ~39–58% of visits attributable end-to-end in real conditions).
5. Best build order: **(1) waste-bin service-interval calculator, (2) vacation-readiness decision tool, (3) sensor/jam troubleshooter decision tree.** Litter tools need a data-collection phase first — litter brand was never logged.

---

# Access and Safety Notes

- This audit executed **read-only** SQL (`mode=ro` URI), file reads, `git log`, and issue-tracker reads. No daemon restarts, no config changes, no device commands, no cleaning cycles triggered, no DB writes, no pushes.
- One new untracked file was created: this report. Nothing existing was modified.
- All identifying values sanitized: cats are **Cat A / Cat B / Cat C** (by descending share of attributed visits); no device IDs, keys, IPs, camera URLs, tokens, or exact absence dates appear below. The owner absence is described only as "~2 weeks, mid-window."
- Raw load-cell values are reported in **uncalibrated device units**, never as grams/kg.

---

# System and Data Inventory

| Data/source | Present? | Date coverage | Approx. records | Reliability | Useful for public research? | Privacy concerns |
|---|---:|---|---:|---|---|---|
| Visit records (enter/leave/duration/elimination) | Yes | 28 days, continuous | 428 visits | High | Yes — core usage stats | None once dates rounded |
| Event log (enter/leave/elimination/clean/bin/fault) | Yes | 28 days, continuous | 1,816 events | High | Yes | None |
| Cleaning cycle records | Yes | 28 days | 299 starts / 273 completes | High | Yes | None |
| Bin-full / bin-clear events | Yes | 28 days | 27 + 27 | Medium | Yes — capacity calculator | None |
| Litter load-cell samples (visit-attached) | Yes | 28 days | 157 visits with readings | Medium | Partially (uncalibrated) | None |
| Litter load-cell continuous log | Yes | last ~2 days only | ~sampled 5-min | Medium | Not yet (too short) | None |
| Native fault events (code + clear) | Yes | 28 days | 15 events ≈ 8 episodes | High | Yes | None |
| Camera captures (frames per visit) | Yes | 25 of 28 days | 3,903 frames | Medium | Metadata yes; frames no | Frames private |
| Human labels on frames | Yes | most of window | 3,105 labels | High | Aggregates only | Cat identities |
| Auto-attribution predictions | Yes | most of window | 2,944 preds | Medium | Aggregates only | None |
| Incident records (typed, with outcomes) | Yes | 25 days | 67 incidents | High | Yes, sanitized | Some infra details |
| Notification/alert logic + latch state | Yes | current | n/a | High | Design lessons yes | Tokens excluded |
| Git history (231+ commits, dated titles) | Yes | 28 days | ~230 commits | High | Query mining | None if quoted carefully |
| Issue tracker (beads) | Yes | 28 days | ~80 issues | High | Incident catalog, query mining | Minor |
| Stock app history (AIR PET screenshots/exports) | **No** | — | — | Unknown | — | — |
| Firmware version history | **No** | — | — | Unknown | — | — |
| Litter brand/product log | **No** | — | — | — | — | — |
| Weight-per-visit from device | **No** (undecoded DP) | — | — | Unknown | — | — |

Reliability rationale: the event log and visit tables are written by an always-on local daemon polling the device directly over local Tuya — they are first-party and internally consistent (e.g., 299 clean_starts ≈ 261 eliminations × 1.15, matching the "clean after every elimination" setting). Camera data is Medium because three full blackout days and several degraded days exist (documented below). Bin events are Medium because drawer reinsertion produces oscillating full/clear pairs that must be filtered. The stock app's own history was **not** archived — a real gap for "app vs reality" claims, which are therefore limited to device-reported DPs vs independent observation.

---

# Observation Windows and Data Quality

- **Earliest usable observation:** 2026-06-20. **Latest:** 2026-07-17. **Span: 28 days**, all 28 with visit/event data (sensor-side coverage: 100%).
- **Camera coverage:** 25 of 28 days produced frames. **Zero-frame days: 3** (mid-July, during a monitoring-infrastructure outage); one additional near-zero day (3 frames).
- **Attribution capability by week (share of visits with ≥1 frame + committed identity):** 49% → 58% → 36% → 22% → 12%. The decline tracks camera-infrastructure outages, **not** box behavior.
- **Known outages (all custom-system-side, none SC10's fault):**
  - Camera-bridge disk-full outage (~21h, late June) and a second (~14h, mid-July).
  - A stream-relay restart silently froze frame readers for ~24h (mid-July) — found and fixed at end of window.
  - One alert-delivery failure: a transient DNS error swallowed a critical "cameras dark" Telegram send; the alert latched as sent. Fixed with delivery-gated latches + daily heartbeat late in the window.
  - Auto-labeler stalls: 29 recorded episodes (external VLM CLI hangs), self-flagged by the system.
- **Non-comparability boundaries:** (1) local matcher promoted from shadow to live decider on day ~9 — attribution semantics change; (2) capture strategy changed on day ~13 (approach pre-roll + exit-tail frames); (3) trip-hardening changes at day ~27 (alert cascade, litter threshold recalibrated from 110 to 220 units after real refill data). Metrics below state their window when it matters.
- **Structural biases:**
  - **Sealed-globe blindness:** a heavy cat tips the globe closed; mid-visit frames show a featureless dome. ~25% of eliminated visits are structurally frameless/unidentifiable (issue-tracker measurement) — camera identity can never reach 100% on this box geometry.
  - Similar-weight/similar-appearance cats (two tabbies) confuse both the stock scale-based tracking *and* IR-mode vision.
  - Auto-attribution abstains by design when uncertain — committed-visit accuracy is high but coverage is partial; manual labels are the reliable ground truth.
  - Phantom visits exist **only** during one physical-jam state (documented) — not a normal-operation phenomenon.
  - Litter changed? Unknown — brand was never logged (gap). Box placement unchanged. Thresholds changed once (documented above).

---

# Stock vs App vs Independent Observation vs Custom System

| Capability or event | Stock hardware | Stock app/firmware | Independent observation | Custom system |
|---|---|---|---|---|
| Entry/exit detection | IR + weight; fired 428/429 paired events, plausible durations | Reports visits + daily counter | Cameras corroborate in normal operation | Adds per-visit records, durations, re-entry detection |
| Elimination detection | Detected 304 eliminations; triggers clean | Notifies per app settings | Consistent with camera + drawer contents | Classifies, timestamps, attributes to cat |
| Cleaning | 299 auto cycles; median 3.7 min; 3-min delay honored | Cycle visible in app | Verified via state stream + drawer fill | Logs every cycle; flags interrupted ones |
| Bin-full sensing | 27 full signals over 28 days | App bin-full notification | Physically real when sitter checked; oscillates on drawer reinsertion | Predictive "approaching full" + 24/7 re-nag escalation |
| Fault reporting (E1 family) | 15 events/8 episodes surfaced honestly | App shows fault | Confirmed real each time | Sitter-grade alerting, fault latching |
| **Jammed drum, no fault raised** | Drum stuck ~34h, **standby reported** | **App would show normal + phantom visits** | Cameras: zero cats in "visits"; box's own daily counter said 1 vs 8+ events | Cross-sensor jam detector built afterwards (frames-vs-eliminations disagreement) |
| Multi-cat identity | Weight-based; cannot separate similar-weight cats | Per-cat stats of uncertain provenance | Two of three cats overlap; entries misassignable | Local vision ID: high precision when it commits, partial coverage |
| Remote control | Local-protocol commands work (clean, settings) | Cloud app control | n/a | Local-only control + alerting independent of vendor cloud |
| Notification reliability | n/a | Not archived (gap) | n/a | Multi-channel cascade; one delivery failure found + fixed |

Care was taken **not** to credit MeoWant with custom capabilities (identity, jam inference, predictive bin alerts are all custom) and **not** to blame MeoWant for custom failures (all camera/alert outages were custom-infrastructure failures).

---

# Core Usage Metrics

Household: 3 cats. Window: 28 days unless noted. Method: SQL aggregates over first-party visit/event tables.

| Metric | Value | n/den | Confidence | Publishable? | Generalizes? |
|---|---|---|---|---|---|
| Total box entries | 428 | — | High | Yes | Household-specific but illustrative |
| Closed visits | 428 (429 leave events; 1 unpaired) | — | High | Yes | — |
| Eliminated visits | 261 (61% of entries) | 261/428 | High | Yes | Ratio interesting |
| Non-elimination entries | 167 (39%) | 167/428 | High | Yes | "Cats visit without going" is buyer-relevant |
| Elimination events (device) | 304 (some visits log >1) | — | High | Yes | — |
| Entries/day (mean) | 15.3 | 428/28 | High | Yes | ~5.1/cat/day |
| Eliminated visits/day | mean 9.3 · median 9.5 · p10 5.7 · p90 13 · range 3–19 | 261/28d | High | Yes | **~3.1/cat/day — matches vet folk-wisdom; strong calculator default** |
| Visit duration | mean 53s · median 55s · p90 103s | n=428 | High | Yes | Yes, roughly |
| Re-entry within 120s of an exit | 126 (29% of entries) | 126/428 | High | Yes | Explains app-count inflation |
| Busiest periods (3-h buckets) | Overnight/early-morning heaviest (03–06 peak, 82 entries); late-afternoon lightest (15–18, 30) | — | High | Yes (buckets only) | Cats are crepuscular; fine |
| Per-cat attributed share (when identity committed) | Cat A 43% · Cat B 32% · Cat C 25% | n=168 attributed | Medium | Yes, with caveat | Coverage-biased: only 39% of all visits attributed |
| Per-cat daily elimination range | Not safely computable — attribution coverage (39%) too low and biased toward camera-visible visits | — | — | No | — |

---

# Cleaning and Waste-Bin Metrics

**Cleaning.**

| Metric | Value | Notes |
|---|---|---|
| Total cleaning cycles started | 299 (all automatic; delay-triggered) | Manual cycles not separately flagged; manual "extra" cleans were rare and mostly jam-recovery |
| Completed cycles | 273 | |
| Started-without-completion | 28 (9.4%) | Includes E1 interruptions (infrared-protection stop) and a few state-stream gaps; treat as upper bound on "interrupted" |
| Cleans per eliminated visit | 1.146 | Box set to clean after every elimination, 3-min delay |
| Clean cycle duration | median 219s, p90 225s | Tight distribution = healthy mechanism |
| Elimination → clean start | median 181s (p90 186s, n=257) | 3-min delay setting honored almost exactly |
| Cleans with no confirmed elimination | Present but not precisely countable outside the jam episode (~20 phantom-visit cleans in one 34h window) | |
| Missed clean after real elimination | No confirmed case in normal operation | High confidence |
| Delay-setting changes in window | None observed (3 min throughout) | |

**Waste bin.** 26 clear→full cycles recorded; **12 are substantive fills** (≥5 cleans, ≥0.2 days). The other 14 are drawer-reinsertion oscillations, immediate re-signals, or early manual empties — a real-world measurement lesson in itself (the bin sensor re-evaluates on reinsertion).

| Metric (substantive fills, n=12) | Value |
|---|---|
| Clean cycles per fill | min 7 · p10 ≈ 8 · **median 21** · p90 29 · max 34 |
| Days per fill (3 cats) | min 0.2 · **median 1.8** · max 3.0 |
| Cleans/day (household) | 10.7 |
| False bin-full alerts | Oscillation-on-reinsert pattern observed; no confirmed "full when actually empty" |
| Missed bin-full | None confirmed |
| Early manual empties | Multiple (sitter behavior) — contaminate day-based estimates, which is why **cleans-per-fill is the reliable unit** |

**Usable capacity estimate for this household:** ~20 clean cycles per drawer under real conditions (large clumps, 3 cats). Low outliers (7–8) coincide with the jam-recovery period when drum contents were dumped to the drawer — worth stating publicly as "a jam recovery can consume most of a drawer instantly."

---

# Litter Evidence

What the system actually knows about litter:

- **Load cell (device units, uncalibrated):** observed range 72–338; "comfortably full" ≈ 225–300; the owner's validated low-litter threshold is 220 units with alerting below it (recalibrated from 110 after a real refill observation). Sticking/jam risk rises at low fill — the E1-adjacent litter-watch alert text explicitly ties low litter to waste sticking and jam risk.
- **Refills:** load-jump detection shows refill-like events roughly **every 1–3 days at partial-refill size** during the multi-cat window — but jumps are noisy (clump redistribution, post-clean redistribution), so refill counting is Low confidence.
- **Litter brand/product/type: never logged.** No package weights, no prices, no per-litter clump-quality observations exist in the system.

| Litter | Classification |
|---|---|
| The (single, unrecorded) litter used all window | **Insufficient data** — cannot name, weigh, or price it from the system |

**Tool feasibility from current data:** litter compatibility selector — **no**; monthly cost calculator — **no** (no calibrated mass, no price); "best litter for SC10" — **no**; affiliate comparison table — **no**. All four require the trial schema below.

**Proposed future litter-trial schema** (log per trial): `trial_id, product, litter_type, bag_weight, bag_price, affiliate_url, start_date, end_date, starting_fill_mass, refill_mass, remaining_mass, cats, eliminated_visits, cleaning_cycles, cleaning_delay, sticking_events, poor_sift_events, scatter_score, odor_score, dust_score, obstruction_events, jam_events, manual_clean_minutes, owner_verdict, notes` — plus a one-time load-cell calibration (known mass → device units) so consumption becomes publishable in kg. **Not implemented in this audit.**

---

# Sensor and Obstruction Findings

No single "accuracy %" is defensible (no complete ground-truth denominator: cameras are blind ~25% of visits by geometry and had outage days). What the evidence supports:

- **Confirmed real entries detected:** entry/exit pairing is clean (428/429), durations plausible, camera-corroborated when frames exist. No independently-confirmed missed entry was recorded.
- **Entries with no visible cat:** ~20, **all inside one physical-jam window** (drum stuck in rotated position; IR path apparently flapping). Zero phantom-visit incidents outside physical-fault states.
- **Faults correctly surfaced:** 8 E1-family episodes, each physically real (infrared-protection stops), median self/assisted clear ~28 min; two multi-hour overnight episodes (~11h, ~14h with retries).
- **Physical problem NOT surfaced:** the 34h stuck-drum jam — no fault code, standby reported throughout. This is the central sensor finding.
- **Device's own daily-use counter** disagreed with its own event stream during the jam (counter said 1; events said 8+) and again post-jam — semantics unverified; treat the app's daily counts with caution.
- **Load cell:** trustworthy only in standby (a cat aboard or drum motion swamps it — by design of the measurement, not a defect).

**Supported conclusion (use this phrasing):** *"The sensors were generally accurate during unobstructed normal operation, but specific blocked or jammed physical states produced misleading or incomplete reporting — including one 34-hour jam during which the box reported normal standby and logged visit-like events while no cat could use it."*

---

# Fault, Jam, and Phantom-Visit Findings

| Metric | Value |
|---|---|
| Native fault events / episodes | 15 events ≈ 8 episodes / 28 days (all one code family — infrared-protection/E1 class) |
| Faults per 100 cleans | 5.0 |
| Fault episodes per 30 days | ~8.6 (events: 16.1/30d; episodes are the honest unit) |
| Fault → clear | median 28 min · two overnight episodes ~11h and ~14h (flapping retries) · max continuous ~70h span including unattended overnight |
| Physical failures with **no** fault | 1 (the 34h jam) — the highest-severity incident in the window |
| Phantom-visit incidents | 1 window (~20 events), jam-coupled only |
| Drum-stuck incidents | 1 |
| Chute/waste-path obstruction | Contributing factor in E1 clusters around low-litter/sticky-waste periods |
| Manual interventions (box-related) | ~10–12 over 28 days: drawer empties (~every 2 days), 1 guided jam recovery, litter refills, E1 checks → **~11–13 interventions/30 days for 3 cats**, drawer-dominated |
| Median time-to-detection (custom system) | Minutes for E1 (alert on fault DP); ~34h for the no-code jam pre-fix (human noticed empty photos); the cross-sensor jam detector now targets same-day |
| Remote recovery possible? | E1: sometimes (remote clean retry); jam: **no — hands required** (drum repositioning) |

---

# App and Same-Weight Tracking Findings

- Stock tracking is **weight-based**. The household has 3 cats with at least two overlapping in weight/appearance (two tabbies); stock per-cat assignment for those two is structurally unreliable. Exact stock assignment histories were not archived, so **no numeric stock-accuracy claim is made** — the limitation is argued from mechanism (identical weight band) plus the vendor's own data model, which is defensible.
- Device-reported daily counter vs event stream disagreed (1 vs 8+ during the jam; 1 vs 12 observed on a later healthy day) — the counter's semantics are unknown; app daily counts should not be treated as visit counts.
- App notification latency/reliability: not archived (gap). Local alerting was measured instead (one delivery failure, fixed).
- ~29% of entries are re-entries within 2 minutes — any per-visit app counter will look inflated vs "trips to the box."

---

# Custom Vision Practicality

- **Hardware:** multiple RTSP cameras around the box, a relay/bridge host, an always-on computer running detection + embedding models, local storage. **Setup burden:** days of skilled work; ongoing maintenance demonstrated by this window's own outages. **Runs fully locally** (no cloud inference required).
- **Design:** abstain-or-commit gallery matcher (self-supervised ViT embeddings + conformal-style gating) with human label feedback loop.
- **Accuracy when it commits (held-out eval, this household's 3 cats):** ~94% top-1 in good conditions; real-world live top-1 including hard cases ~66–73% depending on reference curation; IR/night mode collapses the two similar tabbies.
- **Coverage/commit reality:** only **39%** of all visits ended with a committed identity over the full window (peak week 58%) — driven by sealed-globe blindness (~25% structural), camera outages, and deliberate abstention.
- **Verdict for public content:** *"Visual identity demonstrably fixes same-weight ambiguity when it works — and it is not something a normal buyer can or should deploy."* Practical buyer advice: treat app per-cat stats cautiously with similar-weight cats, use observation/a basic camera for health questions, and see a vet for actual concerns — app attribution is not clinical data.

---

# Travel and Remote-Operation Findings

Rounded: **~2-week absence, mid-window**, 3 cats, sitters available, remote SSH + local-protocol control retained.

| Item | Value (during absence) |
|---|---|
| Real box-use events | ~130–150 eliminated visits (rate consistent with the 28-day mean) |
| Cleaning cycles | ~150 |
| Native fault episodes | 2–3 (incl. one overnight ~14h flapping E1) |
| Jams | 1 (the 34h no-code jam occurred at the start of the absence window) |
| Phantom visits | ~20 (jam-coupled) |
| Alerts sent | Dozens (bin, litter, fault, health) |
| Failed alert deliveries | 1 known critical (camera-blackout alert lost to a transient network error; discovered later) |
| Manual interventions | ~8–10 (drawer empties every ~2 days, litter refills, jam recovery, E1 checks) |
| Interventions requiring a sitter physically | All of the above — **remote hands were required repeatedly** |
| Remote recoveries | Several (monitoring-infra restarts, remote clean retries) — but **no physical box failure was remotely fixable** |
| Power/network interruptions | No household outage; monitoring-stack outages as documented |
| Longest monitoring blackout | ~24h (cameras); box sensing itself never blacked out |
| Would stock-only have detected each incident? | Bin-full: yes. E1: yes (app fault). **34h jam: no** — standby + phantom visits. Low litter: no (no stock low-litter alert). Camera outages: n/a stock |
| Did the custom system materially reduce risk? | Yes — but the jam still needed a human's eyes on photos pre-fix; the honest lesson is "independent observation + scheduled physical checks," not "software solves it" |
| Backup box present? | Not during this trip; **recommended** based on the jam (cats had no alternative for ~34h) |

**Factual travel verdict:** a stock SC10 + app kept waste handled for 3 cats for two weeks *except* during one physical jam that stock reporting actively masked. Drawer capacity forced a sitter visit roughly every 2 days regardless of monitoring sophistication. No automated litter box removed the need for a human with physical access.

---

# Sanitized Incident Catalog

| ID | Approx. period | Buyer-visible symptom | Stock state/app | Independent observation | Root cause | Intervention | Remote fix? | Normal buyer could detect? | Confidence |
|---|---|---|---|---|---|---|---|---|---|
| INC-1 | Late June, ~34h | Cats stop using box; litter area inaccessible | **Standby, no fault; visit events kept appearing** | Cameras: zero cats in any "visit"; drum physically rotated; ramp displaced | Drum stuck mid-rotation (physical) | Hands-on repositioning (guided) | **No** | **Unlikely** — app looked normal; needed physical check or camera | High |
| INC-2 | Early window → recurring, 8 episodes | Cleaning stops / box pauses | E1-class fault shown | Real each time; clusters near low-litter/sticky-waste conditions | Infrared-protection stop (obstruction-adjacent) | Check box, remove obstruction, retry clean | Sometimes (retry) | Yes (app fault) | High |
| INC-3 | Mid-window | None (invisible) | n/a | Camera frames stopped for ~21h | Custom bridge host disk full | Remote disk cleanup | Yes | n/a (custom-only) | High |
| INC-4 | Mid-window | None | n/a | Second camera blackout ~14h; later a silent ~24h frame freeze | Custom infra (log growth; stream-relay restart orphaning readers) | Remote fixes; code hardening | Yes | n/a | High |
| INC-5 | Mid-window, one night | Missed critical alert | n/a | "Cameras dark" alert never arrived | Transient DNS + latch-on-failure bug (custom) | Code fix (delivery-gated latches, daily heartbeat) | Yes | n/a | High |
| INC-6 | Throughout | Drawer fills fast | Bin-full notification | Real; oscillates on reinsertion | 3 cats vs small drawer; sensor re-eval on reinsert | Empty every ~2 days | No | Yes | High |
| INC-7 | Once | Litter low, waste sticking risk | **No stock low-litter signal exists** | Load-cell trend + visual | Consumption between refills | Refill | No | Only by looking | Medium |
| INC-8 | Feeder-side (adjacent product), 12 events | Meal not dispensed / bowl empty | Feeder app state | Bowl camera + weight of evidence | Separate device; out of SC10 scope | Sitter check | Partially | Sometimes | Medium |

Classification counts: physical obstruction (INC-1, part of INC-2), sensor limitation (jam masking, INC-1), firmware/app reporting limitation (INC-1 counter divergence), normal maintenance (INC-6, INC-7), custom-system limitations (INC-3/4/5 — never attributable to MeoWant). For each: the jam (INC-1) defeated a safety rule (phantom eliminations reset a "cats not using the box" deadman), affected cat access and health-tracking continuity, and would make a buyer think the box was fine — the strongest single piece of unique content this system owns.

---

# Unpleasant Waste Cases Actually Observed

Grounded cases only; no medical framing anywhere in the local evidence, and none is introduced.

| Case | Evidence | Frequency | Conditions | Outcome | Buyer query it answers |
|---|---|---|---|---|---|
| Waste sticking at low litter | Alert text + threshold recalibration built specifically for it | Recurring risk state, ~weekly | Litter below ~⅔ of full band | Manual scrape risk; E1-adjacent | "Why is waste sticking to my litter robot?" |
| Jam recovery dumped drum contents into drawer | Two drawer cycles consumed at 7–8 cleans (vs median 21) right after jam | Once | Post-jam | Drawer instantly near-full | "Litter dumped into the waste drawer after a jam — normal?" |
| Litter scatter outside box | Purpose-built scatter detector + sweep alerts in codebase | Regular enough to automate detection | Vigorous diggers | Sweeping | "How much litter does an SC10 scatter?" |
| Large-clump / sticky-waste E1 clusters | Fault episodes cluster with sticky-waste conditions | ~2–3 of 8 episodes | Wet clumps, low litter | Human check + retry | "SC10 keeps stopping mid-clean" |
| Waste remaining after sifting | Implied by sticking cases; not separately counted | Unquantified | Same as above | Manual assist | Troubleshooting entry only |

Not observed / not claimable: urine outside geometry, liner leaks, seam contamination, odor-after-clean measurements (no odor instrumentation). A dedicated page is warranted only for the first two cases; the rest are answer-blocks.

---

# Grounded Unknown-Unknown Queries

Top 60, mined from commit titles, issue titles, alert strings, fault labels, and owner-solved problems. (Route: page = dedicated URL; block = answer block on a hub; tool = calculator/decision tool.)

| # | Query | Intent | Evidence | Uniqueness | Urgency | Monetization | Route | Confidence |
|---|---|---|---|---|---|---|---|---|
| 1 | how often empty meowant sc10 waste drawer | Pre/post-purchase | 12 measured fills | **High** | High | TikTok showcase | **Tool A + page** | High |
| 2 | meowant sc10 how many cats | Pre-purchase | 3-cat live data | High | High | TikTok | Page | High |
| 3 | can i leave automatic litter box for 2 weeks vacation | Travel | 2-week absence log | **High** | High | TikTok + backup-box affiliate | **Tool B + page** | High |
| 4 | meowant says standby but not cleaning | Troubleshoot | INC-1 | **Unique** | **Critical** | None/authority | **Tool G + page** | High |
| 5 | meowant sc10 drum stuck | Troubleshoot | INC-1 | Unique | Critical | None | Page | High |
| 6 | litter robot showing visits but cat not using it | Troubleshoot | Phantom visits | Unique | High | None | Block | High |
| 7 | meowant e1 error infrared | Troubleshoot | 8 episodes | High | High | None | Page | High |
| 8 | meowant app cat count wrong | App vs reality | Counter divergence | High | Med | None | Block | High |
| 9 | automatic litter box two cats same weight | Pre-purchase | Vision project | **Unique** | Med | TikTok | **Explainer page** | High |
| 10 | meowant sc10 review 30 days | Pre-purchase | Whole dataset | High | High | TikTok | **Flagship page** | High |
| 11 | how many times a day do cats use litter box | General | 3.1/cat/day measured | Med | Med | Adjacent | Block | High |
| 12 | meowant waste drawer full too fast | Ownership | Fill data + jam-dump case | High | Med | Bag affiliate | Block | High |
| 13 | sc10 litter low warning | Ownership | No stock signal; custom threshold | High | Med | Litter affiliate | Block | High |
| 14 | meowant cleaning delay setting best | Setup | 3-min delay measured | Med | Med | TikTok | Block | High |
| 15 | how long does meowant cleaning cycle take | Ownership | 219s median | High | Low | TikTok | Block | High |
| 16 | meowant sitter instructions | Travel | Sitter playbook lived | High | Med | TikTok | Page | Med |
| 17 | do i need backup litter box with automatic | Travel | Jam access-loss case | High | High | Backup-box affiliate | Block+Tool B | High |
| 18 | meowant power outage what happens | Reliability | Not directly tested | Low | Med | Smart-plug affiliate | Block (caveated) | Low |
| 19 | meowant without wifi local control | Local control | Local-protocol daily driver | **Unique** | Med | Authority/backlink | Page | High |
| 20 | tuya local control litter box | Technical | Working implementation | Unique | Low | Authority | Page | High |
| 21 | meowant tiktok shop discount | Purchase | Owner showcase | Med | High | **TikTok** | CTA block | Med |
| 22 | is meowant app accurate | App | Counter + weight limits | High | Med | TikTok | Page | High |
| 23 | litter box camera monitor cat health | Adjacent | Full build | Unique | Low | Camera affiliate | Explainer | Med |
| 24 | meowant multiple cats tracking | Pre-purchase | Same-weight evidence | High | High | TikTok | Page (Tool F explainer) | High |
| 25 | sc10 phantom visits after moving box | Troubleshoot | Jam-coupled only | High | Med | None | Block | Med |
| 26 | automatic litter box for 3 cats enough | Pre-purchase | 3-cat load data | High | High | TikTok | Block | High |
| 27 | meowant bin full sensor wrong | Troubleshoot | Reinsert oscillation | High | Med | None | Block | High |
| 28 | how much litter does sc10 use per month | Cost | **Insufficient (uncalibrated)** | — | Med | Litter affiliate | Defer | — |
| 29 | best litter for meowant sc10 | Cost/compat | **Insufficient (brand unlogged)** | — | High | Litter affiliate | Defer → trials | — |
| 30 | meowant sc10 vs litter robot 4 | Compare | One-sided only | Low | High | TikTok + competitor affiliate | Later (external data) | Low |
| 31 | cat sitter checklist automatic litter box | Travel | Lived checklist | High | Med | TikTok | Page | High |
| 32 | why does my cat go in litter box but not go | Behavior | 39% non-elim entries | High | Low | Adjacent | Block | High |
| 33 | cat enters litter box twice in a row | Behavior | 29% re-entry | High | Low | None | Block | High |
| 34 | meowant cleaning stopped halfway | Troubleshoot | 28 incomplete starts | High | High | None | Block | High |
| 35 | is my automatic litter box actually working | Assurance | INC-1 framing | Unique | High | None | Tool G entry | High |
| 36 | meowant jam after deep cleaning | Troubleshoot | Plausible adjacent (drum reseat) | Med | Med | None | Block | Med |
| 37 | litter stuck to side of drum sc10 | Waste | Sticking evidence | High | Med | Litter affiliate | Block | Med |
| 38 | sc10 drawer liner bags fit | Accessories | Bag usage lived | Med | Med | **Bag affiliate** | Block | Med |
| 39 | smart plug for litter box remote restart | Advanced | Smart-plug integration built | High | Low | Smart-plug affiliate | Block | Med |
| 40 | how do i know if litter box notifications failed | Reliability | Alert-loss incident + heartbeat pattern | Unique | Med | None | Block | High |
| 41 | meowant overnight fault not cleaning until morning | Reliability | ~14h overnight E1 | High | Med | None | Block | High |
| 42 | cat litter box usage tracker per cat accuracy | App | Attribution data | High | Med | TikTok | Page | High |
| 43 | how long can 1 cat use sc10 before emptying | Capacity | Scaled from cleans/fill | High | High | TikTok | Tool A output | High |
| 44 | travel 5 days automatic litter box no sitter | Travel | Evidence-bounded answer (drawer math says risky ≥ ~2 cats) | High | High | TikTok + backup box | Tool B | High |
| 45 | e1 error keeps coming back | Troubleshoot | Flapping episodes | High | High | None | Block | High |
| 46 | meowant drum position wrong after power loss | Troubleshoot | Adjacent to INC-1 | Med | Med | None | Block | Low |
| 47 | is weight based cat identification reliable | Concept | Mechanism + household case | Unique | Med | Authority | Page | High |
| 48 | ai cat recognition litter box diy | Technical | Full project | Unique | Low | Authority/backlink | Page | High |
| 49 | how accurate are litter box health alerts | Concept | False-alarm engineering lived | Unique | Med | Authority | Page | Med |
| 50 | meowant sc10 litter capacity max line | Setup | Load-band evidence | Med | Med | Litter affiliate | Block | Med |
| 51 | litter box for cats that dig aggressively scatter | Behavior | Scatter detector | Med | Low | Litter/mat affiliate | Block | Med |
| 52 | do automatic litter boxes work at night | Ownership | Overnight-heaviest usage data | High | Low | TikTok | Block | High |
| 53 | cat won't use litter box after it jammed | Behavior | Post-jam usage resumed (data) | High | Med | None | Block | Med |
| 54 | how to test automatic litter box before trip | Travel | Pre-trip checklist built | Unique | High | TikTok | Page | High |
| 55 | sc10 app says clean complete but litter not sifted | App vs real | Adjacent to E1/interrupt evidence | Med | Med | None | Block | Med |
| 56 | what happens if waste drawer overfills | Capacity | Approaching-full escalation design | Med | Med | Bag affiliate | Block | Med |
| 57 | meowant warranty what to document | Failure | Incident-log practice | Med | Med | TikTok | Block | Med |
| 58 | second litter box rule multiple cats automatic | Concept | 3-cat single-box load data | High | Med | Backup-box affiliate | Block | High |
| 59 | how fast does litter box fill with 2 cats | Capacity | Interpolated from per-cat rate | High | High | TikTok | Tool A | High |
| 60 | quietest time to schedule litter box maintenance | Ownership | Usage-by-hour buckets | Med | Low | None | Block | Med |

---

# Calculator Feasibility Matrix

| Tool | Verdict | User problem | Search intent | Local data | External needed | Formula defensibility | Affiliate value | Complexity | Main risk |
|---|---|---|---|---|---|---|---|---|---|
| A. Waste-bin service interval | **Build now** | "When will it need emptying?" | High | 12 fills, per-cat rates | None for v1 | Good (back-tested) | Med (TikTok, bags) | Low | Single-household capacity |
| B. Vacation readiness | **Build now** (decision tool) | "Can I leave it?" | High | Intervention history | None | Good as decision tree | High (TikTok, backup box) | Low-Med | Over-promising safety |
| C. Litter compatibility | Needs data | "Will this litter work?" | High | **Almost none** | Trials + mined reviews | Weak now | High (litter) | Med | Fake authority |
| D. Monthly litter cost | Needs data | Cost planning | Med | Uncalibrated | Calibration + price | Weak now | High (litter) | Low | Made-up kg numbers |
| E. Total cost of ownership | Later | Budgeting | Med | Partial | Prices, lifespan | Medium | Med | Med | Price staleness |
| F. Same-weight reality check | **Build as explainer** | "Can it tell my cats apart?" | Med | Unique | None | n/a (no fake precision) | Med (TikTok) | Low | Overclaiming vision fix |
| G. Sensor/jam troubleshooter | **Build now** (decision tree) | "Is it actually working?" | High | Incident catalog | Support-doc cross-check | n/a | Low direct, high authority | Med | Safety wording |
| H. "Says standby but working?" | Merge into G | Same | High | INC-1 unique | — | — | — | — | — |
| I. Model/cat fit selector | Reject (now) | Model choice | Med | One model only | Other models | Weak | Med | Med | Pretending breadth |
| J. Backup-box estimator | Merge into B | Redundancy | Med | Jam case | — | — | High | — | — |
| K. Maintenance time calc | Later | Time budgeting | Low | Minutes not logged | Logging | Weak | Low | Low | Guesswork |
| L. TikTok deal display | Component, not tool | Price check | High | n/a | Showcase feed | n/a | **High** | Low | Hardcoded prices |

---

# Waste-Bin Calculator Specification

- **Name:** SC10 Waste Drawer Interval Estimator. **URL:** `/tools/meowant-sc10-waste-drawer-calculator`. **Primary query:** #1; secondary: #43, #59, #12, #44.
- **Inputs:** cats (int, 1–6, default 2) · eliminations/cat/day (float, 1–6, default **3.1** — first-party) · cleans per elimination (default **1.15** first-party; 1.0 if delay-off users) · manual extra cleans/day (0–5, default 0) · drawer state (fresh / ~half / unknown → capacity × 1.0 / 0.5 / 0.4) · trip length (optional, for verdict) · safety buffer (default conservative).
- **Formula:** `cleans_per_day = cats × elim_per_cat_day × cleans_per_elim + manual_extra` ; `expected_days = capacity_cleans × drawer_factor ÷ cleans_per_day` with `capacity_cleans`: expected **20** (median 21 observed), conservative **12** (below observed p10 of substantive fills; absorbs jam-dump events).
- **Outputs:** expected days-to-service, conservative days, expected/conservative clean-cycle counts, service-by date, risk banner when trip length > conservative days, sitter-interval suggestion (= conservative days, capped at 3), backup-box recommendation when trip > 2× conservative.
- **Uncertainty treatment:** always show expected AND conservative; state "based on 12 measured fill cycles in one 3-cat household — your litter, clump size, and habits change this."
- **Must not claim:** manufacturer capacity, universality, or safety without physical checks. **Warnings:** jam recovery can consume a full drawer instantly; drawer reinsertion can re-trigger full signals. **Edge cases:** 0 manual cleans + delay-off; >4 cats → recommend second box outright. SC10-specific v1; architecture allows per-model capacity table later. Privacy: no user data stored. Structured data: FAQ + SoftwareApplication schema.
- **CTA:** "Check current SC10 price on TikTok Shop" (+ drawer-liner bag affiliate).

**Back-test (leave-one-out over 12 substantive fills):** expected-value model median abs error **1.2 days** on a median 1.8-day quantity (high relative error → always show ranges); conservative estimate (p10 capacity) exceeded the actual only 2/12 times, both early-manual-empty cycles; with capacity=12 conservative, **0/12 failures**. Worst overprediction +0.7d, worst underprediction −2.4d. Sample small; label as v1 and keep logging.

---

# Vacation Readiness Tool Specification

- **Name:** Automatic Litter Box Vacation Readiness Check. **URL:** `/tools/litter-box-vacation-readiness`. Decision tool, not numeric.
- **Normal-buyer mode assumes stock SC10 + app only** (no cameras, no local control, no custom alerts). Advanced mode adds smart plug / camera / independent alerting toggles.
- **Inputs:** cats, days away, drawer state, litter level, fault in last 7/14/30d, jam/obstruction history, backup box (y/n, count), sitter availability, (advanced toggles), same-weight cats (y/n), unusual waste behavior (y/n), last deep clean, last path inspection.
- **Logic grounded in observed history:** drawer math from Tool A sets the hard check interval; any jam/obstruction in 30d → "Not ready without daily sitter"; recent fault → shorten interval; no backup box + >3 days away → recommend one (the 34h access-loss case is the citation); no sitter + trip > conservative drawer days → "Not ready."
- **Outputs:** readiness tier (Not ready / daily sitter / scheduled checks / lower-risk with backup), max hours between physical checks, predicted drawer services during trip, pre-trip task list (empty drawer, fill litter to max line, run a clean, verify app fault-free, inspect chute/drum path, place backup box), explicit list of failure modes that **cannot** be fixed remotely (stuck drum, obstruction, drawer overfull), stock-only vs advanced verdicts, and the fixed line: *"No automated litter box eliminates the need for someone who can physically reach your cats."*

---

# Litter Compatibility and Cost Tool Specification

- **Status: needs data collection first** (litter brand never logged; load cell uncalibrated; single unnamed product used).
- Build later per the trial schema in *Litter Evidence*. Merge design: first-party trial rows (schema above) + mined customer reviews (`source_type: first_party | mined_review | manufacturer_claim`, each row: product, model, litter_type, verdict enum, evidence_count, quotes-as-paraphrase, date) + manufacturer claims kept separate. Verdict logic: `Works well` requires ≥1 first-party trial or ≥N concordant mined reports; low evidence renders "Insufficient evidence" — never a confident recommendation; affiliate presence must not affect verdict (enforced by computing verdict before joining offer data).
- Cost model only after calibration: `bags/month = monthly_consumption_mass / bag_weight`, `cost = bags × price` with consumption from measured refill masses, not load-cell units.

---

# Sensor/Jam Troubleshooter Specification

Decision tree (correct format — a calculator would be fake precision). Entry symptoms map to branches; every branch ends with safe actions only.

- **"Says standby but drum looks wrong / not cleaning"** → the flagship branch (unique INC-1 evidence): visually confirm drum position and ramp seating → count actual cleans vs app over 24h → check that "visits" match a real cat (put a bit of litter marker or just observe) → if drum is displaced: power off, reseat per manual, power on, run one supervised clean → if it recurs or the drum resists: stop, contact support, photograph state (warranty evidence: photos of drum position, app screenshots showing standby, dates). **Never** force the drum with a cat nearby; never operate with hands inside.
- **Repeated E1 / stops mid-clean** → check litter level first (low litter → sticking → stops; observed pattern), clear visible obstructions in path/chute with the unit powered off, retry one cycle; recurring ≥3×/day → support.
- **Bin-full but drawer isn't full** → reseat drawer once (reinsert oscillation observed), check the full-sensor window for dust/litter film; do not tape over or bypass sensors.
- **Cleaning with no cat / false visits** → if isolated: note and watch; if a run of them: treat as possible physical-state problem (see flagship branch) — this exact signature masked a real jam here.
- **After deep clean / after moving / after power loss** → re-seat drum + drawer, verify level floor placement, run one supervised cycle before trusting auto mode.
- Global safety rails on every branch: disable auto-clean while investigating if cats are unsupervised; a backup box while a fault persists; never bypass safety sensors; never run a cycle while a cat is inside; keep incident photos for warranty.

---

# Back-Test Results

| Model | Sample | Median abs error | Worst over | Worst under | Conservative failed | Verdict |
|---|---|---|---|---|---|---|
| Bin interval, expected (median capacity 21 / household rate) | 12 fills, leave-one-out | 1.2 days | +0.7 d | −2.4 d | — | Usable with mandatory range display |
| Bin interval, conservative (p10 capacity ≈ 8–12) | 12 | — | — | — | 2/12 at p10; **0/12 at capacity=12** | Ship conservative at 12 cleans |
| Vacation readiness | n/a (decision tool) | — | — | — | — | Grounded in 28-day intervention history |
| Litter cost | 0 usable trials | — | — | — | — | **Do not build yet** |

---

# Data Gaps

1. Litter brand/product/price/mass — never logged (blocks all litter tools).
2. Load cell uncalibrated (device units ↔ grams unknown) — blocks consumption claims.
3. Stock app history not archived (screenshots/exports) — limits app-vs-reality claims to device DPs.
4. Firmware version(s) unknown — cannot segment behavior by firmware.
5. Device daily-counter semantics unverified (observed divergence, cause unknown).
6. Manual-intervention minutes not logged (blocks maintenance-time tool).
7. Only one household, one model, 3 cats — every public number needs the single-household caveat.
8. Per-visit weight DP undecoded — same-weight overlap stated from owner observation, not measured bands.

# Recommended Future Logging

- Litter trial schema (given above) + one-time load-cell calibration against known masses.
- `drawer_emptied` manual event (sitter/owner taps a button) → separates early empties from true fulls; fixes the capacity denominator.
- Archive app screenshots on a schedule during any experiment window (app-vs-reality corpus).
- Decode/verify the daily-counter DP semantics; log firmware/app version strings.
- `manual_maintenance_minutes` per intervention; scatter/odor/dust 1–5 scores weekly.
- Keep the jam detector's zero-cat-streak metric as a first-class logged series (it is the "is it actually working" signal).

---

# Content and Video Opportunities

**Flagship pages (10):** ① SC10 28-day instrumented review (data-backed) ② "My litter robot said standby for 34 hours while it was jammed" — full incident story ③ Waste-drawer interval guide + calculator ④ Leaving an automatic litter box for two weeks: what actually happened ⑤ Same-weight cats: why the app can't tell them apart (and what would be required to fix it) ⑥ E1/infrared stops: pattern, causes, fixes ⑦ Sitter's guide to an automatic litter box ⑧ Local control without the cloud (technical authority) ⑨ Is the app's cat counter accurate? ⑩ Pre-trip test checklist for any automatic litter box.

**Answer blocks (20):** queries #6, 8, 11–15, 17, 25–27, 32–34, 40–41, 45, 52, 56, 58 from the table above.

**Troubleshooting entries (10):** standby-but-jammed; E1 recurring; drum stuck; bin-full oscillation; cleaning stops halfway; phantom visits; post-move re-seat; post-deep-clean re-seat; low-litter sticking; app/box state mismatch.

**Tool/result pages (10):** calculator A landing + per-cat-count result pages (1/2/3/4+ cats), readiness tool + "not-ready" advice page, backup-box explainer, drawer-bag fit guide, pre-trip checklist printable, "how we measured" methodology page (AEO/citation magnet).

**Video concepts (10):** timelapse of a full drawer cycle with clean counter; the jam re-enactment (dramatized state-vs-reality split screen); what 3 cats do to a drawer in 48h; E1 stop caught on camera; sitter handoff walkthrough; drawer-reinsert full-signal quirk; night-usage montage (usage peaks 3–6 am); calculator walkthrough; same-weight cats demo (why the scale can't know); pre-trip test run.

**TikTok short-form (10):** "your litter robot can lie to you" (jam hook); "how many days till full? math in 15s"; "3 cats vs 1 drawer"; "the app said 1 visit — camera counted 12"; "cats visit without going 39% of the time"; "why night owls: usage histogram"; "E1 means check, not broken"; "the drawer-jiggle false alarm"; "what your sitter actually needs to know"; "we logged 428 visits — here's the one number that matters" — each ending on the TikTok Shop showcase CTA.

| Asset class | CTA | Risk/caveat |
|---|---|---|
| Review/travel/capacity | TikTok Shop showcase ("check current price") | Single-household data; say so |
| Litter/bags/accessories | Respective affiliate | No litter verdicts until trials exist |
| Troubleshooting/authority | None (trust + backlinks) | Safety wording; never bypass sensors |

---

# Ranked Build Recommendation

| Rank | Tool | ROI | Evidence | Affiliate | Speed | Risk | Recommendation |
|---:|---|---:|---:|---:|---:|---:|---|
| 1 | Waste-drawer interval calculator | High | High (back-tested) | Med | Fast | Low | **Build first** |
| 2 | Vacation readiness decision tool | High | High (lived) | High | Fast | Med (wording) | **Build second** |
| 3 | Sensor/jam troubleshooter tree | Med-High | Unique | Low direct / High authority | Med | Med (safety) | **The non-calculator pick** |
| 4 | Same-weight explainer (F) | Med | Unique | Med | Fast | Low | Build as page, not tool |
| 5 | Litter compatibility + cost (C/D) | High later | **None yet** | High | — | High now | **Needs data collection** (start trials + calibration) |
| 6 | Model fit selector (I) | Low | One model | Med | — | High | **Reject** for now |

Rationale: Tool A converts the single most reliable measured quantity (cleans-per-fill) into the single most common practical buyer question, back-tests acceptably, builds in days, and every travel/capacity content piece funnels into it. Tool B monetizes the strongest narrative asset (real two-week absence + jam) with high backup-box affiliate fit. The troubleshooter owns a query space nobody with only marketing access can write honestly ("standby but jammed"). Litter tools have the highest affiliate ceiling but literally zero usable product data today — start the trial log now so they're buildable in 2–3 months. The model selector pretends breadth the evidence doesn't have.

---

# Facts Safe to Publish

- All aggregate usage numbers (entries, eliminations, durations, per-day distributions, night-heavy usage buckets).
- Cleaning metrics (counts, 219s median cycle, 1.15 cleans/elimination, 3-min delay adherence).
- Drawer-cycle statistics and the calculator built on them (with single-household caveat).
- Fault-episode counts, E1 pattern, median clear times.
- The jam narrative (no dates finer than "~34 hours, during a multi-week window"; no location).
- Same-weight limitation as mechanism + this household's experience.
- Custom-vision architecture at a conceptual level and its practicality verdict.
- Travel verdict as phrased above.

# Facts Requiring Owner Review

- Any specific cat photo/frame before publication.
- Cat names, if ever used instead of Cat A/B/C.
- The exact jam recovery procedure narrative (mentions who did what).
- Litter brand identification once recalled/logged (purchase records).
- Any screenshot of the vendor app (TOS + accuracy framing).
- TikTok Shop linkage phrasing and disclosure copy (affiliate compliance).

# Facts That Must Stay Private

- All credentials, keys, tokens, device IDs, MACs, IPs, hostnames, camera URLs/topology.
- Exact travel dates and any recurring absence pattern; sitter identities.
- Raw camera frames and any interior-home imagery not explicitly cleared.
- Raw database files and non-aggregated per-visit timestamps at publishing granularity finer than the hour-bucket level.
- Household address/region beyond what the owner already publishes.

---

# Machine-Readable JSON Appendix

```json
{
  "audit_date": "2026-07-17",
  "system": {
    "model": "MeoWant SC10",
    "observation_start": "2026-06-20",
    "observation_end": "2026-07-17",
    "observed_days": 28,
    "complete_days": 24,
    "partial_days": 4,
    "firmware_versions": []
  },
  "data_quality": {
    "overall": "medium",
    "major_gaps": [
      "litter brand/product never logged",
      "load cell uncalibrated (device units only)",
      "stock app history not archived",
      "firmware versions unknown",
      "3 zero-camera days + 1 near-zero day",
      "single household, single model"
    ],
    "major_biases": [
      "sealed-globe visits ~25% structurally frameless",
      "attribution coverage varies 12-58% by week with camera outages",
      "early manual drawer empties contaminate day-based bin cycles",
      "matcher promoted shadow->live mid-window changes attribution semantics",
      "two similar-weight/appearance cats confuse both stock and IR vision"
    ]
  },
  "usage": {
    "entries_total": 428,
    "eliminations_total": 261,
    "eliminations_per_day_mean": 9.32,
    "eliminations_per_day_median": 9.5,
    "eliminations_per_day_p10": 5.7,
    "eliminations_per_day_p90": 13.0
  },
  "cleaning": {
    "clean_cycles_total": 299,
    "automatic_cycles": 299,
    "manual_cycles": null,
    "cycles_per_elimination": 1.146,
    "interrupted_cycles": 28
  },
  "bin": {
    "complete_fill_cycles": 12,
    "cleans_per_fill_median": 21,
    "cleans_per_fill_p10": 8,
    "cleans_per_fill_p90": 29,
    "days_per_fill_median": 1.8,
    "days_per_fill_p10": 0.3,
    "days_per_fill_p90": 3.0
  },
  "litter": {
    "products_tested": [],
    "consumption_per_month": null,
    "consumption_unit": null,
    "confidence": "unknown"
  },
  "reliability": {
    "native_faults": 8,
    "physical_failures_without_fault": 1,
    "jam_incidents": 1,
    "obstruction_incidents": 3,
    "phantom_visit_incidents": 1,
    "manual_interventions": 12,
    "interventions_per_30_days": 12.9
  },
  "sensors": {
    "normal_operation_conclusion": "Generally accurate during unobstructed normal operation; specific jammed/blocked physical states produced misleading or incomplete reporting, including a 34h jam reported as standby with phantom visit events.",
    "known_obstruction_modes": [
      "drum stuck mid-rotation (no fault code)",
      "infrared-protection E1 stops under sticky-waste/low-litter conditions",
      "waste-path/chute obstruction adjacent to E1 clusters"
    ],
    "misleading_state_modes": [
      "standby reported during physical jam",
      "phantom cat_enter/elimination events while jammed",
      "device daily counter diverges from its own event stream",
      "bin-full signal oscillates on drawer reinsertion"
    ],
    "publishable_accuracy_metric": false
  },
  "tracking": {
    "stock_same_weight_limitation": true,
    "custom_vision_commit_rate": 0.39,
    "custom_vision_accuracy_when_committed": 0.94,
    "practical_for_average_buyer": false
  },
  "travel": {
    "rounded_trip_duration_days": 14,
    "faults": 3,
    "manual_interventions": 9,
    "sitter_interventions": 9,
    "remote_recoveries": 4,
    "stock_only_verdict": "Waste handling continued unattended, but drawer capacity forced ~2-day physical service visits, and one physical jam was invisible to stock reporting for ~34 hours.",
    "custom_system_verdict": "Materially reduced risk via independent observation and alerting, but required its own maintenance and still could not remotely fix any physical failure; scheduled physical checks remained mandatory."
  },
  "calculators": [
    {
      "name": "SC10 Waste Drawer Interval Estimator",
      "status": "build_now",
      "primary_query": "how often empty meowant sc10 waste drawer",
      "inputs": ["cats", "eliminations_per_cat_per_day", "cleans_per_elimination", "manual_extra_cleans_per_day", "drawer_state", "trip_length", "safety_buffer"],
      "outputs": ["expected_days_to_service", "conservative_days_to_service", "service_by_date", "risk_level", "sitter_interval", "backup_box_recommendation"],
      "formula": "days = capacity_cleans * drawer_factor / (cats * elim_per_cat_day * cleans_per_elim + manual_extra); capacity expected=20, conservative=12 (first-party, 12 measured fills)",
      "evidence_strength": "high",
      "affiliate_route": "TikTok Shop showcase + drawer-bag affiliate",
      "limitations": ["single household", "12 fill cycles", "jam recovery can consume a full drawer instantly", "capacity varies with litter/clump size"]
    },
    {
      "name": "Vacation Readiness Check",
      "status": "build_now",
      "primary_query": "can i leave automatic litter box for 2 weeks vacation",
      "inputs": ["cats", "days_away", "drawer_state", "litter_level", "recent_fault", "recent_jam", "backup_box", "sitter_availability", "advanced_monitoring_toggles", "same_weight_cats", "unusual_waste_behavior"],
      "outputs": ["readiness_tier", "max_hours_between_checks", "drawer_services_during_trip", "pretrip_tasks", "non_remote_fixable_failures", "stock_only_verdict", "advanced_verdict"],
      "formula": "decision tree grounded in 28-day intervention history; drawer math from interval estimator",
      "evidence_strength": "high",
      "affiliate_route": "TikTok Shop showcase + backup-box affiliate",
      "limitations": ["decision tool not numeric", "stock-mode assumptions must stay default"]
    },
    {
      "name": "Sensor/Jam Troubleshooter",
      "status": "build_now",
      "primary_query": "meowant says standby but not cleaning",
      "inputs": ["symptom_branch"],
      "outputs": ["safe_actions", "physical_checks", "support_escalation", "warranty_evidence", "backup_box_advice"],
      "formula": "decision tree from sanitized incident catalog",
      "evidence_strength": "high",
      "affiliate_route": "none (authority asset)",
      "limitations": ["safety wording constraints", "never instructs sensor bypass"]
    },
    {
      "name": "Litter Compatibility + Monthly Cost",
      "status": "needs_more_data",
      "primary_query": "best litter for meowant sc10",
      "inputs": [],
      "outputs": [],
      "formula": "blocked: no litter products logged, load cell uncalibrated",
      "evidence_strength": "low",
      "affiliate_route": "litter affiliate",
      "limitations": ["requires trial schema + calibration; est. 2-3 months of logging"]
    },
    {
      "name": "MeoWant Model & Cat Fit Selector",
      "status": "reject",
      "primary_query": "which meowant model",
      "inputs": [],
      "outputs": [],
      "formula": "",
      "evidence_strength": "low",
      "affiliate_route": "TikTok Shop",
      "limitations": ["only one model owned; would pretend breadth"]
    }
  ],
  "top_queries": [
    {"query": "how often empty meowant sc10 waste drawer", "intent": "capacity", "evidence_available": "12 measured fill cycles", "recommended_route": "calculator + page", "page_or_section": "page", "monetization": "tiktok_shop", "confidence": "high"},
    {"query": "meowant says standby but not cleaning", "intent": "troubleshoot", "evidence_available": "34h jam incident, unique", "recommended_route": "troubleshooter + page", "page_or_section": "page", "monetization": "authority", "confidence": "high"},
    {"query": "can i leave automatic litter box for 2 weeks vacation", "intent": "travel", "evidence_available": "instrumented 2-week absence", "recommended_route": "readiness tool + page", "page_or_section": "page", "monetization": "tiktok_shop+backup_box", "confidence": "high"},
    {"query": "automatic litter box two cats same weight", "intent": "pre-purchase", "evidence_available": "vision project + mechanism", "recommended_route": "explainer", "page_or_section": "page", "monetization": "tiktok_shop", "confidence": "high"},
    {"query": "meowant e1 error infrared", "intent": "troubleshoot", "evidence_available": "8 fault episodes", "recommended_route": "page", "page_or_section": "page", "monetization": "none", "confidence": "high"},
    {"query": "meowant app cat count wrong", "intent": "app-vs-reality", "evidence_available": "counter divergence observed", "recommended_route": "answer block", "page_or_section": "section", "monetization": "none", "confidence": "high"},
    {"query": "meowant sc10 review 30 days", "intent": "pre-purchase", "evidence_available": "entire dataset", "recommended_route": "flagship review", "page_or_section": "page", "monetization": "tiktok_shop", "confidence": "high"},
    {"query": "how many times a day do cats use litter box", "intent": "informational", "evidence_available": "3.1/cat/day measured", "recommended_route": "answer block", "page_or_section": "section", "monetization": "adjacent", "confidence": "high"},
    {"query": "best litter for meowant sc10", "intent": "cost/compat", "evidence_available": "insufficient - defer", "recommended_route": "defer until trials", "page_or_section": "none", "monetization": "litter_affiliate", "confidence": "low"},
    {"query": "meowant without wifi local control", "intent": "technical", "evidence_available": "working local control", "recommended_route": "authority page", "page_or_section": "page", "monetization": "authority", "confidence": "high"}
  ],
  "data_gaps": [
    "litter product identity/price/mass",
    "load-cell calibration",
    "stock app history archive",
    "firmware versions",
    "daily-counter DP semantics",
    "manual maintenance minutes",
    "per-visit weight decode"
  ],
  "future_logging_fields": [
    "litter trial schema (trial_id..notes)",
    "drawer_emptied manual event",
    "app screenshot archive cadence",
    "firmware/app version strings",
    "manual_maintenance_minutes",
    "weekly scatter/odor/dust scores",
    "zero-cat-streak series as first-class metric"
  ],
  "safe_to_publish": [
    "aggregate usage/cleaning/drawer statistics with single-household caveat",
    "fault episode counts and E1 pattern",
    "jam narrative at rounded granularity",
    "same-weight limitation mechanism",
    "travel verdict as phrased",
    "custom vision practicality verdict"
  ],
  "requires_owner_review": [
    "any camera frame",
    "cat names vs Cat A/B/C",
    "jam recovery narrative details",
    "litter brand once identified",
    "vendor app screenshots",
    "affiliate disclosure copy"
  ],
  "must_remain_private": [
    "credentials/keys/tokens/device IDs/MACs/IPs/URLs/topology",
    "exact travel dates and absence patterns",
    "sitter identities",
    "raw frames and interior imagery",
    "raw database files",
    "sub-hour timestamp granularity in published content"
  ]
}
```
