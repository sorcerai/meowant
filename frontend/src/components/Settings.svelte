<script lang="ts">
  import { onMount } from 'svelte'
  import { getConfig, saveConfig } from '../lib/api'
  import type { SafeConfig } from '../lib/api'

  export let onClose: () => void

  // ── Load state ──────────────────────────────────────────────
  let loading = true
  let loadError = ''
  let orig: SafeConfig | null = null

  // ── Quiet hours ─────────────────────────────────────────────
  let quietStart = '22:00'
  let quietEnd = '08:00'

  // ── Smart-clean ──────────────────────────────────────────────
  let scEnabled = true
  let scIdle = 300

  // ── Per-cat thresholds ───────────────────────────────────────
  // Flat list so #each + bind:value works without object-key reactivity issues
  let thresholdList: { cat: string; val: number; err: string }[] = []

  // ── Feeders ──────────────────────────────────────────────────
  // mealtimes stored as newline-separated string for easy textarea editing
  let feederList: { label: string; mealtimes: string; err: string }[] = []

  // ── Save state ───────────────────────────────────────────────
  let saving = false
  let saved = false
  let saveError = ''

  // ── Validation ───────────────────────────────────────────────
  let quietStartErr = ''
  let quietEndErr = ''
  let scIdleErr = ''

  function isValidTime(t: string): boolean {
    if (!/^\d{2}:\d{2}$/.test(t)) return false
    const [h, m] = t.split(':').map(Number)
    return h >= 0 && h <= 23 && m >= 0 && m <= 59
  }

  function parseMealtimes(raw: string): string[] {
    return raw
      .split(/[\n,]+/)
      .map(s => s.trim())
      .filter(Boolean)
  }

  // Live validation: check all fields and update error strings
  function runValidation() {
    quietStartErr = isValidTime(quietStart) ? '' : 'Must be HH:MM (00:00–23:59)'
    quietEndErr = isValidTime(quietEnd) ? '' : 'Must be HH:MM (00:00–23:59)'
    scIdleErr = Number.isFinite(scIdle) && scIdle >= 10 && scIdle <= 3600
      ? ''
      : 'Must be 10–3600 seconds'

    thresholdList = thresholdList.map(t => ({
      ...t,
      err: Number.isFinite(t.val) && t.val >= 1 && t.val <= 168
        ? ''
        : 'Must be 1–168 hours',
    }))

    feederList = feederList.map(f => {
      const times = parseMealtimes(f.mealtimes)
      const bad = times.filter(t => !isValidTime(t))
      return { ...f, err: bad.length ? `Invalid times: ${bad.join(', ')}` : '' }
    })
  }

  $: isValid =
    !quietStartErr &&
    !quietEndErr &&
    !scIdleErr &&
    thresholdList.every(t => !t.err) &&
    feederList.every(f => !f.err)

  // ── Load ─────────────────────────────────────────────────────
  onMount(async () => {
    try {
      orig = await getConfig()
      quietStart = orig.quiet_start
      quietEnd = orig.quiet_end
      scEnabled = orig.smartclean.enabled
      scIdle = orig.smartclean.idle_seconds
      thresholdList = Object.entries(orig.thresholds).map(([cat, val]) => ({
        cat,
        val,
        err: '',
      }))
      feederList = orig.feeders.map(f => ({
        label: f.label,
        mealtimes: f.mealtimes.join('\n'),
        err: '',
      }))
    } catch {
      loadError = 'Could not load config — check connection.'
    } finally {
      loading = false
    }
  })

  // ── Save ─────────────────────────────────────────────────────
  async function handleSave() {
    runValidation()
    if (!isValid || !orig) return

    saving = true
    saveError = ''

    // Build partial with only changed groups
    const partial: Partial<SafeConfig> = {}

    if (quietStart !== orig.quiet_start || quietEnd !== orig.quiet_end) {
      partial.quiet_start = quietStart
      partial.quiet_end = quietEnd
    }

    if (scEnabled !== orig.smartclean.enabled || scIdle !== orig.smartclean.idle_seconds) {
      partial.smartclean = { enabled: scEnabled, idle_seconds: scIdle }
    }

    const threshChanged = thresholdList.some(t => orig!.thresholds[t.cat] !== t.val)
    if (threshChanged) {
      partial.thresholds = Object.fromEntries(thresholdList.map(t => [t.cat, t.val]))
    }

    const feedersChanged = feederList.some(f => {
      const origFeeder = orig!.feeders.find(o => o.label === f.label)
      return origFeeder && origFeeder.mealtimes.join('\n') !== f.mealtimes
    })
    if (feedersChanged) {
      partial.feeders = feederList.map(f => ({
        label: f.label,
        mealtimes: parseMealtimes(f.mealtimes),
      }))
    }

    try {
      const res = await saveConfig(partial)
      if (res.ok) {
        saved = true
        setTimeout(() => onClose(), 3000)
      } else {
        saveError = res.error ?? 'Save failed — check your inputs'
      }
    } catch {
      saveError = 'Connection error — could not reach server'
    } finally {
      saving = false
    }
  }

  function handleBackdropClick(e: MouseEvent) {
    if (e.target === e.currentTarget) onClose()
  }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="fixed inset-0 flex items-start justify-center px-3 py-5 overflow-y-auto"
  style="background: rgba(0,0,0,0.55); z-index: 100;"
  onclick={handleBackdropClick}
