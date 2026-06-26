<script lang="ts">
  // Phase 1: Clean is wired; Feed buttons are disabled until Phase 2.
  let cleaning = false
  let toast: { msg: string; kind: 'ok' | 'err' } | null = null
  let toastTimer: ReturnType<typeof setTimeout> | null = null

  function showToast(msg: string, kind: 'ok' | 'err') {
    if (toastTimer) clearTimeout(toastTimer)
    toast = { msg, kind }
    toastTimer = setTimeout(() => { toast = null }, 3000)
  }

  async function handleClean() {
    if (cleaning) return
    cleaning = true
    try {
      const r = await fetch('/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'clean' }),
      })
      const data: unknown = await r.json()
      if (r.ok && typeof data === 'object' && data !== null && 'ok' in data && (data as Record<string, unknown>).ok) {
        showToast('Clean cycle started ✓', 'ok')
      } else {
        const err = typeof data === 'object' && data !== null && 'error' in data
          ? String((data as Record<string, unknown>).error)
          : 'Command failed'
        showToast(err, 'err')
      }
    } catch {
      showToast('Connection error', 'err')
    } finally {
      cleaning = false
    }
  }
</script>

<!-- Toast notification -->
{#if toast}
  <div
    class="fixed bottom-20 left-1/2 -translate-x-1/2 z-50 rounded-[10px] px-4 py-2 text-[12px] font-bold text-ink"
    style="
      background: {toast.kind === 'ok' ? '#00b8a9' : '#ff4757'};
      color: {toast.kind === 'ok' ? '#fff' : '#fff'};
      border: 2.5px solid #111;
      box-shadow: 3px 3px 0 #111;
    "
    role="status"
    aria-live="polite"
  >
    {toast.msg}
  </div>
{/if}

<!-- Memphis control bar: four buttons -->
<div class="flex gap-[7px]">

  <!-- Clean — wired to POST /command {action:'clean'} -->
  <button
    type="button"
    class="flex-1 font-extrabold text-[11px] text-white rounded-[10px] py-[9px] px-1 min-h-[40px]
           disabled:opacity-60 disabled:cursor-not-allowed focus-visible:outline-2 focus-visible:outline-sys"
    style="background:#ff4757; border: 2.5px solid #111; box-shadow: 2px 2px 0 #111;"
    disabled={cleaning}
    aria-label="Trigger litter box clean cycle"
    onclick={handleClean}
  >
    {cleaning ? '⟳ …' : '⟳ Clean'}
  </button>

  <!-- Feed ↑ — disabled, Phase 2 -->
  <button
    type="button"
    class="flex-1 font-extrabold text-[11px] text-white rounded-[10px] py-[9px] px-1 min-h-[40px]
           opacity-50 cursor-not-allowed focus-visible:outline-2 focus-visible:outline-sys"
    style="background:#00b8a9; border: 2.5px solid #111; box-shadow: 2px 2px 0 #111;"
    disabled
    title="Feed control coming in Phase 2"
    aria-label="Increase feed amount (coming in Phase 2)"
  >
    🍽 Feed ↑
  </button>

  <!-- Feed ↓ — disabled, Phase 2 -->
  <button
    type="button"
    class="flex-1 font-extrabold text-[11px] text-white rounded-[10px] py-[9px] px-1 min-h-[40px]
           opacity-50 cursor-not-allowed focus-visible:outline-2 focus-visible:outline-sys"
    style="background:#00b8a9; border: 2.5px solid #111; box-shadow: 2px 2px 0 #111;"
    disabled
    title="Feed control coming in Phase 2"
    aria-label="Decrease feed amount (coming in Phase 2)"
  >
    🍽 Feed ↓
  </button>

  <!-- Settings — stub for Phase 2 -->
  <button
    type="button"
    class="flex-none w-11 font-extrabold text-[13px] text-ink bg-white rounded-[10px] py-[9px] min-h-[40px]
           focus-visible:outline-2 focus-visible:outline-sys"
    style="border: 2.5px solid #111; box-shadow: 2px 2px 0 #111;"
    aria-label="Settings (coming soon)"
    onclick={() => {/* settings stub */}}
  >
    ⚙
  </button>

</div>
