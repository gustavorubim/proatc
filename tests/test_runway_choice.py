"""Which runway ATC gives: the flight plan's where the wind allows it, the same one in the ATIS and from every
controller, and never one the aircraft cannot use (closed, out of the taxi network, grass, too short) while another
runway is there."""

from __future__ import annotations

from proatc import ifr
from proatc.atis import AtisBook, Issue, compose, runways_in_use
from proatc.engine import Engine
from proatc.flightplan import FlightPlan
from proatc.intents import Intent
from proatc.navdata import AirportDB, build_index
from proatc.procedures import parse_cifp
from proatc.weather import Weather

# Two US airports. Testville has two pairs of parallels, like Las Vegas: 1L/1R pointing 025 and 8L/8R pointing 090.
# Endtown has 6/24 and 15/33, like many: in a wind from 330 the wind alone picks 33.
APT = """A
1200 test

1   2000 0 0 KXXX Testville
1302 icao_code KXXX
100 45.00 1 0 0.25 1 3 0  8L 36.0000 -115.0300 0 0 3 2 1 0 26R 36.0000 -114.9800 0 0 3 2 1 0
100 45.00 1 0 0.25 1 3 0  8R 35.9970 -115.0250 0 0 3 2 1 0 26L 35.9970 -114.9900 0 0 3 2 1 0
100 45.00 1 0 0.25 1 3 0  1L 35.9850 -115.0200 0 0 3 2 1 0 19R 36.0095 -115.0059 0 0 3 2 1 0
100 45.00 1 0 0.25 1 3 0  1R 35.9850 -115.0160 0 0 3 2 1 0 19L 36.0095 -115.0019 0 0 3 2 1 0
1050 132400 Testville ATIS
1052 118000 Testville Clearance
1053 121900 Testville Ground
1054 119900 Testville Tower

1    100 0 0 KYYY Endtown
1302 icao_code KYYY
100 45.00 1 0 0.25 1 3 0   6 33.9900 -118.0200 0 0 3 2 1 0 24 34.0035 -117.9918 0 0 3 2 1 0
100 45.00 1 0 0.25 1 3 0  15 34.0100 -118.0100 0 0 3 2 1 0 33 33.9866 -117.9937 0 0 3 2 1 0
1054 120100 Endtown Tower
1055 124300 Endtown Approach
""".splitlines()

# A field whose best runway in a south-westerly, 04/22, is drawn but joined to no taxiway (closed), and whose 15/33
# the taxi network never names and no taxiway comes near. Only 09/27 can be reached.
CLOSED = """A
1200 test

1    100 0 0 EGZZ Closedfield
1302 icao_code EGZZ
100 45.00 1 0 0.25 1 3 0  09 52.0000 -1.0200 0 0 3 2 1 0 27 52.0000 -0.9800 0 0 3 2 1 0
100 45.00 1 0 0.25 1 3 0  04 51.9900 -1.0100 0 0 3 2 1 0 22 52.0080 -0.9900 0 0 3 2 1 0
100 45.00 1 0 0.25 1 3 0  15 52.0300 -1.0300 0 0 3 2 1 0 33 52.0100 -1.0150 0 0 3 2 1 0
1053 121700 Closedfield Ground
1054 118100 Closedfield Tower
1201 51.9960 -1.0000 both 0 apron
1201 52.0000 -1.0000 both 1 on_09_27
1201 52.0000 -1.0200 both 2 end_09
1201 52.0000 -0.9800 both 3 end_27
1201 51.9900 -1.0100 both 4 end_04
1201 52.0080 -0.9900 both 5 end_22
1202 0 1 twoway taxiway_E A
1202 2 1 twoway runway 09/27
1202 1 3 twoway runway 09/27
1202 4 5 twoway runway 04/22
""".splitlines()

# A field with a long grass runway beside its paved one, as at many in Europe.
GRASS = """A
1200 test

1    100 0 0 EGWW Grassfield
1302 icao_code EGWW
100 45.00 1 0 0.25 1 3 0  15 52.0200 -1.0000 0 0 3 2 1 0 33 51.9950 -1.0000 0 0 3 2 1 0
100 30.00 3 0 0.25 0 0 0 15R 52.0200 -0.9970 0 0 1 0 0 0 33L 51.9980 -0.9970 0 0 1 0 0 0
1054 118300 Grassfield Tower
""".splitlines()