>
  <!-- Memphis paper card -->
  <div
    class="relative w-full flex flex-col"
    style="
      max-width: 420px;
      background: #fdf3e0;
      border: 2.5px solid #111;
      box-shadow: 5px 5px 0 #111;
      border-radius: 16px;
    "
    role="dialog"
    aria-modal="true"
    aria-label="Settings"
  >
    <!-- ── Header ── -->
    <div
      class="flex items-center justify-between px-4 pt-4 pb-3 sticky top-0"
      style="background: #fdf3e0; border-bottom: 2px solid #111; border-radius: 14px 14px 0 0; z-index: 1;"
    >
      <span class="font-black text-[18px]" style="color: #111;">⚙ SETTINGS</span>
      <button
        type="button"
        onclick={onClose}
        aria-label="Close settings"
        class="flex items-center justify-center font-black text-[18px] rounded-full"
        style="
          min-width: 40px; min-height: 40px;
          background: #111; color: #fdf3e0;
          border: none; cursor: pointer;
          line-height: 1;
        "
      >✕</button>
    </div>

    <!-- ── Body ── -->
    <div class="px-4 py-4 flex flex-col gap-4">

      {#if loading}
        <div class="text-center py-8 font-bold" style="color: #8a7a55;">Loading config…</div>

      {:else if loadError}
        <div
          class="text-center py-6 font-bold rounded-[10px]"
          style="color: #ff4757; border: 1.5px solid #ff4757; background: #fff5f5;"
        >{loadError}</div>

      {:else}

        <!-- ── QUIET HOURS ── -->
        <div
          class="rounded-[12px] px-4 py-3 flex flex-col gap-3"
          style="background: #fff; border: 2.5px solid #111; box-shadow: 3px 3px 0 #3742fa;"
        >
          <div class="text-[10px] font-extrabold tracking-[2px]" style="color: #3742fa;">QUIET HOURS</div>
          <div class="text-[11px]" style="color: #777;">
            No alerts or auto-feeds during quiet hours.
          </div>

          <div class="flex gap-3">
            <!-- Quiet Start -->
            <div class="flex-1 flex flex-col gap-1">
              <label class="text-[11px] font-extrabold" style="color: #111;" for="qs">Start</label>
              <input
                id="qs"
                type="text"
                placeholder="22:00"
                bind:value={quietStart}
                oninput={runValidation}
                class="rounded-[8px] px-3 py-2 text-[13px] font-bold w-full"
                style="
                  border: 2px solid {quietStartErr ? '#ff4757' : '#111'};
                  background: #fdf3e0;
                  color: #111;
                  outline: none;
                "
                aria-describedby={quietStartErr ? 'qs-err' : undefined}
              />
              {#if quietStartErr}
                <span id="qs-err" class="text-[10px] font-bold" style="color: #ff4757;">{quietStartErr}</span>
              {/if}
            </div>

            <!-- Quiet End -->
            <div class="flex-1 flex flex-col gap-1">
              <label class="text-[11px] font-extrabold" style="color: #111;" for="qe">End</label>
              <input
                id="qe"
                type="text"
                placeholder="08:00"
                bind:value={quietEnd}
                oninput={runValidation}
                class="rounded-[8px] px-3 py-2 text-[13px] font-bold w-full"
                style="
                  border: 2px solid {quietEndErr ? '#ff4757' : '#111'};
                  background: #fdf3e0;
                  color: #111;
                  outline: none;
                "
                aria-describedby={quietEndErr ? 'qe-err' : undefined}
              />
              {#if quietEndErr}
                <span id="qe-err" class="text-[10px] font-bold" style="color: #ff4757;">{quietEndErr}</span>
              {/if}
            </div>
          </div>
        </div>

        <!-- ── SMART-CLEAN ── -->
        <div
          class="rounded-[12px] px-4 py-3 flex flex-col gap-3"
          style="background: #fff; border: 2.5px solid #111; box-shadow: 3px 3px 0 #00b8a9;"
        >
          <div class="text-[10px] font-extrabold tracking-[2px]" style="color: #00b8a9;">SMART-CLEAN</div>

          <!-- Enabled toggle -->
          <label class="flex items-center gap-3 cursor-pointer" for="sc-enabled">
            <div
              class="relative flex-none"
              style="width: 40px; height: 22px;"
            >
              <input
                id="sc-enabled"
                type="checkbox"
                bind:checked={scEnabled}
                class="sr-only"
                aria-label="Enable smart-clean"
              />
              <!-- svelte-ignore a11y_click_events_have_key_events -->
              <!-- svelte-ignore a11y_no_static_element_interactions -->
              <div
                class="absolute inset-0 rounded-full transition-colors"
                style="
                  background: {scEnabled ? '#00b8a9' : '#ccc'};
                  border: 2px solid #111;
                  cursor: pointer;
                "
                onclick={() => { scEnabled = !scEnabled }}
              >
                <div
                  class="absolute top-[2px] w-[14px] h-[14px] rounded-full transition-transform"
                  style="
                    background: #fff;
                    border: 1.5px solid #111;
                    transform: translateX({scEnabled ? '18px' : '2px'});
                  "
                ></div>
              </div>
            </div>
            <span class="text-[13px] font-extrabold" style="color: #111;">
              {scEnabled ? 'Enabled' : 'Disabled'}
            </span>
          </label>

          <!-- Idle seconds -->
          {#if scEnabled}
            <div class="flex flex-col gap-1">
              <label class="text-[11px] font-extrabold" style="color: #111;" for="sc-idle">
                Idle seconds before clean
              </label>
              <input
                id="sc-idle"
                type="number"
                min="10"
                max="3600"
                bind:value={scIdle}
                oninput={runValidation}
                class="rounded-[8px] px-3 py-2 text-[13px] font-bold"
                style="
                  border: 2px solid {scIdleErr ? '#ff4757' : '#111'};
                  background: #fdf3e0;
                  color: #111;
                  outline: none;
                  width: 120px;
                "
                aria-describedby={scIdleErr ? 'sc-idle-err' : undefined}
              />
              {#if scIdleErr}
                <span id="sc-idle-err" class="text-[10px] font-bold" style="color: #ff4757;">{scIdleErr}</span>
              {:else}
                <span class="text-[10px]" style="color: #8a7a55;">10–3600 s (e.g. 300 = 5 min)</span>
              {/if}
            </div>
          {/if}
        </div>

        <!-- ── PER-CAT THRESHOLDS ── -->
        {#if thresholdList.length > 0}
          <div
            class="rounded-[12px] px-4 py-3 flex flex-col gap-3"
            style="background: #fff; border: 2.5px solid #111; box-shadow: 3px 3px 0 #ffd32a;"
          >
            <div class="text-[10px] font-extrabold tracking-[2px]" style="color: #8a7a55;">PER-CAT ALERT THRESHOLDS</div>
            <div class="text-[11px]" style="color: #777;">Hours since last litter visit before alerting.</div>

            <div class="flex flex-col gap-3">
              {#each thresholdList as item, i}
                <div class="flex flex-col gap-1">
                  <label
                    class="text-[11px] font-extrabold"
                    style="color: #111;"
                    for="thresh-{item.cat}"
                  >{item.cat}</label>
                  <input
                    id="thresh-{item.cat}"
                    type="number"
                    min="1"
                    max="168"
                    bind:value={thresholdList[i].val}
                    oninput={runValidation}
                    class="rounded-[8px] px-3 py-2 text-[13px] font-bold"
                    style="
                      border: 2px solid {item.err ? '#ff4757' : '#111'};
                      background: #fdf3e0;
                      color: #111;
                      outline: none;
                      width: 100px;
                    "
                    aria-describedby={item.err ? `thresh-err-${item.cat}` : undefined}
                  />
                  {#if item.err}
                    <span id="thresh-err-{item.cat}" class="text-[10px] font-bold" style="color: #ff4757;">{item.err}</span>
                  {:else}
                    <span class="text-[10px]" style="color: #8a7a55;">1–168 hours</span>
                  {/if}
                </div>
              {/each}
            </div>
          </div>
        {/if}

        <!-- ── FEEDERS ── -->
        {#if feederList.length > 0}
          <div
            class="rounded-[12px] px-4 py-3 flex flex-col gap-4"
            style="background: #fff; border: 2.5px solid #111; box-shadow: 3px 3px 0 #ff4757;"
          >
            <div class="text-[10px] font-extrabold tracking-[2px]" style="color: #ff4757;">FEEDERS</div>
            <div class="text-[11px]" style="color: #777;">
              Scheduled mealtimes per feeder. Enter HH:MM values, one per line (or comma-separated).
            </div>

            {#each feederList as item, i}
              <div class="flex flex-col gap-1">
                <label
                  class="text-[11px] font-extrabold"
                  style="color: #111;"
                  for="feeder-{item.label}"
                >{item.label}</label>
                <textarea
                  id="feeder-{item.label}"
                  rows="3"
                  placeholder={"07:00\n12:00\n18:00"}
                  bind:value={feederList[i].mealtimes}
                  oninput={runValidation}
                  class="rounded-[8px] px-3 py-2 text-[13px] font-mono resize-y"
                  style="
                    border: 2px solid {item.err ? '#ff4757' : '#111'};
                    background: #fdf3e0;
                    color: #111;
                    outline: none;
                    width: 100%;
                    box-sizing: border-box;
                    min-height: 72px;
                    font-family: monospace;
                  "
                  aria-describedby={item.err ? `feeder-err-${item.label}` : undefined}
                ></textarea>
                {#if item.err}
                  <span id="feeder-err-{item.label}" class="text-[10px] font-bold" style="color: #ff4757;">{item.err}</span>
                {/if}
              </div>
            {/each}
          </div>
        {/if}

        <!-- ── Save error ── -->
        {#if saveError}
          <div
            class="rounded-[10px] px-3 py-2 text-[12px] font-bold text-center"
            style="color: #ff4757; border: 1.5px solid #ff4757; background: #fff5f5;"
          >{saveError}</div>
        {/if}

        <!-- ── Success state ── -->
        {#if saved}
          <div
            class="rounded-[10px] px-3 py-3 text-center"
            style="background: #00b8a9; border: 2.5px solid #111; box-shadow: 3px 3px 0 #111;"
          >
            <div class="text-[14px] font-black" style="color: #fff;">✅ Saved — reloading daemon…</div>
            <div class="text-[11px] font-bold mt-1" style="color: rgba(255,255,255,0.85);">
              Live data will blip for ~3 s while the daemon restarts.
            </div>
          </div>
        {/if}

        <!-- ── Save button ── -->
        {#if !saved}
          <button
            type="button"
            onclick={handleSave}
            disabled={saving || !isValid}
            class="w-full font-extrabold text-[13px] rounded-[10px] py-[11px] min-h-[44px]
                   disabled:opacity-50 disabled:cursor-not-allowed"
            style="
              background: {isValid && !saving ? '#3742fa' : '#888'};
              color: #fff;
              border: 2.5px solid #111;
              box-shadow: 3px 3px 0 #111;
              cursor: {isValid && !saving ? 'pointer' : 'not-allowed'};
            "
          >
            {saving ? '⟳ Saving…' : 'Save Settings'}
          </button>
        {/if}

      {/if}
    </div>
  </div>
</div>
