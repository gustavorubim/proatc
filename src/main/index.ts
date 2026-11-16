import { randomBytes } from 'node:crypto'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { app, BrowserWindow, ipcMain, shell } from 'electron'
import type { Connection } from '../shared/protocol'
import { type Engine, engineEnv, enginePaths, engineSteps, freePort, startEngine } from './engine'
import { checkForUpdates, getUpdateState, installUpdate, startUpdates } from './updates'

// Tests point the app at a throwaway data folder so they never touch the real one.
if (process.env.PROATC_USER_DATA) app.setPath('userData', process.env.PROATC_USER_DATA)

let window: BrowserWindow | null = null
let engine: Engine | null = null
let connection: Connection | null = null
let engineFailure: string | null = null
const engineLog: string[] = []
const lineListeners: Array<(line: string) => void> = []

function createWindow(): void {
  window = new BrowserWindow({
    width: 1180,
    height: 780,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#1b1e26',
    title: 'Pro ATC',
    // The installed app takes its icon from the executable; this is for running from source.
    icon: app.isPackaged ? undefined : join(app.getAppPath(), 'build', 'icon.png'),
    autoHideMenuBar: true,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })
  // Links open in the browser, never inside the app window.
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) void shell.openExternal(url)
    return { action: 'deny' }
  })
  window.webContents.on('will-navigate', (event) => event.preventDefault())

  if (process.env.ELECTRON_RENDERER_URL) {
    void window.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    void window.loadFile(join(__dirname, '../renderer/index.html'))
  }
  window.on('closed', () => {
    window = null
  })
}

async function launchEngine(): Promise<void> {
  const port = await freePort()
  const token = randomBytes(24).toString('hex')
  connection = { port, token }
  const paths = enginePaths(app.getAppPath(), process.resourcesPath, app.getPath('userData'), app.isPackaged)
  if (paths.home) mkdirSync(paths.home, { recursive: true })
  engine = startEngine(
    engineSteps(paths, port, Boolean(process.env.PROATC_NO_GPU_LIBS)),
    engineEnv(paths, token, process.env),
    paths.packaged ? app.getPath('userData') : paths.projectDir,
    (line) => {
      engineLog.push(line)
      if (engineLog.length > 500) engineLog.shift()
      window?.webContents.send('engine-log', line)
      for (const listener of lineListeners) listener(line)
    },
    (message) => {
      engineFailure = message
      window?.webContents.send('engine-exit', message)
      for (const listener of lineListeners) listener(message)
    }
  )
}

/** Test hook: PROATC_SHOT=<file.png> saves a picture of the window after a delay, then quits. */
function scheduleScreenshot(): void {
  const target = process.env.PROATC_SHOT
  if (!target) return
  const delay = Number(process.env.PROATC_SHOT_DELAY_MS ?? 30000)
  const label = process.env.PROATC_SHOT_CLICK
  if (label) {
    // Optionally press one of the suggested calls first, so the picture shows a conversation.
    setTimeout(() => {
      const script = `[...document.querySelectorAll('button.call')].find((b) => b.textContent === ${JSON.stringify(label)})?.click()`
      void window?.webContents.executeJavaScript(script)
    }, delay * 0.6)
  }
  setTimeout(async () => {
    try {
      const image = await window?.webContents.capturePage()
      if (image) writeFileSync(target, image.toPNG())
      writeFileSync(`${target}.log`, engineLog.join('\n'))
    } finally {
      app.quit()
    }
  }, delay)
}

/**
 * Packaged-app test: PROATC_SELFTEST=<result file>. The engine is asked to check that every
 * dependency loads (see server.py); this side checks that the window loaded and that the
 * engine accepts a connection with the token. The result file starts with "ok" or "fail".
 */
function runSelfTest(): void {
  const target = process.env.PROATC_SELFTEST
  if (!target) return
  let pageLoaded = false
  window?.webContents.once('did-finish-load', () => {
    pageLoaded = true
  })
  const finish = (ok: boolean, detail: string): void => {
    writeFileSync(target, `${ok ? 'ok' : 'fail'}: ${detail}\n\n${engineLog.join('\n')}\n`)
    app.exit(ok ? 0 : 1)
  }
  const timeout = setTimeout(
    () => finish(false, 'timed out'),
    Number(process.env.PROATC_SELFTEST_TIMEOUT_MS ?? 1500000)
  )
  lineListeners.push((line) => {
    if (engineFailure) {
      clearTimeout(timeout)
      finish(false, engineFailure)
    } else if (line.includes('SELFTEST OK')) {
      // The engine is up. Now the real path: the page, through the preload bridge, must have
      // connected to it with the token. The page marks that on its root element.
      let tries = 0
      const poll = setInterval(async () => {
        tries += 1
        const link = await window?.webContents
          .executeJavaScript(`document.querySelector('.app')?.dataset.link ?? 'no page'`)
          .catch(() => 'page error')
        if (link === 'open' || tries >= 60) {
          clearInterval(poll)
          clearTimeout(timeout)
          if (link === 'open' && pageLoaded) finish(true, line)
          else finish(false, `the window did not connect to the engine (page: ${pageLoaded}, link: ${link})`)
        }
      }, 500)
    }
  })
}

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (window?.isMinimized()) window.restore()
    window?.focus()
  })

  ipcMain.handle('connection', () => connection)
  ipcMain.handle('engine-log', () => ({ lines: engineLog, failure: engineFailure }))
  ipcMain.handle('app-version', () => app.getVersion())
  ipcMain.handle('update-state', () => getUpdateState())
  ipcMain.handle('update-check', () => checkForUpdates(true))
  ipcMain.handle('update-install', () => installUpdate(() => engine?.stop()))

  void app.whenReady().then(async () => {
    createWindow()
    runSelfTest()
    await launchEngine()
    scheduleScreenshot()
    // Tests run offline and must not start a download.
    if (!process.env.PROATC_SELFTEST && !process.env.PROATC_SHOT) {
      startUpdates((state) => window?.webContents.send('update-state', state))
    }
  })

  app.on('before-quit', () => engine?.stop())
  app.on('quit', () => engine?.stop())
  app.on('window-all-closed', () => app.quit())
}
