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
  simbrief_user: string
  settings: {
    values: {
      audio: { input_device: number | null; output_device: number | null }
      ptt: { key: string; joystick: string | null; joystick_button: number | null }
      tts: {
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
  | { type: 'silent' }
  | { type: 'timings'; listen: number; think: number; voice: number; total: number }
  | { type: 'atis'; airport: string; mhz: string; text: string }
  | { type: 'chatter'; station: string; callsign: string; said: string; answered: string }
  | Debrief

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

export interface UpdateState {
  state: 'none' | 'checking' | 'downloading' | 'ready' | 'error'
  version?: string
  percent?: number
  message?: string
}

export interface Connection {
  port: number
  token: string
}
