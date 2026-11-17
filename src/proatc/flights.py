"""Whole flights, anywhere in the world, on this machine's X-Plane data: `proatc verify-flights`.

Unit tests hand the engine the station they mean. A real flight does not: the station is whatever
answers on the tuned frequency where the aircraft is, and that is where flights have gone wrong
(a centre that fell silent at its boundary, a neighbouring airport's approach controller answering,
a runway whose approach was never found). This flies hundreds of flights the way the radio loop and a
copilot do, and checks what must hold on every one of them:

- once in contact, never more than a few miles with nobody on the tuned frequency;
- the flight is sent to the destination's own approach or tower, and that controller sets up the arrival;
- no centre sends the flight to another centre a few miles from the airport;
- no bouncing between the same controllers;
- no sentence with a hole in it, and no exception.
"""

from __future__ import annotations

import random
import time
import traceback
from collections import Counter

from .airways import open_airways
from .atis import Issue
from .centers import CenterIndex
from .engine import Engine
from .flightplan import FlightPlan
from .intents import Intent
from .navdata import AirportDB, bearing, distance_nm
from .navfix import open_navfix
from .procedures import NavaidNames
from .radio import current_station
from .verify import BAD_TEXT, Report
from .weather import Cloud, Weather

SPEED_MS = 230.0
WEATHER = Weather(wind_dir_true=270, wind_kt=9, clouds=[Cloud(0.4, 3500)], qnh_hpa=1009)
SILENT_MILES = 6  # this long with nobody on the tuned frequency, after first contact, is a fault
TOO_MANY_HANDOFFS = 14
CLOSE_IN_NM = 30
BOUNCE_WITHIN_NM = 60  # sent to the same controller twice within this distance


def pick_flights(db: AirportDB, count: int, seed: int = 1) -> list[tuple[str, str]]:
    """Pairs of airports that have a tower or an approach controller, 120 to 1,200 miles apart, worldwide."""
    rows = db.db.execute(
        "SELECT DISTINCT a.ident, a.lat, a.lon FROM airports a JOIN freqs f ON f.ident = a.ident "
        "WHERE f.kind IN ('tower', 'approach') ORDER BY a.ident"
    ).fetchall()
    rng = random.Random(seed)
    rng.shuffle(rows)
    pairs: list[tuple[str, str]] = []
    for ident, lat, lon in rows:
        if len(pairs) >= count:
            break
        for _ in range(30):
            other, other_lat, other_lon = rng.choice(rows)
            if other != ident and 120 <= distance_nm(lat, lon, other_lat, other_lon) <= 1200:
                pairs.append((ident, other))
                break
    return pairs


def shared_data(db: AirportDB) -> dict:
    """What every engine of a sweep shares: read once."""
    root = db.xplane_root
    db.centers = CenterIndex.open(root)
    return {"centers": db.centers, "navaids": NavaidNames(root), "navfix": open_navfix(root), "airways": open_airways(root)}


