<script lang="ts">
  import type { BoxHealth, Bowl, Feeder } from '../lib/api'
  import { cleansLeftLabel } from '../lib/format'

  export let box: BoxHealth | null
  export let bowls: Bowl[]
  export let feeders: Feeder[]
  export let state: any

  $: boxStatus = (state?.status as string | undefined)?.toUpperCase() ?? '—'

  $: binLabel = cleansLeftLabel(box?.est_cleans_left ?? null, box?.capacity ?? null)
  $: binFull = box?.bin_full_since != null || (box?.est_cleans_left !== null && (box?.est_cleans_left ?? 99) <= 1)

  $: todayFeeds = feeders.reduce((sum, f) => sum + f.today_count, 0)
  $: feederLabel = feeders.length > 0 ? `today: ${todayFeeds}` : '—'

  // Bowl dots: ● green if full/ok, ○ if empty, ◐ otherwise
  $: bowlDots = bowls.length > 0
    ? bowls.map((b) => {
        const s = b.state?.toLowerCase() ?? ''
        if (s === 'full' || s === 'ok') return { dot: '●', color: '#2ecc71' }
        if (s === 'empty') return { dot: '○', color: '#ff4757' }
        return { dot: '◐', color: '#ffd32a' }
      })
    : []
</script>

<!-- Blue Memphis system strip -->
<div
  class="rounded-[14px] px-3 py-[11px]"
  style="background:#3742fa; border: 2.5px solid #111; box-shadow: 3px 3px 0 #111;"
>
  <div class="text-[9px] font-extrabold tracking-[1.5px] mb-[7px]" style="color:#cdd2ff;">SYSTEM</div>

  <div class="grid grid-cols-4 gap-[6px]">

    <!-- Box status -->
    <div class="rounded-[8px] px-[4px] py-[7px] text-center" style="background:rgba(255,255,255,.12);">
      <div class="text-[8px] font-bold" style="color:#cdd2ff;">BOX</div>
      <div class="text-[11px] font-black text-white leading-tight mt-[2px]">{boxStatus}</div>
    </div>

    <!-- Bin capacity -->
    <div class="rounded-[8px] px-[4px] py-[7px] text-center" style="background:rgba(255,255,255,.12);">
      <div class="text-[8px] font-bold" style="color:#cdd2ff;">BIN</div>
      <div
        class="text-[11px] font-black leading-tight mt-[2px]"
        style="color:{binFull ? '#ff4757' : '#ffd32a'};"
      >{binLabel}</div>
    </div>

    <!-- Feeders -->
    <div class="rounded-[8px] px-[4px] py-[7px] text-center" style="background:rgba(255,255,255,.12);">
      <div class="text-[8px] font-bold" style="color:#cdd2ff;">FEEDERS</div>
      <div class="text-[11px] font-black text-white leading-tight mt-[2px]">{feederLabel}</div>
    </div>

    <!-- Bowls -->
    <div class="rounded-[8px] px-[4px] py-[7px] text-center" style="background:rgba(255,255,255,.12);">
      <div class="text-[8px] font-bold" style="color:#cdd2ff;">BOWLS</div>
      <div class="text-[11px] font-black leading-tight mt-[2px]">
        {#if bowlDots.length > 0}
          {#each bowlDots as b}
            <span style="color:{b.color};">{b.dot}</span>
          {/each}
        {:else}
          <span class="text-white">—</span>
        {/if}
      </div>
    </div>

  </div>
</div>
