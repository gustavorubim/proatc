// What the window shows, as a pure function of the messages received from the engine.

import type { EngineMessage, EngineState } from '../../shared/protocol'

export type LineKind = 'pilot' | 'atc' | 'atis' | 'note' | 'error'

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
  nextId: 1
}

export type Action =
  { type: 'link'; link: Store['link'] } | { type: 'fatal'; text: string } | { type: 'message'; message: EngineMessage }

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

const SOURCE_LABEL = { voice: 'voice', typed: 'typed', clicked: 'clicked' } as const

export function reduce(store: Store, action: Action): Store {
  if (action.type === 'link') return { ...store, link: action.link }
  if (action.type === 'fatal') return { ...store, fatal: action.text }

  const message = action.message
  switch (message.type) {
    case 'state':
      return { ...store, engine: message }
    case 'status':
      return message.text.trim() ? { ...store, startup: message.text.trim() } : store
    case 'tx':
      return { ...store, transmitting: message.on }
    case 'pilot':
      return push(store, 'pilot', 'You', message.text, SOURCE_LABEL[message.source])
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
    default:
      return store
  }
}

/** Fill in the value a suggested call asked for. */
export function fillDestination(text: string, destination: string): string {
  return text.replace('{destination}', destination.trim())
}
