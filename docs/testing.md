# Verifying Pro ATC

How to know a change is right. Written for whoever works on this next, person or agent.

## The short version

Before opening a pull request, run all of this. It takes about two minutes.

```bash
uvx ruff@0.17.5 format --check . && uvx ruff@0.17.5 check .
```

```bash
uv run pytest -q
```

```bash
npm run check
```

If you changed how the app is packaged, started or updated, also:

```bash
npm run pack:dir && npm run smoke
```

If you changed anything a controller says or decides, and X-Plane is installed on the machine, also:

```bash
uv run proatc verify-data --airports 300
```

```bash
uv run proatc verify-flights --flights 300
```

CI runs the first three on Windows, macOS and Linux, and the packaged-app self-test on all three.

## What each layer is for

| Layer | Where | What it proves | What it cannot |
|---|---|---|---|
| Unit tests | `tests/test_*.py` | One rule gives one answer, on small invented airports | That the rules fit together over a flight |
| Golden flights | `tests/test_golden.py`, `tests/golden/*.txt` | Whole flights go the same way, word for word, as the saved transcript | That the transcript was right when it was saved: read it |
| Regressions from review | `tests/test_review.py` | Defects an outside review found stay fixed | |
| Fuzz and stress | `tests/test_robust.py` | Random calls in random states never raise, never produce a sentence with a hole in it; garbage in data files is skipped; six threads do not deadlock; a tick stays under 5 ms | That any answer is correct |
| Flights across a made-up world | `tests/test_world.py` | Whole flights are handed all the way to their destination, through zigzag boundaries, shared frequencies, cities with several airports, airports without approach, a coverage hole and an ocean | That the sim's real data has no surprise of another kind |
| Real flights worldwide | `proatc verify-flights` | The same, between hundreds of real airport pairs on this machine's X-Plane data | Needs X-Plane; not in CI |
| Real-data sweep | `proatc verify-data` | The engine's main paths at hundreds of real airports raise nothing and read right | Needs X-Plane; not in CI |
| Log replay | `proatc replay flight-….json` | A saved flight gives the same answers again, or shows exactly which changed | Timing closer than the twenty-second trace |
| Kept flights | `proatc journals check` | Real flights kept on this machine still go the way a person last accepted | Needs X-Plane and kept journals; not in CI |
| Benchmarks | `proatc bench-speech`, `proatc bench-intents` | Numbers for hearing and understanding, to compare before and after a change | How well it hears a real person |
| App tests | `npm run test` | The window's state for each engine message; launcher and updater decisions | The window itself |
| Packaged self-test | `npm run smoke` | The built app sets up its engine, loads every library and connects its window | A microphone, a joystick, a running sim |

Nothing here flies the sim. A change to behaviour in flight is not verified until someone has flown it; say so in the pull request.

## What only a person can check

Some things no test can hear: two radios sounding at once on a given sound card, a voice engine keeping up on a slow processor, a microphone that is really heard, the Mac and Linux builds in use. The **sound check** (Settings, or `proatc sound-check`) plays each in turn and asks the person a yes/no question after each:

| Step | What it plays | What it asks |
|---|---|---|
| 1 | A controller's sentence on the first radio | Did you hear it? |
| 2 | A weather recording on the second radio (`SecondRadio.broadcast`, on its own `Player(alongside=True)` stream) and the controller over it | Did you hear both at once? |
| 3… | The controller in each voice engine that loads (Kokoro; Phonon if installed) | Was it clear, without breaks? |
| last | Two seconds from the microphone, with the level shown | Did you speak while it recorded? |

Next to each answer it records what the machine can measure without ears: that each output stream opened, how many frames were handed to the sound card, the underruns PortAudio reported (the underflow flag `OutputStream.write` returns; `sd.get_status()` after a whole sentence), the output and input device names, the time from the reply being known to the first sound, how many times faster than real time the speech was made, and whether a piece of speech arrived after the sound before it had run out. A step is a problem when the machine saw a failure or a gap, or the person said no.

The outcome goes into the journal as one `sound_check` note (the prompts are not kept) and into `logs/sound-check.json`, and a one-line verdict is shown in the window. The check never starts by itself, and is refused while the sim is connected and the aircraft is not parked on the ground, or while a controller is talking. The tests (`tests/test_sound_check.py`) stand in for the sound card: nothing in the suite plays a sound or records one, so a passing suite says nothing about how it sounds.

