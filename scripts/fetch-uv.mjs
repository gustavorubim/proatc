// Downloads the uv binary that ships inside the installed app and checks it against a known
// checksum, so a replaced upstream file cannot end up in an installer. uv is what builds the
// engine's Python environment on the user's machine. Update VERSION and every SHA256 together.
//
// Usage: node scripts/fetch-uv.mjs [platform-arch]   (default: the machine this runs on)

import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { chmodSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const VERSION = '0.13.8'
// platform-arch (as Node names them) -> the archive on uv's release page and its checksum.
const ARCHIVES = {
  'win32-x64': {
    name: 'uv-x86_64-pc-windows-msvc.zip',
    sha256: '529e78a9cec6cd7e3e014f7ecd8bda0c113969d4ed0f2efeedb85c30943325b8'
  },
  'darwin-arm64': {
    name: 'uv-aarch64-apple-darwin.tar.gz',
    sha256: '6b71028a6b904f2ae3340ef52e595512de807d06c0a20eaef2f692581b246558'
  },
  'darwin-x64': {
    name: 'uv-x86_64-apple-darwin.tar.gz',
    sha256: 'a04c2ffeac28b2003a2c13caf538ab425fcd7def3e4a41571f4a8fb370a66af3'
  },
  'linux-x64': {
    name: 'uv-x86_64-unknown-linux-gnu.tar.gz',
    sha256: '5acfd9177cfecf0bf87578f059728215ccc884388988f55f7a65041b57387d91'
  }
}

const wanted = process.argv[2] ?? `${process.platform}-${process.arch}`
const archive = ARCHIVES[wanted]
if (!archive) {
  console.error(`No uv build is pinned for ${wanted}. Known: ${Object.keys(ARCHIVES).join(', ')}`)
  process.exit(1)
}
const windows = wanted.startsWith('win32')

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const target = join(root, 'vendor', 'uv')
const stamp = join(target, 'VERSION')
const binary = join(target, windows ? 'uv.exe' : 'uv')
const mark = `${VERSION} ${wanted}`

if (existsSync(binary) && existsSync(stamp) && readFileSync(stamp, 'utf8').trim() === mark) {
  console.log(`uv ${mark} already present`)
  process.exit(0)
}

const url = `https://github.com/astral-sh/uv/releases/download/${VERSION}/${archive.name}`
console.log(`Downloading ${url}`)
const response = await fetch(url)
if (!response.ok) {
  console.error(`Download failed: HTTP ${response.status}`)
  process.exit(1)
}
const bytes = Buffer.from(await response.arrayBuffer())
const actual = createHash('sha256').update(bytes).digest('hex')
if (actual !== archive.sha256) {
  console.error(`Checksum mismatch for ${archive.name}: expected ${archive.sha256}, got ${actual}`)
  process.exit(1)
}

rmSync(target, { recursive: true, force: true })
mkdirSync(target, { recursive: true })
const file = join(target, archive.name)
writeFileSync(file, bytes)
// bsdtar ships with Windows 10 and later and reads zip archives. Named in full because the
// tar that comes with Git, first on the PATH in Git Bash, does not.
const tar = process.platform === 'win32' ? join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'tar.exe') : 'tar'
// The zip holds the binaries at its top level; the tarballs hold them in one folder.
execFileSync(tar, ['-xf', file, '-C', target, ...(windows ? [] : ['--strip-components=1'])])
rmSync(file)
if (!existsSync(binary)) {
  console.error(`${windows ? 'uv.exe' : 'uv'} was not found in the archive`)
  process.exit(1)
}
if (!windows) chmodSync(binary, 0o755)
writeFileSync(stamp, `${mark}\n`)
console.log(`uv ${mark} verified and unpacked to vendor/uv`)
