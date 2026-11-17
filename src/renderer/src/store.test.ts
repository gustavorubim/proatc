import { describe, expect, it } from 'vitest'
import type { EngineMessage } from '../../shared/protocol'
import { fillDestination, initialStore, MAX_LINES, reduce, type Store } from './store'

function feed(messages: EngineMessage[], start: Store = initialStore): Store {
  return messages.reduce((store, message) => reduce(store, { type: 'message', message }), start)
}

describe('store', () => {
  it('builds the conversation with annotations on the right lines', () => {
    const store = feed([
      { type: 'pilot', text: 'Heathrow Ground, request taxi', source: 'clicked' },
      { type: 'understood', intent: 'request_taxi' },
      {
        type: 'atc',
        station: 'Heathrow Ground',
        text: 'Taxi to holding point runway two seven right',
        unprompted: false
      },
      { type: 'timings', listen: 0, think: 0.9, voice: 1.04, total: 1.94 },
      { type: 'silent' },
      { type: 'atis', airport: 'EGLL', mhz: '128.080', text: 'Heathrow information Kilo' },
      { type: 'info', text: 'nobody answers: no station in range on 123.450' },
      { type: 'error', text: 'OpenRouter returned 429' }
    ])
    expect(store.log.map((line) => [line.kind, line.who, line.meta])).toEqual([
      ['pilot', 'You', 'request taxi'],
      ['atc', 'Heathrow Ground', '1.9 s'],
      ['note', '', undefined],
      ['atis', 'EGLL ATIS 128.080', undefined],
      ['note', '', undefined],
      ['error', '', undefined]
    ])
    expect(store.lastInstruction).toEqual({
      station: 'Heathrow Ground',
      text: 'Taxi to holding point runway two seven right'
    })
    expect(new Set(store.log.map((line) => line.id)).size).toBe(6)
  })

  it('tracks transmit state, start-up text and the link', () => {
    let store = feed([
      { type: 'status', text: '\nReady. Hold [ctrl_r] to talk.\n' },
      { type: 'tx', on: true }
    ])
    expect(store.startup).toBe('Ready. Hold [ctrl_r] to talk.')
    expect(store.transmitting).toBe(true)
    store = reduce(store, { type: 'link', link: 'open' })
    store = reduce(store, { type: 'fatal', text: 'The engine stopped.' })
    expect([store.link, store.fatal]).toEqual(['open', 'The engine stopped.'])
  })

  it('keeps only the most recent lines', () => {
    const many: EngineMessage[] = Array.from({ length: MAX_LINES + 20 }, (_, index) => ({
      type: 'info',
      text: `note ${index}`
    }))
    const store = feed(many)
    expect(store.log).toHaveLength(MAX_LINES)
    expect(store.log[0].text).toBe('note 20')
  })

  it('an annotation with no matching line changes nothing', () => {
    expect(feed([{ type: 'understood', intent: 'radio_check' }]).log).toEqual([])
  })

  it('shows the instructor, the copilot and a saved log', () => {
    const store = feed([
      { type: 'pilot', text: 'Taxi holding point two seven', source: 'copilot' },
      { type: 'coach', text: 'Taxi to the runway and stop before it.' },
      { type: 'log_saved', path: 'C:\\logs\\flight-1.txt' },
      { type: 'capture', active: true }
    ])
    expect(store.log.map((line) => [line.kind, line.who])).toEqual([
      ['pilot', 'Copilot'],
      ['coach', 'Instructor'],
      ['note', '']
    ])
    expect(store.savedLog).toBe('C:\\logs\\flight-1.txt')
  })

  it('shows other aircraft faintly, without replacing the last instruction', () => {
    const store = feed([
      { type: 'atc', station: 'Heathrow Tower', text: 'Speedbird 12, cleared to land', unprompted: false },
      {
        type: 'chatter',
        station: 'Heathrow Tower',
        callsign: 'Shamrock four five',
        said: 'Shamrock four five, line up and wait',
        answered: 'Line up and wait, Shamrock four five'
      }
    ])
    expect(store.log.map((line) => [line.kind, line.who])).toEqual([
      ['atc', 'Heathrow Tower'],
      ['chatter', 'Heathrow Tower'],
      ['chatter', 'Shamrock four five']
    ])
    expect(store.lastInstruction?.text).toBe('Speedbird 12, cleared to land')
  })

  it('opens and closes the debrief, and asks another device for its pairing code', () => {
    const debrief = {
      type: 'debrief' as const,
      minutes: 42,
      transmissions: 18,
      controller_calls: 21,
      stations: ['Heathrow Ground'],
      readbacks: { given: 9, corrected: 1, missed: 0 },
      say_again: 0,
      findings: [{ time: '10:02:11', kind: 'readback', text: 'Readback corrected by the controller', said: '' }],
      score: 94,
      verdict: 'Good radio work, with a few things to tidy up.'
    }
    const open = feed([debrief])
    expect(open.debrief?.score).toBe(94)
    expect(open.log).toHaveLength(0)
    expect(reduce(open, { type: 'close_debrief' }).debrief).toBeNull()

    expect(initialStore.pairing).toBe('no')
    const asked = reduce(initialStore, { type: 'pair', pairing: 'wrong' })
    expect(asked.pairing).toBe('wrong')
    expect(reduce(asked, { type: 'pair', pairing: 'no' }).pairing).toBe('no')
  })

  it('says why nobody answered a blind call', () => {
    const store = feed([
      { type: 'pilot', text: 'Santa Paula traffic, final runway two two', source: 'clicked' },
      { type: 'silent', text: 'No controller at Santa Paula: your call went out to other traffic on the frequency.' },
      { type: 'silent' }
    ])
    expect(store.log.map((line) => line.text).slice(1)).toEqual([
      'No controller at Santa Paula: your call went out to other traffic on the frequency.',
      'Readback correct. The controller stays silent.'
    ])
  })

  it('shows the proper call for what was typed, and keeps it for the entry box', () => {
    const worded = feed([
      {
        type: 'phrase',
        said: 'i want to go to the runway',
        text: 'Luton Ground, Golf Alfa Bravo, request taxi',
        intent: { intent: 'request_taxi' },
        note: ''
      }
    ])
    expect(worded.log.map((line) => [line.kind, line.text])).toEqual([
      ['coach', 'Say: “Luton Ground, Golf Alfa Bravo, request taxi”']
    ])
    expect(worded.phrase).toEqual({
      id: 1,
      text: 'Luton Ground, Golf Alfa Bravo, request taxi',
      intent: { intent: 'request_taxi' }
    })
    expect(worded.lastInstruction).toBeNull()
    const refused = feed(
      [{ type: 'phrase', said: 'pizza', text: '', intent: null, note: 'Ask the cabin crew.' }],
      worded
    )
    expect(refused.log[1].text).toBe('Ask the cabin crew.')
    expect(refused.phrase).toEqual({ id: 2, text: '', intent: null })
  })

  it('fills the destination into a suggested call', () => {
    expect(fillDestination('Delivery, request I F R clearance to {destination}', ' Paris ')).toBe(
      'Delivery, request I F R clearance to Paris'
    )
  })
})
