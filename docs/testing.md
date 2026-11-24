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
| Timing tests | `tests/test_session_timing.py`, `tests/sim.py` | The session's job loop, the ATIS broadcaster and the second radio, run together: hand-offs, readbacks and tuning end right when they overlap | Real audio devices, a real sim's tuning delays |
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

### Gate to gate

`fly` starts in the climb and stops at the destination controller's first instruction. `flights.fly_full` flies the whole flight, from a stand at the origin to a stand at the destination, as a competent pilot with a copilot: the engine's suggested calls are the menu, every instruction is read back with the "Read back" call, every hand-off is tuned and checked in on, and the aircraft does what it is told. It asks for the IFR clearance, push and start, and taxi; taxis along the route the controller gave, stopping short of every runway on the way until it is cleared to cross; lines up at the end of the route and takes off when cleared; flies the SID's track, climbs to each cleared level, flies each heading and descent approach gives, intercepts final or flies an R NAV approach from its initial fix, contacts the tower, lands when cleared, vacates onto the taxiways, and taxis to the stand it is given. Without an approach controller it flies its own approach to the runway the tower gave and reports final. The aircraft is a simple model: it moves along its heading at a plausible speed, turns at rate one and climbs or descends toward its cleared level.

The pilot cannot hide a fault of the engine:

