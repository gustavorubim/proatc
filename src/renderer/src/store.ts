// What the window shows, as a pure function of the messages received from the engine.

import type { Debrief, EngineMessage, EngineState } from '../../shared/protocol'

export type LineKind = 'pilot' | 'atc' | 'atis' | 'note' | 'error' | 'coach' | 'chatter'

export interface Line {
  id: number
  kind: LineKind
  who: string
  text: string
  meta?: string
}

export interface Store {
  link: 'connecting' | 'open' | 'closed'
  engine: EngineState | null
  log: Line[]
  transmitting: boolean
  startup: string
  lastInstruction: { station: string; text: string } | null
  fatal: string | null
  savedLog: string | null
  debrief: Debrief | null
  /** A browser on another device: 'ask' for the pairing code, 'wrong' when the engine refused it. */
  pairing: 'no' | 'ask' | 'wrong'
  nextId: number
}

export const MAX_LINES = 300

export const initialStore: Store = {
  link: 'connecting',
  engine: null,
  log: [],
  transmitting: false,
  startup: 'Starting the engine…',
  lastInstruction: null,
  fatal: null,
  savedLog: null,
  debrief: null,
  pairing: 'no',
  nextId: 1
}

export type Action =
  | { type: 'link'; link: Store['link'] }
  | { type: 'fatal'; text: string }
  | { type: 'message'; message: EngineMessage }
  | { type: 'pair'; pairing: Store['pairing'] }
  | { type: 'close_debrief' }

function push(store: Store, kind: LineKind, who: string, text: string, meta?: string): Store {
  const line: Line = { id: store.nextId, kind, who, text, meta }
  return { ...store, log: [...store.log, line].slice(-MAX_LINES), nextId: store.nextId + 1 }
}

/** Attach a small annotation to the most recent line of a kind. */
function annotate(store: Store, kind: LineKind, meta: string): Store {
  for (let index = store.log.length - 1; index >= 0; index--) {
    if (store.log[index].kind === kind) {
      const log = store.log.slice()
      log[index] = { ...log[index], meta }
      return { ...store, log }
    }
  }
  return store
}

const SOURCE_LABEL = { voice: 'voice', typed: 'typed', clicked: 'clicked', copilot: 'copilot' } as const

export function reduce(store: Store, action: Action): Store {
  if (action.type === 'link') return { ...store, link: action.link }
  if (action.type === 'fatal') return { ...store, fatal: action.text }
  if (action.type === 'pair') return { ...store, pairing: action.pairing }
  if (action.type === 'close_debrief') return { ...store, debrief: null }

  const message = action.message
  switch (message.type) {
    case 'state':
      return { ...store, engine: message }
    case 'status':
      return message.text.trim() ? { ...store, startup: message.text.trim() } : store
    case 'tx':
      return { ...store, transmitting: message.on }
    case 'pilot':
      return push(
        store,
        'pilot',
        message.source === 'copilot' ? 'Copilot' : 'You',
        message.text,
        SOURCE_LABEL[message.source]
      )
    case 'understood':
      return annotate(store, 'pilot', message.intent.replaceAll('_', ' '))
    case 'atc': {
      const next = push(store, 'atc', message.station, message.text)
      return { ...next, lastInstruction: { station: message.station, text: message.text } }
    }
    case 'silent':
      return push(store, 'note', '', 'Readback correct. The controller stays silent.')
    case 'timings':
      return annotate(store, 'atc', `${message.total.toFixed(1)} s`)
    case 'atis':
      return push(store, 'atis', `${message.airport} ATIS ${message.mhz}`, message.text)
    case 'info':
      return push(store, 'note', '', message.text)
    case 'error':
      return push(store, 'error', '', message.text)
    case 'coach':
      return push(store, 'coach', 'Instructor', message.text)
    case 'log_saved':
      return { ...push(store, 'note', '', `Flight log saved: ${message.path}`), savedLog: message.path }
    case 'chatter':
      // Other aircraft on the frequency: shown faintly, and never as the last instruction.
      return push(push(store, 'chatter', message.station, message.said), 'chatter', message.callsign, message.answered)
    case 'debrief':
      return { ...store, debrief: message }
    case 'capture':
      return store
    default:
      return store
  }
}

/** Fill in the value a suggested call asked for. */
export function fillDestination(text: string, destination: string): string {
  return text.replace('{destination}', destination.trim())
}
