// Which links from the window may open in the system browser: only the project's own pages on
// GitHub (the "Report a problem" issue form and the releases).

const ORIGIN = 'https://github.com'
const PROJECT = '/gustavorubim/proatc'

/** The link to open, normalised, or null when it may not be opened. */
export function externalLink(url: string): string | null {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return null
  }
  if (parsed.origin !== ORIGIN || parsed.username || parsed.password) return null
  if (parsed.pathname !== PROJECT && !parsed.pathname.startsWith(`${PROJECT}/`)) return null
  return parsed.href
}
