# Development

The engine is Python (`src/proatc`, managed with [uv](https://docs.astral.sh/uv/)); the window is Electron, React and TypeScript (`src/main`, `src/renderer`). They talk over a local WebSocket protected by a per-launch token.

## Desktop app

An Electron window on top of the engine. It starts the engine itself and shows the tuned station, your flight, the airport's ATIS summary and frequencies, the conversation, and the calls you can make right now as buttons. You can speak (hold the push-to-talk key or the on-screen button), click a suggested call, or type one.

```bash
npm install
```

```bash
npm run dev
```

`npm run check` runs format, lint, type-check and tests for the app; `npm run build` builds it. The app talks to the engine over a local WebSocket protected by a per-launch token (`src/proatc/server.py`, `src/shared/protocol.ts`).

## Installer and releases

```bash
npm run dist
```

builds `dist/ProATC-<version>-windows-x64-setup.exe`. The installer is small because it does not contain Python or the speech libraries. It carries the engine's source, its lock file and a copy of `uv` (downloaded by `scripts/fetch-uv.mjs` and checked against a pinned checksum). On first launch the app uses that `uv` to build the engine's Python environment from the lock file, then downloads the speech models. That first launch needs an internet connection and several gigabytes of disk; later launches start in seconds. Everything the installed app stores (environment, models, airport index, your key) is under `%APPDATA%\Pro ATC`.

`npm run pack:dir` builds the unpacked app and `npm run smoke` runs its self-test: environment built from the lock file, engine started, every dependency loaded, window connected. CI runs both on Windows for every change.

To release: `npm run version:set -- 0.6.0`, `uv lock`, commit, then push a tag `v0.6.0`. The release workflow runs the checks, builds the installer, self-tests the packaged app and attaches the installer to a draft GitHub release for you to review and publish. Running the workflow by hand from the Actions tab is a rehearsal: nothing is published and the installer is kept as an artifact for three days.

The installer is not code-signed, so Windows SmartScreen warns the first time it runs.

Updates: the installed app checks GitHub releases when it starts and every six hours, downloads a newer version in the background, and shows "Restart to update" in the header when it is ready; clicking the version number checks on demand (`src/main/updates.ts`). The release workflow uploads `latest.yml` and the installer's blockmap for this.

Brand assets: `uvx --with pillow python scripts/make-icons.py` redraws the app icon (`build/`) and the in-app logo.

## Setup

```bash
uv sync
```

Speech-to-text runs on an NVIDIA graphics card when one is present (about 0.3 s per transmission on an RTX 4070, using roughly 1 GB of video memory) and falls back to the CPU otherwise, which measured 2-6 s while the sim was running. Set `stt.device = "cpu"` in `config.toml` to keep the video memory for the sim.

```bash
uv run proatc setup-models
```

Then copy `.env.example` to `.env` and paste your OpenRouter key. Without a key the app runs in echo mode (it reads your words back), which is enough to test the audio chain.

## Commands

| Command | What it does |
|---|---|
| `uv run proatc check` | Connects to the running sim and prints aircraft state (read-only) |
| `uv run proatc devices` | Lists microphones and speakers |
| `uv run proatc say "text"` | Speaks a sentence through the radio effect |
| `uv run proatc airport EGGW` | Shows an airport's runways and frequencies from the sim's data |
| `uv run proatc atis EGGW --say` | Prints and speaks the ATIS (live sim weather, or sample weather if the sim is off) |
| `uv run proatc keytest` | Shows key names, to choose a push-to-talk key |
| `uv run proatc run` | Starts the push-to-talk loop (default key: right Ctrl) |
| `uv run pytest` | Unit tests |

Most settings are changed in the app's settings screen and remembered in `settings.json`. `config.toml` (copy `config.example.toml`) sets defaults and the few things the app does not offer, such as the speech-to-text model.

## Layout

- `src/proatc/xpbridge.py`: X-Plane Web API client (REST lookup, WebSocket stream, and the COM frequency write)
- `src/proatc/voice_loop.py`: push-to-talk, transcription, controller reply, speech
- `src/proatc/navdata.py`: `apt.dat` parser and airport/frequency index
- `src/proatc/weather.py`, `atis.py`, `phraseology.py`: surface weather, ATIS text, radio pronunciation
- `src/proatc/engine.py`, `ifr.py`, `vectors.py`: the rules engine, the instrument-flight logic and radar vectors
- `src/proatc/procedures.py`, `centers.py`, `flightplan.py`, `taxi.py`: SIDs/STARs/approaches, en-route centres, the filed plan, taxi routes
- `src/proatc/radio.py`: station on the tuned frequency, controller briefing, ATIS broadcast thread
- `src/proatc/stt.py`, `tts.py`, `radio_fx.py`, `audio_io.py`, `ptt.py`: audio chain
- `src/proatc/llm.py`: OpenRouter or local-model client and the free-form controller prompt
- `src/proatc/vfr.py`, `airspace.py`: visual flying, and control zones and restricted areas from the sim's airspace file
- `src/proatc/voices.py`, `chatter.py`, `debrief.py`: a voice per controller, other aircraft on the frequency, the post-flight review
- `src/proatc/server.py`: the app's WebSocket, and the page and pairing for a phone or tablet

The app reads from the sim and changes one thing only, when you ask it to: clicking a frequency in the app, or the "Tune" button that appears after a hand-off, sets that frequency on the COM radio you transmit on. Set `allow_tuning = false` under `[sim]` in `config.toml` to switch that off. Some add-on aircraft manage their own radios; if the aircraft does not take the frequency the app says so.

A flight that starts on the runway (as X-Plane places them by default) gets its clearance from tower; from a stand, tower and ground send you to delivery.
