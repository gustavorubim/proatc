// Starts and stops the Python engine (`proatc serve`) that does all the ATC work.
//
// From a source checkout the engine runs through the developer's own uv. The installed app
// carries its own uv and the engine's source and lock file; on first run it builds a Python
// environment in the user's data folder from that lock file, then starts the engine from it.

import { type ChildProcess, spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:net'
import { join } from 'node:path'

export interface EnginePaths {
  packaged: boolean
  uv: string
  projectDir: string
  /** Installed app only: where the key, settings, models and airport index are kept. */
  home?: string
  /** Installed app only: the Python environment built on first run. */
  venv?: string
}

export interface Step {
  command: string
  args: string[]
  /** Shown while the step runs. */
  note?: string
}

// About a gigabyte, and only useful with an NVIDIA card. PROATC_NO_GPU_LIBS skips them (used by CI).
const GPU_PACKAGES = ['nvidia-cublas-cu12', 'nvidia-cudnn-cu12']

export function enginePaths(appPath: string, resourcesPath: string, userData: string, packaged: boolean): EnginePaths {
  if (!packaged) return { packaged, uv: 'uv', projectDir: appPath }
  return {
    packaged,
    uv: join(resourcesPath, 'bin', process.platform === 'win32' ? 'uv.exe' : 'uv'),
    projectDir: join(resourcesPath, 'engine'),
    home: join(userData, 'engine'),
    venv: join(userData, 'engine-venv')
  }
}

/** The commands that bring the engine up, in order. */
export function engineSteps(paths: EnginePaths, port: number, skipGpuLibs = false): Step[] {
  const serve = ['proatc', 'serve', '--port', String(port)]
  if (!paths.packaged) {
    return [{ command: paths.uv, args: ['run', '--project', paths.projectDir, ...serve] }]
  }
  const skip = skipGpuLibs ? GPU_PACKAGES.flatMap((name) => ['--no-install-package', name]) : []
  return [
    {
      command: paths.uv,
      args: ['sync', '--frozen', '--no-dev', '--no-editable', '--project', paths.projectDir, ...skip],
      note: 'Setting up the engine. The first run downloads Python and the speech libraries (about 2 GB).'
    },
    { command: paths.uv, args: ['run', '--no-sync', '--project', paths.projectDir, ...serve] }
  ]
}

/**
 * Where the engine finds the app's own page, to show it to a phone or tablet. In the installed
 * app the page is kept outside the archive (asarUnpack) so that Python can read it.
 */
export function webDir(rendererDir: string): string {
  return rendererDir.replace(/app\.asar([\\/])/, 'app.asar.unpacked$1')
}

export function engineEnv(
  paths: EnginePaths,
  token: string,
  base: NodeJS.ProcessEnv,
  rendererDir?: string
): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...base, PROATC_TOKEN: token, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' }
  if (rendererDir) env.PROATC_WEB_DIR = webDir(rendererDir)
  if (paths.home) env.PROATC_HOME = paths.home
  if (paths.venv) env.UV_PROJECT_ENVIRONMENT = paths.venv
  return env
}

/** A TCP port nothing is listening on. */
export function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : 0
      server.close(() => resolve(port))
    })
  })
}

export interface Engine {
  stop: () => void
}

function kill(child: ChildProcess | null): void {
  if (!child || child.exitCode !== null || child.pid === undefined) return
  if (process.platform === 'win32') {
    // `uv run` is a parent of the real Python process; kill the whole tree.
    spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true })
  } else {
    child.kill('SIGTERM')
  }
}

/**
 * Run the steps one after another. `onExit` is called once, when a step fails or the last
 * one (the engine itself) ends, unless the engine was stopped on purpose.
 */
export function startEngine(
  steps: Step[],
  env: NodeJS.ProcessEnv,
  cwd: string,
  onLine: (line: string) => void,
  onExit: (message: string) => void
): Engine {
  let current: ChildProcess | null = null
  let stopped = false

  const run = (index: number): void => {
    const step = steps[index]
    if (step.note) onLine(step.note)
    const child = spawn(step.command, step.args, { cwd, env, windowsHide: true })
    current = child
    // Output arrives in arbitrary chunks; hand on whole lines only.
    let pending = ''
    const forward = (chunk: Buffer): void => {
      const lines = (pending + chunk.toString('utf8')).split(/\r?\n/)
      pending = lines.pop() ?? ''
      for (const line of lines) {
        if (line.trim()) onLine(line.trim())
      }
    }
    child.stdout?.on('data', forward)
    child.stderr?.on('data', forward)
    child.on('error', (error) => {
      if (!stopped) onExit(`The engine could not be started: ${error.message}`)
    })
    child.on('exit', (code) => {
      if (pending.trim()) onLine(pending.trim())
      if (stopped) return
      const last = index === steps.length - 1
      if (code === 0 && !last) run(index + 1)
      else
        onExit(
          last ? `The engine stopped (exit code ${code ?? 'unknown'}).` : `Engine setup failed (exit code ${code}).`
        )
    })
  }
  run(0)

  return {
    stop: () => {
      stopped = true
      kill(current)
    }
  }
}
