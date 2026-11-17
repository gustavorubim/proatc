// Starts the packaged app in self-test mode and reports the result.
//
// The app builds the engine's Python environment from the lock file with the uv it carries,
// starts the engine, and checks that every dependency loads, the window opens and the engine
// accepts the app's connection. Nothing is read from or written to the real user data folder.
// Speech models are not downloaded and no microphone is needed.
//
// Usage: node scripts/smoke-test.mjs [path to the app's executable]   (default: the unpacked build in dist/)

import { spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const UNPACKED = {
  win32: join('win-unpacked', 'Pro ATC.exe'),
  darwin: join(`mac-${process.arch}`, 'Pro ATC.app', 'Contents', 'MacOS', 'Pro ATC'),
  linux: join('linux-unpacked', 'proatc')
}
const exe = process.argv[2] ?? join(root, 'dist', UNPACKED[process.platform] ?? UNPACKED.win32)
if (!existsSync(exe)) {
  console.error(`Packaged app not found at ${exe}. Run "npm run pack:dir" first.`)
  process.exit(1)
}

const work = mkdtempSync(join(tmpdir(), 'proatc-smoke-'))
const result = join(work, 'result.txt')
console.log(`Self-testing ${exe}\nData folder: ${work}`)
// A Linux test machine has no user namespaces for Chromium's sandbox, and no display unless one is provided.
const args = process.platform === 'linux' ? ['--no-sandbox'] : []
const run = spawnSync(exe, args, {
  env: {
    ...process.env,
    PROATC_SELFTEST: result,
    PROATC_USER_DATA: join(work, 'user-data'),
    // The GPU libraries are about a gigabyte and a test machine has no GPU to use them.
    PROATC_NO_GPU_LIBS: process.env.PROATC_NO_GPU_LIBS ?? '1'
  },
  stdio: 'inherit',
  timeout: 30 * 60 * 1000
})

const report = existsSync(result) ? readFileSync(result, 'utf8') : '(the app wrote no result)'
console.log(report)
if (run.status !== 0 || !report.startsWith('ok')) {
  console.error(`Self-test failed (exit ${run.status}).`)
  process.exit(1)
}
console.log('Self-test passed.')
