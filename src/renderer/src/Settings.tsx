import { useState } from 'react'
import type { Command, EngineState } from '../../shared/protocol'

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

export function SettingsDialog({
  engine,
  send,
  onClose,
  onKey
}: {
  engine: EngineState
  send: Send
  onClose: () => void
  onKey: () => void
}) {
  const { values, options, capturing } = engine.settings
  const [model, setModel] = useState(values.llm.model)
  const change = (section: string, key: string, value: unknown): void =>
    send({ cmd: 'settings', changes: { [section]: { [key]: value } } })
  const device = (value: string): number | null => (value === '' ? null : Number(value))
  const cost = engine.cost

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
          <Row label="Other traffic" hint="Traffic calls and runway sequencing against X-Plane's AI aircraft">
            <Toggle on={values.atc.traffic} onChange={(on) => change('atc', 'traffic', on)} />
          </Row>

          <h3>Voices</h3>
          {(
            [
              ['voice', 'Controllers'],
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
          <Row
            label="Spent this session"
            hint={`${cost.calls} requests · ${(cost.prompt_tokens + cost.completion_tokens).toLocaleString('en-US')} tokens`}
          >
            <span className="mono">${cost.cost.toFixed(4)}</span>
            <button className="ghost" onClick={() => send({ cmd: 'reset_cost' })}>
              Reset
            </button>
          </Row>
        </div>
      </div>
    </div>
  )
}
