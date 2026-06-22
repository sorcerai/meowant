# Commercial litter-box market vs. meowant (grounded, 2026-06-22)

Grounded multi-model council run (deep group, exa+tinyfish crawl, 5 sources) +
a verification fetch against primary vendor pages. Question: can you buy
vision-ID + local control + floor-scatter off the shelf?

## Verdict

**The combination (vision per-cat ID + fully local/offline + floor-scatter +
human-in-loop) is NOT buyable — DIY-only — and structurally will stay that way.**
7/7 council members agreed (confidence 0.78–0.95).

But vision per-cat ID *alone* is now commercial (this corrects the earlier
"no box does vision ID" claim):

- **Litter-Robot 5 Pro** ($899; double bundle ~$1,873) shipped **WhiskerVision /
  CatID facial recognition** ~**Nov 2025** — dual AI cameras (front + inner),
  per-cat facial ID "even between similar-looking cats, even in the dark," plus
  **WasteID** (#1 vs #2 = pee/poop). BUT: cloud-dependent, data "vaulted in
  secure cloud history," gated behind **Whisker+** (free tier = 5 min live/day,
  2-day storage). Camera is fixed to watch the globe entrance — **no floor-scatter,
  no local/offline control.**
  Sources: litter-robot.com/litter-robot-5-pro.html ; whisker.com/blog/whiskervision-catid-facial-recognition

## Per-product (first-party features, grounded)

| product | vision ID | weight ID | local/offline | floor-scatter | per-cat health |
|---|---|---|---|---|---|
| Litter-Robot 5 Pro | ✅ CatID (cloud) | ✅ | ❌ | ❌ | ✅ (+WasteID #1/#2) |
| Litter-Robot 4 | ❌ | ✅ SmartScale | ❌ (3rd-party HA = cloud-poll) | ❌ | ✅ |
| PETKIT Pura Max 2 / Pura X | ❌ (cam = safety/motion) | ✅ | ❌ (Tuya cloud) | ❌ | ✅ |
| Leo's Loo Too | ❌ | ✅ | ❌ | ❌ | ✅ |
| Furbulous / Neakasa M1 / CatLink | ❌ | ✅/varies | ❌ | ❌ | ✅ |
| LuluPet (historical vision-ID) | (✅) | – | ❌ | ❌ | – | defunct post-crowdfunding |

## Why the combination stays DIY-only (the structural moat)

Smart-litter-box economics = hardware margin + consumable lock-in + **cloud
subscription (MRR)**. Local control cannibalizes the subscription; vision-AI is
the *bait* for that subscription. So **local control and cloud vision-AI are
mutually exclusive by design** — no rational incumbent ships both. Weight-based
ID is "good enough" for mass market, keeping vision-ID a premium cloud upsell.

## meowant's unique, non-buyable parts

- vision per-cat ID **locally** (no cloud, no subscription)
- **floor-scatter detection** (no box does this — cameras watch the globe, not the room)
- **human-in-loop tap-to-label** (LR5 Pro dead-ends at "Unidentified Cat"; we fix + teach)
- runs on a rooted budget box vs $899–2,000 + subscription

## Convergent-design validation (the market arrived at our choices)

- LR5 Pro camera "fixed to capture cats entering and exiting the globe" =
  our hard-won meowcam4 entrance-angle conclusion.
- WasteID (#1 vs #2) = our pee/poop roadmap (bead meowant-7h6).
- "Unidentified Cat" notifications = the same occlusion/missed-ID failure we hit;
  our tap-to-label is a stronger answer.
- CatID "even similar-looking cats" = our hardest case is Ella (longhair tortie);
  Whisker's CEO brags CatID separates his two near-identical torties.

## The gap we now see (separate effort)

**Pee/poop classification** is the one headline feature LR5 Pro has (WasteID) that
we don't yet — tracked in **meowant-7h6** (dp102 weight-threshold classify). It's
"a whole other thing" — kept as its own bead, not started.
