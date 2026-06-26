<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  // Rename 'state' store to avoid collision with Svelte 5's $state rune
  import { cats, box, bowls, feeders, state as sysState, connected } from './lib/stores'
  import { getCats, getBoxHealth, getBowls, getFeeders, getState, subscribeEvents } from './lib/api'
  import CatCard from './components/CatCard.svelte'
  import AlertBanner from './components/AlertBanner.svelte'
  import SystemStrip from './components/SystemStrip.svelte'
  import ControlBar from './components/ControlBar.svelte'

  $: live = $connected && !$sysState?.stale

  let _interval: ReturnType<typeof setInterval> | undefined
  let _unsubSSE: (() => void) | null = null

  async function pollRefresh() {
    const [c, b, bwl] = await Promise.allSettled([getCats(), getBoxHealth(), getBowls()])
    if (c.status === 'fulfilled') cats.set(c.value)
    if (b.status === 'fulfilled') box.set(b.value)
    if (bwl.status === 'fulfilled') bowls.set(bwl.value)
  }

  onMount(async () => {
    const [c, b, bwl, f, s] = await Promise.allSettled([
      getCats(), getBoxHealth(), getBowls(), getFeeders(), getState(),
    ])
    if (c.status === 'fulfilled') cats.set(c.value)
    if (b.status === 'fulfilled') box.set(b.value)
    if (bwl.status === 'fulfilled') bowls.set(bwl.value)
    if (f.status === 'fulfilled') feeders.set(f.value)
    if (s.status === 'fulfilled') sysState.set(s.value)

    _interval = setInterval(pollRefresh, 30_000)

    _unsubSSE = subscribeEvents(
      async () => {
        const [c2, s2] = await Promise.allSettled([getCats(), getState()])
        if (c2.status === 'fulfilled') cats.set(c2.value)
        if (s2.status === 'fulfilled') sysState.set(s2.value)
      },
      () => { connected.set(true) },
      () => { connected.set(false) },
    )
  })

  onDestroy(() => {
    clearInterval(_interval)
    _unsubSSE?.()
  })
</script>

<!-- Paper background, narrow centered column, phone-first -->
<div class="min-h-screen bg-paper py-5 px-3 flex flex-col items-center">
  <div class="w-full max-w-[420px] relative">

    <!-- Sparse confetti accent shapes (decorative, aria-hidden) -->
    <div aria-hidden="true" class="pointer-events-none select-none">
      <!-- Yellow circle -->
      <div
        class="absolute rounded-full"
        style="top:14px; right:46px; width:14px; height:14px; background:#ffd32a; border:2px solid #111; z-index:1;"
      ></div>
      <!-- Teal triangle -->
      <div
        class="absolute"
        style="top:40px; right:18px; width:0; height:0; border-left:6px solid transparent; border-right:6px solid transparent; border-bottom:11px solid #00b8a9; z-index:1;"
      ></div>
      <!-- Red squiggle -->
      <div
        class="absolute font-black"
        style="top:150px; left:-4px; color:#ff4757; font-size:22px; line-height:1; z-index:1;"
      >~</div>
    </div>

    <!-- ── Header ── -->
    <div class="flex items-center justify-between mb-3">
      <div>
        <div class="font-black text-[22px] text-ink leading-none tracking-[0.5px]">MEOWANT</div>
        <div class="text-[9px] font-extrabold tracking-[2px] text-sys">▢ MISSION CONTROL</div>
      </div>

      <!-- Live indicator pill -->
      <div class="flex items-center gap-[6px] bg-ink rounded-full px-[10px] py-[5px]">
        <span
          class="block w-2 h-2 rounded-full"
          style="
            background: {live ? '#2ecc71' : '#888'};
            box-shadow: 0 0 6px {live ? '#2ecc71' : '#888'};
          "
          aria-hidden="true"
        ></span>
        <span class="text-white text-[10px] font-bold">{live ? 'LIVE' : 'OFFLINE'}</span>
      </div>
    </div>

    <!-- ── Alert banner (conditionally rendered by component) ── -->
    <div class="mb-3">
      <AlertBanner cats={$cats} box={$box} />
    </div>

    <!-- ── Cat cards ── -->
    <div class="flex flex-col gap-[11px] mb-3">
      {#each $cats as cat (cat.name)}
        <CatCard {cat} />
      {/each}

      {#if $cats.length === 0}
        <div
          class="text-center rounded-[14px] py-8 font-bold text-[13px]"
          style="color:#8a7a55; border: 2.5px solid #111; box-shadow: 3px 3px 0 #00b8a9;"
        >
          Connecting to Mission Control…
        </div>
      {/if}
    </div>

    <!-- ── System strip ── -->
    <div class="mb-3">
      <SystemStrip box={$box} bowls={$bowls} feeders={$feeders} state={$sysState} />
    </div>

    <!-- ── Control bar ── -->
    <ControlBar />

  </div>
</div>
