"""Benchmarks for the two parts of the app that are not deterministic: hearing and understanding.

- `proatc bench-speech`: radio calls are spoken by the local voices (several of them, clean and through
  the radio effect with noise), then run through speech recognition. Reports the word error rate
  and how often the numbers that matter (runways, squawks, frequencies, levels) come out right.
  Synthetic voices are not people, so this measures changes (a new model, a new setting, a new
  prompt), not how well it hears you.
- `proatc bench-intents`: a fixed set of transcripts, including the mistakes speech recognition
  makes, is run through the language model. Reports how often the intent and each value are right.
  This calls the configured model (about ninety short requests: a fraction of a cent on OpenRouter).

Neither runs in CI: both need models or a key. Run them when changing stt.py, intents.py or the model.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .intents import Intent, IntentParser

# ---- speech -------------------------------------------------------------------

CALLSIGN = "Speedbird one two three"
SPOKEN = (
    "Heathrow Delivery, Speedbird one two three, information Kilo, request I F R clearance to Edinburgh",
    "Cleared to Edinburgh via the Ultib one Juliett departure, squawk four five two one, Speedbird one two three",
    "Heathrow Ground, Speedbird one two three, request push and start",
    "Taxi to holding point runway two seven right via Alfa, Bravo and Lima, hold short runway two seven left",
    "Holding short runway two seven left, request crossing, Speedbird one two three",
    "Heathrow Tower, Speedbird one two three, ready for departure",
    "Runway two seven right, cleared for takeoff, Speedbird one two three",
    "London Control, Speedbird one two three, passing altitude four thousand feet climbing flight level one niner zero",
    "Climb flight level three six zero, Speedbird one two three",
    "Request direct Bibax, Speedbird one two three",
    "Descend flight level two six zero, level by Inpip, Speedbird one two three",
    "Turn left heading zero niner zero, descend altitude three thousand feet, Q N H one zero zero six",
    "Reduce speed one eight zero knots, Speedbird one two three",
    "Cleared I L S approach runway two seven left, report established, Speedbird one two three",
    "Established I L S runway two seven left, Speedbird one two three",
    "Contact Heathrow Tower one one eight decimal five zero five, Speedbird one two three",
    "Going around, Speedbird one two three",
    "Request diversion to Glasgow, Speedbird one two three",
    "Runway vacated, Speedbird one two three",
    "Mayday, mayday, mayday, Speedbird one two three, engine failure, request immediate return",
    "Biggin Tower, Golf Bravo X-ray Yankee Zulu, eight miles south, inbound for landing",
    "Join left hand downwind runway two one, Q N H one zero one eight, Golf Bravo X-ray Yankee Zulu",
    "Request zone transit, Golf Bravo X-ray Yankee Zulu",
    "Farnborough Radar, Golf Bravo X-ray Yankee Zulu, request traffic service",
    "Squawk seven zero zero zero, Golf Bravo X-ray Yankee Zulu",
)
WORD_DIGITS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "tree": "3", "four": "4", "five": "5", "fife": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "niner": "9",
}  # fmt: skip
VOICES = ("am_michael", "af_sarah", "bm_george", "bf_emma")
CONDITIONS = (("clean", None), ("radio", 0.012), ("noisy radio", 0.05))


def normalise(text: str) -> list[str]:
    """Words, lower case, no punctuation, digits one by one: so '27L' and 'two seven left' compare equal."""
    text = re.sub(r"(\d)", r" \1 ", text.lower())
    words = re.sub(r"[^a-z0-9 ]", " ", text).split()
    return [WORD_DIGITS.get(word, word) for word in words]


def word_errors(expected: list[str], heard: list[str]) -> int:
    """Edit distance in words: substitutions, insertions and deletions."""
    row = list(range(len(heard) + 1))
    for i, want in enumerate(expected, 1):
        previous, row[0] = row[0], i
        for j, got in enumerate(heard, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (want != got))
    return row[-1]


def numbers(words: list[str]) -> list[str]:
    """Runs of digits in order: the runway, the squawk, the frequency, the level."""
    return [run.replace(" ", "") for run in re.findall(r"(?:\d ?)+", " ".join(words))]


@dataclass
class SpeechRow:
    voice: str
    condition: str
    words: int = 0
    errors: int = 0
    calls: int = 0
    numbers_right: int = 0
    seconds: float = 0.0

    @property
    def wer(self) -> float:
        return self.errors / self.words if self.words else 0.0


def bench_speech(tts, stt, quick: bool = False, report=print) -> list[SpeechRow]:
    import numpy as np
    from scipy.signal import resample_poly

    from .radio_fx import radio_effect

    phrases = SPOKEN[::3] if quick else SPOKEN
    # Two men and two women, American and British, whichever engine is in use.
    voices = [voice for voice in VOICES if voice in tts.voices()]
    voices = voices or [next(v for v in tts.voices() if v.startswith(group)) for group in ("am", "af", "bm", "bf")]
    rows = []
    for voice in voices[:2] if quick else voices:
        spoken = [tts.speak(phrase, voice=voice, speed=1.0) for phrase in phrases]  # once per voice: the slow part
        for condition, noise in CONDITIONS:
            row = SpeechRow(voice, condition)
            for phrase, (samples, rate) in zip(phrases, spoken):
                if noise is not None:
                    samples = radio_effect(samples, rate, noise)
                audio = resample_poly(samples, 16000, rate).astype(np.float32)
                started = time.perf_counter()
                heard = stt.transcribe(audio, hints=f"{CALLSIGN}. Waypoints: Bibax, Inpip, Ultib.")
                row.seconds += time.perf_counter() - started
                want, got = normalise(phrase), normalise(heard)
                row.words += len(want)
                row.errors += word_errors(want, got)
                row.calls += 1
                row.numbers_right += numbers(want) == numbers(got)
            rows.append(row)
            report(
                f"{voice:11} {condition:12} word errors {row.wer * 100:5.1f}%   numbers right {row.numbers_right}/{row.calls}   "
                f"{row.seconds / row.calls:.2f} s a call"
            )
    words, errors = sum(r.words for r in rows), sum(r.errors for r in rows)
    calls, right = sum(r.calls for r in rows), sum(r.numbers_right for r in rows)
    report(f"overall: word errors {errors / words * 100:.1f}%, numbers right {right}/{calls} ({right / calls * 100:.0f}%)")
    return rows


# ---- understanding ---------------------------------------------------------------

# (what speech recognition wrote, the controller's last instruction, what it means)
OFF_TRACK_SAID = (
    "Speedbird one two three, you appear to be six miles off the departure track, confirm you are following the departure"
)
APPROACH_SAID = "Speedbird one two three, turn left heading zero niner zero, expect I L S approach runway zero one left"
UNDERSTOOD = (
    ("Heathrow Delivery, Speedbird 123, information kilo, request IFR clearance to Edinburgh", "", {"intent": "request_clearance", "flight_rules": "IFR", "atis": "K"}),
    ("delivery speedbird one two three request clearance to paris with information bravo", "", {"intent": "request_clearance", "atis": "B"}),
    ("request VFR departure to the north", "", {"intent": "request_clearance", "flight_rules": "VFR", "compass": "north"}),
    ("request push and start", "", {"intent": "request_pushback"}),
    ("ground speedbird 123 request taxi", "", {"intent": "request_taxi"}),
    ("holding short runway 27 left request crossing", "", {"intent": "request_crossing", "runway": "27L"}),
    ("we're not sure where to go, request progressive taxi", "", {"intent": "request_progressive"}),
    ("tower speedbird 123 ready for departure", "", {"intent": "ready_for_departure"}),
    ("holding short two seven right ready", "", {"intent": "ready_for_departure", "runway": "27R"}),
    ("runway two seven right cleared for take off speedbird one two three", "Speedbird one two three, runway two seven right, cleared for takeoff", {"intent": "readback", "runway": "27R"}),
    ("cleared for takeoff runway 27 left", "Speedbird one two three, runway two seven right, cleared for takeoff", {"intent": "readback", "runway": "27L"}),
    ("squawk 4521 speedbird 123", "Speedbird one two three, cleared to Edinburgh, squawk four five two one", {"intent": "readback", "squawk": "4521"}),
    ("roger", "Speedbird one two three, descend flight level one two zero", {"intent": "readback"}),
    ("wilco speedbird 123", "Speedbird one two three, report established", {"intent": "readback"}),
    ("london control speedbird 123 passing 4000 climbing flight level 190", "", {"intent": "check_in", "altitude": 19000}),
    ("departure speedbird one two three passing two thousand five hundred", "", {"intent": "check_in"}),
    ("request higher", "", {"intent": "request_climb"}),
    ("request flight level three eight zero", "", {"intent": "request_climb", "altitude": 38000}),
    ("request descent", "", {"intent": "request_descent"}),
    ("request direct bee backs", "", {"intent": "request_direct", "fix": "BIBAX"}),
    ("request direct BIBAX", "", {"intent": "request_direct", "fix": "BIBAX"}),
    ("descend flight level 260 level by inpip", "Speedbird one two three, descend flight level two six zero, level by Inpip", {"intent": "readback", "altitude": 26000}),
    ("left heading 090 descend 3000 feet qnh 1006", "Speedbird one two three, turn left heading zero niner zero, descend altitude three thousand feet, Q N H one zero zero six", {"intent": "readback", "heading": 90, "altitude": 3000, "qnh": 1006}),
    ("heading three six zero", "Speedbird one two three, fly heading three six zero", {"intent": "readback", "heading": 360}),
    ("contact tower 118.505", "Speedbird one two three, contact Heathrow Tower one one eight decimal five zero five", {"intent": "readback", "frequency": "118.505"}),
    ("one one eight decimal five speedbird one two three", "Speedbird one two three, contact Heathrow Tower one one eight decimal five", {"intent": "readback", "frequency": "118.500"}),
    ("established ILS runway 27 left", "", {"intent": "established", "runway": "27L"}),
    ("request the RNAV approach", "", {"intent": "request_approach", "approach": "RNAV"}),
    ("can we have the ILS instead", "", {"intent": "request_approach", "approach": "ILS"}),
    ("field in sight request visual approach", "", {"intent": "request_visual"}),
    ("request holding", "", {"intent": "request_hold"}),
    ("ready for the approach", "Speedbird one two three, hold at Lambourne as published, advise when ready for the approach", {"intent": "cancel_hold"}),
    ("going around", "", {"intent": "going_around"}),
    ("missed approach speedbird 123", "", {"intent": "going_around"}),
    ("request diversion to Glasgow", "", {"intent": "request_diversion"}),
    ("we'd like to divert to echo golf papa foxtrot", "", {"intent": "request_diversion", "destination": "EGPF"}),
    ("runway vacated", "", {"intent": "runway_vacated"}),
    ("clear of the runway speedbird 123", "", {"intent": "runway_vacated"}),
    ("mayday mayday mayday speedbird 123 engine failure", "", {"intent": "mayday"}),
    ("pan pan pan pan pan pan speedbird 123 passenger medical", "", {"intent": "pan_pan"}),
    ("cancel mayday", "", {"intent": "cancel_emergency"}),
    ("say again", "", {"intent": "say_again"}),
    ("radio check", "", {"intent": "radio_check"}),
    ("heathrow tower speedbird one two three", "", {"intent": "initial_call"}),
    ("request the wind", "", {"intent": "request_weather"}),
    ("request deviation 20 degrees left of track due weather", "", {"intent": "request_deviation", "direction": "left"}),
    ("clear of weather", "", {"intent": "clear_of_weather"}),
    ("biggin tower golf bravo x-ray yankee zulu eight miles south inbound for landing", "", {"intent": "inbound", "compass": "south"}),
    ("downwind runway 21", "", {"intent": "position_report", "position": "downwind", "runway": "21"}),
    ("final runway two one", "", {"intent": "position_report", "position": "final", "runway": "21"}),
    ("request touch and go", "", {"intent": "request_touch_and_go"}),
    ("request low approach", "", {"intent": "request_low_approach"}),
    ("request circuits", "", {"intent": "request_circuits"}),
    ("request zone transit", "", {"intent": "request_transit"}),
    ("leaving the zone to the north", "", {"intent": "report_leaving"}),
    ("request special VFR", "", {"intent": "request_special_vfr"}),
    ("farnborough radar request traffic service", "", {"intent": "request_flight_following", "service": "traffic"}),
    ("request basic service", "", {"intent": "request_flight_following", "service": "basic"}),
    ("socal approach request flight following", "", {"intent": "request_flight_following"}),
    ("cancel flight following", "", {"intent": "cancel_flight_following"}),
    ("santa paula traffic cessna one two three alpha bravo left downwind runway two two santa paula", "", {"intent": "traffic_call"}),
    ("speedbird 123 request confirmation of assigned altitude", "", {"intent": "request_information", "item": "altitude"}),
    ("confirm cleared level flight level one two zero", "", {"intent": "request_information", "item": "altitude", "altitude": 12000}),
    ("say again the heading", "Speedbird one two three, turn left heading zero niner zero, descend altitude three thousand feet", {"intent": "say_again", "item": "heading"}),
    ("verify squawk", "", {"intent": "request_information", "item": "squawk"}),
    ("request runway assignment for arrival, verify expected runway", "", {"intent": "request_information", "item": "runway"}),
    ("request approach information, current approach in use", "", {"intent": "request_information", "item": "approach"}),
    ("how many track miles to touchdown", "", {"intent": "request_information", "item": "distance"}),
    ("request wind check", "", {"intent": "request_information", "item": "wind"}),
    ("thanks for your help, good day", "", {"intent": "acknowledge"}),
    ("standby", "", {"intent": "acknowledge"}),
    ("unable, speedbird 123", "Speedbird one two three, climb flight level three eight zero", {"intent": "unable"}),
    ("we're not ready yet, need two more minutes", "", {"intent": "request_delay"}),
    ("request return to stand", "", {"intent": "request_return"}),
    ("request runway two seven left for departure", "", {"intent": "request_runway", "runway": "27L"}),
    ("request frequency change to get the ATIS", "", {"intent": "report_leaving"}),
    # Questions about the arrival, from a real flight: each was taken for "other" and answered "unable".
    ("which runway are landing?", "", {"intent": "request_information", "item": "runway"}),
    ("which runway am I landing on", "", {"intent": "request_information", "item": "runway"}),
    ("request runway assignment", "", {"intent": "request_information", "item": "runway"}),
    ("request the transition for the approach, say the transition name and the route", APPROACH_SAID, {"intent": "request_information", "item": "transition"}),
    ("how will we join the approach, vectors or own navigation?", APPROACH_SAID, {"intent": "request_information", "item": "transition"}),
    ("which initial approach fix should we expect", "", {"intent": "request_information", "item": "transition"}),
    # Answers to what the controller asked.
    ("affirm, following the departure", OFF_TRACK_SAID, {"intent": "affirm"}),
    ("yes we are on the departure", OFF_TRACK_SAID, {"intent": "affirm"}),
    ("negative", OFF_TRACK_SAID, {"intent": "negative"}),
    ("negative, request vectors back to the departure", OFF_TRACK_SAID, {"intent": "request_vectors"}),
    ("negative, request direct bee backs", OFF_TRACK_SAID, {"intent": "request_direct", "fix": "BIBAX"}),
    ("now able flight level three eight zero", "Speedbird one two three, roger, maintain flight level three four zero, advise when able", {"intent": "affirm"}),
    ("request vectors to final", "", {"intent": "request_vectors"}),
)  # fmt: skip


def bench_intents(controller, report=print) -> tuple[int, int, list[str]]:
    """(calls with the right intent and values, calls, what went wrong)."""
    parser = IntentParser(controller)
    right, wrong = 0, []
    for transcript, last, expected in UNDERSTOOD:
        intent: Intent = parser.parse(transcript, last, ["BIBAX", "INPIP", "EGPF", "EGPH"])
        problems = []
        for key, want in expected.items():
            got = getattr(intent, key)
            if isinstance(want, (int, float)) and not isinstance(want, bool) and got is not None:
                same = abs(float(got) - float(want)) < 0.6
            else:
                same = got == want
            if not same:
                problems.append(f"{key}: wanted {want!r}, got {got!r}")
        if problems:
            wrong.append(f"{transcript!r}: {'; '.join(problems)}")
        else:
            right += 1
    report(f"{right}/{len(UNDERSTOOD)} understood exactly ({right / len(UNDERSTOOD) * 100:.0f}%)")
    for line in wrong:
        report(f"  wrong  {line}")
    return right, len(UNDERSTOOD), wrong


# ---- off-script calls: the first reading, then the second (resolver.py) ---------------------

CRUISE = {
    "station": "Washington Center (center)",
    "conversation": ["Pilot: Washington Center, November one two three Alfa Bravo, level flight level three six zero",
                     "November one two three Alfa Bravo, Washington Center, radar contact"],
    "flight": {"phase": "airborne", "flight_rules": "IFR", "destination": "KIAD", "cleared_level": "FL360"},
    "plan": {"origin": "KBOS", "destination": "KIAD", "route": "BLZZR6 BLZZR DCT BAF HYPER1", "cruise_ft": 36000},
    "on_ground": False, "altitude_ft": 36000, "open_question": None,
    "calls_that_fit": ['Request lower: "request lower" -> {"intent": "request_descent"}',
                       'Request higher: "request higher" -> {"intent": "request_climb"}',
                       'Direct BAF: "request direct Baf" -> {"intent": "request_direct", "fix": "BAF"}',
                       'Destination weather: "request weather at destination" -> {"intent": "request_weather"}'],
}  # fmt: skip
APPROACH = {
    "station": "Washington Dulles Approach (approach)",
    "conversation": ["Pilot: Washington Dulles Approach, November one two three Alfa Bravo, descending one zero thousand",
                     "November one two three Alfa Bravo, Washington Dulles Approach, radar contact, expect I L S approach runway one left",
                     "November one two three Alfa Bravo, turn left heading one niner zero, descend and maintain four thousand"],
    "flight": {"phase": "airborne", "flight_rules": "IFR", "destination": "KIAD", "cleared_level": "4,000 ft",
               "approach": "I L S runway 1L", "heading": 190},
    "plan": {"origin": "KBOS", "destination": "KIAD"}, "on_ground": False, "altitude_ft": 6000, "open_question": None,
    "calls_that_fit": ['Request R NAV: "request R NAV approach" -> {"intent": "request_approach", "approach": "RNAV"}',
                       'Field in sight: "field in sight" -> {"intent": "request_visual"}',
                       'Request runway 1C: "request runway one center" -> {"intent": "request_runway", "runway": "1C"}'],
}  # fmt: skip
GROUND = {
    "station": "Boston Ground (ground)",
    "conversation": ["Pilot: Boston Ground, November one two three Alfa Bravo, request taxi",
                     "November one two three Alfa Bravo, runway two seven, taxi via Kilo, Mike, Charlie"],
    "flight": {"phase": "taxiing out", "flight_rules": "IFR", "destination": "KIAD", "assigned_runway": "27"},
    "plan": {"origin": "KBOS", "destination": "KIAD"}, "on_ground": True, "altitude_ft": 20, "open_question": None,
    "calls_that_fit": ['Progressive taxi: "request progressive taxi" -> {"intent": "request_progressive"}',
                       'Return to gate: "request return to the gate" -> {"intent": "request_return"}'],
}  # fmt: skip
OFF_TRACK = ("November one two three Alfa Bravo, you appear to be six miles off the departure track, "
             "confirm you are following the departure")  # fmt: skip
DEPARTURE = {
    "station": "Boston Departure (departure)",
    "conversation": ["Pilot: Boston Departure, November one two three Alfa Bravo, passing two thousand climbing five thousand",
                     "November one two three Alfa Bravo, Boston Departure, radar contact, climb and maintain one five thousand",
                     OFF_TRACK],
    "flight": {"phase": "airborne", "flight_rules": "IFR", "destination": "KIAD", "cleared_level": "15,000 ft", "sid": "BLZZR6"},
    "plan": {"origin": "KBOS", "destination": "KIAD"}, "on_ground": False, "altitude_ft": 6200, "open_question": OFF_TRACK,
    "calls_that_fit": ['Affirm: "affirm, following the B L Z Z R six departure" -> {"intent": "affirm"}',
                       'Request vectors: "request vectors to rejoin the departure" -> {"intent": "request_vectors"}'],
}  # fmt: skip

MAYDAY_ASKED = ("November one two three Alfa Bravo, Washington Center, roger mayday, squawk seven seven zero zero, "
                "say intentions")  # fmt: skip
EMERGENCY = {
    **CRUISE,
    "conversation": [*CRUISE["conversation"], "Pilot: Mayday mayday mayday, November one two three Alfa Bravo, engine failure",
                     MAYDAY_ASKED],
    "flight": {**CRUISE["flight"], "emergency": "mayday"},
    "open_question": MAYDAY_ASKED,
    "calls_that_fit": ['Return: "request return to Boston" -> {"intent": "request_return"}',
                       'Divert: "request diversion to {destination}" -> {"intent": "request_diversion"}',
                       'Continue: "continuing to destination" -> {"intent": "affirm"}'],
}  # fmt: skip
FINAL = {
    "station": "Dulles Tower (tower)",
    "conversation": ["Pilot: Dulles Tower, November one two three Alfa Bravo, established I L S runway one left",
                     "November one two three Alfa Bravo, Dulles Tower, continue approach runway one left, number two"],
    "flight": {"phase": "airborne", "flight_rules": "IFR", "destination": "KIAD", "approach": "I L S runway 1L"},
    "plan": {"origin": "KBOS", "destination": "KIAD"}, "on_ground": False, "altitude_ft": 1800, "open_question": None,
    "calls_that_fit": ['Final: "final runway one left" -> {"intent": "position_report", "position": "final"}',
                       'Going around: "going around" -> {"intent": "going_around"}'],
}  # fmt: skip

# (situation, transcript, intents that are right, values that must be right; a set of values means any of them). The pilot does not use the standard
# words: that is the point. An empty set of intents means nothing should be acted on.
OFF_SCRIPT = (
    (CRUISE, "it's getting pretty bumpy up here, any chance we could go a bit lower", {"request_descent"}, {}),
    (CRUISE, "we'd love to cut the corner and head straight for baf if that works for you", {"request_direct"}, {"fix": "BAF"}),
    (CRUISE, "how's the weather looking at dulles", {"request_weather"}, {}),
    (CRUISE, "the ride is smooth, we could go higher if you've got it", {"request_climb"}, {}),
    (CRUISE, "we've got a passenger who isn't well, we'd like to get on the ground as soon as possible",
     {"pan_pan", "request_diversion", "mayday"}, {}),
    (CRUISE, "our left engine is running rough, we'd like to divert to philadelphia", {"request_diversion", "pan_pan", "mayday"}, {}),
    (CRUISE, "the coffee on this flight is terrible", set(), {}),
    (APPROACH, "which runway are we landing on again", {"request_information"}, {"item": "runway"}),
    (APPROACH, "we'd prefer to do the rnav instead of the ils if that's possible", {"request_approach"}, {"approach": "RNAV"}),
    (APPROACH, "we can see the airport now", {"request_visual"}, {}),
    (APPROACH, "how many miles have we got to go", {"request_information"}, {"item": "distance"}),
    # With no runway read, the engine asks which runway: not acted on wrongly.
    (APPROACH, "could we have the center runway instead, one center", {"request_runway"}, {"runway": {"1C", "01C", None}}),
    (APPROACH, "we're going to need a bit of time to sort out a problem, can we hold somewhere", {"request_hold"}, {}),
    (GROUND, "sorry we're a little lost, which way after kilo", {"request_progressive"}, {}),
    (GROUND, "we've got a warning light, we need to head back to the gate", {"request_return"}, {}),
    (GROUND, "give us a couple of minutes, we're not quite ready", {"request_delay"}, {}),
    (DEPARTURE, "yes we're on it, following the blizzard six", {"affirm"}, {}),
    (DEPARTURE, "no I think I lost it, can you point me back", {"request_vectors", "negative"}, {}),
    # Only the conversation says what these mean.
    # "Negative" here gets a direct back to the departure: an answer as good as "affirm".
    (DEPARTURE, "we drifted a little north there, correcting back onto it now", {"affirm", "negative"}, {}),
    (EMERGENCY, "we'll head back to where we came from", {"request_return", "request_diversion"}, {}),
    (EMERGENCY, "we'll keep going, we can make it", {"affirm"}, {}),
    (FINAL, "are we good to land", {"position_report"}, {}),
    (APPROACH, "sorry, missed the last bit", {"say_again"}, {}),
)  # fmt: skip


def _fits(intent, wanted: set, values: dict) -> bool:
    def same(key, value):
        got = getattr(intent, key)
        return got in value if isinstance(value, set) else got == value

    return intent.intent in wanted and all(same(key, value) for key, value in values.items())


def bench_off_script(controller, report=print) -> dict:
    """Off-script calls through both readings, as the app reads them. Counted: right on the first reading; right on
    the second, acted on or asked back; left unclear (the free answer takes them); asked back wrongly (the pilot can
    say "negative"); and WRONG AND ACTED ON, the one that matters most: a call the rules would carry out that the
    pilot did not ask for."""
    from .resolver import read

    parser = IntentParser(controller)
    counts = {"first": 0, "second_act": 0, "second_confirm": 0, "unclear": 0, "wrong_confirm": 0, "wrong_act": 0}
    right = 0
    for about, transcript, wanted, values in OFF_SCRIPT:
        last = next((line for line in reversed(about["conversation"]) if not line.startswith("Pilot:")), "")
        from .resolver import brief

        first = parser.parse(transcript, last, situation=brief(about))
        if first.intent != "other":
            ok = _fits(first, wanted, values) or (not wanted and first.intent in ("acknowledge", "say_again"))
            kind, got = ("first" if ok else "wrong_act"), f"first reading: {first.intent}"
        else:
            reading = read(controller, about, transcript)
            if reading.intent is None:
                ok, kind, got = not wanted, "unclear", f"unclear ({reading.why})"
            else:
                ok = _fits(reading.intent, wanted, values)
                kind = f"{'second' if ok else 'wrong'}_{reading.decision}"
                got = f"second reading: {reading.decision} {reading.intent.intent} ({reading.why})"
        counts[kind] += 1
        right += ok
        report(f"  {'ok  ' if ok else 'MISS'} {transcript!r}: {got}")
    report(
        f"{right}/{len(OFF_SCRIPT)} right ({counts['first']} on the first reading, {counts['second_act']} acted on and "
        f"{counts['second_confirm']} asked back on the second); {counts['unclear']} unclear; "
        f"{counts['wrong_confirm']} asked back wrongly; {counts['wrong_act']} WRONG AND ACTED ON"
    )
    return counts
