// Downloads the uv binary that ships inside the installed app and checks it against a known
// checksum, so a replaced upstream file cannot end up in an installer. uv is what builds the
// engine's Python environment on the user's machine. Update VERSION and SHA256 together.

import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const VERSION = '0.13.8'
const ARCHIVE = 'uv-x86_64-pc-windows-msvc.zip'
const SHA256 = '529e78a9cec6cd7e3e014f7ecd8bda0c113969d4ed0f2efeedb85c30943325b8'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const target = join(root, 'vendor', 'uv')
const stamp = join(target, 'VERSION')
const binary = join(target, 'uv.exe')

if (existsSync(binary) && existsSync(stamp) && readFileSync(stamp, 'utf8').trim() === VERSION) {
  console.log(`uv ${VERSION} already present`)
  process.exit(0)
}

const url = `https://github.com/astral-sh/uv/releases/download/${VERSION}/${ARCHIVE}`
console.log(`Downloading ${url}`)
const response = await fetch(url)
if (!response.ok) {
  console.error(`Download failed: HTTP ${response.status}`)
  process.exit(1)
}
const bytes = Buffer.from(await response.arrayBuffer())
const actual = createHash('sha256').update(bytes).digest('hex')
if (actual !== SHA256) {
  console.error(`Checksum mismatch for ${ARCHIVE}: expected ${SHA256}, got ${actual}`)
  process.exit(1)
}

rmSync(target, { recursive: true, force: true })
mkdirSync(target, { recursive: true })
const archive = join(target, ARCHIVE)
writeFileSync(archive, bytes)
// bsdtar ships with Windows 10 and later and reads zip archives. Named in full because the
// tar that comes with Git, first on the PATH in Git Bash, does not.
const tar = process.platform === 'win32' ? join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'tar.exe') : 'tar'
execFileSync(tar, ['-xf', archive, '-C', target])
rmSync(archive)
if (!existsSync(binary)) {
  console.error('uv.exe was not found in the archive')
  process.exit(1)
}
writeFileSync(stamp, `${VERSION}\n`)
console.log(`uv ${VERSION} verified and unpacked to vendor/uv`)
