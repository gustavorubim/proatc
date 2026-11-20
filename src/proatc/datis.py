"""The runways an airport is really using, from its real digital ATIS.

The wind does not say which runways an airport uses: Las Vegas departs runway 1 with eight knots behind it, and
Los Angeles closes two runways for a night. Where an airport publishes a digital ATIS (about seventy-five in the
United States), its text says which runways land, which depart and which are closed. It is read from a public
service that republishes the FAA's D-ATIS; only the airport's code is sent.

The text is written by controllers, not to a format. What is read from it is checked against the airport's own
runways, and anything not understood is left out: the wind and the flight plan then decide, as elsewhere.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass

import httpx

from .navdata import Airport

URL = "https://atis.info/api/{ident}"
TIMEOUT_S = 8.0
FRESH_S = 600.0  # an ATIS is asked for again after ten minutes
RETRY_S = 300.0  # after a failure, in five
NONE_S = 3600.0  # an airport with no digital ATIS is asked about once an hour
OLD_MIN = 180  # an ATIS issued more than three hours ago is not the airport as it is now
MAX_TEXT = 3000  # characters of one ATIS read: a real one is under a thousand
MAX_ENTRIES = 4  # a combined ATIS, or one for arrivals and one for departures
MAX_BODY = 64_000  # bytes of the service's answer read: more is not an ATIS
LOOK_BACK = 16  # characters before a number looked at to see whether "RWYS" or "/" introduces it

WORDS = {"ZERO": "0", "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5", "SIX": "6", "SEVEN": "7",
         "EIGHT": "8", "NINER": "9", "NINE": "9"}  # fmt: skip
SIDES = {"LEFT": "L", "RIGHT": "R", "CENTER": "C", "CENTRE": "C"}
# A runway as written: 26L, 1L, 08, not part of a longer number (121.9, 2000) or of a name (C6, 10SM).
RUNWAY = re.compile(
    r"(?<![A-Z0-9.])(0?[1-9]|[12]\d|3[0-6])([LRC])?(?![A-Z0-9])(?!\.\d)"
    r"(?! ?(?:THSD|THOUSAND|HND|HUNDRED|FT|FEET|PERCENT|DEGREES?|MILES?|NM|MINUTES?|KTS?)\b)"  # a quantity, not a runway
)
INTRODUCED = re.compile(r"(?:\b(?:RWYS?|RYS?|RUNWAYS?)|/)\s*$")  # "RWYS 4/8": what a number here is, is not in doubt
LANDING = re.compile(
    r"\b(LANDING|LNDG|LDG|LAND|ARRIVALS?|ARRIVING|ARVNG|ARR|APCH|APCHS|APCHES|APPROACH|APPROACHES|ILS|VISUALS?|VISA?|VA|RNAV|RNP"
    r"|LOC|LDA|GPS)\b"
)
DEPARTING = re.compile(r"\b(DEPG|DEPTG|DEPARTING|DEPS?|DEPTS?|DEPARTURES?|DEPARTS?|TAKEOFFS?)\b")
# A clause about something broken, closed, not allowed or to watch out for names runways that are not being announced
# as in use.
NOT_IN_USE = re.compile(
    r"\b(OTS|OUT OF SERVICE|U/S|UNUSBL|UNUSABLE|UNAVBL|UNAVAILABLE|NOT AVBL|NOT AVAILABLE|NOT AUTHORIZED|NOT AUTH|NA|OFM"
    r"|DISREGARD|CLSD|CLOSED|BRAKING|FICON|RCC|CONDITION|HOLD SHORT|HDSRT|H/S|CAUTION|EXITING|INCURSIONS?|CROSSING|NOTAMS?|NOTICE"
    r"|LGTS?|LIGHTS?|PAPI|REILS?|MALSR|ALS|TDZ|CRANES?|READBACK|READ BACK|TRANSPONDERS?|ONLY|EXPECT DELAYS?)\b"
)
# Each number whole ((?!\d)), and the list taken as it is found (*+): a list the text does not close is given up
# at once, rather than tried again every way its spaces and commas could be split, which takes a minute on a
# few dozen numbers.
RUNWAY_LIST = r"\d{1,2}(?!\d)[LRC]?(?:\s*(?:,|/|-|&|AND)?\s*(?:RWY\s+|RY\s+)?\d{1,2}(?!\d)[LRC]?)*+"
# "RWY 27L, 9R CLSD": closed outright, with nothing after it that makes it a part of the runway or a time of day.
CLOSED = re.compile(rf"\b(?:RWYS?|RUNWAYS?|RYS?)\s+({RUNWAY_LIST})\s+(?:IS\s+|ARE\s+)?(?:CLSD|CLOSED)(?=\s*(?:,|$|UFN))")
STOP = re.compile(r"\b(AT|TWY|TAXIWAY|INT|INTERSECTION|BTN|BTWN|BETWEEN|VIA|ON|FROM|FT|FEET|FREQ)\b")  # no runway in use follows
# "TWY B BTWN RWY 17R AND RWY 17L CLSD" closes a taxiway: a runway named after one of these is a place, not the subject.
PLACE = re.compile(r"\b(TWY|TAXIWAY|BTN|BTWN|BETWEEN|OF|TO|FROM|NORTH|SOUTH|EAST|WEST|APCH END|DEP END)\b")
IN_USE = re.compile(r"\b(IN USE|ACTIVE)\b")
JOINED = re.compile(r"\s*(?:AND|&|/)\s*")  # "LNDG AND DEPG": the two words are about the same runways
WORD = re.compile(f"{LANDING.pattern}|{DEPARTING.pattern}|{STOP.pattern}|{RUNWAY.pattern}")


@dataclass(frozen=True)
class RealRunways:
    landing: tuple[str, ...] = ()
    departing: tuple[str, ...] = ()
    closed: frozenset = frozenset()

    @property
    def in_use(self) -> frozenset:
        return frozenset(self.landing) | frozenset(self.departing)


def _plain(text: str) -> str:
    """The text with spoken runways written as numbers: 'TWO FOUR LEFT', '2 4 LEFT' and 'RWY24L' are 24L."""
    text = re.sub(r"\s+", " ", text.upper())
    text = re.sub(r"\b(" + "|".join(WORDS) + r")\b", lambda found: WORDS[found.group(1)], text)
    text = re.sub(r"\b(RWYS?|RYS?|RUNWAYS?)(?=\d)", r"\1 ", text)
    text = re.sub(r"\b(\d) (\d)\b(?! ?\d)", r"\1\2", text)  # "2 4" is 24: a runway is said digit by digit
    return re.sub(r"\b(\d{1,2}) ?(" + "|".join(SIDES) + r")\b", lambda found: found.group(1) + SIDES[found.group(2)], text)


def _named(text: str) -> list[str]:
    return [f"{int(number)}{side}" for number, side in RUNWAY.findall(text)]


def read(text: str, kind: str = "combined") -> tuple[list[str], list[str], list[str]]:
    """(landing, departing, closed) runways as an ATIS names them, numbers without a leading zero. `kind` is
    'combined', 'arr' or 'dep': an arrival ATIS says nothing of departures, and a departure ATIS nothing of arrivals."""
    landing: list[str] = []
    departing: list[str] = []
    closed: list[str] = []

    def give(name: str, lands: bool, departs: bool) -> None:
        if lands:
            landing.append(name)
        if departs:
            departing.append(name)

    for sentence in re.split(r"\.(?:\s+|$)", _plain(text[:MAX_TEXT])):  # longer is not an ATIS: not read
        for shut in CLOSED.finditer(sentence):
            if not PLACE.search(sentence[sentence.rfind(",", 0, shut.start()) + 1 : shut.start()]):
                closed += _named(shut.group(1))
        sentence = CLOSED.sub(" CLSD ", sentence)  # what remains of it names no runway in use
        carried = (False, False)  # what the clause before, in this sentence, was about: (landing, departing)
        for clause in sentence.split(","):
            if NOT_IN_USE.search(clause):
                carried = (False, False)
                continue
            lands, departs = carried
            waiting: list[str] = []  # runways named before the clause said what they are for
            keyword_end = None  # where the last keyword ended, while no runway has come since
            list_end = None  # where the last runway of a list begun by "RWYS" ended: "RWYS 24 AND 25"
            for found in WORD.finditer(clause):
                word = found.group(0)
                if STOP.fullmatch(word):
                    break
                if LANDING.fullmatch(word) or DEPARTING.fullmatch(word):
                    is_landing = bool(LANDING.fullmatch(word))
                    if keyword_end is not None and JOINED.fullmatch(clause[keyword_end : found.start()]):
                        lands, departs = lands or is_landing, departs or not is_landing  # "LNDG AND DEPG RWY 27"
                    else:
                        lands, departs = is_landing, not is_landing
                    keyword_end = found.end()
                    # "RWY 1L VISUAL APPROACH" names a landing runway. "RWY 17L DEPARTURES EXPECT ..." is a note about
                    # departures from a runway, in use or not: a departing runway is one named after the word.
                    for name in waiting:
                        give(name, lands, False)
                    waiting = []
                    continue
                keyword_end = None
                number, side = RUNWAY.fullmatch(word).groups()
                name = f"{int(number)}{side or ''}"
                introduced = bool(INTRODUCED.search(clause[max(0, found.start() - LOOK_BACK) : found.start()])) or (
                    list_end is not None and bool(JOINED.fullmatch(clause[list_end : found.start()]))
                )
                list_end = found.end() if introduced else None
                if not side and not introduced:
                    name += "?"  # a bare number in a list: a runway only if the airport has one of exactly that name
                if lands or departs:
                    give(name, lands, departs)
                else:
                    waiting.append(name)
            if waiting and IN_USE.search(clause):
                lands = departs = True  # "RUNWAY 24 IN USE": for landing and for departing
                for name in waiting:
                    give(name, True, True)
            carried = (lands, departs)
    if kind == "arr":
        departing = []
    if kind == "dep":
        landing = []
    return _unique(landing), _unique(departing), _unique(closed)


def _unique(names: list[str]) -> list[str]:
    return list(dict.fromkeys(names))


def runways(airport: Airport, entries: list[dict], now_minutes: int | None = None) -> RealRunways | None:
    """What the airport's ATIS says of its runways, in the airport's own names. None when it says nothing that
    can be used: no ATIS, an old one, or runways that are not this airport's."""
    own = {end.lstrip("0"): end for runway in airport.runways for end in (runway.end1, runway.end2)}
    opposite = {runway.end1: runway.end2 for runway in airport.runways} | {runway.end2: runway.end1 for runway in airport.runways}
    landing: list[str] = []
    departing: list[str] = []
    closed: list[str] = []
    for entry in entries:
        text, kind = str(entry.get("datis") or "")[:MAX_TEXT], str(entry.get("type") or "combined")
        if not text or _old(str(entry.get("time") or ""), now_minutes):
            continue
        lands, departs, shut = read(text, kind)
        landing += lands
        departing += departs
        closed += shut
    closed_ends = {own[name] for name in closed if name in own}
    closed_ends |= {opposite[end] for end in closed_ends}  # a closed runway is closed from both ends

    def known(names: list[str]) -> tuple[str, ...]:
        # "RWYS 4/8" at an airport with 4L, 4R, 8L and 8R is all four.
        names = [
            each for name in names for each in ([name] if name in own or not name.isdigit() else [name + side for side in "LCR"])
        ]
        names = [name.rstrip("?") for name in names]
        ends = [own[name] for name in _unique(names) if name in own and own[name] not in closed_ends]
        # Both ends of one runway in use at once is a text misread, not an airport: neither is taken.
        return tuple(end for end in ends if opposite[end] not in ends)

    landing_ends, departing_ends = known(landing), known(departing)
    # An ATIS that gives only approaches is of an airport that departs the same runways, and the other way round.
    found = RealRunways(landing_ends or departing_ends, departing_ends or landing_ends, frozenset(closed_ends))
    return found if found.in_use or found.closed else None