# Three parallels, the middle one short: a heavy jet is not sent to it.
TRIPLE = """A
1200 test

1    100 0 0 EGTT Triplefield
1302 icao_code EGTT
100 45.00 1 0 0.25 1 3 0 09L 52.0100 -1.0400 0 0 3 2 1 0 27R 52.0100 -0.9800 0 0 3 2 1 0
100 45.00 1 0 0.25 1 3 0 09C 52.0050 -1.0200 0 0 3 2 1 0 27C 52.0050 -0.9800 0 0 3 2 1 0
100 45.00 1 0 0.25 1 3 0 09R 52.0000 -1.0400 0 0 3 2 1 0 27L 52.0000 -0.9800 0 0 3 2 1 0
1054 118500 Triplefield Tower
""".splitlines()

DELIVERY, GROUND, ATIS_KHZ = 118000, 121900, 132400
NORTH_WEST = Weather(wind_dir_true=330, wind_kt=10, qnh_hpa=1013.2)  # a headwind on both 1 and 26 at Testville
EAST = Weather(wind_dir_true=80, wind_kt=15, qnh_hpa=1013.2)  # a tailwind of fifteen knots on 26
SOUTH_WEST = Weather(wind_dir_true=220, wind_kt=15, qnh_hpa=1013.2)
PLAN = FlightPlan("KXXX", "KYYY", [], 24000, departure_runway="26R", arrival_runway="24", source="simbrief")


def world(tmp_path, rows=APT):
    (tmp_path / "apt.dat").write_text("\n".join(rows), encoding="utf-8")
    build_index(tmp_path / "apt.dat", tmp_path / "a.sqlite")
    db = AirportDB(tmp_path / "a.sqlite")
    return db, Engine(db, seed=1, procedures=lambda ident: parse_cifp([]))


def parked(lat: float, lon: float, aircraft: str = "A320") -> dict:
    return {"tail_number": "N123AB", "lat": lat, "lon": lon, "on_ground": 1, "agl_m": 0.0, "groundspeed_ms": 0.0,
            "elevation_m": 600.0, "heading_true": 0.0, "heading_mag": 0.0, "aircraft_type": aircraft}  # fmt: skip


def issue(weather: Weather) -> Issue:
    return Issue("A", weather, 12 * 3600)


def clearance(db, engine, weather: Weather) -> str:
    delivery = db.station(DELIVERY, 36.002, -115.0)
    return engine.handle(Intent("request_clearance", flight_rules="IFR"), delivery, issue(weather), parked(36.002, -115.0)).text


# ---- the plan's runway ---------------------------------------------------------------------------------------------


def test_the_wind_alone_picks_runway_one_but_the_plans_two_six_right_is_kept_by_the_atis_and_the_clearance(tmp_path):
    db, engine = world(tmp_path)
    testville = db.airport("KXXX")
    assert runways_in_use(testville, 330, 10) == ["1L", "1R"]  # no plan: the wind's choice, as before

    engine.set_plan(PLAN)
    atis = compose(testville, NORTH_WEST, "A", 12 * 3600)
    assert "Landing runway two six left, departing runway two six right." in atis, atis
    said = clearance(db, engine, NORTH_WEST)
    assert ", runway two six right, climb and maintain" in said, said
    assert engine.flight.runway == "26R"
    taxi = engine.handle(Intent("request_taxi"), db.station(GROUND, 36.002, -115.0), issue(NORTH_WEST), parked(36.002, -115.0))
    assert "runway two six right" in taxi.text and "runway one" not in taxi.text, taxi.text


def test_without_the_clearance_ground_and_tower_also_give_the_plans_runway(tmp_path):
    db, engine = world(tmp_path)
    engine.set_plan(PLAN)
    assert engine._departure_runway(db.airport("KXXX"), issue(NORTH_WEST), parked(35.99, -115.02)) == "26R"


def test_the_plans_runway_is_dropped_when_the_wind_does_not_allow_it_and_the_atis_still_agrees(tmp_path):
    db, engine = world(tmp_path)
    engine.set_plan(PLAN)
    atis = compose(db.airport("KXXX"), EAST, "A", 12 * 3600)
    assert "departing runway eight right" in atis and "two six" not in atis, atis
    said = clearance(db, engine, EAST)
    assert ", runway eight right," in said, said


def test_the_plan_may_write_a_runway_with_a_leading_zero_and_it_departs_from_the_side_the_plan_says(tmp_path):
    db, engine = world(tmp_path)
    engine.set_plan(FlightPlan("KXXX", "KYYY", [], 24000, departure_runway="01L", source="simbrief"))
    atis = compose(db.airport("KXXX"), NORTH_WEST, "A", 12 * 3600)
    assert "Landing runway one right, departing runway one left." in atis, atis  # left first would have it landing
    assert ", runway one left, climb and maintain" in clearance(db, engine, NORTH_WEST)


