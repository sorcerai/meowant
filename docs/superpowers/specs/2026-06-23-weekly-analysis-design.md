# Weekly Consolidation + Per-Cat Analysis (gated claude -p analyst) — Design

**Date:** 2026-06-23
**Status:** design (revised after /council run 33f3e783d3fd40a2, 7/7 quorum)
**Beads:** meowant-oac (this), meowant-71z (per-cat thresholds — shares the gap math)

## Goal

Once a week, consolidate every table the system writes (visits/eliminations,
feed_events, bowl_events, incidents, capture attribution) into a **per-cat
7-day rollup**, decide *statistically* what is real signal vs sampling noise,
and surface chronic drift the acute alarms can't see (gradually widening gaps,
creeping weights, falling frequency). Deliver to Telegram, persist a snapshot.

This is the **chronic-drift** counterpart to the acute watchdogs
(deadman/health_watch/canary): those fire on a single bad reading in hours;
this watches slow trends over weeks.

## What the council changed (why this is a revision)

The first draft put `claude -p` in charge of writing free narrative over a
"facts JSON." A 7-model panel rejected that **unanimously**, for three reasons
this revision now treats as load-bearing:

1. **The "facts" aren't facts.** A 7-day delta on n≈14 voids with ~50%
   attribution loss is sampling noise. Numbers need **variance + sample
   adequacy + significance**, computed deterministically, before anyone
   interprets them. → *Statistical Gatekeeper* (below).
2. **Attribution loss is itself a health signal, not metadata.** A sick,
   lethargic, or limping cat moves differently — a changed motion profile is
   exactly what drops a cat into the ~50% *unattributed* bucket. Analyzing only
   cleanly-attributed cats systematically blinds the report to the cats getting
   sick. → attribution% is a *first-class per-cat vital*.
3. **An LLM that interprets raw noise causes harm**: tone drift, false
   reassurance ("all clear" trains the owner to stop reading numbers), and
   model-churn (Sonnet 4→5→6) silently breaking longitudinal comparison. →
   the LLM is **demoted to a gated, number-free consumer of pre-classified
   findings**, never the interpreter of raw metrics.

## Architecture (three layers, strict one-way flow)

```
  ┌─ Layer 1: CONSOLIDATE (pure SQL) ────────────────────────────────┐
  │ collect_facts(conn, now): trailing 7d + prior 7d per cat:         │
  │   counts, gap mean/min/max, duration & weight bands,              │
  │   circadian, per-cat attribution%, feed/bowl/incident rollup      │
  └───────────────────────────────┬──────────────────────────────────┘
  ┌─ Layer 2: GATEKEEP (pure SQL/stats — the safety layer) ───────────┐
  │ assess(facts): for each metric/cat compute                        │
  │   • sample adequacy  (N real voids; Garfield weight+dur filtered) │
  │   • error margin / variance on each point estimate                │
  │   • week-over-week delta WITH significance (is Δ > noise?)        │
  │   • persistence (did it hold ≥2 weeks?)  → only then "drift"      │
  │   • attribution% as its own tracked signal (rising = flag)        │
  │ emits findings[]: {cat, metric, severity∈{nominal,watch,drift,    │
  │   insufficient_data}, value, margin, evidence_keys[]}             │
  └───────────────────────────────┬──────────────────────────────────┘
  ┌─ Layer 3: RENDER ────────────────────────────────────────────────┐
  │ deterministic table  ──────────────────────────►  ALWAYS sent     │
  │   (values ± margins, sample banner, attribution line)             │
  │        │ (only if gates pass: enough data + not shadow)           │
  │        ▼                                                          │
  │ claude -p  (one-shot, no tools/DB/file, never skip-perms)         │
  │   IN:  findings[] only (NOT raw facts)                            │
  │   OUT: validated JSON →                                           │
  │     classifier: per-cat {severity, slugs[] from vetted library}  │
  │     hypotheses: [{text(hedged), evidence_keys[]}]  (cross-cat/wk) │
  │   GATES: schema-valid · ZERO numbers (regex) · every claim cites  │
  │     an evidence_key · no diagnosis/causal verbs · temp 0          │
  │   fail/timeout/validation-error → deterministic table only        │
  └───────────────────────────────────────────────────────────────────┘
```