**This computer** in the settings lists what can be checked without the person: whether PortAudio loaded, whether there are speakers and a microphone, and whether the push-to-talk key can be heard (on macOS, the Input Monitoring permission, asked with `IOHIDCheckAccess`; on Linux, whether the session is Wayland, where it cannot). On macOS and Linux a failed item is also told at start-up. The macOS and Linux branches are tested only with stand-ins for those systems (`tests/test_report.py`).

To get a tester's results, ask for a **problem report** (the header's "Report a problem", or `proatc report`): one zip in `logs/reports/` with the newest journal (and the run before it when the newest is under a minute old), the versions, the settings without secrets, the last sound check and the checks of this computer. It never holds the OpenRouter key, the SimBrief ID or the pairing code: the settings come from the list the settings window shows, and every file is scrubbed of those values on its way in. "Hide my callsign and positions" masks the callsign and rounds coordinates to one decimal in the copy; the journal on disk is untouched.

## Golden flights

`tests/golden/*.txt` are transcripts of scripted flights: every call, every answer, and the flight's state after each. A change that alters what a controller says fails the test with a diff.

When the change is intended:

1. Read the diff. It is the clearest description of what your change does to a flight.
2. Regenerate: `PROATC_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` (PowerShell: `$env:PROATC_UPDATE_GOLDEN=1`).
3. Read the new transcript as a pilot would. Twice already, reading one found a real fault that no assertion had caught.
4. Commit the transcripts with the code.

Add a scenario when you add a phase of flight. Keep scripts short: positions and calls, no assertions.

## A bug report from a real flight

The app's "Save log" writes a `.txt` transcript and a `.json` file. From 0.8 the `.json` holds, for every call, what the pilot was understood to say and the sim's state, plus the engine's seed.

```bash
uv run proatc replay path/to/flight-20261116-201500.json
```

On unchanged code this prints no differences. To fix a reported fault: replay the log, see the wrong answer, change the code, replay again, and see that answer (and only that answer) change. Then add a unit test or a golden step for it.

### The journal: every flight is on disk

From 0.11 nobody has to remember to save a log. Each time the app runs it writes a journal, a line per event as it happens, to `logs/journal/session-YYYYMMDD-HHMMSS.jsonl` in its data folder:

| System | Data folder |
|---|---|
| Windows | `%APPDATA%\Pro ATC\engine` |
| macOS | `~/Library/Application Support/Pro ATC/engine` |
| Linux | `~/.config/Pro ATC/engine` |
| From source | the project folder |

It holds everything "Save log" does, plus a `session` line (version, seed, model), the `plan` whenever it changes, and a `trace` every twenty seconds while something changes: position, tuned frequency, which station answers on it (or none), and the flight's state. The last 40 runs are kept. Nothing leaves the computer; there is no key and no audio in it.

To look into a report: open the newest journal, find the time, and read the `trace` lines around it. A frequency with `"station": null` is one nobody answers on. Then replay it:

```bash
uv run proatc replay "path/to/session-20261117-201500.jsonl"
```

## Kept flights

A journal is a whole real flight. Keeping it turns it into a regression test: before a release, every kept flight is replayed and its answers compared with the ones a person last accepted for it.

**Keep one** after a flight worth keeping (a fault was found in it, or it went through something new):

```bash
uv run proatc journals keep
```

It copies the newest journal (or the one named: `proatc journals keep path/to/session-….jsonl`) into `logs/regression/` in the data folder, names it by date and route (`2026-11-17-1008-EGLL-LFPG`), and accepts what the engine answers to it now. Beside each kept journal, `<name>.accepted.json` lists every replayed answer: where in the journal, what the pilot said, what the controller says now, and what it said in the real flight where that differs. `proatc journals list` shows what is kept.

**Check before a release:**

```bash
uv run proatc journals check
```

For each kept flight it prints the counts (calls replayed, unprompted calls replayed, calls that could not be, moments the controller could speak, new unprompted calls) and every answer that changed since it was accepted, with the time into the flight, the frequency and the position. It exits 1 if anything changed.

**What a change means.** Nothing changed: the release does not alter these flights. A change is either a fault or the intended effect of a change to the code. Read each one as a pilot would. A fixed fault changes the answers after it too (the flight goes another way from there), so expect a run of changes after the first. When every change is intended:

```bash
uv run proatc journals accept 2026-11-17-1008-EGLL-LFPG   # or --all
```

and say in the pull request what changed and why.

