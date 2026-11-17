"""Airports, runways and frequencies from X-Plane's own apt.dat files, indexed into SQLite.

Using the sim's data guarantees that ATC and scenery agree. Custom scenery comes first, in the
order of `Custom Scenery/scenery_packs.ini`, exactly as the sim itself decides which version of
an airport to draw: an add-on airport's taxiways, stands and frequencies replace the default ones.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_DIR

DB_PATH = PROJECT_DIR / "data" / "airports-v3.sqlite"  # bump the name when the schema changes
SCENERY_LIST = Path("Custom Scenery/scenery_packs.ini")
APT_RELATIVE = Path("Global Scenery/Global Airports/Earth nav data/apt.dat")

# apt.dat row codes: 1050-1056 are 8.33 kHz-capable (value in kHz), 50-56 legacy (value in 10 kHz).
FREQ_KINDS = {
    "1050": "atis",
    "1051": "unicom",
    "1052": "delivery",
    "1053": "ground",
    "1054": "tower",
    "1055": "approach",
    "1056": "departure",
}
FREQ_KINDS.update({code[2:]: kind for code, kind in list(FREQ_KINDS.items())})

# How far from the airport each kind of station can be worked, in nautical miles.
RANGE_NM = {"atis": 60, "unicom": 30, "delivery": 10, "ground": 10, "tower": 30, "approach": 60, "departure": 60}
# How high above the airport each kind of station is heard and worked. An airliner in the cruise that is
# tuned to a centre is not talking to the tower of an airfield six miles below that shares the frequency.
CEILING_FT = {
    "atis": 60000,
    "unicom": 10000,
    "delivery": 3000,
    "ground": 3000,
    "tower": 10000,
    "approach": 19500,
    "departure": 19500,
}
STATION_WORD = {
    "atis": "Information",
    "unicom": "Traffic",
    "delivery": "Delivery",
    "ground": "Ground",
    "tower": "Tower",
    "approach": "Approach",
    "departure": "Departure",
}
FREQ_TOLERANCE_KHZ = 5
GROUND_SIDE = ("delivery", "ground", "tower")  # who an aircraft on the ground talks to
INFORMATION_LABEL = re.compile(r"\bAFIS\b|\bInfo(rmation)?\b|\bRadio\b|\bFISO?\b", re.I)  # information, not control
SECTOR = re.compile(r"\((\d{3})\s*-\s*(\d{3})\)")  # "APP (091-240)": the bearings from the airport a sector works

# Around a large city one approach frequency is listed at several airports. Two things decide which of them
# the pilot is talking to, ahead of mere distance: the airports of the flight's own plan, and the station
# already found on that frequency. Shared by every connection to the index in this process.
_PREFERRED: tuple[str, ...] = ()
_HELD: dict[int, tuple[str, str]] = {}


# frequency -> key of the station being talked to on it. A centre and an airport's controller can share a
# frequency: the one in contact stays, and a flight that is handed to a station is answered by that station.
TALKING_TO: dict[int, str] = {}


def expect_station(khz: int, key: str) -> None:
    """A flight has been sent to this station on this frequency."""
    TALKING_TO[khz] = key


def is_preferred(ident: str) -> bool:
    return ident in _PREFERRED


def prefer_airports(idents) -> None:
    """The airports of the flight's plan: on a frequency several airports share, theirs is the station meant."""
    global _PREFERRED
    _PREFERRED = tuple(ident.upper() for ident in idents if ident)
    _HELD.clear()
    TALKING_TO.clear()


SPOKEN_LABEL = re.compile(
    r"([A-Za-z][A-Za-z' ]*?) "
    r"(Tower|Twr|Ground|Gnd|Approach|App|Departure|Dep|Delivery|Del|Clearance|Information|ATIS|Control|Radar|Director|Arrival|Radio)"
    # "Heathrow Tower 1", "De Gaulle Twr North 2", "De Gaulle Arrival NE/NW": the position is not spoken.
    r"(?:[ /]+(?:\d+|North|South|East|West|[NSEW]{1,2}))*",
    re.I,
)
ROLE_WORDS = {"Twr": "Tower", "Gnd": "Ground", "App": "Approach", "Dep": "Departure", "Del": "Delivery", "Atis": "Information"}


