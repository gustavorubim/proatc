"""SIDs, STARs and approaches from X-Plane's CIFP files (Custom Data first, so Navigraph wins).

One file per airport, one leg per line, ARINC 424 fields separated by commas:

    SID:010,2,BPK7F,RW27R,D130B,EG,P,C,...,<alt descr>,<alt 1>,<alt 2>,<transition alt>,...
    STAR:010,2,LAM1X,ALL,LAM,EG,D, ,...
    APPCH:030,A,I27R,BIG,CF27R,...        (route type A: a transition to the approach)
    APPCH:010,I,I27R, ,CF27R,...          (route type = approach type: the final segment)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import phraseology as say

CIFP_DIRS = ("Custom Data/CIFP", "Resources/default data/CIFP")
NAV_FILES = ("Custom Data/earth_nav.dat", "Resources/default data/earth_nav.dat")

# Field positions after the "SID:" / "STAR:" / "APPCH:" prefix.
SEQ, ROUTE_TYPE, NAME, TRANSITION, FIX = 0, 1, 2, 3, 4
DESCRIPTION, PATH = 8, 11  # waypoint description code (its fourth letter is the fix's role), path terminator
ALT_DESCR, ALT1, ALT2, TRANS_ALT, SPEED_DESCR, SPEED = 22, 23, 24, 25, 26, 27
IAF_ROLES = "ACD"  # fourth letter of the description code: an initial approach fix
FAF_ROLE = "F"

# Approach type letter -> how a controller says it, best first.
APPROACH_TYPES = {
    "I": "I L S",
    "J": "G L S",
    "L": "localiser",
    "X": "L D A",
    "H": "R N P",
    "R": "R NAV",
    "P": "G P S",
    "D": "V O R D M E",
    "V": "V O R",
    "S": "V O R",
    "Q": "N D B D M E",
    "N": "N D B",
}
# What a pilot asks for -> the approach type letters that answer it.
APPROACH_FAMILIES = {
    "ILS": "IJ",
    "LOC": "LX",
    "RNAV": "RHP",
    "VOR": "DVS",
    "NDB": "QN",
}
APPROACH_IDENT = re.compile(r"([A-Z])(\d\d[LRC]?)-?([A-Z])?")


@dataclass
class Leg:
    fix: str
    alt_descr: str = ""  # "" or "@" at, "+" at or above, "-" at or below, "B" between altitude_ft (upper) and altitude2_ft
    altitude_ft: int | None = None
    altitude2_ft: int | None = None
    speed_kt: int | None = None  # a speed limit at this fix
    role: str = ""  # A, C or D: initial approach fix; F: final approach fix; M: missed approach point
    path: str = ""  # ARINC 424 path terminator: IF, TF, CF, DF, HM, ...


@dataclass
class Procedure:
    name: str
    transitions: dict[str, list[Leg]] = field(default_factory=dict)  # transition -> legs in order

    def runways(self) -> set[str] | None:
        """Runway ends this procedure serves; None means all."""
        ends: set[str] = set()
        for transition in self.transitions:
            if transition.startswith("RW"):
                ident = transition[2:]
                ends |= {ident[:-1] + side for side in "LR"} if ident.endswith("B") else {ident}
        return ends or None

    def serves(self, runway: str) -> bool:
        runways = self.runways()
        return runways is None or runway in runways

    def fixes(self) -> list[str]:
        """Every named fix, in file order, without repeats."""
        seen: dict[str, None] = {}
        for legs in self.transitions.values():
            for leg in legs:
                if leg.fix:
                    seen.setdefault(leg.fix)
        return list(seen)

    def end_fixes(self) -> set[str]:
        """The last named fix of each transition: where a SID leaves you, where a STAR ends."""
        return {
            next(leg.fix for leg in reversed(legs) if leg.fix)
            for legs in self.transitions.values()
            if any(leg.fix for leg in legs)
        }

    def start_fixes(self) -> set[str]:
        return {next(leg.fix for leg in legs if leg.fix) for legs in self.transitions.values() if any(leg.fix for leg in legs)}

    def chain(self, kind: str, runway: str | None, route: list[str]) -> list[Leg]:
        """The legs as they are flown for this runway and this filed route.

        A SID is its runway part, then the part common to all runways, then the en-route transition
        that ends at a fix on the route. A STAR is the en-route transition that starts at a fix on the
        route, then the common part, then the part for the landing runway.
        """
        runway_parts, common, enroute = [], [], []
        for name, legs in self.transitions.items():
            if name.startswith("RW"):
                ident = name[2:]
                ends = {ident[:-1] + side for side in "LRC"} | {ident[:-1]} if ident.endswith("B") else {ident}
                if runway is None or runway in ends:
                    runway_parts.append(legs)
            elif name in ("ALL", ""):
                common.append(legs)
            else:
                enroute.append((name, legs))
        wanted = [legs for name, legs in enroute if name in route]
        if not wanted and len(enroute) == 1 and not common:
            wanted = [enroute[0][1]]  # the only way in or out
        order = [runway_parts[:1], common, wanted[:1]] if kind == "SID" else [wanted[:1], common, runway_parts[:1]]
        flown: list[Leg] = []
        for part in order:
            for legs in part:
                for leg in legs:
                    if flown and leg.fix and leg.fix == flown[-1].fix:
                        # Where two parts join, the fix appears in both: keep whichever carries the restriction.
                        if leg.altitude_ft or leg.speed_kt:
                            flown[-1] = leg
                        continue
                    flown.append(leg)
        return flown

    def top_altitude(self) -> int | None:
        """The level-off altitude of a SID: its highest 'at' or 'at or below' restriction."""
        limits = [leg.altitude_ft for legs in self.transitions.values() for leg in legs
                  if leg.altitude_ft and leg.alt_descr in ("", "-", "B")]  # fmt: skip
        return max(limits) if limits else None


@dataclass
class Approach:
    ident: str  # I27R
    kind: str  # I
    runway: str  # 27R
    platform_ft: int | None = None  # altitude at which the final approach is intercepted
    missed_ft: int | None = None  # the altitude the published missed approach climbs to
    missed_fix: str | None = None  # where the missed approach ends (usually a hold)
    transitions: dict[str, list[Leg]] = field(default_factory=dict)  # initial approach fix -> legs to the final segment
    final: list[Leg] = field(default_factory=list)  # the final segment, up to the runway
    faf: str | None = None  # the final approach fix

    def initial_fixes(self) -> list[tuple[str, int | None]]:
        """(fix, altitude to cross it at) for each published way into the approach."""
        found = []
        for name, legs in self.transitions.items():
            first = next((leg for leg in legs if leg.fix), None)
            if first:
                altitude = first.altitude_ft or next((leg.altitude_ft for leg in legs if leg.altitude_ft), None)
                found.append((first.fix or name, altitude))
        return found

    @property
    def spoken(self) -> str:
        return APPROACH_TYPES.get(self.kind, "instrument")


@dataclass
class Procedures:
    sids: dict[str, Procedure] = field(default_factory=dict)
    stars: dict[str, Procedure] = field(default_factory=dict)
    approaches: dict[str, Approach] = field(default_factory=dict)
    transition_altitude_ft: int | None = None


def _padded(runway: str) -> str:
    """A runway as the procedure data writes it: '1C' is '01C'."""
    digits = len(runway) - len(runway.lstrip("0123456789"))
    return f"0{runway}" if digits == 1 else runway


def use_airport_runway_names(procedures: Procedures, idents: list[str]) -> Procedures:
    """Rename the runways in an airport's procedures to the names its airport data uses.

    In the United States the sim's airports call a runway '1C' or '4R' while the procedure data says
    '01C' and '04R'. Left as they are, no approach, SID or STAR is ever found for runways 1 to 9."""
    names = {_padded(ident): ident for ident in idents if _padded(ident) != ident}
    if not names:
        return procedures
    for approach in procedures.approaches.values():
        approach.runway = names.get(approach.runway, approach.runway)

    def renamed(transition: str) -> str:
        if not transition.startswith("RW"):
            return transition
        ident = transition[2:]
        if ident.endswith("B") and f"{ident[:-1]}L" in names:  # 'RW04B': both 4L and 4R
            return f"RW{names[f'{ident[:-1]}L'][:-1]}B"
        return f"RW{names.get(ident, ident)}"

    for procedure in (*procedures.sids.values(), *procedures.stars.values()):
        procedure.transitions = {renamed(name): legs for name, legs in procedure.transitions.items()}
    return procedures


def _altitude(text: str) -> int | None:
    text = text.strip()
    if text.startswith("FL") and text[2:].isdigit():
        return int(text[2:]) * 100
    return int(text) if text.isdigit() else None


def parse_cifp(lines) -> Procedures:
    found = Procedures()
    finals: dict[str, list[int]] = {}
    past_runway: set[str] = set()  # approaches whose final segment has reached the runway: the rest is the missed approach
    for line in lines:
        try:
            kind, _, rest = line.partition(":")
            if kind not in ("SID", "STAR", "APPCH"):
                continue
            f = [part.strip() for part in rest.rstrip(";\r\n").split(",")]
            if len(f) <= TRANS_ALT:
                continue
            altitude = _altitude(f[ALT1])
            speed = f[SPEED] if len(f) > SPEED else ""
            leg = Leg(
                f[FIX], f[ALT_DESCR], altitude, _altitude(f[ALT2]), int(speed) if speed.isdigit() else None,
                f[DESCRIPTION][3:4].strip() if len(f[DESCRIPTION]) >= 4 else "", f[PATH],
            )  # fmt: skip
            if kind == "APPCH":
                match = APPROACH_IDENT.fullmatch(f[NAME])
                if not match:
                    continue
                approach = found.approaches.setdefault(f[NAME], Approach(f[NAME], match.group(1), match.group(2)))
                if f[ROUTE_TYPE] == "A":
                    approach.transitions.setdefault(f[TRANSITION] or f[FIX], []).append(leg)
                elif f[NAME] not in past_runway:
                    approach.final.append(leg)
                    if leg.role == FAF_ROLE and leg.fix:
                        approach.faf = leg.fix
                if f[ROUTE_TYPE] != "A":
                    if f[NAME] in past_runway:
                        if altitude:
                            approach.missed_ft = max(approach.missed_ft or 0, altitude)
                        if f[FIX] and not f[FIX].startswith("RW"):
                            approach.missed_fix = f[FIX]
                        continue
                    if f[FIX].startswith("RW"):
                        past_runway.add(f[NAME])
                # The final segment (route type = approach type) and the last leg of each transition
                # carry the altitude the approach is joined at.
                if altitude and f[ROUTE_TYPE] != "A" and not f[FIX].startswith("RW"):
                    finals.setdefault(f[NAME], []).append(altitude)
                elif altitude and f[FIX][:2] in ("CF", "FF", "CI", "FI"):
                    finals.setdefault(f[NAME], []).append(altitude)
                del approach
                continue
            table = found.sids if kind == "SID" else found.stars
            procedure = table.setdefault(f[NAME], Procedure(f[NAME]))
            procedure.transitions.setdefault(f[TRANSITION] or "ALL", []).append(leg)
            transition = _altitude(f[TRANS_ALT])
            if kind == "SID" and transition:
                found.transition_altitude_ft = transition
        except (ValueError, IndexError, KeyError, OverflowError):
            continue  # a malformed row is skipped, not fatal
    for ident, altitudes in finals.items():
        found.approaches[ident].platform_ft = max(altitudes)
    return found


def load(xplane_root: Path, ident: str) -> Procedures:
    for folder in CIFP_DIRS:
        path = xplane_root / folder / f"{ident.upper()}.dat"
        if path.exists():
            return parse_cifp(path.read_text(encoding="utf-8", errors="ignore").splitlines())
    return Procedures()


# ---- choosing ---------------------------------------------------------------


def route_fixes(route: list[str]) -> list[str]:
    """The waypoints of a filed route: no airways, no DCT, no speed/level groups, no procedure names."""
    fixes = []
    for token in route:
        token = token.upper().split("/")[0]
        if token in ("DCT", "SID", "STAR") or not token.isalpha() or not 2 <= len(token) <= 5:
            continue
        fixes.append(token)
    return fixes


def choose_sid(procedures: Procedures, runway: str | None, route: list[str], filed: str | None = None) -> Procedure | None:
    """The filed SID if it serves the runway, otherwise the one that ends at the route's first fix."""
    usable = [sid for sid in procedures.sids.values() if runway is None or sid.serves(runway)]
    if filed:
        for sid in usable:
            if sid.name == filed.upper():
                return sid
    fixes = route_fixes(route)
    for fix in fixes[:2]:
        matches = sorted((sid for sid in usable if fix in sid.end_fixes()), key=lambda sid: sid.name)
        if matches:
            return matches[0]
    return None


