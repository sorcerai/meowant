<script lang="ts">
  import type { Cat } from '../lib/api'
  import { relativeTime, statusColor } from '../lib/format'

  export let cat: Cat

  function fmtTime(iso: string): string {
    return new Date(iso).toLocaleTimeString('en-US', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  }

  const pill: Record<'ok' | 'watch' | 'alert', { label: string; bg: string; color: string }> = {
    ok:    { label: '● OK',    bg: '#00b8a9', color: '#fff' },
    watch: { label: '▲ WATCH', bg: '#ffd32a', color: '#111' },
    alert: { label: '⚠ ALERT', bg: '#ff4757', color: '#fff' },
  }
</script>

<!-- Memphis cat card: white bg, hard 2.5px border, shadow color by status -->
<button
  type="button"
  class="w-full text-left bg-white rounded-[14px] p-3 focus-visible:outline-2 focus-visible:outline-sys"
  style="border: 2.5px solid #111; box-shadow: 3px 3px 0 {statusColor(cat.status)};"
  aria-label="View details for {cat.name}"
  onclick={() => {/* detail view — Phase 2 */}}
>
  <!-- Name + status pill -->
  <div class="flex justify-between items-center">
    <span class="font-black text-[15px] text-ink">🐈 {cat.name.toUpperCase()}</span>
    <span
      class="text-[10px] font-extrabold px-[10px] py-[3px] rounded-full"
      style="background: {pill[cat.status].bg}; color: {pill[cat.status].color};"
    >{pill[cat.status].label}</span>
  </div>

  <!-- Four metric tiles -->
  <div class="grid grid-cols-4 gap-[6px] mt-[9px]">

    <!-- Litter: count today + last occurrence -->
    <div class="bg-paper rounded-[8px] p-[6px] text-center" style="border: 1.5px solid #111;">
      <div class="text-[8px] font-extrabold" style="color:#8a7a55;">LITTER</div>
      <div class="text-[14px] font-black text-ink leading-tight">{cat.litter_count_today}×</div>
      <div class="text-[8px]" style="color:#777;">{relativeTime(cat.last_litter_ts)}</div>
    </div>

    <!-- Ate: last time + duration + location -->
    <div class="bg-paper rounded-[8px] p-[6px] text-center" style="border: 1.5px solid #111;">
      <div class="text-[8px] font-extrabold" style="color:#8a7a55;">ATE</div>
      {#if cat.last_ate}
        <div class="text-[13px] font-black text-ink leading-tight">{fmtTime(cat.last_ate.ts)}</div>
        <div class="text-[8px]" style="color:#777;">{cat.last_ate.duration_s}s</div>
      {:else}
        <div class="text-[14px] font-black text-ink leading-tight">—</div>
        <div class="text-[8px]" style="color:#777;">no data</div>
      {/if}
    </div>

    <!-- Output: Phase 1 placeholder (weight trend lands later) -->
    <div class="bg-paper rounded-[8px] p-[6px] text-center" style="border: 1.5px solid #111;">
      <div class="text-[8px] font-extrabold" style="color:#8a7a55;">OUTPUT</div>
      <div class="text-[14px] font-black leading-tight" style="color:#2ecc71;">~</div>
      <div class="text-[8px]" style="color:#777;">nominal</div>
    </div>

    <!-- Scatter: Phase 1 placeholder -->
    <div class="bg-paper rounded-[8px] p-[6px] text-center" style="border: 1.5px solid #111;">
      <div class="text-[8px] font-extrabold" style="color:#8a7a55;">SCATTER</div>
      <div class="text-[14px] font-black text-ink leading-tight">—</div>
      <div class="text-[8px]" style="color:#777;">soon</div>
    </div>

  </div>
</button>
