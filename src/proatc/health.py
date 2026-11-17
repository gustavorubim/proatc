"""A health report for a flight: what went wrong on the radio that the pilot did not cause.

The journal (flightlog.py) holds every radio event and, every twenty seconds, who answers on
the tuned frequency. Faults found in real flights so far have all shown up there long before
anyone reported them: a frequency nobody answered on, a controller that only said "say again",
two hand-offs seconds apart, a station that changed in the middle of a conversation. `check`
looks for those in a flight's records and says, in a sentence each, what it found.

It is pure: records in, findings out. The app runs it in the debrief; `proatc health` runs it on
a journal from disk. False alarms cost more than a missed fault, because a report that cries wolf
stops being read: a parked aircraft, a paused sim and the seconds between a hand-off and its
tuning are never reported.
"""

from __future__ import annotations

import re
from datetime import datetime

from .navdata import distance_nm

SILENT_AFTER_S = 60  # a frequency nobody answers on, for longer than this while airborne
HANDOFF_WINDOW_S = 120  # time to tune a hand-off's frequency
CLOSE_HANDOFFS_S = 30  # two hand-offs closer than this cannot both be followed
LATE_APPROACH_NM = 15  # first call to approach closer than this to the destination
NEAR_DESTINATION_NM = 40  # a terminal controller this close to the destination counts as its own
READBACK_TROUBLE = 3  # corrections, or the pilot's "say again", from this many on
MAX_GAP_S = 30  # a trace comes every 20 s in flight: a longer gap is the app closed, not time flown
SAME_KHZ = 10  # 118.420 asked for, 118.425 shown by the sim: the same channel

TITLES = {
    "silent_frequency": "Nobody answered on the tuned frequency",
    "dead_end": "Controller had no answer",
    "station_changed": "Station changed on the same frequency",
    "handoff_missed": "Hand-off not followed",
    "handoffs_close": "Hand-offs close together",
    "readback_trouble": "Readback trouble",
    "no_arrival_contact": "Never reached approach or tower",
    "late_approach": "Approach reached late",
    "app_error": "Errors",
}

# The free-form fallback's ways of saying it has nothing for the pilot.
DEAD_END = re.compile(r"\b(unable|say again|say intentions|standby)\b", re.I)
# Journals written before the fallback was marked: only words the rules engine never uses this way.
OLD_DEAD_END = re.compile(r"unable, say again your request|roger, say intentions|, standby\.?$", re.I)
TERMINAL = re.compile(r"\b(approach|arrival|director|radar|tower)\b", re.I)
APPROACH = re.compile(r"\b(approach|arrival|director|radar)\b", re.I)
NO_CONTROLLER = re.compile(r"\b(traffic|unicom|ctaf|multicom)\b", re.I)
DIGITS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
          "eight": "8", "niner": "9", "nine": "9", "decimal": ".", "point": "."}  # fmt: skip
SPOKEN = r"(?:zero|one|two|three|four|five|six|seven|eight|niner|nine)"
HANDOFF = re.compile(
    rf"\b(?:contact|monitor) (?P<name>[A-Za-z][A-Za-z' -]*?) "
    rf"(?P<freq>{SPOKEN}(?: {SPOKEN}){{2}} (?:decimal|point)(?: {SPOKEN}){{1,3}}|1\d\d\.\d{{1,3}})\b"
)
TUNED_INFO = re.compile(r"^COM\d tuned to (\d{3}\.\d{1,3})")
RADIO = ("pilot", "atc", "understood", "silent")


def check(records, locate=None) -> list[dict]:
    """Findings for a flight. `records` are journal or FlightLog entries in time order; `locate(ident)`,
    when given, returns an airport's (lat, lon) so the distance to the destination can be judged."""
    records = [r for r in records if isinstance(r, dict) and isinstance(r.get("t"), int | float)]
    records.sort(key=lambda r: r["t"])
    clock = _active_clock(records)
    findings = (
        silent_frequencies(records, clock)
        + dead_ends(records)
        + station_changes(records)
        + handoffs(records, clock)
        + readback_trouble(records)
        + arrival_contact(records, locate)
        + app_errors(records)
    )
    return sorted(findings, key=lambda f: f["t"])


