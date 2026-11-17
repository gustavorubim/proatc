import { type FormEvent, useCallback, useEffect, useReducer, useRef, useState } from 'react'
import type { Command, Debrief, EngineMessage, EngineState, Suggestion, UpdateState } from '../../shared/protocol'
import { AirportMap } from './AirportMap'
import logo from './assets/logo.svg'
import { SettingsDialog } from './Settings'
import { fillDestination, initialStore, type Line, reduce } from './store'
import { updateView } from './updates'

const KIND_LABEL: Record<string, string> = {
  atis: 'ATIS',
  delivery: 'Delivery',
  ground: 'Ground',
  tower: 'Tower',
  departure: 'Departure',
  approach: 'Approach',
  unicom: 'Unicom'
}

const CODE_KEY = 'proatc-pairing-code'

/** In a browser on another device there is no app around the page: it pairs with a code instead. */
const inApp = Boolean(window.proatc)

function savedCode(): string {
  try {
    return localStorage.getItem(CODE_KEY) ?? ''
  } catch {
    return ''
  }
}

const AIRSPACE_LABEL: Record<string, string> = {
  CTR: 'CTR',
  B: 'CLASS B',
  C: 'CLASS C',
  D: 'CLASS D',
  R: 'RESTRICTED',
  P: 'PROHIBITED',
  Q: 'DANGER'
}

function useEngine() {
  const [store, dispatch] = useReducer(reduce, initialStore)
  const socket = useRef<WebSocket | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let closed = false
    let retry: ReturnType<typeof setTimeout> | undefined

    const open = async (): Promise<void> => {
      let address: string
      if (inApp) {
        const connection = await window.proatc?.connection()
        if (closed) return
        if (!connection) {
          dispatch({ type: 'fatal', text: 'The engine has not been started.' })
          return
        }
        address = `ws://127.0.0.1:${connection.port}/?token=${connection.token}`
      } else {
        const code = savedCode()
        if (!code) {
          dispatch({ type: 'pair', pairing: 'ask' })
          return
        }
        address = `ws://${window.location.host}/?token=${encodeURIComponent(code)}`
      }
      const ws = new WebSocket(address)
      socket.current = ws
      ws.onopen = () => dispatch({ type: 'link', link: 'open' })
      ws.onmessage = (event) => {
        try {
          dispatch({ type: 'message', message: JSON.parse(String(event.data)) as EngineMessage })
        } catch {
          // A malformed message is dropped; the next state message repairs the view.
        }
      }
      ws.onclose = (event) => {
        if (closed) return
        dispatch({ type: 'link', link: 'connecting' })
        if (!inApp && event.code === 1008) {
          dispatch({ type: 'pair', pairing: 'wrong' }) // the code was refused: ask again instead of retrying
          return
        }
        retry = setTimeout(() => void open(), 1000) // the engine takes a moment to start listening
      }
    }
    void open()
    // Until the engine is listening, its console output is the only sign of progress
    // (the first run installs Python and the speech libraries).
    window.proatc?.onEngineLog((text) => dispatch({ type: 'message', message: { type: 'status', text } }))
    window.proatc?.onEngineExit((text) => dispatch({ type: 'fatal', text }))
    void window.proatc?.engineLog().then(({ lines, failure }) => {
      if (closed) return
      if (failure) dispatch({ type: 'fatal', text: failure })
      else if (lines.length) dispatch({ type: 'message', message: { type: 'status', text: lines[lines.length - 1] } })
    })
    return () => {
      closed = true
      clearTimeout(retry)
      socket.current?.close()
    }
  }, [attempt])

  const pair = useCallback((code: string) => {
    try {
      localStorage.setItem(CODE_KEY, code.trim().toUpperCase())
    } catch {
      // Without storage the code is asked for again on the next visit.
    }
    dispatch({ type: 'pair', pairing: 'no' })
    setAttempt((count) => count + 1)
  }, [])

  const send = useCallback((command: Command) => {
    if (socket.current?.readyState === WebSocket.OPEN) socket.current.send(JSON.stringify(command))
  }, [])

  return { store, send, pair, closeDebrief: () => dispatch({ type: 'close_debrief' }) }
}

