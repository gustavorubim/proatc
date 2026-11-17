"""A sweep of the engine over the real airports of an X-Plane installation.

Unit tests use a handful of invented airports. The sim has about thirty thousand real ones, with
every oddity real data has: runways without numbers, procedures that reference fixes nobody can
place, taxi networks with one-way loops, frequencies with no label. This runs the engine's main
paths at many airports and reports every exception and every controller sentence that reads wrong.

Run it with `proatc verify-data` on a machine with X-Plane installed. Nothing is written to the sim.
"""

# ruff: noqa: B023  (every lambda here is called at once, inside the loop that makes it)
from __future__ import annotations

import math
import random
import re
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field

from . import chatter, restrictions, vfr
from .airspace import open_airspace
from .airways import open_airways
from .atis import Issue, compose, runways_in_use
from .centers import CenterIndex
from .engine import Engine
from .flightplan import FlightPlan
from .intents import Intent
from .navdata import AirportDB
from .navfix import open_navfix
from .procedures import NavaidNames, choose_approach, route_fixes, spoken_name
from .taxi import assign_stand, route_to_point, route_to_runway
from .weather import Cloud, Weather

WINDS = (0, 90, 180, 270)
# What a controller's sentence must never contain: a value that was not filled in, or broken joins.
BAD_TEXT = re.compile(r"None|\bnan\b|\{|\}|, ,|,,|  |,$|, $|\bat \.|runway ,|heading ,|\(\)")


@dataclass
class Report:
    airports: int = 0
    checks: Counter = field(default_factory=Counter)
    failures: list[tuple[str, str, str]] = field(default_factory=list)  # (airport, check, what went wrong)
    seconds: float = 0.0

    def summary(self) -> str:
        lines = [
            f"{self.airports} airports, {sum(self.checks.values())} checks in {self.seconds:.0f} s, {len(self.failures)} failures"
        ]
        for check, count in sorted(self.checks.items()):
            failed = sum(1 for _, name, _ in self.failures if name == check)
            lines.append(f"  {check:22} {count:6} run {failed:5} failed")
        kinds = Counter((check, what.splitlines()[-1][:110]) for _, check, what in self.failures)
        for (check, what), count in kinds.most_common(25):
            where = [ident for ident, name, text in self.failures if name == check and text.splitlines()[-1][:110] == what][:5]
            lines.append(f"  x{count:<4} {check}: {what}   e.g. {' '.join(where)}")
        return "\n".join(lines)


