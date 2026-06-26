export type Cat = {
  name: string
  status: 'ok' | 'watch' | 'alert'
  last_litter_ts: string | null
  hours_since: number | null
  threshold_h: number
  litter_count_today: number
  attribution_uncertain?: boolean
  last_ate: { ts: string; location: string; duration_s: number } | null
}

export type BoxHealth = {
  bin_full_since: string | null
  capacity: number | null
  cleans_since_empty: number | null
  est_cleans_left: number | null
  auto_clean: boolean
  faults: string[]
}

export type Bowl = {
  location: string
  state: string | null
  last_consumption_secs: number | null
  auto_feeds_today: number
}

export type Feeder = {
  label: string
  last_feed_ts: string | null
  today_count: number
}

const j = async <T>(p: string): Promise<T> => {
  const r = await fetch(p)
  if (!r.ok) throw new Error(p)
  return r.json()
}

export type CatDetailT = Cat & {
  timeline: {
    kind: string
    ts: string
    duration_s?: number
    location?: string
    eliminated?: boolean
    confidence?: number
  }[]
  weekly: any
  photos: string[]
}

export const getCatDetail = (name: string) => j<CatDetailT>('/cat/' + encodeURIComponent(name))

export const getCats = () => j<Cat[]>('/cats')
export const getBoxHealth = () => j<BoxHealth>('/boxhealth')
export const getBowls = () => j<Bowl[]>('/bowls')
export const getFeeders = () => j<Feeder[]>('/feeders')
export const getState = () => j<any>('/state')

export const feed = (feeder: string, portions = 1) =>
  fetch('/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'feed', feeder, portions }),
  }).then(r => r.json())

export function subscribeEvents(
  onEvent: (e: any) => void,
  onOpen?: () => void,
  onError?: () => void,
): () => void {
  const es = new EventSource('/events')
  es.onopen = () => onOpen?.()
  es.onmessage = (m) => {
    try { onEvent(JSON.parse(m.data)) } catch (e) { console.warn('SSE parse error', e) }
  }
  es.onerror = () => onError?.()
  return () => es.close()
}
