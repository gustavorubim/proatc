import { describe, expect, it } from 'vitest'
import { mayReveal } from './reveal'

describe('reveal', () => {
  it('shows only the flight logs and problem reports the engine writes', () => {
    expect(mayReveal('C:\\Users\\pilot\\AppData\\Roaming\\Pro ATC\\engine\\logs\\flight-20261117-120000.txt')).toBe(
      true
    )
    expect(
      mayReveal('C:\\Users\\pilot\\AppData\\Roaming\\Pro ATC\\engine\\logs\\reports\\proatc-report-20261117-120000.zip')
    ).toBe(true)
    expect(mayReveal('/home/pilot/.config/Pro ATC/engine/logs/reports/proatc-report-20261117-120000.zip')).toBe(true)
    expect(mayReveal('C:\\Users\\pilot\\AppData\\Roaming\\Pro ATC\\engine\\.env')).toBe(false)
    expect(mayReveal('/home/pilot/.config/Pro ATC/engine/logs/reports/other.zip')).toBe(false)
    expect(mayReveal('/home/pilot/.config/Pro ATC/engine/settings.json')).toBe(false)
    expect(mayReveal(42)).toBe(false)
  })
})
