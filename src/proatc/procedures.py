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
ALT_DESCR, ALT1, TRANS_ALT = 22, 23, 25

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
APPROACH_IDENT = re.compile(r"([A-Z])(\d\d[LRC]?)-?([A-Z])?")


@dataclass
class Leg:
    fix: str
    alt_descr: str = ""
    altitude_ft: int | None = None


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

    @property
    def spoken(self) -> str:
        return APPROACH_TYPES.get(self.kind, "instrument")


@dataclass
class Procedures:
    sids: dict[str, Procedure] = field(default_factory=dict)
    stars: dict[str, Procedure] = field(default_factory=dict)
    approaches: dict[str, Approach] = field(default_factory=dict)
    transition_altitude_ft: int | None = None


def _altitude(text: str) -> int | None:
    text = text.strip()
    if text.startswith("FL") and text[2:].isdigit():
        return int(text[2:]) * 100
    return int(text) if text.isdigit() else None


def parse_cifp(lines) -> Procedures:
    found = Procedures()
    finals: dict[str, list[int]] = {}
    for line in lines:
        kind, _, rest = line.partition(":")
        if kind not in ("SID", "STAR", "APPCH"):
            continue
        f = [part.strip() for part in rest.rstrip(";\r\n").split(",")]
        if len(f) <= TRANS_ALT:
            continue
        altitude = _altitude(f[ALT1])
        if kind == "APPCH":
            match = APPROACH_IDENT.fullmatch(f[NAME])
            if not match:
                continue
            approach = found.approaches.setdefault(f[NAME], Approach(f[NAME], match.group(1), match.group(2)))
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
        procedure.transitions.setdefault(f[TRANSITION] or "ALL", []).append(Leg(f[FIX], f[ALT_DESCR], altitude))
        transition = _altitude(f[TRANS_ALT])
        if kind == "SID" and transition:
            found.transition_altitude_ft = transition
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


def choose_approach(procedures: Procedures, runway: str) -> Approach | None:
    """The most precise approach published for this runway."""
    order = list(APPROACH_TYPES)
    options = [a for a in procedures.approaches.values() if a.runway == runway and a.kind in order]
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
