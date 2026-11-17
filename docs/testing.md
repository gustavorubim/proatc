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

```bash
uv run proatc verify-flights --full --flights 300
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
| Gate to gate | `tests/test_world_full.py`, `proatc verify-flights --full` | Whole flights from stand to stand: clearance, taxi and crossings, takeoff, vectors to final, landing, the taxi in, every call from the suggested ones | That a person flies like the model pilot does |
| Real-data sweep | `proatc verify-data` | The engine's main paths at hundreds of real airports raise nothing and read right | Needs X-Plane; not in CI |
| Log replay | `proatc replay flight-….json` | A saved flight gives the same answers again, or shows exactly which changed | Timing between calls |
| Benchmarks | `proatc bench-speech`, `proatc bench-intents` | Numbers for hearing and understanding, to compare before and after a change | How well it hears a real person |
| App tests | `npm run test` | The window's state for each engine message; launcher and updater decisions | The window itself |
| Packaged self-test | `npm run smoke` | The built app sets up its engine, loads every library and connects its window | A microphone, a joystick, a running sim |

Nothing here flies the sim. A change to behaviour in flight is not verified until someone has flown it; say so in the pull request.

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

### Gate to gate

`fly` starts in the climb and stops at the destination controller's first instruction. `flights.fly_full` flies the whole flight, from a stand at the origin to a stand at the destination, as a competent pilot with a copilot: the engine's suggested calls are the menu, every instruction is read back with the "Read back" call, every hand-off is tuned and checked in on, and the aircraft does what it is told. It asks for the IFR clearance, push and start, and taxi; taxis along the route the controller gave, stopping short of each runway to be crossed until it is cleared; lines up and takes off when cleared; flies the SID's track, climbs to each cleared level, follows the STAR once it is given, flies each heading and descent approach gives, intercepts final or flies an R NAV approach from its initial fix, contacts the tower, lands when cleared, vacates, and taxis to the stand it is given. Without an approach controller it flies its own approach to the runway the tower gave and reports final. The aircraft is a simple model: it moves along its heading at a plausible speed, turns at rate one and climbs or descends toward its cleared level.

A flight has a problem, named with the stage of the flight it happened in and the last radio work, when:

- an exception is raised, or a controller's sentence or a suggested call matches `BAD_TEXT`;
- a suggested call is not answered (the app would fall back to the language model), or no call moves the flight on;
- a correct readback is refused, or the controller says the pilot erred (a level bust, a heading not flown, a runway incursion, a low altitude alert, off the route) although it did as it was told;
- it is handed to a frequency nobody answers on, or another controller answers there, or it is left on a silent one in a centre's airspace;
- it is vectored to another airport, to a runway that is not there, or the vectors never bring it to final (more than 12 headings, or 150 miles with the destination's controllers);
- it is never handed to the destination's controller, or not to the tower by four miles out;
- it lands without a landing clearance, is cleared to land on another runway than the one it was vectored to, is not told to vacate, or cannot land (over the threshold high or off the centreline);
- a clearance is below the airport, there is no taxi route, a route does not reach its runway or stand, or the flight does not end parked.

```bash
uv run proatc verify-flights --full --flights 300
```

```bash
uv run proatc verify-flights --full --show KBOS-KIAD
```

Both ends of each flight have a tower that controls traffic (not an information service). In CI, `tests/test_world_full.py` flies 24 flights gate to gate across test_world's made-up world (about 15 seconds), with taxiway networks and stands added to half its towered airports, a short runway that the way to the main one crosses, and published procedures at one airport. `PROATC_STRESS=1` flies every pair, 278 flights in about three minutes. `tests/test_gate.py` holds a test for each fault below.

When this was first run, nearly nine flights in ten had a problem (the table below). The engine faults it found, all fixed as general rules:

- a tower with no ground controller offered "request push and start" and did not answer it;
- on the ground, a departure or approach controller that shares the tower's frequency answered a parked aircraft; in the air, a flight sent to the tower, the departure controller, a centre or another centre on a shared frequency was answered by whoever else works it;
- an information service listed as a tower ("Landshut Info") was called "Tower", and the flight was handed to it instead of the airport's real tower;
- the initial climb was below the runway at a high airport (5,000 ft at an airport 5,500 ft high);
- a tower or departure controller whose airport no centre covers never handed a departure on (now to the centre ahead); a tower handed an arrival to a centre as if it were a departure; an oceanic sector never gave a descent, so flights reached their destination in the cruise;
- runways the taxiways do not reach were given for departure; taxiways that meet only on a runway had no route at all (now along the runway, with a clearance as for a crossing); where two runways meet on a route only one was a crossing; stands far from any taxiway were assigned; taxiways the scenery names "None" were spoken, and stand names with doubled spaces read wrong;
- a level bust and a low altitude alert were called on the glide path, after the approach clearance;
- a base leg that drifted in, and a downwind leg flown wide beyond thirty miles, never got the next vector; a dogleg was given from far to the side, through the centreline past the runway;
- the approach handed a flight to the tower abeam the airport forty miles out, where the tower is out of range;
- an R NAV approach was cleared via an initial fix on the far side of the airport, or raised from its published altitude to the sector altitude of a mountain behind the airport; a waypoint with digits ("AA690") was read as a word;
- the tower cleared a flight to land on the runway in use when approach had brought it straight in to the other end;
- procedure data whose altitudes do not fit the airport's elevation put the approach below the field.

| Run (0.14 data, real X-Plane, `verify-flights --full`) | Flights with a problem |
|---|---|
| `--flights 100`, before the engine fixes (the model pilot as it is now) | 87 |
| `--flights 100`, after | 3 |
| `--flights 300`, after | 5 |
| 1,000 other flights (`pick_flights(db, 1000, seed=2, towered=True)`), after | 16 (27 before the last round of fixes) |

A flight stops at its first serious problem, so the "before" count hides faults behind the first one. What is left, not fixed: stands that no taxiway comes within 300 to 1,400 metres of, or taxiways that reach no runway at all (KADW, KCSG), which is the scenery's data; arrivals that reach their destination too high to land, over high ground (Himalaya, Alps: the vectoring altitude is the highest sector altitude within 25 miles) or after a stretch with no centre in the sim's data; a few flights left on a silent frequency where centres' boundaries leave a gap; and a flight across the date line near the pole whose destination tower is not found on the far side (as in `fly`). The model pilot does not fly holds, circling, missed approaches or speed restrictions on the STAR; it flies with no wind and no other traffic, and takes its next heading, level, approach and runway from the replies and the engine's state rather than from hearing them.

The model pilot had faults of its own, found the same way and fixed in `flights.py`, not counted above: it checked in on an oceanic frequency with "Check in" instead of a position report, vacated onto another runway's surface, took the far side of the airport for final, never descended where no controller covers the sky, and could not get down from the initial fix of an R NAV approach. The first run, before any of that, had 88 of 100 flights with a problem.

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

1. `npm run version:set -- X.Y.Z`, `uv lock`, commit, push a tag `vX.Y.Z`.
2. The release workflow builds and self-tests the installers for Windows, macOS and Linux into a **draft** release.
3. **Wait until every build job has finished** and all files are attached (three installers, their permanent-name copies, three `.yml` files, the `.zip` and the `.blockmap`). Publishing earlier releases a version with no installer: this has happened once.
4. Publish, then check the three download links in the README.

## Rules for this repository

- It is public. No keys, no personal identifiers, no private notes, no planning documents. Use placeholders in tests.
- Stage files by name. Other work may be going on in the same checkout.
- Work on a branch, open a pull request with a Conventional Commit title, merge when CI passes.
- The language model interprets speech; it never decides what a controller says. Anything operational goes through the rules engine.