def test_the_plans_arrival_runway_is_the_one_approach_tower_and_atis_give_at_the_destination(tmp_path):
    db, engine = world(tmp_path)
    endtown = db.airport("KYYY")
    assert runways_in_use(endtown, 330, 10) == ["33"]  # the wind's choice
    engine.set_plan(PLAN)
    assert "Landing and departing runway two four." in compose(endtown, NORTH_WEST, "A", 12 * 3600)
    engine.flight.has_been_airborne = True
    airborne = {**parked(34.3, -118.0), "on_ground": 0, "agl_m": 3000.0, "elevation_m": 3000.0, "groundspeed_ms": 120.0}
    arrival = ifr._setup_arrival(engine, db.station(124300, 34.3, -118.0), issue(NORTH_WEST), airborne, Intent("inbound"))
    assert arrival.runway == "24"
    engine.flight.arrival, engine.flight.runway = None, None  # the tower, with no approach before it
    assert engine._landing_runway(endtown, issue(NORTH_WEST), airborne, Intent("inbound")) == "24"


def test_a_plan_loaded_after_the_atis_was_heard_brings_a_new_letter_with_the_new_runway(tmp_path):
    db, engine = world(tmp_path)
    testville, book = db.airport("KXXX"), AtisBook()
    before = book.current(testville, NORTH_WEST, 12 * 3600)
    assert book.current(testville, NORTH_WEST, 12 * 3600 + 60).letter == before.letter  # nothing changed
    engine.set_plan(PLAN)
    after = book.current(testville, NORTH_WEST, 12 * 3600 + 120)
    assert after.letter != before.letter
    assert "departing runway two six right" in book.text(testville, NORTH_WEST, 12 * 3600 + 180)[1]


# ---- runways that cannot be used ---------------------------------------------------------------------------------


def test_a_runway_no_taxiway_joins_is_never_given_while_one_that_can_be_reached_is_there(tmp_path):
    db, engine = world(tmp_path, CLOSED)
    field = db.airport("EGZZ")
    assert runways_in_use(field, 220, 15) == ["22"]  # the wind alone, before the taxiways are known
    assert engine.closed_runways(field) == {"04", "22", "15", "33"}  # 15/33: never named, no taxiway near it
    here, now = parked(51.996, -1.0), issue(SOUTH_WEST)
    assert engine.active_runways(field, now) == ["27"]
    assert engine._departure_runway(field, now, here) == "27"
    assert engine._landing_runway(field, now, here, Intent("inbound")) == "27"
    taxi = engine.handle(Intent("request_taxi"), db.station(121700, 51.996, -1.0), now, here)
    assert "runway two seven" in taxi.text and "two two" not in taxi.text, taxi.text
    # The ATIS leaves it out too, once the engine has seen the taxiways: it gives no runway no controller will.
    assert "Runway two seven in use." in compose(field, SOUTH_WEST, "A", 12 * 3600)


def test_a_plan_naming_a_closed_runway_does_not_bring_it_back(tmp_path):
    db, engine = world(tmp_path, CLOSED)
    engine.set_plan(FlightPlan("EGZZ", "KYYY", [], 24000, departure_runway="22", source="simbrief"))
    assert engine._departure_runway(db.airport("EGZZ"), issue(SOUTH_WEST), parked(51.996, -1.0)) == "27"


def test_a_runway_the_network_leaves_out_but_a_taxiway_reaches_is_not_taken_for_closed(tmp_path):
    rows = [*CLOSED, "1201 52.0150 -1.0200 both 6 beside_15_33", "1202 0 6 twoway taxiway_E B"]
    db, engine = world(tmp_path, rows)
    assert engine.closed_runways(db.airport("EGZZ")) == {"04", "22"}


def test_a_grass_runway_beside_a_paved_one_is_not_a_runway_in_use(tmp_path):
    db, engine = world(tmp_path, GRASS)
    field = db.airport("EGWW")
    assert runways_in_use(field, 330, 12) == ["33"]  # was 33 and 33L, with departures sent to the grass
    assert engine._departure_runway(field, issue(Weather(wind_dir_true=330, wind_kt=12)), parked(52.0, -1.0)) == "33"


def test_a_heavy_jet_is_not_sent_to_the_short_runway_of_three_parallels(tmp_path):
    db, engine = world(tmp_path, TRIPLE)
    field, west = db.airport("EGTT"), issue(Weather(wind_dir_true=270, wind_kt=10))
    assert runways_in_use(field, 270, 10) == ["27L", "27C", "27R"]  # 27C (2,750 m) and 27R depart
    near_27c = (52.0052, -0.9805)
    assert engine._departure_runway(field, west, parked(*near_27c, aircraft="A320")) == "27C"  # long enough for it
    assert engine._departure_runway(field, west, parked(*near_27c, aircraft="B744")) == "27R"
