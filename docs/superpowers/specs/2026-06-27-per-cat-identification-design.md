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
  is physically gone; cats show IR eyeshine.

**Prerequisite bug:** the `captures.is_ir` flag is **broken** — 0 rows flagged
`is_ir=1` despite unmistakable IR frames in the store. The matcher must know which
mode a frame is in (color-only signals like collars are useless in IR), so
**fixing `is_ir` detection is prerequisite #1.**

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
| Color collar (orange=Ucok / blue=Garfield) | tabby-splitter | ✗ (color gone) | existing hardware only |
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
