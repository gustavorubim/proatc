"""A stress test that runs everywhere: whole flights across a made-up world, flown the way the app flies them.

The real-data sweep (`proatc verify-flights`) needs X-Plane, so it cannot run in CI. This world is built
here, from nothing, with the things that have broken real flights:

- centres whose boundaries zigzag, so a straight route clips corners and runs along boundaries;
- two centres that work the same frequency, and a centre that shares a frequency with an approach controller;
- cities where several airports list the same approach frequency;
- airports with approach sectors, with a tower and no approach, and with no controller at all;
- a hole in the centres' coverage, and an oceanic sector with no radar;
- airports in the American style, in the European style, and runways without a leading zero.

`fly` is the same function the real-data sweep uses: the station is whatever answers on the tuned frequency
where the aircraft is, every hand-off is tuned and checked in on, and a flight has a problem if it is left on
a silent frequency, sent to the wrong airport's controller, bounced between controllers, or never handed to
its destination. Every flight here must have none.

Set PROATC_STRESS=1 to fly every pair of airports instead of a sample.
"""

import os
import random

import pytest

from proatc.centers import CenterIndex, parse_atc
from proatc.flights import fly
from proatc.navdata import AirportDB, build_index, distance_nm, prefer_airports
from proatc.procedures import parse_cifp

WEST, EAST, SOUTH, NORTH = 0.0, 24.0, 40.0, 52.0
COLUMNS, ROWS = 4, 3
HOLE = (3, 0)  # no centre here: the sim's data has such gaps
SAMPLE = 90


def boundary(column: int, lat: float) -> float:
    """Longitude of the boundary west of `column` at this latitude: a zigzag, straight at the world's edges."""
    base = WEST + column * (EAST - WEST) / COLUMNS
    if column in (0, COLUMNS):
        return base
    return base + (0.45 if int(lat * 2) % 2 == 0 else -0.45)


def centre_rows() -> list[str]:
    lines: list[str] = []
    step = (NORTH - SOUTH) / ROWS
    for row in range(ROWS):
        for column in range(COLUMNS):
            if (column, row) == HOLE:
                continue
            low, high = SOUTH + row * step, SOUTH + (row + 1) * step
            lats = [low + index * 0.5 for index in range(int((high - low) / 0.5) + 1)]
            points = [(lat, boundary(column, lat)) for lat in lats] + [(lat, boundary(column + 1, lat)) for lat in reversed(lats)]
            # Two centres on one frequency, at opposite corners; the rest each have their own.
            freq = 13210 if (column, row) in ((0, 0), (2, 2)) else 12500 + (row * COLUMNS + column) * 25
            lines += ["CONTROLLER", f"NAME SECTOR {column}{row}", f"FACILITY_ID {'KZ' if column == 0 else 'ED'}{column}{row}",
                      "ROLE ctr", f"FREQ {freq}", "AIRSPACE_POLYGON_BEGIN 0 60000"]  # fmt: skip
            lines += [f"POINT {lat:.4f} {lon:.4f}" for lat, lon in points]
            lines += ["AIRSPACE_POLYGON_END", "CONTROLLER_END", ""]
    # An oceanic sector west of the world, without radar.
    lines += ["CONTROLLER", "NAME WESTSEA OCEANIC", "FACILITY_ID EGGX", "ROLE ctr", "FREQ 12790", "AIRSPACE_POLYGON_BEGIN 0 60000",
              f"POINT {SOUTH} -12.0", f"POINT {NORTH} -12.0", f"POINT {NORTH} {WEST}", f"POINT {SOUTH} {WEST}",
              "AIRSPACE_POLYGON_END", "CONTROLLER_END", ""]  # fmt: skip
    return lines


