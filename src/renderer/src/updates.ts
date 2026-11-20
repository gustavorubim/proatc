// How the update state reads in the header.

import type { UpdateState } from '../../shared/protocol'

export interface UpdateView {
  label: string
  tone: 'ok' | 'wait' | 'bad' | 'quiet'
  /** What a click does: look for an update, restart into one, or nothing while busy. */
  action: 'check' | 'install' | 'open' | null
  hint: string
}

export function updateView(update: UpdateState, version: string): UpdateView {
  const current = version ? `v${version}` : 'Pro ATC'
  switch (update.state) {
    case 'checking':
      return { label: 'Checking for updates…', tone: 'wait', action: null, hint: current }
    case 'downloading':
      return {
        label: `Downloading v${update.version}${update.percent ? ` ${update.percent}%` : '…'}`,
        tone: 'wait',
        action: null,
        hint: 'The new version downloads in the background. You can keep flying.'
      }
    case 'ready':
      return {
        label: `Restart to update to v${update.version}`,
        tone: 'ok',
        action: 'install',
        hint: 'Closes Pro ATC, installs the new version and starts it again. Nothing is installed until you click.'
      }
    case 'available':
      return {
        label: `Get v${update.version}`,
        tone: 'ok',
        action: 'open',
        hint: 'A new version is out. This opens the download page: on this system it is installed by hand.'
      }
    case 'error':
      return { label: 'Update failed, try again', tone: 'bad', action: 'check', hint: update.message ?? '' }
    default:
      return {
        label: current,
        tone: 'quiet',
        action: 'check',
        hint: update.message ?? 'Click to check for a new version'
      }
  }
}