def choose_star(procedures: Procedures, route: list[str], filed: str | None = None) -> Procedure | None:
    """The filed STAR, otherwise the one that starts at one of the route's last fixes."""
    if filed and filed.upper() in procedures.stars:
        return procedures.stars[filed.upper()]
    fixes = route_fixes(route)
    for fix in reversed(fixes[-2:]):
        matches = sorted((star for star in procedures.stars.values() if fix in star.start_fixes()), key=lambda s: s.name)
        if matches:
            return matches[0]
    return None


def choose_approach(procedures: Procedures, runway: str, family: str | None = None) -> Approach | None:
    """The most precise approach published for this runway, or the best of one family ('RNAV', 'VOR', ...)."""
    order = list(APPROACH_TYPES)
    kinds = APPROACH_FAMILIES.get(family, "") if family else order
    options = [a for a in procedures.approaches.values() if a.runway == runway and a.kind in order and a.kind in kinds]
    return min(options, key=lambda a: (order.index(a.kind), a.ident), default=None)


def circling_approach(procedures: Procedures, runways: list[str]) -> Approach | None:
    """An approach to another runway of the airport, to circle from: the most precise one."""
    order = list(APPROACH_TYPES)
    options = [a for a in procedures.approaches.values() if a.runway in runways and a.kind in order]
    return min(options, key=lambda a: (order.index(a.kind), a.ident), default=None)


