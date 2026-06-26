<script lang="ts">
  import { onMount } from 'svelte'
  import type { CatDetailT } from '../lib/api'
  import { getCatDetail } from '../lib/api'
  import { relativeTime, deriveBadge } from '../lib/format'

  export let name: string
  export let onClose: () => void

  let data: CatDetailT | null = null
  let loading = true
  let error = ''

  $: badge = data ? deriveBadge(data.status, data.attribution_uncertain) : null

  onMount(async () => {
    try {
      data = await getCatDetail(name)
    } catch (e) {
      error = 'Could not load details — check connection.'
    } finally {
      loading = false
    }
  })

  function handleBackdropClick(e: MouseEvent) {
    if (e.target === e.currentTarget) onClose()
  }

  function fmtDuration(s: number | undefined): string {
    if (s == null) return ''
    if (s < 60) return `${s}s`
    return `${Math.round(s / 60)}m ${s % 60}s`
  }

</script>

<!-- Full-screen fixed backdrop — click outside card to close -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="fixed inset-0 flex items-center justify-center px-3"
  style="background: rgba(0,0,0,0.55); z-index: 100;"
  onclick={handleBackdropClick}
>
  <!-- Memphis paper card -->
  <div
    class="relative w-full max-h-[88vh] overflow-y-auto flex flex-col"
    style="
      max-width: 420px;
      background: #fdf3e0;
      border: 2.5px solid #111;
      box-shadow: 5px 5px 0 #111;
      border-radius: 16px;
    "
    role="dialog"
    aria-modal="true"
    aria-label="Details for {name}"
  >
    <!-- Header -->
    <div
      class="flex items-center justify-between px-4 pt-4 pb-3 sticky top-0"
      style="background: #fdf3e0; border-bottom: 2px solid #111; border-radius: 14px 14px 0 0;"
    >
      <div class="flex items-center gap-2">
        <span class="font-black text-[18px]" style="color: #111;">🐈 {name.toUpperCase()}</span>
        {#if badge}
          <span
            class="text-[10px] font-extrabold px-[10px] py-[3px] rounded-full"
            style="background: {badge.bg}; color: {badge.color};"
          >{badge.label}</span>
        {/if}
      </div>
      <button
        type="button"
        onclick={onClose}
        aria-label="Close details"
        class="flex items-center justify-center font-black text-[18px] rounded-full"
        style="
          min-width: 40px; min-height: 40px;
          background: #111; color: #fdf3e0;
          border: none; cursor: pointer;
          line-height: 1;
        "
      >✕</button>
    </div>

    <!-- Body -->
    <div class="px-4 py-3 flex flex-col gap-4">

      {#if loading}
        <div class="text-center py-8 font-bold" style="color: #8a7a55;">Loading…</div>

      {:else if error}
        <div
          class="text-center py-6 font-bold rounded-[10px]"
          style="color: #ff4757; border: 1.5px solid #ff4757; background: #fff5f5;"
        >{error}</div>

      {:else if data}

        <!-- ── TIMELINE ── -->
        <div>
          <div
            class="text-[10px] font-extrabold tracking-[2px] mb-2"
            style="color: #8a7a55;"
          >TIMELINE</div>

          {#if data.timeline.length === 0}
            <div class="text-center py-4 font-bold text-[13px]" style="color: #8a7a55;">
              No recent activity recorded.
            </div>

          {:else}
            <div class="flex flex-col gap-[6px]">
              {#each data.timeline as event}
                <div
                  class="flex items-start gap-3 rounded-[10px] px-3 py-2"
                  style="background: #fff; border: 1.5px solid #111;"
                >
                  <!-- Icon -->
                  <span class="text-[18px] leading-none mt-[1px]" aria-hidden="true">
                    {event.kind === 'litter' ? '🚽' : '🍽'}
                  </span>

                  <!-- Details -->
                  <div class="flex-1 min-w-0">
                    <div class="flex items-center justify-between gap-1">
                      <span class="font-extrabold text-[12px]" style="color: #111;">
                        {event.kind === 'litter' ? 'Litter box' : 'Ate'}
                        {#if event.location}
                          <span class="font-normal text-[11px]" style="color: #777;">
                            · {event.location}
                          </span>
                        {/if}
                      </span>
                      <span class="text-[11px] shrink-0" style="color: #8a7a55;">
                        {relativeTime(event.ts)}
                      </span>
                    </div>

                    <!-- Sub-details -->
                    <div class="flex flex-wrap gap-x-3 mt-[2px]">
                      {#if event.duration_s != null}
                        <span class="text-[11px]" style="color: #777;">
                          {fmtDuration(event.duration_s)}
                        </span>
                      {/if}
                      {#if event.kind === 'litter'}
                        {#if event.eliminated === true}
                          <span class="text-[11px]" style="color: #00b8a9; font-weight: 700;">
                            eliminated ✓
                          </span>
                        {:else if event.eliminated === false}
                          <span class="text-[11px]" style="color: #8a7a55;">
                            no elimination
                          </span>
                        {/if}
                        {#if event.confidence != null}
                          <span class="text-[10px]" style="color: #aaa;">
                            conf {Math.round(event.confidence * 100)}%
                          </span>
                        {/if}
                      {/if}
                    </div>
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        </div>

        <!-- ── WEEKLY ── -->
        <div>
          <div
            class="text-[10px] font-extrabold tracking-[2px] mb-2"
            style="color: #8a7a55;"
          >WEEKLY REPORT</div>

          {#if !data.weekly}
            <div
              class="rounded-[10px] px-3 py-3 text-[12px] font-bold text-center"
              style="background: #fff; border: 1.5px solid #111; color: #8a7a55;"
            >
              No weekly report yet.
            </div>
          {:else}
            <div
              class="rounded-[10px] px-3 py-3 flex flex-col gap-[6px]"
              style="background: #fff; border: 1.5px solid #111;"
            >
              <!-- Core stats -->
              <div class="grid grid-cols-2 gap-[6px]">
                <div class="rounded-[8px] p-2 text-center" style="background: #fdf3e0; border: 1.5px solid #111;">
                  <div class="text-[8px] font-extrabold" style="color: #8a7a55;">LITTER VISITS</div>
                  <div class="text-[18px] font-black" style="color: #111;">{data.weekly.voids ?? '—'}</div>
                  <div class="text-[9px]" style="color: #777;">{data.weekly.per_day != null ? data.weekly.per_day + '/day avg' : ''}</div>
                </div>
                <div class="rounded-[8px] p-2 text-center" style="background: #fdf3e0; border: 1.5px solid #111;">
                  <div class="text-[8px] font-extrabold" style="color: #8a7a55;">AVG GAP</div>
                  <div class="text-[18px] font-black" style="color: #111;">
                    {data.weekly.gap_h?.mean != null ? data.weekly.gap_h.mean + 'h' : '—'}
                  </div>
                  <div class="text-[9px]" style="color: #777;">between visits</div>
                </div>
              </div>

              <!-- Weight if present -->
              {#if data.weekly.weight?.mean != null}
                <div class="text-[11px] font-bold pt-1" style="color: #111;">
                  Avg elimination weight:
                  <span style="color: #00b8a9;">{data.weekly.weight.mean}g</span>
                  {#if data.weekly.weight.n}
                    <span class="font-normal" style="color: #777;">({data.weekly.weight.n} samples)</span>
                  {/if}
                </div>
              {/if}

              <!-- Prev week comparison -->
              {#if data.weekly.prev}
                <div class="text-[10px]" style="color: #8a7a55;">
                  Prior week: {data.weekly.prev.voids ?? '—'} visits
                  {#if data.weekly.prev.gap_mean_h != null}
                    · {data.weekly.prev.gap_mean_h}h avg gap
                  {/if}
                </div>
              {/if}
            </div>
          {/if}
        </div>

        <!-- ── PHOTOS ── -->
        <div>
          <div
            class="text-[10px] font-extrabold tracking-[2px] mb-2"
            style="color: #8a7a55;"
          >REFERENCE PHOTOS</div>

          {#if !data.photos || data.photos.length === 0}
            <div
              class="rounded-[10px] px-3 py-3 text-[12px] font-bold text-center"
              style="background: #fff; border: 1.5px solid #111; color: #8a7a55;"
            >
              No reference photos on file.
            </div>
          {:else}
            <div
              class="rounded-[10px] px-3 py-3"
              style="background: #fff; border: 1.5px solid #111;"
            >
              <div class="text-[12px] font-bold mb-2" style="color: #111;">
                {data.photos.length} reference photo{data.photos.length === 1 ? '' : 's'}
              </div>
              <div class="grid grid-cols-3 gap-[6px]">
                {#each data.photos as photo}
                  <img
                    src={photo}
                    alt="reference photo"
                    loading="lazy"
                    class="w-full aspect-square object-cover rounded-[8px]"
                    style="border: 1.5px solid #111;"
                  />
                {/each}
              </div>
            </div>
          {/if}
        </div>

      {/if}
    </div>
  </div>
</div>