- it acts only on what it hears: headings, levels, speeds, the approach and its runway, the circling runway, the landing clearance and every frequency come from the controllers' words and readback items, never from the engine's state; it reads charts only for what a pilot has them for (the taxi route on the map, an approach's fixes, the minimum safe altitudes);
- it tunes by frequency, from what it was told or from the chart, and never tells the engine who should answer;
- it stops at every runway its route goes onto unless it was cleared to cross it, and does not move the aircraft by magic (only circling to another runway is not flown);
- every descent is checked against the minimum safe altitude where the aircraft is (the sector altitude within 25 miles of the airport, or an altitude an approach to the runway publishes; the terrain below further out), and every intercept against where it joins final and how high above a three degree path;
- each flight has one of six winds, light to a 30 knot crosswind, and one in three has a different wind from the middle of the flight on;
- it stops at the first fault in a hand-off rather than finding its own way.

A flight has a problem, named with the stage of the flight it happened in and the last radio work, when:

- an exception is raised, or a controller's sentence or a suggested call matches `BAD_TEXT`;
- a suggested call is not answered (the app would fall back to the language model), or no call moves the flight on;
- a correct readback is refused, or the controller says the pilot erred (a level bust, a heading not flown, a runway incursion, a low altitude alert, off the route) although it did as it was told;
- it is handed to a frequency nobody answers on, or another controller answers there, or it is left on a silent one in a centre's airspace;
- it is vectored to another airport, cleared below the minimum safe altitude, given an intercept that joins final too close or too high, or the vectors never bring it to final (more than 12 headings, or 150 miles with the destination's controllers);
- it is never handed to the destination's controller, or not to the tower by four miles out;
- it lands without a landing clearance, is cleared to land on another runway than the one its approach clearance named, is not told to vacate, or cannot land (over the threshold high or off the centreline);
- there is no taxi route, a route crosses a runway the controller did not name, a route does not reach its runway or stand, or the flight does not end parked.

```bash
uv run proatc verify-flights --full --flights 300
```

```bash
uv run proatc verify-flights --full --show KBOS-KIAD
```

Both ends of each flight have a tower that controls traffic (not an information service). In CI, `tests/test_world_full.py` flies 24 flights gate to gate across test_world's made-up world (about 20 seconds), with taxiway networks and stands added to half its towered airports, a short runway that the way to the main one crosses, and published procedures at one airport. `PROATC_STRESS=1` flies every pair, 278 flights. `tests/test_gate.py` holds a test for each fault below.

Engine faults found, all fixed as general rules:

- a tower with no ground controller offered "request push and start" and did not answer it;
- on the ground, a departure or approach controller that shares the tower's frequency answered a parked aircraft; in the air, a flight sent to the tower, the departure controller, a centre or another centre on a shared frequency was answered by whoever else works it; who a flight was sent to was not forgotten when a new flight began;
- "Landshut Info" was called "Tower"; "BREMEN INFO", listed at an airport, is Bremen Information, and flights are handed to the airport's real tower;
- the initial climb was below the runway at a high airport (5,000 ft at an airport 5,500 ft high);
- a tower or departure controller whose airport no centre covers never handed a departure on (now to the centre ahead on the way to the destination, never one the climb-out only points at; a centre gives no level outside its airspace); a tower handed an arrival to a centre as if it were a departure; an oceanic sector never gave a descent;
- a runway the scenery's taxi network writes "04R" was not found for the airport's "4R"; a runway the network does not tag was crossed without a clearance; where two runways meet on a route only one was a crossing; with no taxiway to the threshold there was no departure at all (now from an entry in the nearer half of the runway); a route started on the runway the aircraft had just left; a runway the scenery shows no taxiway joining was given for departure; stands no taxiway reaches were assigned; taxiways named "None" and stand names with doubled spaces read wrong;
- base was turned so close in that the 30 degree intercept joined final a mile or two from the runway, far above the glide path; a base leg that drifted in, and a wide downwind beyond thirty miles, never got the next vector; a dogleg was given from far to the side; no vector beyond 25 miles now goes below the terrain there;
- approach handed a flight to the tower abeam the airport forty miles out, or while it was flying away over final toward an initial fix;
- an R NAV approach was cleared via an initial fix on the far side of the airport, or one nobody could get down from; one on the far side that the arrival ends at was then ruled out too, and a pilot on the arrival to Spokane was sent 69 miles round the field to another approach's fix (the R NAV Y's, without saying "Y", when the R NAV Z had been asked for); a waypoint with digits ("AA690") was read as a word; final was joined closer in than the joining altitude allows;
- the approach altitude could be below the field, or, with nothing published, below the sector altitude;
- the tower cleared a flight to land on the runway in use when approach had brought it straight in to the other end;
- at Miami, landing 27 and 30 by its real ATIS, the centre told a flight planned for 30 to expect 27, the first the ATIS named, while approach would have cleared 30;
- the aircraft's altitude was its true height, not its pressure altitude: level at flight level 250 on standard pressure in air ten degrees warmer than standard, a flight was told "check your altitude" and "expedite descent";
- the box asking for the destination of a clearance request, opened before the flight plan had loaded, stayed open once the plan gave the destination and the clearance was given, hiding the calls offered from then on.

Two attempts to fix faults were wrong and were taken back after review: taxi routes along a runway (a backtrack needs its own clearance from the tower, read back and checked against traffic, which is not modelled), and an initial fix cleared at its own published altitude below the sector altitude, up to 45 miles out. An airport whose taxiways reach a runway only along it has no taxi route, as before.

| Run (0.14 data, real X-Plane, `verify-flights --full`, 300 flights) | Flights with a problem |
|---|---|
| `--flights 100`, before the engine fixes (the model pilot as it is now) | 87 |
| `--flights 100`, after | 3 |
| `--flights 300`, after | 5 |
| 1,000 other flights (`pick_flights(db, 1000, seed=2, towered=True)`), after | 16 (27 before the last round of fixes) |
| `--flights 300`, 0.15 as released (after the review's safety fixes and the level-bust fix below) | 70, of which 49 "no taxi route" and 14 "not parked" (no route along a runway without a backtrack clearance, a known limit), 5 too high to land over high ground, 3 stands too far from a taxiway, 1 the date-line flight. No level busts, no hand-off faults. |
| First run, the first model pilot (100 flights) | 88 of 100 |
| First model pilot, after the first engine fixes | 5 |
| First model pilot, after the fixes from review | 77 (49 of them no taxi route once routes along runways were taken back) |
| The pilot that acts only on what it hears, first run | 105 |
| The same pilot, after the engine faults it showed were fixed | 55 |

On 1,000 flights (`verify-flights --full --flights 1000`, the pilot that acts only on what it hears):

| Run | With a problem | No taxi route | No way to land | Intercept too high | Crossing not given |
|---|---|---|---|---|---|
| These fixes, before their review | 218 | 165 | 26 | 15 | 3 |
| After the review's fixes (0.17) | 291 | 251 | 22 | 7 | 1 |
| With backtrack clearances (0.18) | 86 | 33 | 25 | 11 | 2 |
| With the arrival's vertical profile and the runway choice (0.19) | 51 | 21 | 9 | 4 | 2 |

The review's fixes made the count worse on purpose. An intersection departure must now leave the runway the aircraft needs (1,800 m for a jet). The 86 more flights with no taxi route were jets that had been sent off part-way along a short runway with less than that ahead. At a real airport like those, the aircraft enters the runway and backtracks to the threshold; 0.18 clears that ("backtrack runway two seven left, line up and wait"), which took the count from 291 to 86. The 33 departures still without a taxi route are the scenery's: a runway its taxi network leaves out altogether (UAUU 15R, CYUL 28), or one no taxiway touches (the dry-lakebed runways at KEDW). A final may now be joined up to 30 miles out, so an arrival held at a high sector altitude is less often too high. Where the vectors still turn it in at about 20 miles, it is up to 2,500 ft above the path (7 flights, all with a sector altitude well above the airport).

0.19 came from a real flight into Los Angeles: cleared for the ILS thirty miles out at 7,800 ft, 2,400 ft above the glide path, then handed to the tower sixteen miles out 2,700 ft above it. Outside the 25 mile circle of the sector altitudes the joining altitude had been raised to the terrain's floor and never lowered again, and nothing checked that the aircraft could get down. Approach now gives the next descent as the aircraft comes down to the last one, clears the approach only from where a descent of 400 ft a mile reaches the glide path, turns an aircraft out once when it is too high close in, turns a base leg toward final without the clearance beyond the circle and clears it inside, and says so when an aircraft on the localizer is more than 1,000 ft above the path. The first version of this turned an aircraft out again on every look, and called a turn of 180 degrees a heading not flown and a descent on the glide path a level bust; the sweep found each. It also found that the circle had been measured from the runway's threshold, not the airport (a vector at Denver 200 ft below the floor), and that the SID and STAR parts of runways 1 to 9 never matched ("01R" against "1R"). The runway choice: the flight plan's runway is used where the wind allows it (the wind alone had picked another direction at Las Vegas), the ATIS and every controller give the same one, and a grass runway beside a paved one, or a runway no taxiway reaches, is not in use: 12 fewer departures with no taxi route. The 4 intercepts still too high are over high ground (the Alps, the Andes, the Caucasus), where final is joined beyond the sector altitude's circle.

The next flight into Los Angeles, on 0.19.1, was stepped down and then cleared for the ILS thirty miles out at 7,800 ft again: low enough to reach the glide path at that altitude, which is where the 0.19 check stopped. The path is at 7,800 ft twenty-four miles from the runway, where no glideslope is received. It flew level through the path and was 1,600 ft above it by eighteen miles. The approach is no longer cleared from the terrain's floor: the clearance comes inside the sector altitude's circle, with the descent to the approach's own altitude (4,000 ft there, level before the glide path).

The same flight's journal had no record of the controller saying that approach clearance, though the engine had given it. The "Read back" button offered the clearance from the moment it was decided, seconds before its voice was ready (the voice of that clearance takes about three seconds to make and eighteen to say); a readback clicked then settled the instruction, and it was never said. `tests/test_session_timing.py` reproduces it with a slow voice. An instruction is now open to a readback only once it is on the air.

Also found by the 0.18 work, from a kept real flight: 0.17 called a taxiway passing 28 m beyond the end of Boston's runway 4L a crossing ("hold short runway four left", then a runway incursion). A route now crosses a runway only on its pavement, up to 15 m past an end; the test pilot holds short where told.

For comparison, 0.16 as released, flown by the earlier model pilot (which took the runway and the approach from the engine, so it saw fewer faults): 189 of 1,000 with a problem, 166 of them no taxi route. From the climb to the destination's controller (`verify-flights --flights 1000`), 0.16 and 0.17 both have 2 problems in 1,000 flights, both on the same flight across the date line near the pole.

An independent review of the first version of these fixes found that some were unsafe: taxi routes along the departure runway with no clearance for it, an initial approach fix cleared below the minimum safe altitude, altitude alerts silenced however far out once cleared for the approach, intercepts that could not be flown. They were reverted to the safe behaviour. Fixing the alerts then made the controller call a correct descent a level bust 24 times in 300 flights; past the initial fix of a cleared procedure, or on the localizer on a long final at or above the glide path, it no longer does.

The first model pilot hid faults: it took the approach and the runway from the engine, tuned with the engine's own hand-off lookup, teleported to the threshold and from the runway, flew in one wind, and found its own way after a hand-off fault. The 5 of 300 it reported were not the truth.

What was left before backtracks (on 300 flights, before the review fixes): 40 flights from or to an airport whose taxiways reach the runway in use only along a runway (cleared as a backtrack since 0.18); 14 arrivals over high ground (the Andes, the Himalaya, the Alps) where, with only the sector altitude to vector by, final is joined too high to land; 3 stands that no taxiway comes within 300 metres of. Not modelled by the pilot: holds, circling (it is put on the circling runway's threshold), missed approaches, STAR speed restrictions, wind drift and other traffic.

## Timing tests: two things at once

Three things run at the same time in the app: the session's job loop (the pilot's calls and the copilot), the ATIS broadcaster (which also makes the controllers' unprompted calls and plays the chatter) and the second radio. Every other test calls them one step at a time, so none can see a fault that needs two of them at the same moment. One such fault lost a real flight its hand-off to approach: a centre said "contact approach" while the copilot was tuning the previous hand-off, and the copilot checked in with the wrong centre.

`tests/sim.py` runs the real `Session`, `AtisBroadcaster` and `SecondRadio` threads against stand-ins: a sim whose aircraft flies a straight line and whose radio reports a new frequency after a delay (or never, or refuses it, or a few kHz off what was written), a sound card on which a sentence lasts as long as it takes to say and which notes every sentence cut off by another, voices that can take seconds to make (`voice_delay`, as the real engines do), a microphone that hears only while the key is down, a push-to-talk key and a language model. Every wait and sleep in `voice_loop.py` and `radio.py` goes through a clock (`proatc/clock.py`); the tests pass `SimTime`, which runs one thread at a time and moves time on only when all of them are waiting. A minute of flight takes a fraction of a second and gives the same result on every run.

To add a scenario, in `tests/test_session_timing.py`:

```python
def test_what_happens_as_a_sentence(rigs):
    rig = rigs(tune_delay=1.8)  # copilot, auto_tune, refuse, chatter, seed, ...
    rig.controller_says(5, handoff_to_next_centre)  # the controller on frequency says it at 5 s
    rig.run_until(12)
    rig.tune_by_hand(EAST_2)  # or rig.say(text, Intent(...)), rig.click(...)
    rig.run_until(50)
    ok(rig, rig.settled_on(EAST, "Eastland Control"))  # frequency, checked in, nothing owed, nobody talked over
```

`rig.transcript()` prints the run with times and the station on frequency. **A new fault involving the copilot, or two things happening at once, is reproduced here first**, then fixed. Run a new scenario twenty times in a row: it must pass every time.

## Dead ends

A call the rules engine has no rule for falls to the language model, which may only inform or decline: for the pilot, a dead end. `tests/deadends.py` builds the whole matrix on invented airports, without X-Plane or a model: every intent the app knows, on every kind of station (delivery, ground, tower, departure, approach, centre, oceanic, unicom, information service), in every main state of an instrument and a visual flight, one of them just after the controller has asked a question ("confirm you are following the departure").

```bash
uv run python -m tests.deadends            # the matrix, state by state
uv run python -m tests.deadends --missing  # only the unanswered calls a pilot plausibly makes
uv run python -m tests.deadends --false    # answers that are not true of the flight, with the rule that made them
```

An answer only counts when it is true. Every reply is read against the flight's state at that moment: a level after "climb", "descend" or "maintain" must be the level the flight is now cleared to, and a climb is never given to an aircraft above it nor a descent to one below; a heading, squawk or speed said must be the one assigned; a frequency must be the hand-off or one of the airport's own; a runway cleared for takeoff or landing must exist and suit the wind; no approach is cleared on the ground. Each cell records which function made the reply, and the answers are counted by kind: specific, a bare "roger", a bare "unable"/"say again", "contact …" (another controller's), silence. `tests/test_answers.py` holds the plausible gaps at zero; `tests/test_answers_state.py` holds the false answers at zero and reads the text of the cells an independent review found wrong (on final, after landing, after a diversion, after a hand-off, on the takeoff roll, on an information service). Only `other` (speech nobody foresaw) and a blind call to other traffic are left to the model. A new intent is measured as soon as it is in `intents.py`.

| Matrix (0.14) | Plausible cells | No rule | False | Specific | Roger | Unable | Contact | Silence |
|---|---|---|---|---|---|---|---|---|
| Main, before | 2,220 | 742 | | | | | | |
| First version of this work, new intents | 4,110 | 0 | 61 | 2,674 | 90 | 378 | 361 | 546 |
| After the review | 4,110 | 0 | 0 | 2,615 | 250 | 320 | 379 | 546 |

The rogers that grew are mostly an information service acknowledging a request it can neither grant nor refuse, which is what it should do.

A yes or a no counts as plausible only where a question was asked.

## Fuzz tests

Every random choice is seeded. A failure message names the seed and the step (`seed 137 step 22 Westfield Tower`); `one_flight(make, stations, 137)` in `tests/test_robust.py` reproduces it. Do not loosen `BAD_TEXT` in `src/proatc/verify.py` to make a failure pass: it lists what a controller's sentence must never contain.

## The real-data sweep

`proatc verify-data` runs clearances, taxi routes, procedures, arrivals, VFR calls and chatter at the airports with the most frequencies and a random sample of the rest. It reports exceptions and sentences that read wrong, grouped, with example airports. Last full run: 400 airports, about 80,000 checks in under a minute, no failures.

## Benchmarks

Not run in CI: they need the speech models or a language model.

- `proatc bench-speech` speaks 25 radio calls in four voices, clean and through the radio effect with two levels of noise, and measures word errors and whether the numbers survive. `--quick` runs a third of it.
- `proatc bench-intents` runs 89 transcripts through the configured model and checks the intent and every value. It costs a fraction of a cent on OpenRouter.
- `proatc bench-off-script` runs 23 calls in the pilot's own words ("it's getting bumpy, any chance of a bit lower"), each in a situation (cruise, approach, ground, an open controller question, a mayday, short final), through both readings as the app reads them. It counts calls placed on the first reading, acted on or asked back on the second, left unclear, and the one that matters most: WRONG AND ACTED ON, a call the rules would carry out that the pilot did not ask for. It exits with an error when there is one.

Baseline on 0.8 (Windows, NVIDIA graphics card, `small.en`, default model `anthropic/claude-haiku-5.5`):

| Benchmark | Result |
|---|---|
| `bench-speech`, 300 calls | word errors 8.3%; numbers right in 284 of 300 calls (95%); 0.15 s a call |
| `bench-speech`, by condition | clean 8.0%, radio effect 7.5%, noisy radio 9.3% word errors |
| `bench-intents`, 61 transcripts | 60 understood exactly (98%); the miss was a blind call taken for a position report, which the engine treats the same way |
| `bench-intents` on 0.15, 89 transcripts | 85 understood exactly (96%). The misses: "roger" to an instruction taken as an acknowledgement (treated as the readback anyway); a blind call taken for a position report; "request frequency change to get the ATIS" taken as a request for information; "negative, request vectors back to the departure" taken as "negative" (it still gets a way back, direct to the next waypoint) |
| `bench-intents` on 0.16, 89 transcripts | 85 understood exactly (96%). "Negative, request vectors back to the departure" is now right; "request wind check" is now taken as a request for the weather, which is also answered with the wind. The other three misses are as on 0.15 |
| `bench-off-script` on 0.16, 23 calls | 23 right: 22 on the first reading, "the coffee on this flight is terrible" left unclear; 0 wrong and acted on. Without the situation given to the first reading: 20 right, 3 wrong and acted on ("correcting back onto it now" to the off-track question, "head back to where we came from" after a mayday, "are we good to land") |

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