**It stays on this computer.** A journal holds the pilot's callsign and every position of the flight. The corpus lives in the data folder, never in the repository; do not commit a journal, an accepted file or a line of either. To paste a result into a public issue, use `proatc journals check --anonymous`: it masks the callsign and rounds positions to a tenth of a degree. Read it before pasting all the same: a callsign the pilot spoke in an unusual way can survive the mask.

**What a replay reproduces, and what it cannot.**

- The seed, so squawk codes, stands and delays are the same; the plan, at the points it was loaded or changed; the engine's clock, run on the journal's times and stopped while the sim was paused, so a readback reminder runs out where it did.
- The controller may speak unprompted at every record that holds the sim's state, the trace above all. A call it now makes where none was logged, or no longer makes, is reported. The app looks every half second and the trace every twenty, so an unprompted call made now up to thirty seconds before or after the logged one is taken as the same call (`shifted` in the counts), not reported.
- A readback the copilot or the "Read back" button made is made again from what the engine now expects, as they would. What the pilot said or typed is replayed as said.
- The station lookup's memory of who answers on each frequency is cleared before each replay.
- Not reproduced: the reported weather (the journal does not hold the METAR, and X-Plane's METAR file changes daily, so kept flights use the sim's weather at the aircraft for every airport); other aircraft between calls (the trace leaves them out); anything the language model said. The holding and traffic settings are the ones the flight was kept with.
- A kept flight depends on this machine's X-Plane data. After an X-Plane or navigation data update, expect changes; read them, then accept.
### Health report

Before reading a journal line by line, let it be read for you:

```bash
uv run proatc health
```

```bash
uv run proatc health "path/to/session-20261117-201500.jsonl"
```

Without a path it takes the newest journal, from the project folder or the installed app's data folder. It prints what went wrong on the radio's side of the flight, grouped, with the time, the frequency, the stations and the position for each, and exits with 1 when there is anything to report. The app shows the same list under "Flight health" in the debrief, and writes it into the journal as a `health` line.

What it looks for (`src/proatc/health.py`):

| Finding | What it means |
|---|---|
| Nobody answered on the tuned frequency | Airborne for over a minute on a frequency with `"station": null` |
| Controller had no answer | The free-form fallback said "unable, say again your request", "say intentions" or "standby" (new journals mark those replies `"fallback": true`; older ones are read by their words) |
| Station changed on the same frequency | Mid-conversation and airborne, a different station started answering on the same frequency |
| Hand-off not followed | "Contact X on 128.525" in flight, and 128.525 was not tuned within two minutes |
| Hand-offs close together | Two hand-offs under thirty seconds apart |
| Readback trouble | Three or more corrected or repeated readbacks, or three or more "say again" |
| Never reached approach or tower | Landed without talking to an approach or tower controller near the destination |
| Approach reached late | The first call to approach was inside 15 NM of the destination |
| Errors | Errors the app reported during the flight |

Each was found on a real flight. A false alarm costs more than a missed fault, so these are not reported: a parked aircraft, a paused sim, the time the app was closed, an ATIS frequency, the seconds between a hand-off and its tuning, a hand-off on the ground, a station that changes when nobody is talking on the frequency. Distance to the destination needs X-Plane's airport data; without it, it is judged from where the aircraft landed.

When a new kind of fault is found in a journal, add a detector and a test for it, including one for the ordinary flight it must stay quiet about.

## Stress tests: whole flights

Unit tests hand the engine the station they mean. A real flight does not: the station is whatever answers on the tuned frequency where the aircraft is. Every serious fault found in flight so far lived in that gap: a centre that fell silent at its boundary, a neighbouring airport's approach controller answering and giving vectors to the wrong runway, two hand-offs seconds apart, runways whose approach was never found. `flights.fly` flies a flight the way the radio loop and a copilot do, and a flight has a problem when it is:

- left on a frequency nobody answers on, in airspace that has a centre;
- sent to a frequency nobody answers on;
- vectored to an airport that is not its destination;
- bounced between the same controllers;
- sent to another centre a few miles from the airport;
- never handed to its destination's controller, or not given an instruction by it.

Two things use it.

**In CI, on every change:** `tests/test_world.py` builds a world from nothing with the things that broke real flights, and flies 90 flights across it (17 seconds). `PROATC_STRESS=1 uv run pytest tests/test_world.py` flies every pair, over 400 flights. Both must have no problems. When a new kind of fault is found in flight, add what caused it to that world.