def airport_rows(rng: random.Random) -> tuple[list[str], list[str]]:
    lines = ["A", "1200 test", ""]
    idents: list[str] = []

    def add(ident: str, lat: float, lon: float, kind: str, runway: tuple[str, str], approach_khz: int | None = None) -> None:
        idents.append(ident)
        lines.append(f"1    {rng.randrange(0, 4000)} 0 0 {ident} Field {ident}")
        lines.append(f"1302 icao_code {ident}")
        lines.append(
            f"100 45.00 1 1 0.25 1 3 0  {runway[0]} {lat:.4f} {lon - 0.02:.4f} 0 0 3 2 1 0 {runway[1]} {lat:.4f} {lon + 0.02:.4f} 0 0 3 2 1 0"
        )
        number = len(idents)
        if kind == "major":
            lines.append(f"1050 {127000 + number * 25} ATIS")
            lines.append(f"1053 {121600 + number * 25} Ground")
            lines.append(f"1054 {118000 + number * 25} Tower")
            if number % 3 == 0:  # approach in sectors, as at large airports
                lines.append(f"1055 {approach_khz or 119000 + number * 25} APP (271-090)")
                lines.append(f"1055 {120000 + number * 25} APP (091-270)")
            else:
                lines.append(f"1055 {approach_khz or 119000 + number * 25} Approach")
            lines.append(f"1056 {124000 + number * 25} Departure")
        elif kind == "tower":
            lines.append(f"1054 {118000 + number * 25} Tower")
        elif kind == "info":
            lines.append(f"1054 {118000 + number * 25} INFO")
        else:
            lines.append("1051 122800 UNICOM")
        lines.append("")

    kinds = ["major"] * 9 + ["tower"] * 5 + ["info"] * 2 + ["unicom"] * 2
    for index, kind in enumerate(kinds):
        lat, lon = rng.uniform(SOUTH + 0.4, NORTH - 0.4), rng.uniform(WEST + 0.4, EAST - 0.4)
        american = lon < (EAST - WEST) / COLUMNS
        ident = f"{'K' if american else 'ED'}{'ABCDEFGHJKLMNPQRSTUV'[index]}{'X' if american else ''}{index % 10}"[:4].ljust(
            4, "Z"
        )
        add(ident, lat, lon, kind, ("1C", "19C") if american else ("09", "27"))
    # Two cities: a large airport and a small one eight miles away that lists the same approach frequency,
    # which one of the centres works as well.
    for index, (lat, lon) in enumerate(((44.0, 9.0), (49.0, 16.0))):
        shared = 12500 * 10 + (1 + index) * 250 if index == 0 else 132100  # a centre's frequency, on purpose
        add(f"EDC{index}", lat, lon, "major", ("09", "27"), approach_khz=shared)
        add(f"EDS{index}", lat + 0.13, lon, "major", ("17", "35"), approach_khz=shared)
    return lines, idents


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    folder = tmp_path_factory.mktemp("world")
    rng = random.Random(2026)
    rows, idents = airport_rows(rng)
    (folder / "apt.dat").write_text("\n".join(rows), encoding="utf-8")
    build_index(folder / "apt.dat", folder / "world.sqlite")
    db = AirportDB(folder / "world.sqlite")
    db.centers = CenterIndex(parse_atc(centre_rows()))
    shared = {"centers": db.centers, "procedures": lambda ident: parse_cifp([])}
    airports = {ident: db.airport(ident) for ident in idents}
    pairs = [
        (a, b)
        for a in idents
        for b in idents
        if a != b and 110 <= distance_nm(airports[a].lat, airports[a].lon, airports[b].lat, airports[b].lon) <= 1300
    ]
    yield db, shared, pairs
    prefer_airports([])


def test_the_world_has_what_breaks_flights(world):
    db, _, pairs = world
    centres = db.centers.centers
    assert len(centres) == COLUMNS * ROWS  # eleven sectors and the ocean; one sector missing
    assert sum(1 for centre in centres if centre.has_frequency(132100)) == 2
    assert db.centers.at(SOUTH + 1, EAST - 1) is None and db.centers.at(46.0, -5.0).oceanic
    edge = [boundary(1, SOUTH + step * 0.5) for step in range(6)]
    assert max(edge) - min(edge) > 0.8  # the boundary zigzags
    kinds = {ident: {freq.kind for freq in db.airport(ident).freqs} for pair in pairs for ident in pair}
    assert any("approach" in found for found in kinds.values()) and any(found == {"tower"} for found in kinds.values())
    assert any(found == {"unicom"} for found in kinds.values())
    assert len(pairs) > 300


def test_every_flight_across_the_world_is_handed_all_the_way_to_its_destination(world):
    db, shared, pairs = world
    if not os.environ.get("PROATC_STRESS"):
        pairs = random.Random(7).sample(pairs, SAMPLE)
    failed: list[str] = []
    reached = 0
    for origin, destination in pairs:
        problems, said = fly(db, shared, origin, destination)
        reached += bool(said)
        if problems:
            failed.append(f"{origin}-{destination}: {problems}\n    " + "\n    ".join(said[-6:]))
    assert not failed, f"{len(failed)} of {len(pairs)} flights had a problem:\n" + "\n".join(failed[:8])
    assert reached > len(pairs) * 0.8  # and they were worked, not merely silent


def test_a_flight_can_be_printed_for_a_bug_report(world):
    db, shared, pairs = world
    origin, destination = next(pair for pair in pairs if pair[1] == "EDC0")
    problems, said = fly(db, shared, origin, destination)
    assert problems == [] and said
    assert any("contact" in line for line in said) and "EDC0" in said[-1]  # ends with the destination's own controller