def _old(issued: str, now_minutes: int | None) -> bool:
    """Whether an ATIS issued at this time ('1256', Zulu) is too old to describe the airport now."""
    if now_minutes is None or not re.fullmatch(r"\d{4}", issued):
        return False
    age = (now_minutes - (int(issued[:2]) * 60 + int(issued[2:]))) % 1440
    return OLD_MIN < age < 1440 - 60  # an hour of grace for a clock that is behind


def publishes(ident: str) -> bool:
    """Whether an airport with this code can have a digital ATIS: the United States and its territories."""
    return len(ident) == 4 and (ident[0] == "K" or ident[:2] in ("PA", "PH", "PG", "TJ"))


def fetch(ident: str) -> list[dict] | None:
    """The airport's ATIS as the service gives it: a list of entries, empty when it has none. None on a failure."""
    try:
        with httpx.stream(
            "GET", URL.format(ident=ident), timeout=TIMEOUT_S, follow_redirects=True, headers={"User-Agent": "ProATC"}
        ) as response:
            if response.status_code == 404:
                return []
            response.raise_for_status()
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_BODY:
                    return None  # far more than an ATIS: not read any further
        data = json.loads(body)
    except (httpx.HTTPError, ValueError):
        return None
    if isinstance(data, dict):
        return []  # {"error": ...}: no digital ATIS at this airport
    return [entry for entry in data if isinstance(entry, dict)][:MAX_ENTRIES] if isinstance(data, list) else None


