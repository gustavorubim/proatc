// Messages between the Python engine (src/proatc/server.py) and the app. Keep in step with events.py.

export interface Suggestion {
  label: string
  text: string
  intent: Record<string, unknown>
  needs?: 'destination'
  primary?: boolean
}

export interface Frequency {
  mhz: string
  kind: string
  label: string
  tuned: boolean
}

/** An airport's map: metres east and north of its reference point. Sent once, when asked for. */
export interface Chart {
  ident: string
  name: string
  runways: { name: string; width: number; ends: [string, number, number][] }[]
  /** Polygons of rings of x, y, x, y…; a polygon's first ring is its outline, the rest are holes. */
  pavement: number[][][]
  /** Centrelines, as x1, y1, x2, y2… per taxiway. */
  taxiways: { name: string; lines: number[] }[]
  labels: { name: string; at: [number, number] }[]
  stands: { name: string; at: [number, number] }[]
  bounds: [number, number, number, number]
}

/** Where the aircraft is on that map, and the taxi route it was given. */
export interface TaxiView {
  ident: string
  position: [number, number]
  heading: number
  route?: {
    /** From the aircraft to the place it must stop: the next runway to hold short of, or the end. */
    cleared: [number, number][]
    /** The rest of the route, not yet cleared. */
    beyond: [number, number][]
    goal: { label: string; at: [number, number] }
    hold?: { runway: string; at: [number, number] }
  }
}

export interface EngineState {
  type: 'state'
  ready: boolean
  status: {
    sim: boolean
    has_key: boolean
    model: string
    ptt_key: string
    stt_device: string | null
    can_tune?: boolean
    provider?: string
  }
  aircraft: { callsign?: string; aircraft_type?: string; on_ground?: boolean; altitude_ft?: number } | null
  radio: {
    mhz: string | null
    station: { callsign: string; kind: string; airport: string | null; distance_nm: number } | null
  } | null
  airport: {
    ident: string
    name: string
    frequencies: Frequency[]
    atis_letter?: string
    atis_name?: string
    runways_in_use?: string[]
    wind?: string
    pressure?: string
    temperature?: string
  } | null
  flight: {
    phase?: string
    flight_rules?: string
    destination?: string
    assigned_squawk?: string
    assigned_runway?: string
    sid?: string
    star?: string
    cleared_altitude_ft?: number
    approach?: string
    heading?: number
    speed_kt?: number
    direct_to?: string
    holding?: string
    next_frequency?: string
    emergency?: string
    stand?: string
    hold_short?: string
    service?: string
    cleared_level?: string
  } | null
  plan: {
    origin?: string
    destination?: string
    route?: string
    cruise_ft?: number
    sid?: string
    star?: string
    source?: string
  } | null
  /** Controlled or restricted airspace the aircraft is inside, smallest first. */
  airspace?: { kind: string; name: string; floor_ft: number; ceiling_ft: number | null }[]
  /** In the air: the en-route centre for where the aircraft is. */
  centre?: { mhz: string; callsign: string; tuned: boolean } | null
  /** On the ground at an airport: the aircraft on its map. */
  map?: TaxiView | null
  simbrief_user: string
  settings: {
    values: {
      audio: { input_device: number | null; output_device: number | null }
      ptt: { key: string; joystick: string | null; joystick_button: number | null }
      tts: {
        engine: 'kokoro' | 'phonon'
        voice: string
        atis_voice: string
        pilot_voice: string
        speed: number
        speak_pilot_text: boolean
        vary_voices: boolean
      }
      llm: { model: string; provider: string; local_url: string; local_model: string }
      radio: { enabled: boolean }
      atc: { holding: string; copilot: string; traffic: boolean; chatter: string }
      sim: { allow_tuning: boolean; auto_tune: boolean }
      remote: { enabled: boolean }
    }
    options: {
      inputs: { id: number; name: string }[]
      outputs: { id: number; name: string }[]
      voices: string[]
      joysticks: string[]
    }
    capturing: boolean
  }
  cost: { calls: number; prompt_tokens: number; completion_tokens: number; cost: number }
  /** The app's screen in a browser on another device. */
  remote?: { available: boolean; enabled: boolean; running: boolean; url: string; code: string }
  suggestions: Suggestion[]
}

export interface Debrief {
  type: 'debrief'
  minutes: number
  transmissions: number
  controller_calls: number
  stations: string[]
  readbacks: { given: number; corrected: number; missed: number }
  say_again: number
  findings: { time: string; kind: string; text: string; said: string }[]
  score: number | null
  verdict: string
  notes?: string
}

export type EngineEvent =
  | { type: 'info' | 'error' | 'status'; text: string }
  | { type: 'tx'; on: boolean }
  | { type: 'pilot'; text: string; source: 'voice' | 'typed' | 'clicked' | 'copilot' }
  | { type: 'coach'; text: string }
  | { type: 'capture'; active: boolean }
  | { type: 'log_saved'; path: string }
  | { type: 'understood'; intent: string }
  | { type: 'atc'; station: string; text: string; unprompted: boolean }
  | { type: 'silent'; text?: string }
  | { type: 'timings'; listen: number; think: number; voice: number; total: number }
  | { type: 'atis'; airport: string; mhz: string; text: string }
  | { type: 'chatter'; station: string; callsign: string; said: string; answered: string }
  | Debrief
  | ({ type: 'chart' } & Chart)

export type EngineMessage = EngineState | EngineEvent

export type Command =
  | { cmd: 'ptt'; down: boolean }
  | { cmd: 'say'; text: string }
  | { cmd: 'click'; text: string; intent: Record<string, unknown> }
  | { cmd: 'set_key'; key: string }
  | { cmd: 'simbrief'; user: string }
  | { cmd: 'plan'; destination: string; cruise: string; route: string }
  | { cmd: 'clear_plan' }
  | { cmd: 'tune'; mhz: string }
  | { cmd: 'settings'; changes: Record<string, Record<string, unknown>> }
  | { cmd: 'capture_ptt' }
  | { cmd: 'clear_joystick' }
  | { cmd: 'explain' }
  | { cmd: 'what_next' }
  | { cmd: 'save_log' }
  | { cmd: 'reset_cost' }
  | { cmd: 'debrief' }
  | { cmd: 'new_flight' }
  | { cmd: 'chart'; ident: string }

export interface UpdateState {
  state: 'none' | 'checking' | 'downloading' | 'ready' | 'error' | 'available'
  version?: string
  percent?: number
  message?: string
}

export interface Connection {
  port: number
  token: string
}
