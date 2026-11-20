import path from 'node:path'
import type { EnginePaths } from './engine'

// Which files the window may show in the file manager: only those the engine writes for the
// person to keep, inside the engine's own logs folder. Paths are checked relative to that folder.
const REVEALABLE = [
  /^flight-\d{8}-\d{6}\.txt$/, // "Save log"
  /^reports[\\/]proatc-report-\d{8}-\d{6}\.zip$/ // "Report a problem"
]

// A network path (\\host\share or //host/share): showing it would make the system contact that host.
const NETWORK_PATH = /^[\\/]{2}/

/**
 * The engine's logs folder. The installed app gives the engine a home in its data folder; from
 * a source checkout the engine uses PROATC_HOME if it is set, otherwise the project folder.
 */
export function logsRoot(paths: EnginePaths, env: NodeJS.ProcessEnv): string {
  return path.join(paths.home ?? (env.PROATC_HOME || paths.projectDir), 'logs')
}

/** `api` is the platform's path module; tests pass path.win32 or path.posix. */
export function mayReveal(target: unknown, root: string, api: path.PlatformPath = path): target is string {
  if (typeof target !== 'string' || NETWORK_PATH.test(target)) return false
  const relative = api.relative(api.resolve(root), api.resolve(target))
  if (!relative || api.isAbsolute(relative) || relative.split(/[\\/]/)[0] === '..') return false
  return REVEALABLE.some((pattern) => pattern.test(relative))
}