def fly(db: AirportDB, shared: dict, origin_ident: str, destination_ident: str) -> tuple[list[tuple[str, str]], list[str]]:
    """One flight from the climb to the destination controller's first instruction.

    Each hand-off is tuned and checked in on, as a copilot does. Returns (problems as (kind, detail),
    the radio work as lines of text)."""
    origin, destination = db.airport(origin_ident), db.airport(destination_ident)
    engine = Engine(db, seed=1, **shared)
    total = distance_nm(origin.lat, origin.lon, destination.lat, destination.lon)
    cruise = 36000 if total > 300 else 24000 if total > 160 else 12000
    engine.set_plan(FlightPlan(origin_ident, destination_ident, [], cruise))
    engine.flight.has_been_airborne, engine.flight.origin_ident = True, origin_ident
    heading = bearing(origin.lat, origin.lon, destination.lat, destination.lon)
    problems: list[tuple[str, str]] = []
    said: list[str] = []
    feet, khz, silent, handoffs, in_contact = float(cruise), None, 0, 0, False
    sent_to: Counter = Counter()
    last_sent: dict[str, float] = {}
    for step in range(int(min(40.0, total * 0.25)), int(total) - 3):
        part = step / total
        lat = origin.lat + (destination.lat - origin.lat) * part
        lon = origin.lon + (destination.lon - origin.lon) * part
        remaining = total - step
        cleared = engine.flight.cleared_ft or cruise
        feet = max(cleared, feet - 320) if feet > cleared else feet
        raw = {
            "tail_number": "N123AB", "lat": lat, "lon": lon, "on_ground": 0, "elevation_m": feet * 0.3048,
            "agl_m": max(feet - destination.elev_ft, 500) * 0.3048, "groundspeed_ms": SPEED_MS, "heading_true": heading,
            "heading_mag": heading, "ias_kt": 280.0 if feet > 12000 else 240.0, "aircraft_type": "A20N", "zulu_seconds": 43200.0,
        }  # fmt: skip
        if khz is None:
            # First contact: the centre for where the aircraft is. Where the data has none, nobody is called
            # until the destination is in range, as a pilot would do from the frequency list.
            here = engine.centers.at(lat, lon, feet) if engine.centers else None
            if here and here.freqs_khz:
                khz = here.freqs_khz[0]
            elif remaining <= 45:
                target = engine._handoff(destination, "approach", raw) or engine._handoff(destination, "tower", raw)
                if not target:
                    return problems, said
                khz = round(float(target[2]) * 1000)
            else:
                continue
        raw["com1_khz"] = khz
        _, station = current_station(db, raw)
        if station is None:
            silent += 1
            if silent == SILENT_MILES and in_contact:
                here = engine.centers.at(lat, lon, feet) if engine.centers else None
                if here and here.freqs_khz and not engine.flight.told_approach:
                    # There is a centre for this airspace and the flight was never sent to it.
                    problems.append(
                        ("left on a dead frequency", f"{khz / 1000:.3f}, {remaining:.0f} NM out, in {here.name}'s airspace")
                    )
                elif engine.flight.told_approach:
                    problems.append(("sent to a frequency nobody answers on", f"{khz / 1000:.3f}, {remaining:.0f} NM out"))
                # Otherwise the sim's data has no centre here. The pilot looks for the next one, as from the side panel.
                khz, in_contact = None, False
            continue
        silent = 0
        reply = None
        issue = Issue("K", WEATHER, 43200.0) if station.airport is not None else None
        if station.key not in engine.flight.contacted:
            reply = engine.handle(Intent("check_in"), station, issue, raw, 0.0)
            in_contact = True
        reply = reply or engine.tick(station, issue, raw)
        if station.airport is not None and station.airport.ident != destination_ident and engine.flight.arrival is not None:
            problems.append(
                ("vectored to another airport", f"{station.callsign} of {station.airport.ident} for {destination_ident}")
            )
            return problems, said
        if station.airport is not None and station.airport.ident == destination_ident:
            # The destination's controller: it must have something to say, and set up the arrival.
            said.append(f"{remaining:5.0f} NM FL{feet / 100:03.0f} {station.callsign}: {reply.text if reply else '(nothing)'}")
            if reply is None or not reply.text:
                problems.append(("the destination's controller gave no instruction", station.callsign))
            elif BAD_TEXT.search(reply.text):
                problems.append(("reads wrong", reply.text))
            elif station.kind in ("approach", "departure") and engine.flight.arrival is None:
                problems.append(("approach did not set up the arrival", f"{station.callsign}: {reply.text[:90]}"))
            return problems, said
        if reply is None or not reply.text:
            continue
        said.append(f"{remaining:5.0f} NM FL{feet / 100:03.0f} {station.callsign}: {reply.text}")
        if BAD_TEXT.search(reply.text):
            problems.append(("reads wrong", reply.text))
        engine.flight.pending = None
        if "altitude" in reply.expect:
            engine.flight.cleared_ft = reply.expect["altitude"]
        if "frequency" in reply.expect:
            handoffs += 1
            khz = round(float(reply.expect["frequency"]) * 1000)
            to = reply.text.split("contact ", 1)[-1]
            sent_to[to] += 1
            if not engine.flight.told_approach and remaining < CLOSE_IN_NM:
                problems.append(("sent to another centre close in", f"{to}, {remaining:.0f} NM out"))
            again_soon = to in last_sent and last_sent[to] - remaining < BOUNCE_WITHIN_NM
            last_sent[to] = remaining
            if handoffs > TOO_MANY_HANDOFFS or again_soon:
                problems.append(("bounced between controllers", "; ".join(f"{name} x{n}" for name, n in sent_to.most_common(3))))
                return problems, said
    if in_contact and khz and (engine._handoff(destination, "approach") or engine._handoff(destination, "tower")):
        problems.append(("never reached the destination's controller", f"last tuned {khz / 1000:.3f}"))
    return problems, said


def sweep_flights(db: AirportDB, pairs: list[tuple[str, str]], progress=None) -> Report:
    """Fly each pair and collect what broke, in the same shape of report as the airport sweep."""
    report = Report()
    shared = shared_data(db)
    started = time.perf_counter()
    for number, (origin, destination) in enumerate(pairs):
        if progress and number % 50 == 0:
            progress(f"{number}/{len(pairs)} {origin} to {destination}")
        report.airports += 1
        report.checks["flight"] += 1
        name = f"{origin}-{destination}"
        try:
            problems, _ = fly(db, shared, origin, destination)
        except Exception:
            report.failures.append((name, "flight", traceback.format_exc(limit=5).strip()))
            continue
        for kind, detail in problems:
            report.failures.append((name, "flight", f"{kind}: {detail}"))
    report.seconds = time.perf_counter() - started
    return report