function PairDialog({ wrong, onPair }: { wrong: boolean; onPair: (code: string) => void }) {
  const [code, setCode] = useState('')
  return (
    <div className="scrim">
      <form
        className="dialog"
        onSubmit={(event) => {
          event.preventDefault()
          if (code.trim()) onPair(code)
        }}
      >
        <h2>Connect to Pro ATC</h2>
        <p>
          {wrong ? 'That code was not accepted. ' : ''}
          Enter the pairing code shown in the app on your computer, under Settings, Phone or tablet.
        </p>
        <input
          autoFocus
          className="mono"
          placeholder="8 characters"
          value={code}
          autoCapitalize="characters"
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => setCode(event.target.value)}
        />
        <div className="row">
          <button type="submit" className="primary" disabled={!code.trim()}>
            Connect
          </button>
        </div>
      </form>
    </div>
  )
}

function DebriefDialog({ debrief, onClose }: { debrief: Debrief; onClose: () => void }) {
  const { readbacks } = debrief
  return (
    <div className="scrim" onClick={onClose}>
      <div className="dialog debrief" onClick={(event) => event.stopPropagation()}>
        <div className="settings-head">
          <h2>Debrief</h2>
          <button className="ghost" onClick={onClose}>
            Done
          </button>
        </div>
        <div className="debrief-top">
          {debrief.score !== null && (
            <div className="score">
              {debrief.score}
              <small>of 100</small>
            </div>
          )}
          <div>
            <div className="verdict">{debrief.verdict}</div>
            <div className="muted">
              {debrief.transmissions} transmissions in {debrief.minutes} min · {debrief.stations.length} stations ·
              readbacks: {readbacks.given} given, {readbacks.corrected} corrected, {readbacks.missed} missed
            </div>
          </div>
        </div>
        {debrief.notes && <p className="debrief-notes">{debrief.notes}</p>}
        {debrief.findings.length > 0 && (
          <ul className="findings">
            {debrief.findings.map((finding, index) => (
              <li key={index} className={`finding finding-${finding.kind}`} title={finding.said}>
                <span className="mono">{finding.time}</span>
                {finding.text}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

function UpdatePill() {
  const [update, setUpdate] = useState<UpdateState>({ state: 'none' })
  const [version, setVersion] = useState('')
  useEffect(() => {
    void window.proatc?.version().then(setVersion)
    void window.proatc?.updateState().then(setUpdate)
    window.proatc?.onUpdateState(setUpdate)
  }, [])
  const view = updateView(update, version)
  const click = (): void => {
    if (view.action === 'install') void window.proatc?.installUpdate()
    else if (view.action === 'open') void window.proatc?.openReleases()
    else if (view.action === 'check') void window.proatc?.checkForUpdates().then(setUpdate)
  }
  return (
    <button className={`update update-${view.tone}`} title={view.hint} disabled={view.action === null} onClick={click}>
      {view.label}
    </button>
  )
}

function Pill({ ok, label, detail }: { ok: boolean | 'wait'; label: string; detail?: string }) {
  const tone = ok === 'wait' ? 'wait' : ok ? 'ok' : 'bad'
  return (
    <span className={`pill pill-${tone}`} title={detail}>
      <span className="dot" />
      {label}
    </span>
  )
}

function formatLevel(feet: number): string {
  return feet >= 10000 ? `FL${Math.round(feet / 100)}` : `${feet.toLocaleString('en-US')} ft`
}

function PlanCard({ engine, send }: { engine: EngineState | null; send: (command: Command) => void }) {
  const plan = engine?.plan
  const [user, setUser] = useState('')
  const [destination, setDestination] = useState('')
  const [cruise, setCruise] = useState('')
  const [manual, setManual] = useState(false)
  const saved = engine?.simbrief_user ?? ''
  const ready = Boolean(engine?.ready)

  if (plan) {
    return (
      <section className="card">
        <div className="eyebrow">
          Flight plan{plan.source === 'simbrief' ? ' · SimBrief' : ''}
          <button className="link" onClick={() => send({ cmd: 'clear_plan' })}>
            clear
          </button>
        </div>
        <div className="route-ends">
          <span className="mono">{plan.origin ?? '····'}</span>
          <span className="arrow">→</span>
          <span className="mono">{plan.destination}</span>
          {plan.cruise_ft ? <span className="chip">{formatLevel(plan.cruise_ft)}</span> : null}
        </div>
        {plan.route && <div className="route mono">{plan.route}</div>}
      </section>
    )
  }
  return (
    <section className="card">
      <div className="eyebrow">Flight plan</div>
      {manual ? (
        <form
          className="plan-form"
          onSubmit={(event) => {
            event.preventDefault()
            if (destination.trim()) send({ cmd: 'plan', destination, cruise, route: '' })
          }}
        >
          <input
            placeholder="Destination, e.g. LFPG"
            value={destination}
            onChange={(e) => setDestination(e.target.value)}
          />
          <input placeholder="Cruise, e.g. 360" value={cruise} onChange={(e) => setCruise(e.target.value)} />
          <div className="row">
            <button type="button" className="link" onClick={() => setManual(false)}>
              use SimBrief
            </button>
            <button type="submit" className="primary" disabled={!ready || !destination.trim()}>
              Set
            </button>
          </div>
        </form>
      ) : (
        <form
          className="plan-form"
          onSubmit={(event) => {
            event.preventDefault()
            send({ cmd: 'simbrief', user: user.trim() || saved })
          }}
        >
          <input
            placeholder={saved ? `SimBrief user (${saved})` : 'SimBrief username or pilot ID'}
            value={user}
            onChange={(e) => setUser(e.target.value)}
          />
          <div className="row">
            <button type="button" className="link" onClick={() => setManual(true)}>
              enter by hand
            </button>
            <button type="submit" className="primary" disabled={!ready || !(user.trim() || saved)}>
              Load from SimBrief
            </button>
          </div>
        </form>
      )}
    </section>
  )
}

function RadioPanel({ engine, send }: { engine: EngineState | null; send: (command: Command) => void }) {
  const radio = engine?.radio
  const airport = engine?.airport
  const flight = engine?.flight
  const station = radio?.station
  const canTune = Boolean(engine?.status.can_tune && engine?.status.sim)
  return (
    <aside className="side">
      <section className="card tuned">
        <div className="eyebrow">Tuned</div>
        <div className="freq">{radio?.mhz ?? '---.---'}</div>
        <div className="station">
          {station ? station.callsign : engine?.status.sim ? 'No station in range' : 'Simulator not connected'}
        </div>
        {station?.airport && (
          <div className="muted">
            {station.airport} · {station.distance_nm} NM
          </div>
        )}
        {station && !station.airport && <div className="muted">En-route centre</div>}
      </section>

      <section className="card">
        <div className="eyebrow">
          Flight
          {flight?.phase && (
            <button
              className="link"
              title="Forget every clearance and start again, for a new flight or after moving the aircraft"
              onClick={() => send({ cmd: 'new_flight' })}
            >
              new flight
            </button>
          )}
        </div>
        <div className="strip">
          <div>
            <label>Callsign</label>
            <span>{engine?.aircraft?.callsign ?? '—'}</span>
          </div>
          <div>
            <label>Type</label>
            <span>{engine?.aircraft?.aircraft_type ?? '—'}</span>
          </div>
          <div>
            <label>Squawk</label>
            <span className="mono">{flight?.assigned_squawk ?? '—'}</span>
          </div>
          <div>
            <label>Runway</label>
            <span className="mono">{flight?.assigned_runway ?? '—'}</span>
          </div>
        </div>
        <div className="phase">
          {flight?.phase ?? 'no flight'}
          {flight?.destination ? ` · ${flight.flight_rules ?? ''} to ${flight.destination}` : ''}
        </div>
        {(flight?.sid ||
          flight?.star ||
          flight?.cleared_altitude_ft ||
          flight?.approach ||
          flight?.hold_short ||
          flight?.service ||
          flight?.stand) && (
          <div className="chips ifr">
            {flight.cleared_altitude_ft ? (
              <span className="chip chip-atis" title="Last level you were cleared to">
                {flight.cleared_level ?? formatLevel(flight.cleared_altitude_ft)}
              </span>
            ) : null}
            {flight.sid && <span className="chip">SID {flight.sid}</span>}
            {flight.star && <span className="chip">STAR {flight.star}</span>}
            {flight.approach && <span className="chip">{flight.approach.replaceAll(' ', '')}</span>}
            {flight.heading ? <span className="chip">HDG {String(flight.heading).padStart(3, '0')}</span> : null}
            {flight.speed_kt ? <span className="chip">{flight.speed_kt} kt</span> : null}
            {flight.direct_to && <span className="chip">DCT {flight.direct_to}</span>}
            {flight.holding && <span className="chip chip-atis">HOLD {flight.holding}</span>}
            {flight.hold_short && <span className="chip chip-alert">HOLD SHORT {flight.hold_short}</span>}
            {flight.stand && <span className="chip">STAND {flight.stand}</span>}
            {flight.service && <span className="chip chip-atis">{flight.service.toUpperCase()}</span>}
            {flight.emergency && <span className="chip chip-alert">{flight.emergency.toUpperCase()}</span>}
          </div>
        )}
        {(engine?.airspace ?? []).map((space) => (
          <div className="airspace" key={space.name} title="Airspace you are in">
            <span className={`chip ${'RPQ'.includes(space.kind) ? 'chip-alert' : 'chip-atis'}`}>
              {AIRSPACE_LABEL[space.kind] ?? space.kind}
            </span>
            {space.name} · {space.floor_ft ? `${space.floor_ft.toLocaleString('en-US')} ft` : 'surface'} to{' '}
            {space.ceiling_ft ? `${space.ceiling_ft.toLocaleString('en-US')} ft` : 'unlimited'}
          </div>
        ))}
      </section>

      <PlanCard engine={engine} send={send} />

      {engine?.centre && (
        <section className="card">
          <div className="eyebrow">En-route centre here</div>
          <ul className="freqs">
            <li
              className={engine.centre.tuned ? 'on' : ''}
              title={canTune ? 'Click to tune this frequency on your COM radio' : 'Click to copy'}
              onClick={() =>
                canTune
                  ? send({ cmd: 'tune', mhz: engine.centre!.mhz })
                  : void navigator.clipboard.writeText(engine.centre!.mhz)
              }
            >
              <span className="mono">{engine.centre.mhz}</span>
              <span className="name">{engine.centre.callsign}</span>
            </li>
          </ul>
        </section>
      )}
      {airport && (
        <section className="card grow">
          <div className="eyebrow">
            {airport.ident} · {airport.name}
          </div>
          {airport.atis_letter && airport.atis ? (
            <dl className="atis" title="The current ATIS, written down">
              {airport.atis.map((row, index) => (
                <div key={row.label} className={row.alert ? 'alert' : ''}>
                  <dt>{row.label}</dt>
                  <dd>
                    {index === 0 ? <span className="chip chip-atis">{airport.atis_name}</span> : null}
                    {index === 0 ? row.value.split(' · ')[1] : row.value}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            airport.atis_letter && (
              <div className="chips">
                <span className="chip chip-atis" title="Current ATIS information">
                  {airport.atis_name}
                </span>
                {airport.runways_in_use?.map((runway) => (
                  <span className="chip" key={runway} title="Runway in use">
                    RWY {runway}
                  </span>
                ))}
                <span className="chip">{airport.wind}</span>
                <span className="chip">{airport.pressure}</span>
                <span className="chip">{airport.temperature}</span>
              </div>
            )
          )}
          <ul className="freqs">
            {airport.frequencies.map((frequency) => (
              <li
                key={`${frequency.kind}${frequency.mhz}`}
                className={frequency.tuned ? 'on' : ''}
                title={canTune ? 'Click to tune this frequency on your COM radio' : 'Click to copy'}
                onClick={() =>
                  canTune
                    ? send({ cmd: 'tune', mhz: frequency.mhz })
                    : void navigator.clipboard.writeText(frequency.mhz)
                }
              >
                <span className="mono">{frequency.mhz}</span>
                <span className="name">{frequency.label || KIND_LABEL[frequency.kind] || frequency.kind}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </aside>
  )
}

function LogLine({ line }: { line: Line }) {
  return (
    <div className={`line line-${line.kind}`}>
      {line.who && (
        <div className="who">
          {line.who}
          {line.meta && <span className="meta">{line.meta}</span>}
        </div>
      )}
      <div className="text">{line.text}</div>
    </div>
  )
}

function KeyDialog({
  hasKey,
  onSave,
  onClose
}: {
  hasKey: boolean
  onSave: (key: string) => void
  onClose: () => void
}) {
  const [key, setKey] = useState('')
  const submit = (event: FormEvent): void => {
    event.preventDefault()
    if (key.trim()) onSave(key.trim())
    onClose()
  }
  return (
    <div className="scrim" onClick={onClose}>
      <form className="dialog" onClick={(event) => event.stopPropagation()} onSubmit={submit}>
        <h2>OpenRouter key</h2>
        <p>
          {hasKey
            ? 'A key is already saved. Enter a new one to replace it.'
            : 'No key yet: the controller only echoes.'}{' '}
          The key is stored in the <code>.env</code> file next to the engine and is sent only to OpenRouter.
        </p>
        <input
          type="password"
          autoFocus
          placeholder="sk-or-…"
          value={key}
          onChange={(event) => setKey(event.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <div className="row">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={!key.trim()}>
            Save key
          </button>
        </div>
      </form>
    </div>
  )
}

const MAP_KEY = 'proatc-map'

function savedMapChoice(): boolean {
  try {
    return localStorage.getItem(MAP_KEY) !== 'off'
  } catch {
    return true
  }
}

export function App() {
  const { store, send, pair, closeDebrief } = useEngine()
  const engine = store.engine
  const [draft, setDraft] = useState('')
  const [asking, setAsking] = useState<Suggestion | null>(null)
  const [destination, setDestination] = useState('')
  const [keyOpen, setKeyOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [mapOpen, setMapOpen] = useState(savedMapChoice)
  const [wording, setWording] = useState(false)
  // The proper call the engine worded: sent with its intent while the box still holds it unchanged.
  const [worded, setWorded] = useState<{ text: string; intent: Record<string, unknown> | null } | null>(null)
  const phrase = store.phrase
  useEffect(() => {
    if (!phrase) return
    setWording(false)
    if (phrase.text) {
      setDraft(phrase.text)
      setWorded({ text: phrase.text, intent: phrase.intent })
    }
  }, [phrase])
  useEffect(() => {
    if (!wording) return
    const giveUp = setTimeout(() => setWording(false), 20000)
    return () => clearTimeout(giveUp)
  }, [wording])
  const askWording = (): void => {
    if (!draft.trim()) return
    setWording(true)
    send({ cmd: 'phrase', text: draft.trim() })
  }
  const askedChart = useRef('')
  const logEnd = useRef<HTMLDivElement | null>(null)

  // The map of the airport the aircraft stands on is asked for once, when it is first needed.
  const mapIdent = engine?.map?.ident ?? ''
  const chartIdent = store.chart?.ident ?? ''
  const link = store.link
  useEffect(() => {
    if (link !== 'open') askedChart.current = ''
    else if (mapIdent && mapIdent !== chartIdent && askedChart.current !== mapIdent) {
      askedChart.current = mapIdent
      send({ cmd: 'chart', ident: mapIdent })
    }
  }, [mapIdent, chartIdent, link, send])
  const map = engine?.map && store.chart?.ident === engine.map.ident ? engine.map : null
  const toggleMap = (): void => {
    setMapOpen((open) => {
      try {
        localStorage.setItem(MAP_KEY, open ? 'off' : 'on')
      } catch {
        // Without storage the choice lasts until the window closes.
      }
      return !open
    })
  }

  useEffect(() => {
    logEnd.current?.scrollIntoView({ block: 'end' })
  }, [store.log.length])

  useEffect(() => {
    if (store.savedLog) void window.proatc?.reveal(store.savedLog)
  }, [store.savedLog])

  const ready = Boolean(engine?.ready) && store.link === 'open'
  const click = (suggestion: Suggestion): void => {
    if (suggestion.needs === 'destination') {
      setAsking(suggestion)
      return
    }
    send({ cmd: 'click', text: suggestion.text, intent: suggestion.intent })
  }
  const submitDestination = (event: FormEvent): void => {
    event.preventDefault()
    if (!asking || !destination.trim()) return
    send({
      cmd: 'click',
      text: fillDestination(asking.text, destination),
      intent: { ...asking.intent, destination: destination.trim() }
    })
    setAsking(null)
    setDestination('')
  }
  const submitDraft = (event: FormEvent): void => {
    event.preventDefault()
    if (!draft.trim()) return
    if (worded?.intent && draft.trim() === worded.text) send({ cmd: 'click', text: worded.text, intent: worded.intent })
    else send({ cmd: 'say', text: draft.trim() })
    setWorded(null)
    setDraft('')
  }

  return (
    <div className="app" data-link={store.link}>
      <header className="top">
        <div className="brand">
          <img src={logo} alt="" />
          Pro ATC
          <small>VOICE ATC</small>
        </div>
        <div className="pills">
          {inApp && <UpdatePill />}
          {engine && engine.cost.calls > 0 && (
            <span
              className="cost mono"
              title={`OpenRouter this session: ${engine.cost.calls} requests, ${(
                engine.cost.prompt_tokens + engine.cost.completion_tokens
              ).toLocaleString('en-US')} tokens`}
            >
              ${engine.cost.cost.toFixed(engine.cost.cost < 1 ? 4 : 2)}
            </span>
          )}
          {map && (
            <button className="quiet" title="The airport map, with your taxi route" onClick={toggleMap}>
              {mapOpen ? 'Hide map' : 'Map'}
            </button>
          )}
          <button
            className="quiet"
            disabled={!ready}
            title="Save this flight's radio log"
            onClick={() => send({ cmd: 'save_log' })}
          >
            Save log
          </button>
          <button
            className="quiet"
            disabled={!ready}
            title="Review this flight's radio work"
            onClick={() => send({ cmd: 'debrief' })}
          >
            Debrief
          </button>
          <button className="quiet" disabled={!engine} onClick={() => setSettingsOpen(true)}>
            Settings
          </button>
          <Pill ok={ready ? true : store.fatal ? false : 'wait'} label={ready ? 'Engine ready' : 'Engine starting'} />
          <Pill
            ok={Boolean(engine?.status.sim)}
            label={engine?.status.sim ? 'X-Plane connected' : 'X-Plane not connected'}
          />
          {engine?.status.provider === 'local' ? (
            <Pill ok label="Local model" detail={engine.status.model} />
          ) : (
            inApp && (
              <button className="pill-button" onClick={() => setKeyOpen(true)}>
                <Pill
                  ok={Boolean(engine?.status.has_key)}
                  label={engine?.status.has_key ? 'OpenRouter key set' : 'Add OpenRouter key'}
                  detail={engine?.status.model}
                />
              </button>
            )
          )}
        </div>
      </header>

      <div className="body">
        <RadioPanel engine={engine} send={send} />

        <main className="main">
          {store.lastInstruction && (
            <div className="instruction">
              <div className="eyebrow">
                Last instruction · {store.lastInstruction.station}
                <button className="link" disabled={!ready} onClick={() => send({ cmd: 'explain' })}>
                  explain
                </button>
              </div>
              <div>{store.lastInstruction.text}</div>
            </div>
          )}

          {map && mapOpen && store.chart && <AirportMap chart={store.chart} view={map} />}

          <div className="log">
            {store.fatal && <div className="line line-error">{store.fatal}</div>}
            {!ready && !store.fatal && (
              <div className="starting">
                <div className="spinner" />
                {store.startup}
              </div>
            )}
            {ready && store.log.length === 0 && (
              <div className="empty">
                Hold <kbd>{engine?.status.ptt_key}</kbd> and speak, click a call below, or type one.
                {!engine?.status.sim && ' Start X-Plane and load a flight for real airports and frequencies.'}
              </div>
            )}
            {store.log.map((line) => (
              <LogLine key={line.id} line={line} />
            ))}
            <div ref={logEnd} />
          </div>

          <div className="composer">
            {asking ? (
              <form className="ask" onSubmit={submitDestination}>
                <span>Destination for the clearance request</span>
                <input
                  autoFocus
                  placeholder="e.g. Paris Charles de Gaulle"
                  value={destination}
                  onChange={(event) => setDestination(event.target.value)}
                />
                <button type="button" className="ghost" onClick={() => setAsking(null)}>
                  Cancel
                </button>
                <button type="submit" className="primary" disabled={!destination.trim()}>
                  Transmit
                </button>
              </form>
            ) : (
              <div className="calls">
                <button
                  className="call hint"
                  disabled={!ready}
                  title="The instructor tells you what to say now"
                  onClick={() => send({ cmd: 'what_next' })}
                >
                  What do I say?
                </button>
                {engine?.flight?.next_frequency && engine.status.can_tune && (
                  <button
                    className="call tune"
                    title="Tune the frequency you were just given on your COM radio"
                    onClick={() => send({ cmd: 'tune', mhz: engine.flight!.next_frequency! })}
                  >
                    Tune {engine.flight.next_frequency}
                  </button>
                )}
                {(engine?.suggestions ?? []).map((suggestion) => (
                  <button
                    key={suggestion.label}
                    className={suggestion.primary ? 'call primary' : 'call'}
                    title={suggestion.text}
                    disabled={!ready}
                    onClick={() => click(suggestion)}
                  >
                    {suggestion.label}
                  </button>
                ))}
                {ready && engine?.status.sim && (engine?.suggestions.length ?? 0) === 0 && (
                  <span className="muted">Tune a controller's frequency to see the calls you can make.</span>
                )}
              </div>
            )}

            <form className="entry" onSubmit={submitDraft}>
              <button
                type="button"
                className={store.transmitting ? 'ptt live' : 'ptt'}
                disabled={!ready}
                title={`Hold to talk (or hold ${engine?.status.ptt_key ?? 'the push-to-talk key'})`}
                onMouseDown={() => send({ cmd: 'ptt', down: true })}
                onMouseUp={() => send({ cmd: 'ptt', down: false })}
                onMouseLeave={() => store.transmitting && send({ cmd: 'ptt', down: false })}
                onTouchStart={(event) => {
                  event.preventDefault() // a touch must not also arrive as a mouse click
                  send({ cmd: 'ptt', down: true })
                }}
                onTouchEnd={(event) => {
                  event.preventDefault()
                  send({ cmd: 'ptt', down: false })
                }}
              >
                {store.transmitting ? 'Transmitting' : 'Hold to talk'}
              </button>
              <input
                placeholder="Type a call, or what you want in your own words"
                value={draft}
                disabled={!ready}
                onChange={(event) => setDraft(event.target.value)}
              />
              <button
                type="button"
                className="word"
                disabled={!ready || !draft.trim() || wording || draft.trim() === worded?.text}
                title="Turn what you typed into the proper radio call. Nothing is sent until you press Send or say it"
                onClick={askWording}
              >
                {wording ? 'Wording…' : 'Phraseology'}
              </button>
              <button type="submit" className="primary" disabled={!ready || !draft.trim()}>
                Send
              </button>
            </form>
          </div>
        </main>
      </div>

      {settingsOpen && engine && (
        <SettingsDialog
          engine={engine}
          send={send}
          onClose={() => setSettingsOpen(false)}
          onKey={() => setKeyOpen(true)}
          inApp={inApp}
        />
      )}
      {store.debrief && <DebriefDialog debrief={store.debrief} onClose={closeDebrief} />}
      {store.pairing !== 'no' && <PairDialog wrong={store.pairing === 'wrong'} onPair={pair} />}
      {keyOpen && (
        <KeyDialog
          hasKey={Boolean(engine?.status.has_key)}
          onSave={(key) => send({ cmd: 'set_key', key })}
          onClose={() => setKeyOpen(false)}
        />
      )}
    </div>
  )
}