**Before every release, on a machine with X-Plane:**

```bash
uv run proatc verify-flights --flights 1000
```

It prints the problems grouped by kind with example flights. To see one flight's radio work:

```bash
uv run proatc verify-flights --show KBOS-KIAD
```

When this was first run, 180 of 300 flights had a problem. The rules that fixed them are general, not per airport: a station has a ceiling as well as a range; the station being talked to keeps answering; a flight that is handed to a station is answered by that station; the next centre is given before the boundary and not for a clipped corner, nor straight back, nor twice in twenty miles; an airport without approach is handed over inside its tower's range; a sector across the date line does not cover the world. A release that makes the numbers below worse is not ready.

| Run (0.14, real X-Plane data) | Flights with a problem |
|---|---|
| `verify-flights --flights 300`, before the rules | 180 |
| `verify-flights --flights 300`, after | 1 |
| 1,000 other flights, after | 2 |

The ones left: a route across the date line near the pole (Siberia to Alaska), and one where two entries for the same centre in the sim's data overlap. Both recover later in the flight.

## Fuzz tests

Every random choice is seeded. A failure message names the seed and the step (`seed 137 step 22 Westfield Tower`); `one_flight(make, stations, 137)` in `tests/test_robust.py` reproduces it. Do not loosen `BAD_TEXT` in `src/proatc/verify.py` to make a failure pass: it lists what a controller's sentence must never contain.

## The real-data sweep

`proatc verify-data` runs clearances, taxi routes, procedures, arrivals, VFR calls and chatter at the airports with the most frequencies and a random sample of the rest. It reports exceptions and sentences that read wrong, grouped, with example airports. Last full run: 400 airports, about 80,000 checks in under a minute, no failures.

## Benchmarks

Not run in CI: they need the speech models or a language model.

- `proatc bench-speech` speaks 25 radio calls in four voices, clean and through the radio effect with two levels of noise, and measures word errors and whether the numbers survive. `--quick` runs a third of it.
- `proatc bench-intents` runs 60 transcripts through the configured model and checks the intent and every value. It costs a fraction of a cent on OpenRouter.

Baseline on 0.8 (Windows, NVIDIA graphics card, `small.en`, default model `anthropic/claude-haiku-5.5`):

| Benchmark | Result |
|---|---|
| `bench-speech`, 300 calls | word errors 8.3%; numbers right in 284 of 300 calls (95%); 0.15 s a call |
| `bench-speech`, by condition | clean 8.0%, radio effect 7.5%, noisy radio 9.3% word errors |
| `bench-intents`, 61 transcripts | 60 understood exactly (98%); the miss was a blind call taken for a position report, which the engine treats the same way |

The two voice engines, compared on 0.9 (same machine; `proatc bench-speech --engine kokoro` and `--engine phonon`):

| | Kokoro | Phonon (trial) |
|---|---|---|
| Wait from the reply being known to the first sound, 16 calls | median 1.5 s, longest 6.0 s | median 0.25 s, longest 1.9 s |
| Word errors when heard back, 300 calls | 8.0% | 9.0% |
| Calls with every number right | 282 of 300 (94%) | 276 of 300 (92%) |

Phonon made 1 of 16 calls more slowly than it is spoken, on a 24-thread processor: on a slower one its speech may break up. Nobody has listened to either result: the clarity figures are speech recognition hearing the voices back.

Run the relevant one before and after changing `stt.py`, the recognition prompt, `intents.py` or the default model, and put both results in the pull request.

## Releasing

1. `uv run proatc journals check`: no kept flight changes, or every change has been read and accepted (see "Kept flights").
2. `npm run version:set -- X.Y.Z`, `uv lock`, commit, push a tag `vX.Y.Z`.
3. The release workflow builds and self-tests the installers for Windows, macOS and Linux into a **draft** release.
4. **Wait until every build job has finished** and all files are attached (three installers, their permanent-name copies, three `.yml` files, the `.zip` and the `.blockmap`). Publishing earlier releases a version with no installer: this has happened once.
5. Publish, then check the three download links in the README.

## Rules for this repository

- It is public. No keys, no personal identifiers, no private notes, no planning documents. Use placeholders in tests.
- Stage files by name. Other work may be going on in the same checkout.
- Work on a branch, open a pull request with a Conventional Commit title, merge when CI passes.
- The language model interprets speech; it never decides what a controller says. Anything operational goes through the rules engine.