def pick_airports(db: AirportDB, count: int, seed: int = 1) -> list[str]:
    """Half the airports with the most frequencies (the big ones), half at random from the rest that have any."""
    rows = db.db.execute("SELECT ident, COUNT(*) FROM freqs GROUP BY ident ORDER BY COUNT(*) DESC, ident").fetchall()
    idents = [ident for ident, _ in rows]
    big = idents[: count // 2]
    rest = idents[count // 2 :]
    random.Random(seed).shuffle(rest)
    return big + rest[: count - len(big)]


def _raw(airport, north_nm: float, east_nm: float, feet: float, heading: float, ground: bool = False) -> dict:
    lat = airport.lat + north_nm / 60
    lon = airport.lon + east_nm / (60 * max(math.cos(math.radians(airport.lat)), 0.05))
    return {
        "tail_number": "N123AB", "lat": lat, "lon": lon, "on_ground": int(ground), "elevation_m": feet * 0.3048,
        "agl_m": 0.0 if ground else max(feet - airport.elev_ft, 100) * 0.3048, "groundspeed_ms": 0.0 if ground else 90.0,
        "heading_mag": heading, "heading_true": heading, "ias_kt": 0.0 if ground else 210.0, "aircraft_type": "A20N",
        "zulu_seconds": 43200.0,
    }  # fmt: skip


def sweep(db: AirportDB, idents: list[str], progress=None) -> Report:
    report = Report()
    root = db.xplane_root
    shared = {
        "centers": CenterIndex.open(root), "navaids": NavaidNames(root), "navfix": open_navfix(root),
        "airspace": open_airspace(root), "airways": open_airways(root),
    }  # fmt: skip
    started = time.perf_counter()

    def check(ident: str, name: str, action) -> object:
        report.checks[name] += 1
        try:
            return action()
        except Exception:
            report.failures.append((ident, name, traceback.format_exc(limit=4).strip()))
            return None

    def said(ident: str, name: str, reply) -> None:
        text = getattr(reply, "text", reply) or ""
        if text and BAD_TEXT.search(text):
            report.failures.append((ident, name, f"reads wrong: {text}"))

    for number, ident in enumerate(idents):
        airport = check(ident, "airport", lambda ident=ident: db.airport(ident))
        if airport is None or not airport.runways:
            continue
        report.airports += 1
        if progress and number % 50 == 0:
            progress(f"{number}/{len(idents)} {ident}")
        rng = random.Random(ident)
        wind = rng.choice(WINDS)
        weather = Weather(wind_dir_true=wind, wind_kt=9, clouds=[Cloud(0.4, 3500)], qnh_hpa=1009)
        issue = Issue("K", weather, 43200.0)
        for direction in WINDS:
            check(ident, "runways in use", lambda d=direction: runways_in_use(airport, d, 12) or 1 / 0)
        said(ident, "atis", check(ident, "atis", lambda: compose(airport, weather, "K", 43200.0, 0.0)))

        engine = Engine(db, seed=1, **shared)
        procedures = check(ident, "procedures", lambda: engine.procedures(ident))
        active = runways_in_use(airport, wind, 9)
        if procedures is not None:
            for kind, table in (("SID", procedures.sids), ("STAR", procedures.stars)):
                for procedure in list(table.values())[:5]:
                    route = sorted(procedure.end_fixes() | procedure.start_fixes())
                    for runway in active[:2]:
                        check(ident, f"{kind} legs", lambda p=procedure, k=kind, r=runway, rt=route: p.chain(k, r, rt))
                        build = restrictions.build_sid if kind == "SID" else restrictions.build
                        check(
                            ident,
                            f"{kind} restrictions",
                            lambda b=build, p=procedure, r=runway, rt=route: b(engine, p, airport, r, rt),
                        )
                    said(
                        ident,
                        f"{kind} name",
                        check(
                            ident, f"{kind} name", lambda p=procedure: spoken_name(p, shared["navaids"], airport.lat, airport.lon)
                        ),
                    )
            for runway in active[:2]:
                approach = check(ident, "approach", lambda r=runway: choose_approach(procedures, r))
                if approach is not None:
                    check(ident, "approach fixes", approach.initial_fixes)

        network = check(ident, "taxi network", lambda: engine.network(airport))
        if network is not None and network.usable and network.parking:
            for stand in rng.sample(network.parking, min(2, len(network.parking))):
                for runway in active[:2]:
                    check(
                        ident,
                        "taxi route",
                        lambda s=stand, r=runway: route_to_runway(network, s.lat, s.lon, r, engine._threshold(airport, r)),
                    )
                check(ident, "taxi to stand", lambda s=stand: route_to_point(network, airport.lat, airport.lon, (s.lat, s.lon)))
            check(ident, "stand", lambda: assign_stand(network, "jets", "BAW", (airport.lat, airport.lon), rng))
        check(ident, "zone", lambda: vfr.zone(engine, airport))
        for runway in active[:1]:
            check(ident, "circuit", lambda r=runway: vfr.pattern_side(engine, airport, r))

        # A departure and an arrival through whatever stations the airport has, IFR then VFR.
        far = db.airport("EGLL" if ident != "EGLL" else "LFPG")
        stations = {
            kind: db.station_of(airport, kind) for kind in ("delivery", "ground", "tower", "approach", "departure", "unicom")
        }
        parked = _raw(airport, 0.0, 0.0, airport.elev_ft, 0.0, ground=True)
        if network is not None and network.parking:
            stand = network.parking[0]
            parked["lat"], parked["lon"] = stand.lat, stand.lon
        for rules in ("IFR", "VFR"):
            engine = Engine(db, seed=2, **shared)
            if rules == "IFR" and far is not None:
                engine.set_plan(FlightPlan(ident, far.ident, [], 34000, callsign="BAW123"))
            for kind in ("delivery", "ground", "tower", "unicom"):
                station = stations[kind]
                if station is None:
                    continue
                for name in (
                    "request_clearance",
                    "request_pushback",
                    "request_taxi",
                    "request_progressive",
                    "ready_for_departure",
                ):
                    intent = Intent(name, flight_rules=rules)
                    reply = check(
                        ident, f"{rules} ground calls", lambda s=station, i=intent: engine.handle(i, s, issue, parked, 0.0)
                    )
                    said(ident, f"{rules} ground calls", reply)
                    engine.flight.pending = None
                    check(ident, "suggestions", lambda s=station: engine.suggestions(s, issue, parked))
                    said(ident, "unprompted", check(ident, "unprompted", lambda s=station: engine.tick(s, issue, parked)))
            airborne = Engine(db, seed=3, **shared)
            airborne.flight.has_been_airborne = True
            if rules == "IFR" and far is not None:
                airborne.set_plan(FlightPlan(far.ident, ident, [], 34000, callsign="BAW123"))
                airborne.flight.origin_ident, airborne.flight.rules, airborne.flight.cleared_ft = far.ident, "IFR", 9000
            for kind in ("approach", "tower", "unicom"):
                station = stations[kind]
                if station is None:
                    continue
                for north, east, feet, heading, name in (
                    (-28.0, 6.0, airport.elev_ft + 8000, 350.0, "check_in"),
                    (-20.0, 6.0, airport.elev_ft + 6000, 350.0, "inbound"),
                    (-12.0, 8.0, airport.elev_ft + 4000, 350.0, "request_visual"),
                    (-6.0, 3.0, airport.elev_ft + 2500, 340.0, "position_report"),
                    (-3.0, 0.5, airport.elev_ft + 1000, 0.0, "going_around"),
                    (-9.0, -4.0, airport.elev_ft + 2500, 60.0, "request_transit"),
                ):
                    raw = _raw(airport, north, east, feet, heading)
                    intent = Intent(name, flight_rules=rules, position="final" if name == "position_report" else None)
                    reply = check(
                        ident, f"{rules} air calls", lambda s=station, i=intent, r=raw: airborne.handle(i, s, issue, r, 0.0)
                    )
                    said(ident, f"{rules} air calls", reply)
                    airborne.flight.pending = None
                    said(ident, "unprompted", check(ident, "unprompted", lambda s=station, r=raw: airborne.tick(s, issue, r)))
                    airborne.flight.pending = None
                    check(ident, "suggestions", lambda s=station, r=raw: airborne.suggestions(s, issue, r))
                    check(ident, "summary", airborne.summary)
        for station in stations.values():
            if station is not None:
                found = check(
                    ident, "chatter", lambda s=station: chatter.exchange(s, issue, engine, rng, "Speedbird one two three")
                )
                if found:
                    said(ident, "chatter", found[1])
                    said(ident, "chatter", found[2])
    report.seconds = time.perf_counter() - started
    return report


def route_names(plan: FlightPlan) -> list[str]:
    return route_fixes(plan.route)