class RealAtis:
    """The real runways of the airports asked about, kept and refreshed in the background. Asking never waits
    for the network: what is known is returned, and a look-up is started when it is missing or old."""

    def __init__(self, fetch=fetch, background: bool = True, clock=time.monotonic):
        self._fetch, self._background, self._clock = fetch, background, clock
        self._lock = threading.Lock()
        self._known: dict[str, RealRunways | None] = {}
        self._again_at: dict[str, float] = {}
        self._asking: set[str] = set()

    def runways(self, airport: Airport) -> RealRunways | None:
        ident = airport.ident
        if not publishes(ident):
            return None
        with self._lock:
            due = self._clock() >= self._again_at.get(ident, 0.0) and ident not in self._asking
            if due:
                self._asking.add(ident)
        if due:
            if self._background:
                threading.Thread(target=self._look_up, args=(airport,), name=f"datis-{ident}", daemon=True).start()
            else:
                self._look_up(airport)
        with self._lock:
            return self._known.get(ident)

    def _look_up(self, airport: Airport) -> None:
        ident = airport.ident
        try:
            entries = self._fetch(ident)
            now = time.gmtime()
            found = runways(airport, entries, now.tm_hour * 60 + now.tm_min) if entries else None
            wait = RETRY_S if entries is None else FRESH_S if entries else NONE_S
            with self._lock:
                if entries is not None:
                    self._known[ident] = found  # a failure keeps what was known
                self._again_at[ident] = self._clock() + wait
        finally:
            with self._lock:
                self._asking.discard(ident)
