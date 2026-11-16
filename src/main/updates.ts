// Checks GitHub releases for a newer version, downloads it in the background, and installs
// it when the user presses "Restart to update".

import { app, shell } from 'electron'
import { autoUpdater } from 'electron-updater'
import type { UpdateState } from '../shared/protocol'

const CHECK_EVERY_MS = 6 * 60 * 60 * 1000
const RELEASES = 'https://github.com/gustavorubim/proatc/releases/latest'

/**
 * Whether this copy can replace itself. The Windows installer can. On macOS that needs an app
 * signed with an Apple developer certificate, which this one is not. On Linux only an AppImage can.
 * Where it cannot, a new version is announced and the download page is opened instead.
 */
export function canSelfUpdate(platform: string, env: NodeJS.ProcessEnv): boolean {
  if (platform === 'win32') return true
  return platform === 'linux' && Boolean(env.APPIMAGE)
}

/** Open the page where the new version can be downloaded by hand. */
export function openReleases(): void {
  void shell.openExternal(RELEASES)
}

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
  if (state.state === 'downloading' || state.state === 'ready' || state.state === 'available') return state
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

  const automatic = canSelfUpdate(process.platform, process.env)
  autoUpdater.autoDownload = automatic
  autoUpdater.autoInstallOnAppQuit = automatic

  autoUpdater.on('update-available', (info) =>
    set(
      automatic
        ? { state: 'downloading', version: info.version, percent: 0 }
        : { state: 'available', version: info.version }
    )
  )
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
