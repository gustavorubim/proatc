# Pro ATC for X-Plane 12

Free voice ATC: your own OpenRouter key for the language model, local speech-to-text (faster-whisper) and local text-to-speech (Kokoro).

**Status: a full instrument flight, gate to gate, without other traffic.**

Pro ATC does not use X-Plane's own ATC. It reads the aircraft's state and the sim's data files, and everything a controller says is decided by its own rules engine. The language model only interprets what the pilot said (`intents.py`); replies are built from templates, so runways, routes, levels, squawks and frequencies are never invented.

With a flight plan loaded (from SimBrief, or a destination and cruise level typed in):

| Step | Controller | What you get |
|---|---|---|
| Clearance | Delivery | Destination, the SID that fits your route and runway, initial altitude from the SID, squawk |
| Push, taxi | Ground | Push and start, a taxi route over the airport's real taxiways |
| Takeoff | Tower | Takeoff clearance with the wind, then a hand-off once airborne |
| Climb | Departure | Identified, step climbs up to FL240 or your cruise level, hand-off to the centre whose airspace you are in |
| Cruise | Centres | Climb to cruise, hand-offs as you cross sector boundaries, step descents starting at the right distance, with the STAR |
| Arrival | Approach | Descent, radar vectors (downwind, base, intercept) with speed control, approach clearance for the best published approach |
| Landing | Tower, Ground | Landing clearance, vacate, taxi to a stand |

Readbacks are checked at every step: a wrong runway, squawk, frequency, pressure, level or heading gets "negative" and the instruction again. Procedures come from X-Plane's CIFP data (`procedures.py`; Navigraph data in `Custom Data` is used when present), centres and their boundaries from `atc.dat` (`centers.py`), the vectoring pattern is in `vectors.py` and the radar controllers' logic in `ifr.py`. VFR circuits are covered too. Anything the engine does not cover falls back to a free-form reply constrained to the same facts.

What the controllers also do: climbs and descents come in steps of about 10,000 ft, the next one as you approach your cleared level; approach gives speed control (220, 180, then 160 knots to four miles) and your range from touchdown with the approach clearance; tower says "continue approach" to an arrival still far out and clears it to land inside four and a half miles; a go-around sends you back to approach for new vectors; a direct routing to a waypoint on your filed route is granted on request. They also watch you: a level bust, an assigned heading not being flown, the wrong squawk on first contact, and an instruction nobody read back (repeated once after 30 seconds). An airline flight number filed in SimBrief (for example BAW123) is spoken as "Speedbird one two three"; otherwise the registration is used.

Arrivals follow the STAR: its level restrictions are issued one by one ("descend flight level two six zero, level by Inpip") at the distance a three degree path needs, and you are told if you cross a waypoint high. Approach can hold you at the published hold at the end of the STAR: always on request, and on its own initiative according to `[atc] holding` in `config.toml` (`never`, the default; `sometimes` for about one arrival in four; `always`); it releases you after the stated delay, or when you say you are ready. Vectors and descents respect the minimum safe altitude around the airport and the minimum off-route altitude of the terrain below (`navfix.py`), and a radar controller calls a low altitude alert if you descend below 1,000 ft above the ground away from the approach.

Weather: while X-Plane is on real weather, each airport's ATIS, runway in use and pressure come from its own METAR (`metar.py`, read from `Output/real weather`), so the destination is right from any distance; with preset or custom weather the sim's weather at the aircraft is used. The ATIS reports gusts, thunderstorms, snow, fog and low visibility procedures, a controller announces a new ATIS to an aircraft on frequency, and you can ask any controller for the wind and pressure, ask a centre for the destination weather, and request a deviation around weather.

Known simplifications: no other traffic, so holds are for delay only and there is no sequencing; vectors follow one fixed pattern; SID level restrictions are not monitored; landing and departures share the runways in use; station names are whatever X-Plane's data calls them.

Phase 2, still in place:

- Airports, runways and frequencies come from X-Plane's own `apt.dat` (indexed once into `data/airports.sqlite`).
- Tune an ATIS frequency within 60 NM and the ATIS plays in a loop, built from the sim's weather: runway in use, wind, visibility, cloud, temperature, QNH or altimeter, information letter.
- A controller answers only when the tuned frequency belongs to a station in range (ground and delivery 10 NM, tower 30 NM, approach and departure 60 NM), and it answers as that station with the real runway in use, wind, pressure and hand-off frequencies.
- Without a sim connection the controller talks freely so the audio chain can be tested.

Not yet: other traffic, separate landing and departure runways, custom scenery airports.

## Desktop app

![Pro ATC window](docs/screenshot.png)

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

To release: `npm run version:set -- 0.2.0`, `uv lock`, commit, then push a tag `v0.2.0`. The release workflow runs the checks, builds the installer, self-tests the packaged app and attaches the installer to a draft GitHub release for you to review and publish. Running the workflow by hand from the Actions tab is a rehearsal: nothing is published and the installer is kept as an artifact for three days.

The installer is not code-signed, so Windows SmartScreen warns the first time it runs.

Updates: the installed app checks GitHub releases when it starts and every six hours, downloads a newer version in the background, and shows "Restart to update" in the header when it is ready; clicking the version number checks on demand (`src/main/updates.ts`). The release workflow uploads `latest.yml` and the installer's blockmap for this. It only works if the release assets can be downloaded without signing in, which means a public repository: while this repository is private the check fails and the app says so.

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

Copy `config.example.toml` to `config.toml` to change the key, devices, models or voice.

## Layout

- `src/proatc/xpbridge.py`: X-Plane Web API client (REST lookup, WebSocket stream, and the COM frequency write)
- `src/proatc/voice_loop.py`: push-to-talk, transcription, controller reply, speech
- `src/proatc/navdata.py`: `apt.dat` parser and airport/frequency index
- `src/proatc/weather.py`, `atis.py`, `phraseology.py`: surface weather, ATIS text, radio pronunciation
- `src/proatc/engine.py`, `ifr.py`, `vectors.py`: the rules engine, the instrument-flight logic and radar vectors
- `src/proatc/procedures.py`, `centers.py`, `flightplan.py`, `taxi.py`: SIDs/STARs/approaches, en-route centres, the filed plan, taxi routes
- `src/proatc/radio.py`: station on the tuned frequency, controller briefing, ATIS broadcast thread
- `src/proatc/stt.py`, `tts.py`, `radio_fx.py`, `audio_io.py`, `ptt.py`: audio chain
- `src/proatc/llm.py`: OpenRouter client and placeholder controller prompt

The app reads from the sim and changes one thing only, when you ask it to: clicking a frequency in the app, or the "Tune" button that appears after a hand-off, sets that frequency on the COM radio you transmit on. Set `allow_tuning = false` under `[sim]` in `config.toml` to switch that off. Some add-on aircraft manage their own radios; if the aircraft does not take the frequency the app says so.

A flight that starts on the runway (as X-Plane places them by default) gets its clearance from tower; from a stand, tower and ground send you to delivery.