# ---- reading the records ----------------------------------------------------------------


def _finding(kind: str, t: float, text: str, **detail) -> dict:
    return {"kind": kind, "t": t, "time": _clock(t), "text": text, "detail": detail}


def _clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def _raw(record: dict) -> dict:
    return (record.get("context") or {}).get("raw") or {}


def _airborne(record: dict) -> bool | None:
    """Whether the aircraft was flying at this record: None when the record does not say."""
    context = record.get("context") or {}
    raw = context.get("raw") or {}
    if "on_ground" in raw:
        return not raw["on_ground"]
    aircraft = context.get("aircraft") or {}
    return (not aircraft["on_ground"]) if "on_ground" in aircraft else None


def _paused(record: dict) -> bool:
    return bool(_raw(record).get("paused"))


def _khz(mhz) -> int | None:
    try:
        return int(round(float(mhz) * 1000))
    except (TypeError, ValueError):
        return None


def _same(a: int | None, b: int | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= SAME_KHZ


def _tuned(record: dict) -> int | None:
    """The frequency the record says is tuned: the tune confirmation's own words win over the context,
    which can be read a moment before the radio took the new frequency."""
    if record.get("type") == "info":
        match = TUNED_INFO.match(record.get("text", ""))
        if match:
            return _khz(match.group(1))
    return _khz((record.get("context") or {}).get("tuned"))


def _phase(record: dict) -> str:
    return ((record.get("context") or {}).get("flight") or {}).get("phase", "")


def _position(record: dict) -> tuple[float, float] | None:
    raw = _raw(record)
    if isinstance(raw.get("lat"), int | float) and isinstance(raw.get("lon"), int | float):
        return raw["lat"], raw["lon"]
    aircraft = (record.get("context") or {}).get("aircraft") or {}
    if isinstance(aircraft.get("lat"), int | float) and isinstance(aircraft.get("lon"), int | float):
        return aircraft["lat"], aircraft["lon"]
    return None


def _where(record: dict) -> str:
    """Position and height in a few words, to find the place on a map."""
    place = _position(record)
    if place is None:
        return "position unknown"
    raw = _raw(record)
    feet = raw.get("elevation_m")
    height = f", {round(feet * 3.28084 / 100) * 100:,.0f} ft" if isinstance(feet, int | float) else ""
    return f"{place[0]:.3f}, {place[1]:.3f}{height}"


def _active_clock(records: list[dict]) -> dict[int, float]:
    """Seconds of sim time at each record: a paused sim and a closed app do not count.

    Durations measured on it ("silent for two minutes", "not tuned within two minutes") are the
    pilot's time, not the wall clock's."""
    clock, total = {}, 0.0
    for previous, record in zip([None, *records], records, strict=False):
        if previous is not None and not _paused(previous):
            total += min(max(record["t"] - previous["t"], 0.0), MAX_GAP_S)
        clock[id(record)] = total
    return clock


def _mhz(khz: int) -> str:
    return f"{khz / 1000:.3f}"


# ---- the detectors --------------------------------------------------------------------


def silent_frequencies(records: list[dict], clock: dict) -> list[dict]:
    """Airborne on a frequency where no station answers (the trace's `"station": null`) for over a minute."""
    findings: list[dict] = []
    stretch: list[dict] = []

    def close(end: dict | None) -> None:
        if not stretch:
            return
        last = end if end is not None else stretch[-1]
        seconds = clock[id(last)] - clock[id(stretch[0])]
        if seconds > SILENT_AFTER_S:
            first, khz = stretch[0], _tuned(stretch[0])
            findings.append(_finding(
                "silent_frequency", first["t"],
                f"Nobody answered on {_mhz(khz)} for {_duration(seconds)} while airborne.",
                frequency=_mhz(khz), seconds=round(seconds), position=_where(first),
                last_station=_station_before(records, first),
            ))  # fmt: skip
        stretch.clear()

    for record in records:
        context = record.get("context")
        if record.get("type") != "trace" or not context:
            continue
        silent = context.get("tuned") and context.get("station") is None and _airborne(record) and not _paused(record)
        if silent and stretch and not _same(_tuned(record), _tuned(stretch[0])):
            close(record)
        if silent:
            stretch.append(record)
        else:
            close(record)
    close(None)
    return findings


def _station_before(records: list[dict], record: dict) -> str:
    """The last controller that spoke before this record, to say where the flight came from."""
    spoke = [r for r in records if r["t"] < record["t"] and r.get("type") == "atc" and r.get("station")]
    return spoke[-1]["station"] if spoke else "none"


def _duration(seconds: float) -> str:
    minutes, rest = divmod(round(seconds), 60)
    if not minutes:
        return f"{rest} seconds"
    return f"{minutes} min {rest:02d} s" if rest else f"{minutes} min"


def dead_ends(records: list[dict]) -> list[dict]:
    """Replies with nothing for the pilot: the free-form fallback's "unable, say again your request",
    "say intentions", "standby". Exact where the journal marks the fallback; by the words before that."""
    replies = [r for r in records if r.get("type") == "atc" and not r.get("unprompted")]
    marked = any("fallback" in r for r in records if r.get("type") == "atc")
    if marked:
        dead = [r for r in replies if r.get("fallback") and DEAD_END.search(r.get("text", ""))]
    else:
        dead = [r for r in replies if OLD_DEAD_END.search(r.get("text", ""))]
    if not dead:
        return []
    asked = [_understood_before(records, r) for r in dead]
    stations = list(dict.fromkeys(r.get("station", "") for r in dead))
    times = ", ".join(_clock(r["t"]) for r in dead)
    times_said = "once" if len(dead) == 1 else f"{len(dead)} times"
    return [_finding(
        "dead_end", dead[0]["t"],
        f"The controller had no answer {times_said} and asked the pilot to say again or say intentions.",
        count=len(dead), times=times, stations=", ".join(stations),
        understood_as=", ".join(a for a in dict.fromkeys(asked) if a) or "unknown",
        exact="yes" if marked else "no: matched by the words (journal written before replies were marked)",
    )]  # fmt: skip


def _understood_before(records: list[dict], reply: dict) -> str:
    """What the pilot's call that got this reply was understood as."""
    for record in reversed([r for r in records if r["t"] <= reply["t"] and r is not reply]):
        if record.get("type") == "understood":
            return record.get("intent", "")
        if record.get("type") == "atc":
            return ""  # an earlier exchange: this call was not understood as anything
    return ""


def station_changes(records: list[dict]) -> list[dict]:
    """On one tuned frequency, mid-conversation and airborne, a different station started answering.

    A station that changes as the aircraft flies past an airport nobody is talking to is the
    radio working as it should; only a change after the pilot has spoken on the frequency is reported."""
    findings: list[dict] = []
    khz, names, talked, changes, start = None, [], False, 0, None

    def close() -> None:
        if changes:
            sequence = " -> ".join(names)  # plain characters: a Windows console cannot print every arrow
            findings.append(_finding(
                "station_changed", start["t"],
                f"On {_mhz(khz)} the answering station changed {_times(changes)} during a conversation: {sequence}.",
                frequency=_mhz(khz), stations=sequence, changes=changes, position=_where(start),
            ))  # fmt: skip

    for record in records:
        context = record.get("context") or {}
        if record.get("type") == "info" or not context.get("tuned"):
            continue  # the tune confirmation is read before the new station is found
        here, station = _tuned(record), context.get("station")
        if not _same(here, khz):
            close()
            khz, names, talked, changes, start = here, [station] if station else [], False, 0, None
        elif station and names and station != names[-1] and talked and _airborne(record):
            changes += 1
            names.append(station)
            start = start or record
        elif station and not names:
            names.append(station)
        if record.get("type") in ("pilot", "atc") and station:
            talked = True
    close()
    return findings


def _times(count: int) -> str:
    return "once" if count == 1 else "twice" if count == 2 else f"{count} times"


def _spoken_frequency(words: str) -> int | None:
    if words[0].isdigit():
        return _khz(words)
    return _khz("".join(DIGITS[word] for word in words.split()))


def handoffs(records: list[dict], clock: dict) -> list[dict]:
    """In flight, "contact X <frequency>": the frequency must be tuned within two minutes. And two
    hand-offs under thirty seconds apart cannot both be followed."""
    given = []
    for record in records:
        if record.get("type") != "atc":
            continue
        match = HANDOFF.search(record.get("text", ""))
        khz = _spoken_frequency(match.group("freq")) if match else None
        if khz:
            given.append((record, match.group("name"), khz))
    findings = []
    for index, (record, name, khz) in enumerate(given):
        later = given[index + 1] if index + 1 < len(given) else None
        if later and later[0]["t"] - record["t"] < CLOSE_HANDOFFS_S and not _same(later[2], khz):
            findings.append(_finding(
                "handoffs_close", record["t"],
                f"Two hand-offs came {round(later[0]['t'] - record['t'])} seconds apart: to {name} and then to {later[1]}.",
                first=f"{name} {_mhz(khz)} at {_clock(record['t'])}",
                second=f"{later[1]} {_mhz(later[2])} at {_clock(later[0]['t'])}",
                station=record.get("station", ""),
            ))  # fmt: skip
        if _airborne(record) and not _followed(records, clock, record, khz):
            if clock[id(records[-1])] - clock[id(record)] < HANDOFF_WINDOW_S:
                continue  # the flight's record ends before the pilot had the time to tune it
            replaced = f"{later[1]} {_mhz(later[2])}" if later and _replaced(records, record, later[0], khz) else "no"
            findings.append(_finding(
                "handoff_missed", record["t"],
                f"Handed to {name} on {_mhz(khz)}, which was not tuned within two minutes.",
                station=record.get("station", ""), frequency=_mhz(khz), replaced_by_later_handoff=replaced,
                tuned_instead=_tuned_after(records, clock, record), position=_where(record),
            ))  # fmt: skip
    return findings


def _followed(records: list[dict], clock: dict, handoff: dict, khz: int) -> bool:
    deadline = clock[id(handoff)] + HANDOFF_WINDOW_S
    return any(_same(_tuned(r), khz) for r in records if r["t"] >= handoff["t"] and clock[id(r)] <= deadline and r is not handoff)


def _replaced(records: list[dict], handoff: dict, later: dict, khz: int) -> bool:
    """A later hand-off came before this one's frequency was tuned."""
    return not any(_same(_tuned(r), khz) for r in records if handoff["t"] < r["t"] <= later["t"])


def _tuned_after(records: list[dict], clock: dict, handoff: dict) -> str:
    deadline = clock[id(handoff)] + HANDOFF_WINDOW_S
    seen: list[int] = []
    for record in records:
        khz = _tuned(record)
        if record["t"] > handoff["t"] and clock[id(record)] <= deadline and khz and not any(_same(khz, s) for s in seen):
            seen.append(khz)
    return ", ".join(_mhz(khz) for khz in seen) or "nothing"


def readback_trouble(records: list[dict]) -> list[dict]:
    """Many readbacks corrected or repeated, or many calls the pilot had to have said again."""
    corrected = [r for r in records if r.get("type") == "atc" and _correction(r)]
    said_again = [r for r in records if r.get("type") == "understood" and r.get("intent") == "say_again"]
    if len(corrected) < READBACK_TROUBLE and len(said_again) < READBACK_TROUBLE:
        return []
    first = min([*corrected, *said_again], key=lambda r: r["t"])
    return [_finding(
        "readback_trouble", first["t"],
        f"{len(corrected)} readbacks were corrected or repeated and the pilot asked for {len(said_again)} "
        f"transmissions again.",
        corrected=len(corrected), said_again=len(said_again),
        examples=" | ".join(r.get("text", "") for r in corrected[:3]) or "none",
    )]  # fmt: skip


def _correction(record: dict) -> bool:
    event = record.get("event")
    if event:
        return event in ("readback_corrected", "readback_missed", "readback_incomplete")
    text = record.get("text", "")
    return ", negative, " in text or ("I say again, " in text and "go around" not in text)


def arrival_contact(records: list[dict], locate=None) -> list[dict]:
    """A flight that landed without ever talking to approach or tower, or that reached approach
    inside fifteen miles of its destination: the hand-off chain broke before the airport."""
    landing = _landing(records)
    destination = _destination(records, locate) or (_position(landing) if landing else None)
    end = landing["t"] if landing else float("inf")
    calls = [r for r in records if r.get("type") in ("pilot", "atc") and r["t"] < end and _airborne(r)]
    if not calls or destination is None:
        return []

    def near(record: dict) -> bool:
        place = _position(record)
        return place is not None and distance_nm(*place, *destination) <= NEAR_DESTINATION_NM

    terminal = [r for r in calls if TERMINAL.search(_station(r)) and near(r)]
    findings = []
    last = _station(calls[-1])
    if landing and not terminal and not NO_CONTROLLER.search(last):
        findings.append(_finding(
            "no_arrival_contact", landing["t"],
            "The flight landed without talking to an approach or tower controller at its destination.",
            last_station=last or "none", landed_at=_where(landing),
        ))  # fmt: skip
    approach = [r for r in calls if r.get("type") == "pilot" and APPROACH.search(_station(r)) and near(r)]
    if approach and _came_from_afar(records, approach[0], destination):
        miles = distance_nm(*_position(approach[0]), *destination)
        if miles < LATE_APPROACH_NM:
            findings.append(_finding(
                "late_approach", approach[0]["t"],
                f"The first call to approach was only {miles:.0f} NM from the destination.",
                station=_station(approach[0]), distance_nm=round(miles, 1),
                before=_station_before(records, approach[0]), position=_where(approach[0]),
            ))  # fmt: skip
    return findings


def _came_from_afar(records: list[dict], call: dict, destination: tuple[float, float]) -> bool:
    """Whether the aircraft flew well outside approach's distance before this call: a circuit does not."""
    flown = [_position(r) for r in records if r["t"] < call["t"] and _airborne(r)]
    return any(distance_nm(*place, *destination) > LATE_APPROACH_NM + 10 for place in flown if place)


def _station(record: dict) -> str:
    return (record.get("context") or {}).get("station") or ""


def _landing(records: list[dict]) -> dict | None:
    """The first record on the ground after the aircraft has flown."""
    flown = False
    for record in records:
        airborne = _airborne(record)
        if airborne:
            flown = True
        elif flown and (airborne is False or _phase(record) in ("landed", "taxiing in")):
            return record
    return None


def _destination(records: list[dict], locate) -> tuple[float, float] | None:
    if locate is None:
        return None
    plans = [r.get("plan") for r in records if r.get("type") == "plan" and r.get("plan")]
    ident = plans[-1].get("destination") if plans else None
    try:
        return locate(ident) if ident else None
    except Exception:
        return None  # an airport the data does not know: judged from the landing instead


def app_errors(records: list[dict]) -> list[dict]:
    """Errors the app reported during the flight: a frequency the aircraft would not take, a model that failed."""
    errors = [r for r in records if r.get("type") == "error"]
    if not errors:
        return []
    texts = list(dict.fromkeys(r.get("text", "") for r in errors))
    return [_finding(
        "app_error", errors[0]["t"],
        "The app reported an error." if len(errors) == 1 else f"The app reported {len(errors)} errors.",
        count=len(errors), first=texts[0], others=" | ".join(texts[1:4]) or "none",
    )]  # fmt: skip


# ---- the report -------------------------------------------------------------------------


def as_text(findings: list[dict]) -> str:
    """The findings grouped by kind, for the terminal."""
    if not findings:
        return "Flight health: nothing to report.\n"
    lines = [f"Flight health: {len(findings)} finding{'s' if len(findings) != 1 else ''}"]
    for kind, title in TITLES.items():
        group = [f for f in findings if f["kind"] == kind]
        if not group:
            continue
        lines += ["", f"{title} ({len(group)})"]
        for finding in group:
            lines.append(f"  {finding['time']}  {finding['text']}")
            lines += [f"            {key.replace('_', ' ')}: {value}" for key, value in finding["detail"].items()]
    return "\n".join(lines) + "\n"


def for_app(findings: list[dict]) -> list[dict]:
    """The findings as the app's debrief shows them: plain strings, nothing to compute."""
    return [
        {
            "time": f["time"],
            "kind": f["kind"],
            "title": TITLES.get(f["kind"], f["kind"]),
            "text": f["text"],
            "detail": "; ".join(f"{key.replace('_', ' ')}: {value}" for key, value in f["detail"].items()),
        }
        for f in findings
    ]
