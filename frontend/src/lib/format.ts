export type BadgeStyle = { label: string; bg: string; color: string }

export const pillConfig: Record<'ok' | 'watch' | 'alert', BadgeStyle> = {
  ok:    { label: '● OK',    bg: '#00b8a9', color: '#fff' },
  watch: { label: '▲ WATCH', bg: '#ffd32a', color: '#111' },
  alert: { label: '⚠ ALERT', bg: '#ff4757', color: '#fff' },
}

export function deriveBadge(
  status: 'ok' | 'watch' | 'alert',
  attribution_uncertain?: boolean,
): BadgeStyle {
  if (attribution_uncertain) {
    return { label: "❓ CAN'T CONFIRM", bg: '#efe2b3', color: '#6b5d2f' }
  }
  return pillConfig[status]
}

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
