"""Whole flights gate to gate across the made-up world of test_world.py, flown as `proatc verify-flights --full`
flies them on real data: clearance, push and start, taxi with a runway crossing, takeoff, departure, the centres,
the approach, landing, vacating and the taxi to a stand.

The world is test_world's, with more in it: some airports have a taxiway network with stands, a short second
runway that the way to the main one crosses, and one airport has published procedures (a STAR and an I L S, from
test_ifr's CIFP_EAST). Every flight must have no problem.

Set PROATC_STRESS=1 to fly every pair of towered airports instead of a sample.
"""

import os
import random

import pytest

from proatc.centers import CenterIndex, parse_atc
from proatc.flights import fly_full
from proatc.navdata import AirportDB, build_index, distance_nm, prefer_airports
from proatc.procedures import parse_cifp
from proatc.vfr import INFORMATION
from tests.test_ifr import CIFP_EAST
from tests.test_world import airport_rows, centre_rows

SAMPLE = 24
WITH_PROCEDURES = "EDC0"  # a city airport with a shared approach frequency, runway 09/27
EVERY_OTHER = 2  # every other towered airport gets a taxiway network


def taxiways(lat: float, west: float, east: float) -> list[str]:
    """A taxiway network for a runway along a parallel, from `west` to `east`: a parallel taxiway to the south with
    a link to each end and the middle, and south of it a short runway the way to the apron crosses."""
    middle = (west + east) / 2
    nodes = {
        "rw_west": (lat, west), "rw_middle": (lat, middle), "rw_east": (lat, east),
        "tw_west": (lat - 0.003, west), "tw_middle": (lat - 0.003, middle), "tw_east": (lat - 0.003, east),
        "short_west": (lat - 0.006, middle - 0.0075), "short_middle": (lat - 0.006, middle),
        "short_east": (lat - 0.006, middle + 0.0075), "apron": (lat - 0.009, middle),
    }  # fmt: skip
    number = {name: index for index, name in enumerate(nodes)}
    rows = [f"100 30.00 1 1 0.25 1 3 0  08 {lat - 0.006:.4f} {middle - 0.0075:.4f} 0 0 3 2 1 0 26 {lat - 0.006:.4f} "
            f"{middle + 0.0075:.4f} 0 0 3 2 1 0"]  # fmt: skip
    rows += [f"1201 {at[0]:.6f} {at[1]:.6f} both {number[name]} {name}" for name, at in nodes.items()]
    edges = [
        ("rw_west", "rw_middle", "runway", None), ("rw_middle", "rw_east", "runway", None),
        ("short_west", "short_middle", "runway", "08/26"), ("short_middle", "short_east", "runway", "08/26"),
        ("tw_west", "tw_middle", "taxiway_E", "A"), ("tw_middle", "tw_east", "taxiway_E", "A"),
        ("rw_west", "tw_west", "taxiway_E", "A1"), ("rw_middle", "tw_middle", "taxiway_E", "A2"),
        ("rw_east", "tw_east", "taxiway_E", "A3"), ("tw_middle", "short_middle", "taxiway_E", "B"),
        ("short_middle", "apron", "taxiway_E", "B"),
    ]  # fmt: skip
    rows += [f"1202 {number[a]} {number[b]} twoway {kind} {name or 'MAIN'}" for a, b, kind, name in edges]
    rows += [
        f"1300 {lat - 0.0095:.6f} {middle - 0.0010:.6f} 0.0 gate heavy|jets 1", "1301 E airline",
        f"1300 {lat - 0.0095:.6f} {middle + 0.0010:.6f} 0.0 gate heavy|jets 2", "1301 E airline",
    ]  # fmt: skip
    return rows


def with_taxiways(rows: list[str], idents: set[str]) -> list[str]:
    """The world's airport rows, with a taxiway network added after the runway of each airport in `idents`."""
    out: list[str] = []
    ident = None
    for line in rows:
        out.append(line)
        if line.startswith("1302 icao_code "):
            ident = line.split()[-1]
        elif line.startswith("100 ") and ident in idents:
            tokens = line.split()
            end1, lat, west, end2, east = tokens[8], float(tokens[9]), float(tokens[10]), tokens[17], float(tokens[19])
            network = taxiways(lat, west, east)
            out += [row.replace("MAIN", f"{end1}/{end2}") for row in network]
    return out


