import { useState } from 'react'
import type { Command, ComputerCheck, EngineState, SoundCheckEvent } from '../../shared/protocol'

type Send = (command: Command) => void

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="setting">
      <div>
        <div className="setting-label">{label}</div>
        {hint && <div className="setting-hint">{hint}</div>}
      </div>
      <div className="setting-control">{children}</div>
    </div>
  )
}

function Toggle({ on, onChange }: { on: boolean; onChange: (on: boolean) => void }) {
  return (
    <button
      type="button"
      className={on ? 'toggle on' : 'toggle'}
      role="switch"
      aria-checked={on}
      onClick={() => onChange(!on)}
    >
      <span />
    </button>
  )
}

const VOICE_REGION: Record<string, string> = { a: 'American', b: 'British' }

function voiceName(voice: string): string {
  const [code, name] = voice.split('_')
  const who = code[1] === 'f' ? 'woman' : 'man'
  return `${name[0].toUpperCase()}${name.slice(1)} (${VOICE_REGION[code[0]] ?? code[0]} ${who})`
}

const MARK = { ok: '✓', bad: '✗', unknown: '?' } as const

function mark(check: ComputerCheck): keyof typeof MARK {
  return check.ok === true ? 'ok' : check.ok === false ? 'bad' : 'unknown'
}

/** What could be checked about this computer without anyone: a sentence each, and the fix when it failed. */
function ComputerChecklist({ checks }: { checks: ComputerCheck[] }) {
  return (
    <ul className="checklist">
      {checks.map((check) => (
        <li key={check.id} className={`check check-${mark(check)}`}>
          <span className="check-mark" aria-hidden>
            {MARK[mark(check)]}
          </span>
          <div>
            <div className="setting-label">{check.label}</div>
            <div className="setting-hint">{check.text}</div>
            {check.fix && <div className="check-fix">{check.fix}</div>}
          </div>
        </li>
      ))}
    </ul>
  )
}

/** The sound check as it goes: what is playing, the question to answer, and at the end the verdict. */
function SoundCheckPanel({
  check,
  onAnswer,
  onClose
}: {
  check: SoundCheckEvent
  onAnswer: (heard: boolean | null) => void
  onClose: () => void
}) {
  if (check.state === 'refused' || check.state === 'done') {
    return (
      <div className="sound-check">
        <div className="verdict">{check.state === 'refused' ? check.text : check.verdict}</div>
        {check.steps && (
          <ul className="findings">
            {check.steps.map((step, index) => (
              <li key={index} className={step.problems.length ? 'finding finding-runway' : 'finding'}>
                <span className="mono">{step.answer === null ? '–' : step.answer ? 'yes' : 'no'}</span>
                <div>
                  <div className="setting-label">{step.title}</div>
                  <div className="setting-hint">{step.text}</div>
                </div>
              </li>
            ))}
          </ul>
        )}
        <div className="row">
          <button className="ghost" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    )
  }
  return (
    <div className="sound-check" aria-live="polite">
      <div className="eyebrow">
        {check.step ? `Step ${check.step} of ${check.total} · ` : ''}
        {check.title}
      </div>
      <p>{check.text}</p>
      {check.question && <div className="setting-label">{check.question}</div>}
      <div className="row">
        <button className="ghost" onClick={() => onAnswer(null)}>
          Stop
        </button>
        {check.question && (
          <>
            <button onClick={() => onAnswer(false)}>No</button>
            <button className="primary" onClick={() => onAnswer(true)}>
              Yes
            </button>
          </>
        )}
      </div>
    </div>
  )
}

