import { describe, it, expect } from 'vitest'
import { relativeTime, statusColor, cleansLeftLabel } from './format'

describe('format', () => {
  it('relativeTime handles null', () => { expect(relativeTime(null)).toBe('—') })
  it('statusColor maps statuses', () => {
    expect(statusColor('ok')).toContain('00b8a9')
    expect(statusColor('alert')).toContain('ff4757')
  })
  it('cleansLeftLabel', () => {
    expect(cleansLeftLabel(3, 9)).toBe('~3 left')
    expect(cleansLeftLabel(null, null)).toBe('—')
  })
})
