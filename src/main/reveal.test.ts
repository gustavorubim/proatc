import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { logsRoot, mayReveal } from './reveal'

const WIN_ROOT = 'C:\\Users\\pilot\\AppData\\Roaming\\Pro ATC\\engine\\logs'
const POSIX_ROOT = '/home/pilot/.config/Pro ATC/engine/logs'
const win = (target: unknown): boolean => mayReveal(target, WIN_ROOT, path.win32)
const posix = (target: unknown): boolean => mayReveal(target, POSIX_ROOT, path.posix)

describe('reveal', () => {
  it('shows the flight logs and problem reports the engine writes in its logs folder', () => {
    expect(win(`${WIN_ROOT}\\flight-20261117-120000.txt`)).toBe(true)
    expect(win(`${WIN_ROOT}\\reports\\proatc-report-20261117-120000.zip`)).toBe(true)
    expect(posix(`${POSIX_ROOT}/flight-20261117-120000.txt`)).toBe(true)
    expect(posix(`${POSIX_ROOT}/reports/proatc-report-20261117-120000.zip`)).toBe(true)
  })

  it('shows nothing else in the data folder', () => {
    expect(win('C:\\Users\\pilot\\AppData\\Roaming\\Pro ATC\\engine\\.env')).toBe(false)
    expect(win(WIN_ROOT)).toBe(false)
    expect(posix(`${POSIX_ROOT}/reports/other.zip`)).toBe(false)
    expect(posix('/home/pilot/.config/Pro ATC/engine/settings.json')).toBe(false)
    expect(posix(`${POSIX_ROOT}/journal/flight-20261117-120000.txt`)).toBe(false)
    expect(posix(42)).toBe(false)
    expect(posix(undefined)).toBe(false)
  })

  it('refuses a network path, even with the right file name', () => {
    expect(win('\\\\host\\share\\logs\\flight-20260101-000000.txt')).toBe(false)
    expect(win('//host/share/logs/flight-20260101-000000.txt')).toBe(false)
    expect(posix('//host/share/logs/flight-20260101-000000.txt')).toBe(false)
    // Even when the logs folder itself is on a share.
    expect(mayReveal('\\\\host\\share\\logs\\flight-20260101-000000.txt', '\\\\host\\share\\logs', path.win32)).toBe(
      false
    )
  })

  it('refuses the right file name outside the logs folder', () => {
    expect(win('C:\\Temp\\logs\\flight-20260101-000000.txt')).toBe(false)
    expect(win('D:\\Users\\pilot\\AppData\\Roaming\\Pro ATC\\engine\\logs\\flight-20260101-000000.txt')).toBe(false)
    expect(posix('/tmp/logs/flight-20260101-000000.txt')).toBe(false)
    expect(posix('/tmp/logs/reports/proatc-report-20261117-120000.zip')).toBe(false)
  })

  it('refuses a path that climbs out of the logs folder', () => {
    expect(win(`${WIN_ROOT}\\..\\..\\other\\logs\\flight-20260101-000000.txt`)).toBe(false)
    expect(posix(`${POSIX_ROOT}/../../../../tmp/logs/flight-20260101-000000.txt`)).toBe(false)
    expect(posix(`${POSIX_ROOT}/reports/../../x/logs/flight-20260101-000000.txt`)).toBe(false)
    // A ".." that stays inside is fine once resolved.
    expect(posix(`${POSIX_ROOT}/reports/../flight-20260101-000000.txt`)).toBe(true)
  })

  it('finds the logs folder the engine writes to', () => {
    const packaged = { packaged: true, uv: 'uv', projectDir: '/opt/app/engine', home: '/data/engine' }
    const source = { packaged: false, uv: 'uv', projectDir: '/src/proatc' }
    expect(logsRoot(packaged, { PROATC_HOME: '/elsewhere' })).toBe(path.join('/data/engine', 'logs'))
    expect(logsRoot(source, {})).toBe(path.join('/src/proatc', 'logs'))
    expect(logsRoot(source, { PROATC_HOME: '/elsewhere' })).toBe(path.join('/elsewhere', 'logs'))
  })
})