The deterministic table is the **durable longitudinal record** (persisted,
compared week-over-week). The LLM layer is **ephemeral commentary** regenerated
each week — never the archive, never load-bearing.

## The LLM's role (owner-chosen: classifier **and** hypothesis-generator)

A single `claude -p` call returns one object with two sections, both number-free
and both gated:

- **Classifier** (readability): per cat, pick `severity` and 0+ `slugs` from a
  **pre-written vetted library** (e.g. "gaps a little wider than last week",
  "visiting on his usual schedule"). The deterministic renderer interpolates the
  real numbers around the slugs. Fabrication is *structurally impossible* — the
  model can only mis-bucket, which is auditable against the Layer-2 findings.
- **Hypotheses** (analytical upside a template can't reach): cross-cat /
  cross-week observations — "all three shifted circadian timing together
  (environmental?)", "Garfield's real-void rate fell while his pokes rose" —
  each **hedged** ("one possibility…", forbidden: "indicates/means/clearly")
  and each anchored to `evidence_keys` so the owner verifies in seconds.

Both sections pass the numeric-regex gate (any digit not present in the findings
→ reject whole output → deterministic table). The hypotheses section will be
mostly empty for the first weeks — that is correct, not a bug.

## Components

**`mw/weekly.py`**
- `collect_facts(conn, now, *, cats=("Ucok","Ella","Garfield")) -> dict` — Layer 1.
  Reuses the gap/circadian/duration SQL validated 2026-06-23. Garfield's
  counts/bands weight+duration-filtered (real void = weight present AND dur>40s).
  Timestamp normalization on read (old rows `+00:00` UTC; most naive-local).
- `assess(facts) -> list[Finding]` — Layer 2. Sample adequacy, error margins,
  significance-tested deltas, ≥2-week persistence for "drift", attribution% as
  its own finding. Below adequacy → `severity="insufficient_data"`.
- `facts_only_text(facts, findings) -> str` — deterministic table: values ±
  margins, an explicit sample banner ("Ella: N=11 voids, low power"), and a
  per-cat attribution line. This is the always-sent artifact and the fallback.
- `narrate(findings, *, run=None) -> dict|None` — Layer 3 LLM. Renders findings
  into the prompt, calls `claude -p` (default `run` = `subprocess.run(["claude",
  "-p",...])`; injectable fake in tests), parses + validates JSON. Returns the
  validated dict, or `None` on any failure/violation (caller falls to table).
- `validate_llm_output(obj, findings) -> bool` — schema check; no digits
  (regex); every slug from the vetted library; every hypothesis cites a real
  `evidence_key`; no forbidden causal/confidence words.
- `WeeklyAnalyst(conn, notify, now_fn=time.time, run=None, *,
  state_path="weekly_state.json", interval_days=7, min_void_n=?, shadow=True)`:
  - `due(now)` — `now - last_run >= interval_days`; last_run persisted (deadman
    `state_path` pattern) so restart neither double-fires nor skips.
  - `run_once(now)`: if not due, return. Else collect → assess → build table →
    if (not shadow and gates pass) `narrate`; compose text (table + LLM section,
    or table alone) → persist via `store.log_weekly_report` (always store
    facts + findings; store LLM output when present) → `notify(...)` latched on
    confirmed delivery (`is not False`) → stamp last_run. **Shadow mode** runs
    `narrate` and stores its output but does NOT include it in the sent message
    — for the first weeks, so we can eyeball LLM behavior against the table
    before trusting it.
  - `run()`: `while True: try run_once(now()) except->stderr; sleep(~6h)`. Never dies.

**`mw/store.py`** — `weekly_reports` table (in `SCHEMA`): `id, ts,
period_start, period_end, facts_json, findings_json, narrative_json`. Funcs:
`log_weekly_report(...)`, `latest_weekly_report(conn)`, `recent_weekly_reports(conn, limit=8)`.

**`meowantd.py`** — construct `WeeklyAnalyst` (gated on `weekly.enabled`), start
on a daemon thread after the existing watcher block.

**`mw/telegram_bot.py`** — `/weekly` → latest stored report (table always; LLM
section if present and not shadow).

**Config** (`config.json`, gitignored) — `weekly` block: `{enabled,
interval_days, claude_cmd, claude_timeout_s, cats, min_void_n, shadow}`.

## Model-drift defense

Pin a small **regression suite**: hand-curated `findings.json → expected
{severity buckets, allowed slugs}` fixtures. CI/test asserts the validator and
(with a recorded/faked `run`) the classifier mapping stay stable. The
deterministic table needs no such guard — it is the longitudinal source of
truth; LLM upgrades may change prose, never the archived numbers.

## Reliability & safety

- **Numbers are deterministic and significance-tested; narrative degrades to
  numbers.** Any LLM failure/timeout/validation-error → table only. Fail to
  LOUD-numbers, never silent.
- **Attribution% is monitored**, so a cat going quiet *by becoming
  unidentifiable* still shows up.
- **No fabricated stats by construction:** the LLM never receives raw point
  estimates as "facts," never emits a digit, never sets a threshold or triggers
  an action. Layer 1/2 own all math.
- **Independence:** a weekly failure can't mask any acute signal (separate
  thread/table; deadman/health_watch/canary untouched).
