export function relativeTime(iso: string | null): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  const mins = Math.round((Date.now() - t) / 60000)
  if (mins < 1) return 'now'
  if (mins < 60) return `${mins}m ago`
  const h = Math.round(mins / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.round(h / 24)}d ago`
}

export function statusColor(s: 'ok' | 'watch' | 'alert'): string {
  return { ok: '#00b8a9', watch: '#ffd32a', alert: '#ff4757' }[s]
}

export function cleansLeftLabel(left: number | null, cap: number | null): string {
  if (left === null || cap === null) return '—'
  return `~${left} left`
}
