# Self-Healing & Safety Monitoring — Design

**Date:** 2026-06-22
**Status:** design (pending review)

## Goal

Keep the cats **monitored** while the owner travels — never silently miss a sick-cat
signal. Self-healing is in service of that, not of daemon uptime for its own sake.

**Prime invariant:** *unknown state must become LOUD, never self-resolved into silence.*

This design was pressure-tested by a 7-model council (run 322e12b0). The council
near-unanimously rejected autonomous LLM code-editing on a health monitor (passing tests
certify known regressions, not that an LLM didn't take a signal-destroying shortcut — e.g.
wrapping a failing labeler in `try/except pass`, which stays green but silently drops
health events). The design below reflects that verdict: **the LLM never executes a
mutating command or ships code unattended.**

## North-star signal chain (what we protect)

device poll (dp24/dp102) → visit/elimination log → camera capture → per-cat vision ID
→ health alerts (no-go, named pee/poop) → Telegram. A break anywhere here risks a missed
sick-cat signal. The **raw elimination signal (dp102) sits upstream of the fragile
vision/labeler chain** — so a dying cat can be caught from raw data even if all the fancy
parts are down. That fact drives Component 1.

## Components

### 1. Dead-man's switch (THE cat-safety mechanism — build first)

A **separate, dumb, independent launchd process** — NOT the meowant daemon, NOT touchable
by any remediation logic. No LLM. Reads `meowant.db` directly (a file, no daemon
dependency) and fires Telegram if a validated health signal has gone stale:
- **Global:** no `eliminated` visit (raw dp102-derived, upstream of the labeler) in N
  hours (default 12, honoring quiet hours). This is `d9e` extracted OUT of the daemon
  into an independent process so nothing downstream can silence it.
- **Per-cat:** a cat with an established baseline that has gone silent beyond its norm
  while others are active (the masking gap — global alarm can't catch one cat). Best-effort
  (needs attribution); global is the always-on floor.
- **Daemon liveness:** `meowant.db` / `:8765/state` not advancing → the daemon itself is
  stuck (launchd KeepAlive handles process death; this catches "alive but wedged").

Telegram is the **delivery** path (independent, owner's phone, never muted). The switch is
the **sensor** — it must be its own process so it doesn't share fate with what it watches.
Known caveat: the second (restroom) litter box is unmonitored; if kept, this alarm is
partial — consolidating to the SC10 before travel makes it complete (separate decision).

### 2. Deterministic remediation (known incidents, no LLM)

Extend the existing watchdogs (`capture_health`, `health_watch`) from **notify-only** to
**detect → attempt a hardcoded fix → verify → log → escalate-if-unfixed**. Plain Python,
deterministic, idempotent, rate-limited. Covers only KNOWN incidents:
- **labeler stall** → check agy on PATH + backlog; **diagnose-and-escalate, NOT
  restart** (resolved 2026-06-22, owner-confirmed): the labeler is a thread inside
  meowantd, so `kickstart -k` is process self-suicide that can't verify the fix, and
  restart churn *caused* the 2026-06-22 stall. Process death is covered by launchd
  KeepAlive and wedged-but-alive by the dead-man's liveness probe, so an in-process
  restart adds risk, not coverage. Auto-restart, if ever wanted, belongs only in the
  dead-man's switch's separate process. ~~`launchctl kickstart -k`~~
- **stream down** → re-probe; brief wait; if persistent, escalate.
- **(extend as new known incidents recur)**

Rate-limit hard: max K fix-attempts per incident per window, then escalate (no restart
loops). Every attempt + outcome → the `incidents` table. Remediation NEVER touches the
health-signal *logic* (thresholds, classification, alert code) — those escalate.

### 3. Novel-incident diagnosis (thin `claude -p` one-shot, proposes only)

When a watchdog fires and **no deterministic playbook resolves it**, invoke a single
`claude -p` (or `afk chat`) one-shot with: the incident, current state, and the last ~20
rows from the `incidents` table. It returns a **diagnosis + a proposed action or code
diff**. The proposal is posted to Telegram with an **approve/reject button** (reusing the
tap-to-label callback infra). On approve → the proposed action runs / the diff is applied
(tests must pass) + redeployed. **The one-shot executes NOTHING itself** — no action
surface, so no gate/sandbox machinery is needed. This is the only place an LLM is used.

### 4. `incidents` SQLite table (memory / runbook / audit)

New table in `meowant.db` (same `store.py` lock pattern):
`id, ts, kind, signal (json), action_taken, outcome (fixed|failed|escalated|proposed),
notes`. Serves: audit trail, the runbook ("has this happened, did the fix work"), warm
context for the Component-3 one-shot, and a `GROUP BY kind` "how things are going" rollup.
Small, structured, single-domain — SQLite is the right fit; no external memory daemon.

### 5. Invariant canary

A periodic check that **raw elimination events ≈ attributed/labeled events** over a
rolling window. Divergence (the labeler silently dropping or mis-attributing) → fire. This
is the guard against a "fixed-away" bypass that passes unit tests — a property tests can't
fake because it runs against live data.

## Safety principles (from the council verdict)

1. **No autonomous LLM action or code ship.** LLM proposes; human taps to approve.
2. **The dead-man's switch is independent and un-healable** — separate process, raw
   signal, the remediation layer cannot modify or silence it.
3. **Deterministic remediation is hardcoded + rate-limited** and never edits health-signal
   logic.
4. **Fail loud.** Anything the playbook can't resolve escalates; unknown ≠ silence.
5. **Separate channels.** The dead-man's switch uses a life-critical Telegram channel kept
   free of chatty auto-fix reports, so it's never muted into the noise.

## Error handling

Every component fails toward LOUD: a remediation that errors → escalate; a one-shot that
errors/times out → escalate with the raw incident; the dead-man's switch wrapping any
exception → still fire (better a false alarm than silence). The dead-man's switch has the
fewest dependencies by design (one file read + one Telegram POST).

## Testing

- Dead-man's switch: injected `now`/last-signal times → fires/doesn't at the boundary;
  fires on its own exception (fail-loud).
- Remediation: fake grabber/labeler/exec → attempts the right fix, verifies, escalates on
  failure, respects the rate limit.
- incidents store: insert/query/rollup.
- Invariant canary: synthetic raw-vs-labeled divergence fires.
- One-shot: mocked LLM → proposal posted, nothing executed; approve path runs the action.

## Build order

1. **Component 1 (dead-man's switch)** — the actual cat-safety net. Highest value, simplest.
2. **Component 4 (incidents table)** + **Component 2 (deterministic remediation)**.
3. **Component 5 (invariant canary)**.
4. **Component 3 (one-shot diagnosis + tap-approve)** — last; least safety-critical.

## Explicitly out of scope (YAGNI / rejected by council)

- Autonomous LLM command execution or unattended code deploys.
- agent-afk as a supervisor harness (its gate/loop machinery only earns its keep with
  autonomous action, which we removed).
- Reverie / external memory daemon for the healer (SQLite fits; Reverie can distill from
  the incidents table later if a narrative view is wanted).
