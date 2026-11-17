"""Benchmarks for the two parts of the app that are not deterministic: hearing and understanding.

- `proatc bench-speech`: radio calls are spoken by the local voices (several of them, clean and through
  the radio effect with noise), then run through speech recognition. Reports the word error rate
  and how often the numbers that matter (runways, squawks, frequencies, levels) come out right.
  Synthetic voices are not people, so this measures changes (a new model, a new setting, a new
  prompt), not how well it hears you.
- `proatc bench-intents`: a fixed set of transcripts, including the mistakes speech recognition
  makes, is run through the language model. Reports how often the intent and each value are right.
  This calls the configured model (about seventy-five short requests: a fraction of a cent on OpenRouter).

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