- **Persisted cadence** survives restarts; reload via `launchctl kickstart -k`.
- **Shadow-first**: prose isn't shown to the owner until it's been watched
  against the table for a few weeks.

## Testing

- `collect_facts`: seeded 2-week multi-cat in-memory DB → exact per-cat counts,
  gap mean/min/max, circadian, deltas, attribution%; Garfield pokes excluded by
  the filter; mixed-timestamp rows normalize.
- `assess`: low-N cat → `insufficient_data`; within-noise delta → `nominal`
  (not "drift"); significant + persistent delta → `drift`; one-week spike →
  `watch` not `drift`; rising attribution% emits its own finding.
- `validate_llm_output`: rejects output containing a digit; rejects a slug
  outside the library; rejects a hypothesis with no/invalid evidence_key;
  rejects forbidden causal words; accepts a clean classifier+hypotheses object.
- `narrate`: fake `run` returning valid JSON → parsed dict; raising / non-zero /
  empty / schema-invalid / contains-number → returns None (→ table fallback).
- `WeeklyAnalyst`: `due()` around the 7-day boundary (frozen clock); not-due →
  no-op; due → collect/assess/persist/notify/stamp; **shadow=True** → LLM
  output stored but NOT in the sent message; notify False → no stamp (retry next
  poll); restart reads persisted last_run.
- `store.weekly_reports`: log/latest/recent round-trip.
- bot: `/weekly` returns latest (table + LLM section when present).
- regression suite: pinned findings→severity fixtures stable.
- meowantd wiring presence test.

## Phasing

1. **Layers 1+2 + deterministic table** (collect_facts, assess, facts_only_text,
   WeeklyAnalyst with the LLM path off, store table, /weekly, Telegram). This is
   the safety artifact and is independently useful — ship and run it.
2. **Layer 3 in shadow** (narrate + validate + regression suite; stored, not
   shown) for a few weeks of real data.
3. **Expose the LLM section** once shadow output tracks the table and N is
   adequate. This also proves the gated-`claude -p` contract for self-healing C3.

## Relationship to self-healing C3

C3 (queued one-shot novel-incident diagnoser) is the same building block —
`claude -p` as a gated, read-only, number-free consumer that executes nothing.
This weekly analyst is the low-stakes proving ground for that contract; build
the gates here, reuse them in C3.

## Out of scope (later)

- Auto-applying suggested per-cat thresholds (71z owns that; here, observability).
- Per-cat eating attribution (who ate from the bowl).
- The LLM *detecting* anomalies (it classifies/hypothesizes over Layer-2
  findings; detection stays deterministic).
- Monthly/seasonal rollups; longer-horizon trend modeling.