@dataclass
class Runway:
    end1: str
    lat1: float
    lon1: float
    end2: str
    lat2: float
    lon2: float
    length_m: float
    surface: int

    def ends(self) -> list[tuple[str, float]]:
        """(ident, true heading when using that end) for both directions."""
        heading = bearing(self.lat1, self.lon1, self.lat2, self.lon2)
        return [(self.end1, heading), (self.end2, (heading + 180) % 360)]


@dataclass
class Freq:
    khz: int
    kind: str
    label: str


@dataclass
class Airport:
    ident: str
    name: str
    lat: float
    lon: float
    elev_ft: float
    runways: list[Runway] = field(default_factory=list)
    freqs: list[Freq] = field(default_factory=list)
    offset: int = 0  # byte range of this airport's record in apt.dat, for loading taxiways on demand
    length: int = 0

    @property
    def spoken_name(self) -> str:
        return clean_name(self.name)


@dataclass
class Station:
    airport: Airport
    kind: str
    khz: int
    label: str
    distance_nm: float

    @property
    def callsign(self) -> str:
        """The name in the sim's data when it is a real callsign ('Luton Tower'), else built from the airport name."""
        match = SPOKEN_LABEL.fullmatch(self.label.strip())
        if not match and self.kind in ("tower", "unicom") and re.search(r"\bAFIS\b|\bFISO?\b|\bInfo\b", self.label, re.I):
            return f"{self.airport.spoken_name} Information"
        if match:
            role = match.group(2).title()
            place = " ".join(match.group(1).split())  # labels in the sim's data sometimes carry doubled spaces
            return f"{place.title()} {ROLE_WORDS.get(role, role)}"
        return f"{self.airport.spoken_name} {STATION_WORD[self.kind]}"

    @property
    def key(self) -> str:
        return f"{self.airport.ident}:{self.kind}"

    @property
    def place(self) -> str:
        """Callsign without the role word: 'Luton'."""
        return self.callsign.rsplit(" ", 1)[0]


def clean_name(name: str) -> str:
    name = re.sub(r"\[.*?\]|\(.*?\)|[-/]", " ", name)
    name = re.sub(r"\b(Intl|International|Airport|Airfield|Regional|Rgnl|Municipal|Muni|Field|Fld)\b\.?", " ", name, flags=re.I)
    return " ".join(name.split()) or name.strip()


