import { describe, expect, it } from 'vitest'
import { updateView } from './updates'

describe('update pill', () => {
  it('shows the version and offers a check when idle', () => {
    expect(updateView({ state: 'none' }, '0.2.0')).toMatchObject({ label: 'v0.2.0', tone: 'quiet', action: 'check' })
    expect(updateView({ state: 'none', message: 'You have the latest version.' }, '0.2.0').hint).toBe(
      'You have the latest version.'
    )
    expect(updateView({ state: 'none' }, '').label).toBe('Pro ATC')
  })

  it('is not clickable while checking or downloading', () => {
    expect(updateView({ state: 'checking' }, '0.2.0')).toMatchObject({ action: null, tone: 'wait' })
    expect(updateView({ state: 'downloading', version: '0.3.0', percent: 42 }, '0.2.0')).toMatchObject({
      label: 'Downloading v0.3.0 42%',
      action: null
    })
    expect(updateView({ state: 'downloading', version: '0.3.0' }, '0.2.0').label).toBe('Downloading v0.3.0…')
  })

  it('points to the download page where the app cannot replace itself', () => {
    expect(updateView({ state: 'available', version: '0.7.0' }, '0.6.0')).toMatchObject({
      label: 'Get v0.7.0',
      action: 'open',
      tone: 'ok'
    })
  })

  it('offers the restart once the download is done, and a retry after a failure', () => {
    expect(updateView({ state: 'ready', version: '0.3.0' }, '0.2.0')).toMatchObject({
      label: 'Restart to update to v0.3.0',
      action: 'install',
      tone: 'ok'
    })
    expect(updateView({ state: 'error', message: 'The update could not be downloaded.' }, '0.2.0')).toMatchObject({
      action: 'check',
      tone: 'bad',
      hint: 'The update could not be downloaded.'
    })
  })
})
