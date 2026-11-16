// Sets the version in package.json, package-lock.json and pyproject.toml together.
// Usage: npm run version:set -- 0.2.0

import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const version = process.argv[2]
if (!/^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$/.test(version ?? '')) {
  console.error('Usage: npm run version:set -- 1.2.3')
  process.exit(1)
}
const root = join(dirname(fileURLToPath(import.meta.url)), '..')

for (const name of ['package.json', 'package-lock.json']) {
  const path = join(root, name)
  const data = JSON.parse(readFileSync(path, 'utf8'))
  data.version = version
  if (data.packages?.['']) data.packages[''].version = version
  writeFileSync(path, `${JSON.stringify(data, null, 2)}\n`)
}

// Python spells a pre-release differently (0.2.0-beta.1 is 0.2.0b1), so only plain versions are synced.
if (!version.includes('-')) {
  const pyproject = join(root, 'pyproject.toml')
  const text = readFileSync(pyproject, 'utf8')
  writeFileSync(pyproject, text.replace(/^version = ".*"$/m, `version = "${version}"`))
}
console.log(`Version set to ${version}. Run "uv lock" to refresh uv.lock.`)
