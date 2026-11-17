// Which files the window may show in the file manager: only those the engine writes for the person to keep.
const REVEALABLE = [
  /[\\/]logs[\\/]flight-\d{8}-\d{6}\.txt$/, // "Save log"
  /[\\/]logs[\\/]reports[\\/]proatc-report-\d{8}-\d{6}\.zip$/ // "Report a problem"
]

export function mayReveal(path: unknown): path is string {
  return typeof path === 'string' && REVEALABLE.some((pattern) => pattern.test(path))
}
