import { describe, expect, it } from 'vitest'
import { externalLink } from './links'

describe('external links', () => {
  it("opens the project's own GitHub pages", () => {
    expect(externalLink('https://github.com/gustavorubim/proatc/issues/new')).toBe(
      'https://github.com/gustavorubim/proatc/issues/new'
    )
    expect(externalLink('https://github.com/gustavorubim/proatc/issues/new?title=Problem')).not.toBeNull()
    expect(externalLink('https://github.com/gustavorubim/proatc')).not.toBeNull()
    expect(externalLink('https://github.com/gustavorubim/proatc/releases/latest')).not.toBeNull()
  })

  it('opens nothing else', () => {
    expect(externalLink('https://example.com/')).toBeNull()
    expect(externalLink('https://github.com/someone/else')).toBeNull()
    expect(externalLink('https://github.com/gustavorubim/proatc-evil')).toBeNull()
    expect(externalLink('https://github.com/gustavorubim/proatc/../../someone/else')).toBeNull()
    expect(externalLink('https://github.com.evil.com/gustavorubim/proatc')).toBeNull()
    expect(externalLink('https://github.com@evil.com/gustavorubim/proatc')).toBeNull()
    expect(externalLink('https://user@github.com/gustavorubim/proatc')).toBeNull()
    expect(externalLink('http://github.com/gustavorubim/proatc')).toBeNull()
    expect(externalLink('file:///C:/Windows/System32/calc.exe')).toBeNull()
    expect(externalLink('not a url')).toBeNull()
  })
})
