<script lang="ts">
  import type { Cat, BoxHealth } from '../lib/api'

  export let cats: Cat[]
  export let box: BoxHealth | null

  // Derive the single most-pressing alert message (alert > watch > box issues)
  $: alertCat = cats.find((c) => c.status === 'alert')
  $: watchCat = cats.find((c) => c.status === 'watch')

  $: boxMsg = (() => {
    if (!box) return null
    if (box.bin_full_since) return 'Litter bin is full — needs emptying'
    if (box.est_cleans_left !== null && box.est_cleans_left <= 1)
      return `Bin almost full — ~${box.est_cleans_left} clean left`
    return null
  })()

  $: message = alertCat
    ? `${alertCat.name.toUpperCase()} — needs attention (${alertCat.hours_since ?? '?'}h since litter)`
    : watchCat
    ? `${watchCat.name.toUpperCase()} — ${watchCat.hours_since ?? '?'}h since litter (watch threshold ${watchCat.threshold_h}h)`
    : boxMsg
</script>

{#if message}
  <!-- Yellow Memphis alert banner with hard shadow -->
  <div
    class="flex items-center gap-2 rounded-[12px] px-[11px] py-[9px]"
    style="background:#ffd32a; border: 2.5px solid #111; box-shadow: 3px 3px 0 #111;"
    role="alert"
  >
    <span class="text-[16px]" aria-hidden="true">⚠</span>
    <span class="text-[11.5px] font-extrabold text-ink">{message}</span>
  </div>
{/if}