export function SettingsDialog({
  engine,
  send,
  onClose,
  onKey,
  inApp,
  soundCheck,
  onAnswer,
  onCloseCheck
}: {
  engine: EngineState
  send: Send
  onClose: () => void
  onKey: () => void
  /** False in a browser on another device, where the key and the model cannot be changed. */
  inApp: boolean
  soundCheck: SoundCheckEvent | null
  onAnswer: (heard: boolean | null) => void
  onCloseCheck: () => void
}) {
  const { values, options, capturing } = engine.settings
  const [model, setModel] = useState(values.llm.model)
  const [localModel, setLocalModel] = useState(values.llm.local_model)
  const [localUrl, setLocalUrl] = useState(values.llm.local_url)
  const local = values.llm.provider === 'local'
  const remote = engine.remote
  const change = (section: string, key: string, value: unknown): void =>
    send({ cmd: 'settings', changes: { [section]: { [key]: value } } })
  const device = (value: string): number | null => (value === '' ? null : Number(value))
  const cost = engine.cost
  const checking = soundCheck?.state === 'running' || soundCheck?.state === 'question'

  return (
    <div className="scrim" onClick={onClose}>
      <div className="dialog settings" onClick={(event) => event.stopPropagation()}>
        <div className="settings-head">
          <h2>Settings</h2>
          <button className="ghost" onClick={onClose}>
            Done
          </button>
        </div>

        <div className="settings-body">
          <h3>Audio</h3>
          <Row label="Microphone">
            <select
              value={values.audio.input_device ?? ''}
              onChange={(e) => change('audio', 'input_device', device(e.target.value))}
            >
              <option value="">Windows default</option>
              {options.inputs.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </Row>
          <Row label="Speakers or headset">
            <select
              value={values.audio.output_device ?? ''}
              onChange={(e) => change('audio', 'output_device', device(e.target.value))}
            >
              <option value="">Windows default</option>
              {options.outputs.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </Row>
          <Row label="Radio effect" hint="Makes controllers sound like a VHF radio">
            <Toggle on={values.radio.enabled} onChange={(on) => change('radio', 'enabled', on)} />
          </Row>
          <Row
            label="Sound check"
            hint={
              engine.sound_check?.last ||
              'Plays each sound in turn and asks whether you heard it. Parked on the ground, or with X-Plane closed'
            }
          >
            <button
              disabled={Boolean(engine.sound_check?.running) || checking}
              onClick={() => send({ cmd: 'sound_check' })}
            >
              {checking ? 'Checking…' : 'Sound check'}
            </button>
          </Row>
          {soundCheck && <SoundCheckPanel check={soundCheck} onAnswer={onAnswer} onClose={onCloseCheck} />}

          <h3>Push-to-talk</h3>
          <Row
            label="Key or button"
            hint={
              capturing
                ? 'Press the key or joystick button now. Esc cancels.'
                : `Keyboard: ${values.ptt.key}` +
                  (values.ptt.joystick
                    ? ` · ${values.ptt.joystick}, button ${(values.ptt.joystick_button ?? 0) + 1}`
                    : '')
            }
          >
            <button
              className={capturing ? 'primary' : ''}
              disabled={capturing}
              onClick={() => send({ cmd: 'capture_ptt' })}
            >
              {capturing ? 'Listening…' : 'Change…'}
            </button>
            {values.ptt.joystick && !capturing && (
              <button className="ghost" onClick={() => send({ cmd: 'clear_joystick' })}>
                Remove joystick
              </button>
            )}
          </Row>
          {options.joysticks.length === 0 && <div className="setting-hint">No joystick or yoke detected.</div>}

          <h3>Copilot</h3>
          <Row
            label="Copilot on the radio"
            hint="Reads instructions back for you, or also tunes hand-offs and checks in with the next controller"
          >
            <select value={values.atc.copilot} onChange={(e) => change('atc', 'copilot', e.target.value)}>
              <option value="off">Off</option>
              <option value="readbacks">Readbacks</option>
              <option value="full">Readbacks, tuning and check-ins</option>
            </select>
          </Row>
          <Row label="Tune hand-offs automatically" hint="After you read the frequency back">
            <Toggle on={values.sim.auto_tune} onChange={(on) => change('sim', 'auto_tune', on)} />
          </Row>
          <Row label="Let the app tune the radio" hint="Clicking a frequency sets it on your COM radio">
            <Toggle on={values.sim.allow_tuning} onChange={(on) => change('sim', 'allow_tuning', on)} />
          </Row>

          <h3>Controllers</h3>
          <Row label="Holding before the approach" hint="You can always ask for a hold">
            <select value={values.atc.holding} onChange={(e) => change('atc', 'holding', e.target.value)}>
              <option value="never">Only on request</option>
              <option value="sometimes">Sometimes</option>
              <option value="always">Always</option>
            </select>
          </Row>
          <Row
            label="Listen to the second radio"
            hint="An ATIS on the COM you are not transmitting on is heard too, when the aircraft's audio panel has it on"
          >
            <Toggle on={values.atc.second_radio} onChange={(on) => change('atc', 'second_radio', on)} />
          </Row>
          <Row label="Other traffic" hint="Traffic calls and runway sequencing against X-Plane's AI aircraft">
            <Toggle on={values.atc.traffic} onChange={(on) => change('atc', 'traffic', on)} />
          </Row>
          <Row
            label="Chatter from other aircraft"
            hint="Other flights heard on the frequency. Sound only: they are not in the sim"
          >
            <Toggle
              on={values.atc.chatter !== 'off'}
              onChange={(on) => change('atc', 'chatter', on ? 'some' : 'off')}
            />
          </Row>
          {values.atc.chatter !== 'off' && (
            <Row label="How much chatter">
              <select value={values.atc.chatter} onChange={(e) => change('atc', 'chatter', e.target.value)}>
                <option value="some">Some</option>
                <option value="busy">Busy</option>
              </select>
            </Row>
          )}

          <h3>Voices</h3>
          <Row
            label="Voice engine"
            hint="Phonon is on trial: the controller starts speaking sooner. Its voices are downloaded the first time"
          >
            <select value={values.tts.engine} onChange={(e) => change('tts', 'engine', e.target.value)}>
              <option value="kokoro">Kokoro</option>
              <option value="phonon">Phonon (trial)</option>
            </select>
          </Row>
          <Row
            label="A voice for each controller"
            hint="British voices in the UK, American in the US and Canada. Off: one voice for all"
          >
            <Toggle on={values.tts.vary_voices} onChange={(on) => change('tts', 'vary_voices', on)} />
          </Row>
          {(
            [
              ['voice', values.tts.vary_voices ? 'Controllers (when not varied)' : 'Controllers'],
              ['atis_voice', 'ATIS'],
              ['pilot_voice', 'Copilot and typed calls']
            ] as const
          ).map(([key, label]) => (
            <Row key={key} label={label}>
              <select value={values.tts[key]} onChange={(e) => change('tts', key, e.target.value)}>
                {(options.voices.length ? options.voices : [values.tts[key]]).map((voice) => (
                  <option key={voice} value={voice}>
                    {voiceName(voice)}
                  </option>
                ))}
              </select>
            </Row>
          ))}
          <Row label="Speaking speed" hint={`${values.tts.speed.toFixed(2)}×`}>
            <input
              type="range"
              min="0.8"
              max="1.5"
              step="0.05"
              defaultValue={values.tts.speed}
              onMouseUp={(e) => change('tts', 'speed', Number((e.target as HTMLInputElement).value))}
              onKeyUp={(e) => change('tts', 'speed', Number((e.target as HTMLInputElement).value))}
            />
          </Row>
          <Row label="Read my typed and clicked calls aloud">
            <Toggle on={values.tts.speak_pilot_text} onChange={(on) => change('tts', 'speak_pilot_text', on)} />
          </Row>

          <h3>Language model</h3>
          {inApp && (
            <Row
              label="Where the model runs"
              hint={
                local
                  ? 'On this computer or network, through Ollama or LM Studio: no key, no cost'
                  : 'OpenRouter, with your own key'
              }
            >
              <select value={values.llm.provider} onChange={(e) => change('llm', 'provider', e.target.value)}>
                <option value="openrouter">OpenRouter</option>
                <option value="local">Local model</option>
              </select>
            </Row>
          )}
          {inApp && local && (
            <>
              <Row label="Local model" hint="As the local server names it, for example llama3.1 or qwen2.5:7b">
                <input
                  value={localModel}
                  spellCheck={false}
                  onChange={(e) => setLocalModel(e.target.value)}
                  onBlur={() =>
                    localModel.trim() &&
                    localModel !== values.llm.local_model &&
                    change('llm', 'local_model', localModel.trim())
                  }
                />
              </Row>
              <Row label="Address" hint="An OpenAI-compatible chat address">
                <input
                  value={localUrl}
                  spellCheck={false}
                  onChange={(e) => setLocalUrl(e.target.value)}
                  onBlur={() =>
                    localUrl.trim() && localUrl !== values.llm.local_url && change('llm', 'local_url', localUrl.trim())
                  }
                />
              </Row>
            </>
          )}
          {inApp && !local && (
            <>
              <Row label="OpenRouter model" hint="Used to understand what you say">
                <input
                  value={model}
                  spellCheck={false}
                  onChange={(e) => setModel(e.target.value)}
                  onBlur={() => model.trim() && model !== values.llm.model && change('llm', 'model', model.trim())}
                />
              </Row>
              <Row label="OpenRouter key" hint={engine.status.has_key ? 'A key is saved' : 'No key yet'}>
                <button onClick={onKey}>{engine.status.has_key ? 'Replace…' : 'Add…'}</button>
              </Row>
            </>
          )}
          <Row
            label="Spent this session"
            hint={`${cost.calls} requests · ${(cost.prompt_tokens + cost.completion_tokens).toLocaleString('en-US')} tokens`}
          >
            <span className="mono">${cost.cost.toFixed(4)}</span>
            <button className="ghost" onClick={() => send({ cmd: 'reset_cost' })}>
              Reset
            </button>
          </Row>

          {inApp && remote && (
            <>
              <h3>Phone or tablet</h3>
              <Row
                label="Show this screen on another device"
                hint={
                  remote.available
                    ? 'On the same network only. Windows may ask to allow the connection the first time'
                    : 'Available in the installed app'
                }
              >
                <Toggle on={values.remote.enabled} onChange={(on) => change('remote', 'enabled', on)} />
              </Row>
              {remote.enabled && remote.running && (
                <Row label="Open in the device's browser" hint="Then enter the pairing code">
                  <span className="mono remote-url">{remote.url}</span>
                  <span className="mono remote-code">{remote.code}</span>
                </Row>
              )}
            </>
          )}

          {engine.computer && engine.computer.length > 0 && (
            <>
              <h3>This computer</h3>
              <ComputerChecklist checks={engine.computer} />
            </>
          )}
        </div>
      </div>
    </div>
  )
}