def latlon(lat: str, lon: str) -> tuple[float, float]:
    """A latitude and longitude from a data file. Raises ValueError for anything that is not a place on Earth."""
    place = (float(lat), float(lon))
    if not (-90.0 <= place[0] <= 90.0 and -180.0 <= place[1] <= 180.0):  # also false for NaN and infinity
        raise ValueError(f"not a position: {lat} {lon}")
    return place


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 3440.065 * math.asin(math.sqrt(a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


# ---- parsing ----------------------------------------------------------------


def parse_apt(lines: Iterable[str], position=None) -> Iterator[Airport]:
    """Yield land airports that have at least one runway.

    `position`, when given, returns the byte offset of the line being processed.
    """
    current: Airport | None = None
    meta: dict[str, str] = {}

    def finish() -> Airport | None:
        if current is None or not current.runways:
            return None
        if position:
            current.length = position() - current.offset
        if "datum_lat" in meta and "datum_lon" in meta:
            current.lat, current.lon = float(meta["datum_lat"]), float(meta["datum_lon"])
        else:
            points = [(r.lat1, r.lon1) for r in current.runways] + [(r.lat2, r.lon2) for r in current.runways]
            current.lat = sum(p[0] for p in points) / len(points)
            current.lon = sum(p[1] for p in points) / len(points)
        if meta.get("icao_code"):
            current.ident = meta["icao_code"]
        # Legacy rows (10 kHz precision) often repeat an 8.33 kHz row: keep the precise one.
        seen: dict[tuple[str, int], Freq] = {}
        for freq in sorted(current.freqs, key=lambda f: f.khz % 10 == 0):
            seen.setdefault((freq.kind, round(freq.khz / 25)), freq)
        current.freqs = list(seen.values())
        return current

    for line in lines:
        try:
            if not line or line[0] not in "15":
                continue
            code, _, rest = line.partition(" ")
            if code in ("1", "16", "17"):
                done = finish()
                if done:
                    yield done
                current, meta = None, {}
                if code == "1":
                    parts = rest.split(None, 4)
                    if len(parts) >= 4:
                        current = Airport(parts[3], parts[4].strip() if len(parts) > 4 else parts[3], 0.0, 0.0, float(parts[0]))
                        current.offset = position() if position else 0
            elif current is None:
                continue
            elif code == "100":
                t = line.split()
                if len(t) >= 20:
                    (lat1, lon1), (lat2, lon2) = latlon(t[9], t[10]), latlon(t[18], t[19])
                    current.runways.append(
                        Runway(t[8], lat1, lon1, t[17], lat2, lon2, distance_nm(lat1, lon1, lat2, lon2) * 1852, int(t[2]))
                    )
            elif code == "1302":
                parts = rest.split(None, 1)
                if len(parts) == 2:
                    meta[parts[0]] = parts[1].strip()
            elif code in FREQ_KINDS:
                parts = rest.split(None, 1)
                if parts and parts[0].isdigit():
                    khz = int(parts[0]) * (1 if len(code) == 4 else 10)
                    current.freqs.append(Freq(khz, FREQ_KINDS[code], parts[1].strip() if len(parts) > 1 else ""))
        except (ValueError, IndexError, KeyError, OverflowError):
            continue  # a malformed row is skipped, not fatal
    done = finish()
    if done:
        yield done


def find_apt_dat(xplane_path: str | None = None) -> Path:
    candidates = []
    if xplane_path:
        candidates.append(Path(xplane_path))
    # X-Plane's installer records where it put the sim: in a different place on each system.
    for install_file in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "x-plane_install_12.txt",  # Windows
        Path.home() / "Library" / "Preferences" / "x-plane_install_12.txt",  # macOS
        Path.home() / ".x-plane" / "x-plane_install_12.txt",  # Linux
    ):
        if install_file.is_file():
            candidates += [
                Path(line.strip())
                for line in install_file.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip()
            ]
    for root in candidates:
        if (root / APT_RELATIVE).exists():
            return root / APT_RELATIVE
    raise FileNotFoundError("Could not find X-Plane's apt.dat. Set sim.xplane_path in config.toml to your X-Plane 12 folder.")


def scenery_sources(global_apt: Path) -> list[Path]:
    """Every apt.dat the sim reads, highest priority first: custom scenery packs in the order of
    scenery_packs.ini, with the default airports where that file places them."""
    root = global_apt.parents[3]
    listing = root / SCENERY_LIST
    sources: list[Path] = []
    if listing.exists():
        for line in listing.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.startswith("SCENERY_PACK "):
                continue  # SCENERY_PACK_DISABLED and headers
            entry = line[len("SCENERY_PACK ") :].strip()
            if entry == "*GLOBAL_AIRPORTS*":
                sources.append(global_apt)
                continue
            folder = Path(entry) if Path(entry).is_absolute() else root / entry
            apt = folder / "Earth nav data" / "apt.dat"
            if apt.exists():
                sources.append(apt)
    if global_apt not in sources:
        sources.append(global_apt)
    return sources


def _stamp(sources: list[Path]) -> str:
    return "|".join(f"{path}:{path.stat().st_size}:{int(path.stat().st_mtime)}" for path in sources)


# ---- index ------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sources (id INTEGER PRIMARY KEY, path TEXT);
CREATE TABLE airports (ident TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL, elev_ft REAL, offset INT, length INT, source INT);
CREATE TABLE runways (ident TEXT, end1 TEXT, lat1 REAL, lon1 REAL, end2 TEXT, lat2 REAL, lon2 REAL, length_m REAL, surface INT);
CREATE TABLE freqs (ident TEXT, khz INT, kind TEXT, label TEXT);
CREATE INDEX airports_lat ON airports (lat);
CREATE INDEX runways_ident ON runways (ident);
CREATE INDEX freqs_ident ON freqs (ident);
CREATE INDEX freqs_khz ON freqs (khz);
"""


def build_index(apt_path: Path | list[Path], db_path: Path = DB_PATH) -> int:
    """Index one apt.dat, or several in priority order (the first to define an airport wins)."""
    sources = apt_path if isinstance(apt_path, list) else [apt_path]
    apt_path = sources[-1]  # the default airports: the X-Plane folder is found from this path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temp = db_path.with_suffix(".building")
    temp.unlink(missing_ok=True)
    db = sqlite3.connect(temp)
    db.executescript(SCHEMA)
    count = 0
    offset = 0  # byte offset of the line parse_apt is looking at

    def decoded(handle):
        nonlocal offset
        position = 0
        for raw in handle:
            offset = position
            position += len(raw)
            yield raw.decode("utf-8", "ignore").rstrip()
        offset = position

    for number, source in enumerate(sources):
        db.execute("INSERT INTO sources VALUES (?,?)", (number, str(source)))
        with open(source, "rb") as handle:
            for airport in parse_apt(decoded(handle), position=lambda: offset):
                cursor = db.execute(
                    "INSERT OR IGNORE INTO airports VALUES (?,?,?,?,?,?,?,?)",
                    (
                        airport.ident,
                        airport.name,
                        airport.lat,
                        airport.lon,
                        airport.elev_ft,
                        airport.offset,
                        airport.length,
                        number,
                    ),
                )
                if not cursor.rowcount:
                    continue  # already defined by a scenery pack of higher priority
                db.executemany(
                    "INSERT INTO runways VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (airport.ident, r.end1, r.lat1, r.lon1, r.end2, r.lat2, r.lon2, r.length_m, r.surface)
                        for r in airport.runways
                    ],
                )
                db.executemany(
                    "INSERT INTO freqs VALUES (?,?,?,?)", [(airport.ident, f.khz, f.kind, f.label) for f in airport.freqs]
                )
                count += 1
    db.executemany("INSERT INTO meta VALUES (?,?)", [("source", str(apt_path)), ("stamp", _stamp(sources))])
    db.commit()
    db.close()
    temp.replace(db_path)
    return count


class AirportDB:
    def __init__(self, db_path: Path = DB_PATH):
        # One connection shared by the voice loop and the ATIS thread; every use is a short read.
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.centers = None  # centers.CenterIndex, set by the session: en-route stations on the radio
        self.metars = None  # metar.MetarStore, set by the session: reported weather for any airport

    @classmethod
    def open(cls, xplane_path: str | None = None, db_path: Path = DB_PATH) -> AirportDB:
        """Open the index, building it on first use or when apt.dat has changed."""
        apt_path = find_apt_dat(xplane_path)
        sources = scenery_sources(apt_path)
        stamp = _stamp(sources)
        current = None
        if db_path.exists():
            try:
                probe = sqlite3.connect(db_path)
                row = probe.execute("SELECT value FROM meta WHERE key='stamp'").fetchone()
                probe.close()  # an open handle would block replacing the file on Windows
                current = row[0] if row else None
            except sqlite3.Error:
                current = None
        if current != stamp:
            print("Indexing X-Plane airports (one-off, about a minute)...")
            print(f"  {build_index(sources, db_path)} airports indexed.")
        return cls(db_path)

    def airport(self, ident: str) -> Airport | None:
        row = self.db.execute(
            "SELECT ident, name, lat, lon, elev_ft, offset, length FROM airports WHERE ident = ?", (ident.upper(),)
        ).fetchone()
        if not row:
            return None
        airport = Airport(*row[:5], offset=row[5], length=row[6])
        airport.runways = [
            Runway(*r)
            for r in self.db.execute(
                "SELECT end1, lat1, lon1, end2, lat2, lon2, length_m, surface FROM runways WHERE ident = ?", (airport.ident,)
            )
        ]
        airport.freqs = [
            Freq(*f)
            for f in self.db.execute("SELECT khz, kind, label FROM freqs WHERE ident = ? ORDER BY rowid", (airport.ident,))
        ]
        return airport

    def record_lines(self, airport: Airport) -> list[str]:
        """This airport's raw apt.dat rows (taxiways, stands), read straight from the file that defines it."""
        source = self.db.execute(
            "SELECT s.path FROM airports a JOIN sources s ON s.id = a.source WHERE a.ident = ?", (airport.ident,)
        ).fetchone()[0]
        with open(source, "rb") as handle:
            handle.seek(airport.offset)
            return handle.read(airport.length).decode("utf-8", "ignore").splitlines()

    @property
    def xplane_root(self) -> Path:
        """The X-Plane folder this index was built from."""
        source = self.db.execute("SELECT value FROM meta WHERE key='source'").fetchone()[0]
        return Path(source).parents[3]

    def station_of(self, airport: Airport, kind: str, bearing_from: float | None = None) -> Station | None:
        """The airport's own station of this kind, for hand-offs: the sector that works the bearing the
        aircraft is on when the frequencies are labelled by sector, otherwise the first listed."""
        listed = [f for f in airport.freqs if f.kind == kind]
        # An airport can list an information service as a tower ("BREMEN INFO") beside its real tower: the
        # flight is sent to the one that controls traffic.
        listed.sort(key=lambda f: bool(INFORMATION_LABEL.search(f.label or "")))
        freq = listed[0] if listed else None
        if bearing_from is not None:
            for candidate in listed:
                sector = SECTOR.search(candidate.label or "")
                if sector:
                    start, end = int(sector.group(1)), int(sector.group(2))
                    inside = start <= bearing_from <= end if start <= end else bearing_from >= start or bearing_from <= end
                    if inside:
                        freq = candidate
                        break
        return Station(airport, kind, freq.khz, freq.label, 0.0) if freq else None

    def nearest(self, lat: float, lon: float, radius_nm: float = 30, limit: int = 5) -> list[tuple[float, Airport]]:
        dlat = radius_nm / 60
        dlon = dlat / max(math.cos(math.radians(lat)), 0.05)
        rows = self.db.execute(
            "SELECT ident, lat, lon FROM airports WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()
        ranked = sorted((distance_nm(lat, lon, a_lat, a_lon), ident) for ident, a_lat, a_lon in rows)
        return [(dist, self.airport(ident)) for dist, ident in ranked[:limit] if dist <= radius_nm]

    def station(self, khz: int, lat: float, lon: float, feet: float | None = None) -> Station | None:
        """The in-range station transmitting on this frequency, if any: within its distance, and, when the
        aircraft's altitude is given, within its height above the airport."""
        dlat = 61 / 60
        dlon = dlat / max(math.cos(math.radians(lat)), 0.05)
        rows = self.db.execute(
            "SELECT a.ident, a.lat, a.lon, f.khz, f.kind, f.label, a.elev_ft FROM freqs f JOIN airports a ON a.ident = f.ident "
            "WHERE f.khz BETWEEN ? AND ? AND a.lat BETWEEN ? AND ? AND a.lon BETWEEN ? AND ?",
            (khz - FREQ_TOLERANCE_KHZ, khz + FREQ_TOLERANCE_KHZ, lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()
        in_range = []
        for ident, a_lat, a_lon, f_khz, kind, label, elev_ft in rows:
            dist = distance_nm(lat, lon, a_lat, a_lon)
            too_high = feet is not None and feet - (elev_ft or 0.0) > CEILING_FT[kind] and ident not in _PREFERRED
            if dist <= RANGE_NM[kind] and not too_high:
                in_range.append((dist, ident, f_khz, kind, label))
        if not in_range:
            _HELD.pop(khz, None)
            return None
        held = _HELD.get(khz)
        sent_to = [found for found in in_range if f"{found[1]}:{found[3]}" == TALKING_TO.get(khz)]
        planned = [found for found in in_range if found[1] in _PREFERRED]
        kept = [found for found in in_range if held == (found[1], found[3])]
        # The station the flight was sent to, else the flight's own airport, else the station already being talked
        # to, else the nearest. One airport can list a frequency twice (tower and departure): on the ground it is
        # the tower, ground or delivery that answers.
        ground_side = feet is None
        best = min(
            sent_to or planned or kept or in_range,
            key=lambda found: (found[0], found[1], ground_side and found[3] not in GROUND_SIDE, found[2:]),
        )
        _HELD[khz] = (best[1], best[3])
        dist, ident, f_khz, kind, label = best
        return Station(self.airport(ident), kind, f_khz, label, dist)
