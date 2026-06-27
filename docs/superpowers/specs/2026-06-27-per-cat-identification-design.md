# Per-Cat Identification — Combined Vision Design

**Status:** design / spec
**Date:** 2026-06-27
**Scope:** the data flywheel (Unit A) + the per-cat ID matcher (Unit B) + sequencing, as one vision doc.

## Goal & North Star

Confident per-cat elimination tracking for 3 cats (Ucok, Ella, Garfield) so health
signals (a cat that stops eliminating / shows a UTI frequency spike) are attributed
to the right cat.

**North star:** a sick cat must NEVER be silently masked. Abstaining ("can't
confirm — go look / tag me") is acceptable and expected; a **confident-WRONG**
attribution is the enemy, because it can both false-alarm AND mask (crediting a
sick cat's visit to a healthy one).

**Why now:** the current VLM labeler (agy→Gemma, few-shot with reference photos)
hits ~73% frame accuracy with camera-domain refs, but ~27% of *committed* visits
are confidently wrong-cat — concentrated on the Garfield/Ucok tabby collision.
That is precisely the danger mode the north star forbids.

## 2026-06-27 — Council Review + Measured Corrections (supersedes stale claims below)

A `/council` pass (deep panel, architecture lens) plus a web-grounding fetch and a
direct DB measurement changed three load-bearing assumptions in the original draft.
Where the rest of this doc conflicts with this section, **this section wins.**

**Correction 1 — the per-frame confidence signal is DEAD (measured).** Every one of
the 1046 `captures.pred_conf` rows is exactly `1.000`; `visits.confidence` is `1.0`
for 87/91. The VLM emits saturated confidence, so there is **no graded score to
calibrate a conformal/abstain threshold on today.** Consequence: conformal must
calibrate on a *real* nonconformity score — the embedder's gallery distance
(`1 − cos(x, μ_cat)`), not the VLM's confidence. The usable "contested" signal that
*does* exist is `label_source='auto-conflict'` (405 frames where intra-visit frames
disagreed). Contested-ness must key on auto-conflict / tabby pred-disagreement,
**not** on `pred_conf`.

**Correction 2 — "Garfield glows in IR" is NOT supported (measured, inconclusive→
contradicted).** Of IR frames, only 4 are predicted Garfield and they are *darker*
(p99 brightness 207 vs Ucok 230; bright-pixel fraction lower too), not brighter. The
sample is tiny and label-confounded, so this is "not build-on-able," not "proven
false" — but **do not architect a free deterministic IR discriminator from coat/eye
reflectivity.** The retroreflective-tape idea (Gemini, council) remains a *possible*
physical fix but is gated by the owner's "no new collars/hardware" non-goal.

**Correction 3 — the IR attribution collapse is one-directional (measured).** In IR
the labeler routes **208 frames → Ucok vs 4 → Garfield.** It doesn't merely confuse
the tabbies in IR; it *collapses both into Ucok.* That asymmetry is the 27%-wrong
mode, quantified — and it means a sick Garfield at night is the single most likely
silent-mask. Any IR attribution to a tabby is suspect by default.

**Council-endorsed architecture deltas (adopted):**
- **Embedder: prefer MegaDescriptor-T over DINOv2-S — BAKE-OFF RUN 2026-06-27.**
  Web grounding said MegaDescriptor (Swin/WildlifeDatasets) beats DINOv2 on animal
  re-ID. Leave-one-out NN on our 84 human-labeled frames (cat-cropped via SSDLite)
  measured: **daytime is a wash** (DINOv2-S 89.7% vs MegaDescriptor-T 88.2%, n=68,
  within noise) — the benchmark did NOT transfer to daylight on our cameras. But
  **on IR (n=16) MegaDescriptor-T leads 81.2% vs 68.8%** — and IR is the failure
  domain. So: **MegaDescriptor-T for the build, DINOv2-S fallback.** Two bigger
  findings: (a) BOTH embedders crush the VLM (~87-90% LOO vs 73%), confirming the
  move off the VLM; (b) **both score 0/2 on Ucok** — with 1 match frame he is
  unidentifiable by construction, so the embedder choice is dominated by getting
  Ucok + IR labels. Re-run once Ucok ≥10 frames. (Note: LOO-on-gallery, not a
  held-out test — overstates absolute accuracy; the day-vs-IR *delta* is the signal.)
- **Unit of analysis is the VISIT (tracklet), not the frame.** Segment each visit,
  anchor identity at the best-lit frame, and use **tracklet-averaged embeddings**
  (suppresses blur/angle noise). Collapses ~100× of the per-frame classifications.
- **Conformal PREDICTION SETS, not a scalar threshold.** Use class-conditional
  (Mondrian) split-conformal → size-1 commits; size-2 `{Garfield,Ucok}` abstains.
  This replaces "tune one margin" (which is uncalibratable while Ucok has N=2 and
  conf is saturated). Expect Ucok to mostly abstain until labeled — which is *safe.*
- **Co-presence/exclusion is a HARD veto, not a soft prior** — and a free label
  generator (3-way simultaneous sightings yield Ucok labels; Ucok N=2 is THE
  bottleneck — label him first).
- **Asymmetric timer-reset (the safety invariant):** a visit may reset a cat's
  deadman quota only if its attribution is size-1 and uncontested; **contested/
  unknown tabby visits reset NEITHER tabby's timer.** This makes "never silently
  mask a sick cat" true *by construction* and is the precondition for re-enabling
  the per-cat deadman (`deadman.per_cat_enabled`, currently OFF). Plus DeepSeek's
  point: a spike in the *unknown* bucket is itself a raw health signal.
- **IR: detection-only by default.** Day-calibrated conformal gives no IR guarantee
  (exchangeability breaks). Bootstrap IR labels via trajectory continuity /
  co-presence; a **self-supervised tracklet-contrastive adapter** on the NAS harvest
  (pull same-tracklet, push concurrent-tracklet) earns IR adaptation with zero human
  labels — *before* any LoRA.

**Pre-trip status:** none of the above ships before the trip. The current safety net
(`check_no_go`, attribution-independent, 12h, ON) is already *off the classifier* —
exactly what the council says it must be — so it is trip-safe as-is. The work above
is post-trip, fed by the harvest. Council run: `a5cf261ac8234980` (synthesis logged).

## Non-Goals / Out of Scope

- New collars or added hardware (owner declined). We use existing collars only.
- Training an image model from scratch, or LoRA as a *first* step.
- Replacing the safety nets — the attribution-independent aggregate catch-all
  (`deadman.check_no_go` on `store.last_eliminated_ts`) remains the backstop and
  is unchanged by this work.

## Load-Bearing Reality: Dual-Mode Imaging

Frames are **dual-mode**, verified against real captures:
- **Color** when the room has ambient light (daytime; lit evenings/nights).
- **True IR grayscale** in real darkness (e.g., 02:03am, no lights) — coat color
  is physically gone. (Earlier draft claimed reliable IR eyeshine as a tabby
  splitter; **measurement on 2026-06-27 did NOT support it** — see the corrections
  section below.)

**Prerequisite #1 — DONE.** `captures.is_ir` detection is implemented
(`mw/imgutil.is_grayscale`, channel-spread < 10.0) and backfilled: of 1490
captures, **506 are flagged IR / ~984 color, 0 NULL** (`scripts/backfill_is_ir.py`,
migration in `mw/store._MIGRATIONS`). The matcher can now gate color-only signals
(collars) on `is_ir`.

`meowcam4` runs **thingino** firmware (vs Wyze on the others) and images
differently — treat it as a distinct imaging sub-domain when building galleries.

## Data Inventory (as of 2026-06-27)

- **1490 captures over ~7 days** (Jun 20–27).
- ~Even split: **725 day-hour / 765 night-hour** frames. Night is a *mix* of
  lit-color and true-IR; **many night frames are EMPTY** (camera grabbed while the
  cat was in another camera's view) → cat/no-cat filtering is required first.
- **Only 86 reliably labeled** (84 human + 2 corrected). ~1405 are noisy auto
  labels (922 `auto` + 405 `auto-conflict` + 10 `auto-none`), ~67 pending.
- Per-cat human/anchor labels: Ucok 2, Garfield 36, Ella 46 (all daytime).
- **Conclusion: the constraint is LABELING, not capture.** A week of color+IR cat
  frames already exists to bootstrap from; it's mostly unlabeled.

## Architecture: Multi-Signal Fusion Matcher

Lands in the existing `mw/identify.py` `Matcher` slot (replaces `NullMatcher`).
No single channel decides; fuse whatever signals the frame's mode supports, then
abstain unless the fused decision clears a calibrated margin.

| Signal | Color frame | IR frame | Notes |
|---|---|---|---|
| DINOv2-S gallery (visual embedding + kNN/centroid) | strong | weaker | the workhorse; zero-shot first |
| Color collar (**pink=Ucok** / blue=Garfield) | tabby-splitter | ✗ (color gone) | owner-confirmed 2026-06-27; existing hardware only |
| Silhouette / fur-length | ✓ | ✓ | Ella (longhair) vs short tabbies — survives IR |
| Co-presence / exclusion | ✓ | ✓ | opportunistic now (station cams); PRIMARY once a room/hallway cam is added |
| LoRA-adapted embedder | later | later | only if zero-shot plateaus AND enough labeled data |

**Garfield/Ucok collision strategy:** color collar splits them in color frames; in
IR, fall to silhouette + co-presence; when still ambiguous, **confusable-pair
merge** — output "a tabby, unsure which" and abstain on the split rather than
guess.

## Abstain & Safety Calibration

- **Conformal / margin-based threshold** to bound the wrong-attribution rate.
- **Safety-max for health decisions:** target ε ≤ ~2% wrong-cat; accept a high
  "can't confirm" rate. (Optional later: looser threshold for cosmetic dashboard
  stats only — never for health.)
- **Invariant (must hold):** both *abstain* AND *misattribution* must fail toward
  "check," never toward "fine." A confident-wrong label must not merge a sick
  cat's silence into another cat's healthy record. The aggregate catch-all stays
  attribution-independent.

## Data Flywheel (Unit A)

1. **Passive collector:** an always-on cat detector (reuse `TorchvisionCatFilter`)
   runs on the warm-reader frames, decoupled from litterbox events, saving
   cat-positive frames to an **external drive** (dedup near-identical consecutive
   frames; retention cap to bound size). Build this **pre-trip** so it harvests a
   trip's worth of color+IR data while the owner is away.
2. **Cat/no-cat filter first** — discard empty frames before clustering.
3. **Cluster-and-propagate labeling:** embed cat frames (DINOv2-S), cluster, and
   tag each *cluster* once — anchored on the litterbox-tagged frames as labeled
   seeds + owner Telegram taps. Turns thousands of frames into labels with ~dozens
   of decisions. Label by exception where clusters are ambiguous (esp. tabby/IR).

## Components (interfaces & responsibilities)

- `is_ir` detector (fix) — per-frame color-vs-IR classification, stored on
  `captures.is_ir`. Consumed by the matcher to gate color-only signals.
- `Matcher` impl in `identify.py` — `predict(image_path) -> (cat_id, confidence)`;
  fuses signals, returns `(None, conf)` to abstain. Inference on the Mac Studio.
- Gallery store — per-cat embeddings (DINOv2-S), segmented by imaging mode
  (color / IR) and by camera sub-domain (Wyze vs thingino meowcam4).
- Collar/color signal — color-frame-only feature (orange/blue) feeding the fusion.
- Silhouette signal — fur-length/body-shape feature, both modes.
- Co-presence resolver — uses other cameras' detections to exclude cats; scales
  with added coverage.
- Passive collector + labeler (Unit A) — as above; gpubox for any LoRA training.

## Sequencing

1. **Fix `is_ir` detection** (prerequisite for mode-aware fusion).
2. **Passive collector** — build pre-trip; harvest while away.
3. **DINOv2-S zero-shot gallery matcher** + color-collar + silhouette fusion +
   conformal abstain → into `identify.py`. Bootstrap gallery from the existing
   week (cluster-and-propagate).
4. **Co-presence** — wire as a signal; promote to primary as room cams are added.
5. **LoRA-adapt** the embedder (gpubox) — ONLY if zero-shot plateaus and labeled
   data (esp. IR + Ucok) is sufficient.

## Testing / Validation

- **Held-out accuracy** at the visit level (precision-when-committed + wrong-cat
  rate + abstain rate), per cat and per imaging mode.
- **Leakage caveat:** human-tagged frames used as gallery references must be held
  OUT of the test set — `autolabel.validate()` currently does not, so it now
  overstates accuracy (see [[reference-meowant-labeler-accuracy]]).
- **Safety check:** verify no configuration produces a confident-wrong that masks
  a silent cat; the aggregate catch-all must still fire independently.
- Baseline to beat: VLM ~73% frame / 73% visit-precision with 27% wrong-cat.

## Open Experiments / Risks

- **Co-presence coverage:** station-only today; viability as a primary channel
  depends on the owner adding a room/hallway cam (they're open to it).
- **IR collar readability:** existing collars do NOT reliably resolve in IR at the
  camera angle/resolution — so collars are a color-frame signal only (no new
  hardware). True-dark ID rests on silhouette + co-presence + abstain.
- **meowcam4 (thingino)** sub-domain may need its own gallery slice.
- **Ella's tag** is often hidden by her long fur — unreliable as a marker; she
  rides on silhouette.

## Related

- [[reference-meowant-camera-stack]] — cryze_v2 + MediaMTX topology, the cameras.
- [[reference-meowant-labeler-accuracy]] — measured VLM accuracy + the leakage caveat.
- Safety backstop: `deadman.check_no_go` (attribution-independent aggregate net).
