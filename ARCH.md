# ARCH.md — Meowant SC10 architecture guardrails

> This file is a **construction blueprint for builder agents**: the invariants
> that keep the system on-plan. It is intentionally SHORT (negatives > positives;
> negatives don't rot). Inject this into agent context at session start. When a
> change seems to require violating one, STOP and ask — do not silently improvise.

## What this is (2-line positive anchor)

Local-first control + per-cat identification for a Meowant SC10 litter box. The
daemon owns **one** Tuya device over LAN, turns DPS changes into semantic visits,
photographs + identifies each cat on-Mac. **No cloud dependency in the runtime path.**

## Negative invariants (forbidden — a change that breaks one is drift, not a fix)

1. **No cloud in the runtime path.** The poll loop, capture, labeler, and
   smart-clean must not make outbound network calls to a cloud. Tuya control is
   LAN-only (AES-GCM, `192.168.2.75:6668`). Outbound calls (agy vision, ntfy) are
   *ancillary*, never on the device-control path.
2. **Only the daemon talks to the physical device.** `Daemon` owns the single
   Tuya socket. Capture, labeler, alerts, and the Flask API read device state
   through the daemon's maintained `state` or the `EventBus` — never open a
   second connection to the box.
3. **Attribution reads `captures.label`, NOT `visits.cat_id`.** `visits.cat_id`
   is not synced from the auto-labeler (bug `meowant-6v5`, open). Per-cat blame,
   scatter attribution, and ID must read `captures.label` (the source of truth).
   Do not "clean up" code that reads `captures.label` by switching it to
   `visits.cat_id` — that reintroduces the bug.
4. **One SQLite connection + lock.** All threads share the single
   `check_same_thread=False` connection under its module-level lock. Never open
   a second connection (SQLite + threads + two writers = corruption).
5. **Cheap filter before expensive model.** The torchvision SSDLite cat/no-cat
   filter runs on every frame BEFORE `agy` is called. Do not call `agy` on raw
   frames — it is the expensive teacher; the filter exists to gate it.
6. **Auto-label never clobbers a human label.** `apply_auto_label` is no-clobber:
   a `label_source='human'` (or any non-null human label) is authoritative. Auto
   labels are `label_source='auto'`/`'conflict'` and must not overwrite human
   work.

## When this file is wrong

If a task genuinely requires breaking an invariant, the file is the thing to
update FIRST (with a beads issue + reason), not the code. A silent violation is
the exact drift this file exists to prevent.
