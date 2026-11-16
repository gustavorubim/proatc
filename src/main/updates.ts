// Checks GitHub releases for a newer version, downloads it in the background, and installs
// it when the user presses "Restart to update".

import { app } from 'electron'
import { autoUpdater } from 'electron-updater'
import type { UpdateState } from '../shared/protocol'

const CHECK_EVERY_MS = 6 * 60 * 60 * 1000

let state: UpdateState = { state: 'none' }
let onChange: (state: UpdateState) => void = () => {}

function set(next: UpdateState): void {
  state = next
  onChange(next)
}

export function getUpdateState(): UpdateState {
  return state
}

/** Looks for a newer release. `manual` is true when the user pressed the button, so failures are shown. */
export async function checkForUpdates(manual = false): Promise<UpdateState> {
  if (!app.isPackaged) {
    set({ state: 'none', message: 'Updates are only checked in the installed app.' })
    return state
  }
  // Don't restart a download that is under way or already finished.
  if (state.state === 'downloading' || state.state === 'ready') return state
  if (manual) set({ state: 'checking' })
  try {
    await autoUpdater.checkForUpdates()
  } catch (error) {
    console.error('update check failed:', error)
    if (manual || state.state === 'checking') {
      set({ state: 'error', message: 'Could not check for updates. Check your internet connection.' })
    }
  }
  return state
}

/** Restart into the downloaded version. `beforeQuit` stops the engine first so its files can be replaced. */
export function installUpdate(beforeQuit: () => void): void {
  if (state.state !== 'ready') return
  beforeQuit()
  autoUpdater.quitAndInstall(true, true)
}

/** Starts background update checks. `listener` is called whenever the update state changes. */
export function startUpdates(listener: (state: UpdateState) => void): void {
  onChange = listener
  if (!app.isPackaged) return

  autoUpdater.autoDownload = true
  autoUpdater.autoInstallOnAppQuit = true

  autoUpdater.on('update-available', (info) => set({ state: 'downloading', version: info.version, percent: 0 }))
  autoUpdater.on('download-progress', (progress) => {
    if (state.state === 'downloading') set({ ...state, percent: Math.round(progress.percent) })
  })
  autoUpdater.on('update-not-available', () => set({ state: 'none', message: 'You have the latest version.' }))
  autoUpdater.on('update-downloaded', (info) => set({ state: 'ready', version: info.version }))
  autoUpdater.on('error', (error) => {
    console.error('updater error:', error)
    // A failed background check stays quiet; a failed download is worth showing.
    if (state.state === 'downloading') {
      set({ state: 'error', version: state.version, message: 'The update could not be downloaded.' })
    }
  })

  void checkForUpdates()
  // A long session should still notice a release.
  setInterval(() => void checkForUpdates(), CHECK_EVERY_MS)
}