def towered(db: AirportDB, ident: str) -> bool:
    return any(freq.kind == "tower" and not INFORMATION.search(freq.label or "") for freq in db.airport(ident).freqs)


@pytest.fixture(scope="module")
def full_world(tmp_path_factory):
    folder = tmp_path_factory.mktemp("full_world")
    rows, idents = airport_rows(random.Random(2026))
    first = folder / "first.sqlite"
    (folder / "first.dat").write_text("\n".join(rows), encoding="utf-8")
    build_index(folder / "first.dat", first)
    with_tower = [ident for ident in idents if towered(AirportDB(first), ident)]
    taxied = set(with_tower[::EVERY_OTHER]) | {WITH_PROCEDURES}
    (folder / "apt.dat").write_text("\n".join(with_taxiways(rows, taxied)), encoding="utf-8")
    build_index(folder / "apt.dat", folder / "world.sqlite")
    db = AirportDB(folder / "world.sqlite")
    db.centers = CenterIndex(parse_atc(centre_rows()))
    procedures = {WITH_PROCEDURES: parse_cifp(CIFP_EAST)}
    shared = {"centers": db.centers, "procedures": lambda ident: procedures.get(ident) or parse_cifp([])}
    airports = {ident: db.airport(ident) for ident in with_tower}
    pairs = [
        (a, b)
        for a in with_tower
        for b in with_tower
        if a != b and 110 <= distance_nm(airports[a].lat, airports[a].lon, airports[b].lat, airports[b].lon) <= 1300
    ]
    yield db, shared, pairs, taxied
    prefer_airports([])


def sample(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if os.environ.get("PROATC_STRESS"):
        return pairs
    # Always the airport with procedures, both ways, and a random sample of the rest.
    chosen = [pair for pair in pairs if WITH_PROCEDURES in pair][:2]
    return chosen + random.Random(11).sample([pair for pair in pairs if pair not in chosen], SAMPLE - len(chosen))


def test_the_world_has_taxiways_a_crossing_and_procedures(full_world):
    db, shared, pairs, taxied = full_world
    assert len(taxied) >= 5 and len(pairs) > 100
    assert shared["procedures"](WITH_PROCEDURES).approaches
    lines = db.record_lines(db.airport(WITH_PROCEDURES))
    assert sum(line.startswith("1300 ") for line in lines) == 2 and any("runway 08/26" in line for line in lines)


def test_every_flight_across_the_world_goes_from_stand_to_stand(full_world):
    db, shared, pairs, taxied = full_world
    failed: list[str] = []
    for origin, destination in sample(pairs):
        problems, said = fly_full(db, shared, origin, destination)
        if problems:
            failed.append(f"{origin}-{destination}: " + "\n    ".join(f"{kind}: {detail}" for kind, detail in problems[:3]))
        elif not said[-1].endswith("parked"):
            failed.append(f"{origin}-{destination}: ended '{said[-1]}'")
    assert not failed, f"{len(failed)} flights had a problem:\n" + "\n".join(failed[:6])


def test_a_flight_between_taxied_airports_crosses_a_runway_and_flies_the_published_approach(full_world):
    db, shared, pairs, taxied = full_world
    origin = next(a for a, b in pairs if b == WITH_PROCEDURES and a in taxied)
    problems, said = fly_full(db, shared, origin, WITH_PROCEDURES)
    assert problems == []
    text = "\n".join(said)
    assert "cross runway zero eight" in text  # out, between the apron and the parallel taxiway
    published = ("cleared I L S approach runway two seven", "cleared V O R D M E approach runway zero niner")
    assert any(approach in text for approach in published) and "cleared to land" in text  # which one: the wind's
    assert "taxi to stand" in text and said[-1].endswith("parked")