# ---- speaking ---------------------------------------------------------------


class NavaidNames:
    """Names of radio navaids, to say 'Brookmans Park' for BPK. Loaded on first use."""

    def __init__(self, xplane_root: Path | None):
        self.root = xplane_root
        self._names: dict[str, list[tuple[float, float, str]]] | None = None

    def _load(self) -> None:
        self._names = {}
        if not self.root:
            return
        path = next((self.root / name for name in NAV_FILES if (self.root / name).exists()), None)
        if not path:
            return
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            t = line.split(None, 10)
            # VOR (3), NDB (2) and standalone DME (13) rows: type lat lon elev freq range var ident area region name
            if len(t) == 11 and t[0] in ("2", "3", "13"):
                name = re.sub(r"\s+(VOR|VORTAC|VOR-DME|VOR/DME|NDB|DME|TACAN|NDB-DME|DME-ILS)\b.*$", "", t[10].strip())
                self._names.setdefault(t[7], []).append((float(t[1]), float(t[2]), name))

    def name(self, ident: str, lat: float, lon: float) -> str | None:
        """The navaid with this ident nearest the given position, if it is within about 200 NM."""
        if self._names is None:
            self._load()
        options = self._names.get(ident, [])
        if not options:
            return None
        nearest = min(options, key=lambda o: (o[0] - lat) ** 2 + (o[1] - lon) ** 2)
        if abs(nearest[0] - lat) > 3.5 or abs(nearest[1] - lon) > 6:
            return None
        return nearest[2].title()


def spoken_name(procedure: Procedure, navaids: NavaidNames | None = None, lat: float = 0.0, lon: float = 0.0) -> str:
    """'BPK7F' -> 'Brookmans Park seven Foxtrot'; 'ULTI1J' -> 'Ultib one Juliett'; 'HAROB6' -> 'Harob six'."""
    match = re.fullmatch(r"([A-Z]+)(\d)([A-Z]?)", procedure.name)
    if not match:
        return " ".join(say.PHONETIC.get(c, c) for c in procedure.name)
    stem, number, suffix = match.groups()
    word = None
    if len(stem) <= 3 and navaids:
        word = navaids.name(stem, lat, lon)
    if not word:
        # Procedure names are cut to fit; the full waypoint is one of the procedure's fixes.
        full = next((fix for fix in procedure.fixes() if fix.startswith(stem) and fix.isalpha()), stem)
        word = full.title() if len(full) > 3 else " ".join(say.PHONETIC[c] for c in full)
    tail = f" {say.PHONETIC[suffix]}" if suffix else ""
    return f"{word} {say.DIGITS[int(number)]}{tail}"
