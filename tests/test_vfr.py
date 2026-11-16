"""Visual flying: airfields without a controller, information services, circuit joins, zone transits,
special VFR, radar services by country, and airspace from the sim's own airspace file."""

import random

import pytest

from proatc import chatter, vfr
from proatc.airspace import AirspaceIndex, fallback_zone, parse_openair
from proatc.atis import Issue
from proatc.engine import Engine
from proatc.intents import Intent, parse_json
from proatc.navdata import AirportDB, build_index
from proatc.procedures import parse_cifp
from proatc.weather import Cloud, Weather
from tests.test_ifr import CALLSIGN, at
from tests.test_next import with_traffic

RUNWAY = "100 45.00 1 1 0.25 1 3 0  09 {lat:.4f} {west:.4f} 0 0 3 2 1 0 27 {lat:.4f} {east:.4f} 0 0 3 2 1 0"
FIELDS = "\n".join(
    [
        "A",
        "1200 test",
        "",
        "1    100 0 0 EGXX Westfield",
        RUNWAY.format(lat=51.0, west=-1.02, east=-0.98),
        "1053 121700 Westfield Ground",
        "1054 118100 Westfield Tower",
        "1055 124300 Westfield Radar",
        "",
        "1    100 0 0 EGZZ Grassfield",
        RUNWAY.format(lat=52.0, west=0.98, east=1.02),
        "1051 135475 Grassfield Traffic",
        "",
        "1    50 0 0 ESXX Fjordby",
        RUNWAY.format(lat=60.0, west=14.98, east=15.02),
        "1054 119100 AFIS",
        "1101 27 right",
        "",
        "1    2000 0 0 KXAA Smallville Muni",
        RUNWAY.format(lat=41.0, west=-100.02, east=-99.98),
        "1051 122800 CTAF/UNICOM",
        "",
        "1    600 0 0 KXBB Deltaville",
        RUNWAY.format(lat=40.0, west=-100.02, east=-99.98),
        "1101 27 right",
        "1054 118300 Deltaville Tower",
        "1055 125000 Deltaville Approach",
        "",
        "1    300 0 0 LFXX Eastfield",
        RUNWAY.format(lat=49.0, west=2.98, east=3.02),
        "1054 119700 Eastfield Tower",
        "1055 126000 Eastfield Approach",
    ]
)
OPENAIR = """
AC CTR
AN WESTFIELD CTR
AL GND
AH 2500 MSL
DP 50:54:00 N 001:09:00 W
DP 51:06:00 N 001:09:00 W
DP 51:06:00 N 000:51:00 W
DP 50:54:00 N 000:51:00 W

AC D
AN DELTAVILLE
AL SFC
AH 3100 MSL
DP 39:55:00 N 100:06:00 W
DP 40:05:00 N 100:06:00 W
DP 40:05:00 N 099:54:00 W
DP 39:55:00 N 099:54:00 W

AC R
AN R-101 (RANGE)
AL 1500 AGL
AH FL65
DP 51:18:00 N 001:03:00 W
DP 51:21:00 N 001:03:00 W
DP 51:21:00 N 000:57:00 W
DP 51:18:00 N 000:57:00 W

AC A
AN UPPER
AL FL195
AH UNL
DP 40:00:00 N 010:00:00 W
DP 60:00:00 N 010:00:00 W
DP 60:00:00 N 010:00:00 E
""".splitlines()
ISSUE = Issue("C", Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1013.2), 12 * 3600)
MURK = Issue("D", Weather(wind_dir_true=270, wind_kt=6, visibility_sm=2, clouds=[Cloud(0.8, 700)], qnh_hpa=1005), 12 * 3600)
FOG = Issue("E", Weather(wind_dir_true=270, wind_kt=2, visibility_sm=0.4, qnh_hpa=1005), 12 * 3600)


@pytest.fixture
def world(tmp_path):
    apt = tmp_path / "apt.dat"
    apt.write_text(FIELDS, encoding="utf-8")
    build_index(apt, tmp_path / "a.sqlite")
    db = AirportDB(tmp_path / "a.sqlite")
    engine = Engine(db, seed=1, procedures=lambda ident: parse_cifp([]), airspace=AirspaceIndex(parse_openair(OPENAIR)))
    return db, engine


def parked(lat, lon):
    return at(lat, lon, feet=100, on_ground=True)


def labels(engine, station, raw, issue=ISSUE):
    return [item["label"] for item in engine.suggestions(station, issue, raw)]


# ---- airspace ---------------------------------------------------------------------


def test_airspace_file_is_parsed_and_looked_up():
    spaces = parse_openair(OPENAIR)
    assert [(s.kind, s.name, s.floor_ft, s.ceiling_ft, s.floor_agl) for s in spaces] == [
        ("CTR", "WESTFIELD CTR", 0.0, 2500.0, False),
        ("D", "DELTAVILLE", 0.0, 3100.0, False),
        ("R", "R-101 (RANGE)", 1500.0, 6500.0, True),
    ]  # class A is not kept
    index = AirspaceIndex(spaces)
    assert index.zone(51.0, -1.0, 100).name == "WESTFIELD CTR" and index.zone(52.0, 1.0, 100) is None
    assert [s.name for s in index.at(51.0, -1.0, 2000)] == ["WESTFIELD CTR"] and index.at(51.0, -1.0, 3000) == []
    assert index.at(51.32, -1.0, 1000, ground_ft=300) == [] and len(index.at(51.32, -1.0, 2000, ground_ft=300)) == 1
    space, miles = index.ahead(51.18, -1.0, 180, 2000, 10, ("CTR", "D"))
    assert space.name == "WESTFIELD CTR" and miles == 5
    assert index.ahead(51.2, -1.0, 0, 2000, 10, ("CTR", "D")) == (None, None)  # flying away from it
    assert index.ahead(51.0, -1.0, 180, 2000, 10, ("CTR", "D")) == (None, None)  # already inside
    assert index.zone(51.0, -1.0, 100).describe() == {"kind": "CTR", "name": "Westfield Ctr", "floor_ft": 0, "ceiling_ft": 2500}
    circle = fallback_zone("Nowhere", 10.0, 10.0, 400)
    assert circle.covers(10.05, 10.0) and not circle.covers(10.2, 10.0) and circle.ceiling_ft == 3000


def test_station_kinds_and_circuit_directions(world):
    db, engine = world
    assert vfr.service(db.station(135475, 52.0, 1.0)) == "ctaf" and vfr.service(db.station(122800, 41.0, -100.0)) == "unicom"
    information = db.station(119100, 60.0, 15.0)
    assert vfr.service(information) == "afis" and information.callsign == "Fjordby Information"
    assert vfr.service(db.station(118100, 51.0, -1.0)) is None
    assert (
        vfr.pattern_side(engine, db.airport("KXBB"), "27") == "right"
        and vfr.pattern_side(engine, db.airport("KXBB"), "09") == "left"
    )
    assert vfr.compass(350) == "north" and vfr.compass(135) == "south east"
    assert vfr.zone(engine, db.airport("EGXX")).name == "WESTFIELD CTR"
    assert vfr.zone(engine, db.airport("LFXX")).name == "Eastfield control zone"  # towered, not in the data: a circle
    assert vfr.zone(engine, db.airport("EGZZ")) is None and vfr.zone(engine, db.airport("ESXX")) is None
    assert parse_json('{"intent": "request_transit", "compass": "North-East", "service": "traffic"}') == Intent(
        "request_transit", compass="north east", service="traffic"
    )


# ---- nobody there -----------------------------------------------------------------


def test_a_blind_call_at_an_airfield_without_a_controller_gets_no_answer(world):
    db, engine = world
    traffic_frequency = db.station(135475, 52.0, 1.0)
    inbound = at(51.87, 1.0, feet=2000, heading=360)
    calls = {item["label"]: item for item in engine.suggestions(traffic_frequency, ISSUE, inbound)}
    assert list(calls) == ["Inbound", "Overhead", "Downwind", "Base", "Final", "Going around", "Leaving the area"]
    assert calls["Inbound"]["text"] == (
        f"Grassfield traffic, {CALLSIGN}, eight miles south, inbound for landing runway two seven, Grassfield"
    )
    assert calls["Downwind"]["text"] == f"Grassfield traffic, {CALLSIGN}, left downwind runway two seven, Grassfield"
    reply = engine.handle(Intent.from_dict(calls["Inbound"]["intent"]), traffic_frequency, ISSUE, inbound)
    assert reply.text == "" and reply.note == "No controller at Grassfield: your call went out to other traffic on the frequency."
    assert engine.flight.pending is None and engine.flight.rules == "VFR"
    assert engine.handle(Intent("request_weather"), traffic_frequency, ISSUE, inbound).text == ""  # nobody to ask
    on_the_ground = parked(52.0, 1.0)
    assert labels(Engine(db), traffic_frequency, on_the_ground) == ["Taxiing", "Departing", "Departing, staying in the pattern"]


def test_a_unicom_operator_gives_an_airport_advisory(world):
    db, engine = world
    unicom = db.station(122800, 41.0, -100.0)
    here = parked(41.0, -100.0)
    assert labels(engine, unicom, here)[-2:] == ["Airport advisory", "Radio check"]
    assert engine.handle(Intent("request_weather"), unicom, ISSUE, here).text == (
        f"{CALLSIGN}, Smallville unicom, wind two seven zero at one zero, altimeter two niner niner two, runway two seven in use"
    )
    assert engine.handle(Intent("radio_check"), unicom, ISSUE, here).text == f"{CALLSIGN}, Smallville unicom, loud and clear"
    assert engine.handle(Intent("traffic_call"), unicom, ISSUE, here).text == ""


# ---- information services ---------------------------------------------------------


def test_an_information_service_informs_and_never_clears(world):
    db, engine = world
    information = db.station(119100, 60.0, 15.0)
    here = parked(60.0, 15.0)
    taxi = engine.handle(Intent("request_taxi"), information, ISSUE, here)
    assert taxi.text == (
        f"{CALLSIGN}, Fjordby Information, runway two seven in use, wind two seven zero degrees one zero knots, "
        "Q N H one zero one three, no reported traffic"
    )
    assert taxi.expect == {} and engine.flight.rules == "VFR"
    assert engine.handle(Intent("ready_for_departure"), information, ISSUE, here).text == (
        f"{CALLSIGN}, no reported traffic, wind two seven zero degrees one zero knots, take off at your discretion"
    )
    engine._observe({**here, "on_ground": 0, "agl_m": 300.0})
    north = at(60.05, 15.0, feet=1500, heading=180)
    assert engine.handle(Intent("inbound"), information, ISSUE, north).text == (
        f"{CALLSIGN}, runway two seven, right hand circuit, Q N H one zero one three, no reported traffic, report downwind"
    )
    assert engine.handle(Intent("position_report", position="downwind"), information, ISSUE, north).text == (
        f"{CALLSIGN}, roger, no reported traffic, report final"
    )
    final = at(60.0, 15.06, feet=600, heading=270)
    assert engine.handle(Intent("position_report", position="final"), information, ISSUE, final).text == (
        f"{CALLSIGN}, runway free, wind two seven zero degrees one zero knots, land at your discretion"
    )
    on_the_runway = (60.0, 15.0, 50.0, 270.0, True)
    assert (
        "runway occupied"
        in engine.handle(Intent("position_report", position="final"), information, ISSUE, with_traffic(final, on_the_runway)).text
    )
    assert "Inbound" in labels(engine, information, north) and "Request taxi" in labels(Engine(db), information, here)


# ---- the tower --------------------------------------------------------------------


def test_the_circuit_is_joined_from_where_you_are(world):
    db, engine = world
    tower, airport = db.station(118300, 40.0, -100.0), db.airport("KXBB")
    spots = {
        "north, abeam": (at(40.03, -100.0, feet=1800), ("enter right downwind", "downwind")),
        "north east": (at(40.03, -99.93, feet=1800), ("enter right base", "final")),
        "east, on the centreline": (at(40.0, -99.85, feet=1800), ("make straight in", "three mile final")),
        "south": (at(39.97, -100.0, feet=1800), ("cross midfield, enter right downwind", "downwind")),
    }
    for where, (raw, expected) in spots.items():
        assert vfr.join(engine, airport, "27", raw, True) == expected, where
    assert vfr.join(engine, db.airport("EGXX"), "27", at(50.97, -1.0, feet=1500), False) == (
        "join left hand downwind",
        "downwind",
    )
    assert (
        vfr.join(engine, db.airport("EGXX"), "27", at(51.03, -1.0, feet=1500), False)[0] == "join overhead for left hand downwind"
    )

    call = engine.handle(Intent("inbound", flight_rules="VFR"), tower, ISSUE, spots["north east"][0])
    assert call.text == (
        f"{CALLSIGN}, Deltaville Tower, enter right base runway two seven, altimeter two niner niner two, report final"
    )
    # Outside a control zone in Europe, the join comes with the clearance to enter it.
    british = db.station(118100, 51.0, -1.0)
    far = at(50.85, -1.0, feet=1500, heading=360)
    assert engine.handle(Intent("inbound", flight_rules="VFR"), british, ISSUE, far).text == (
        f"{CALLSIGN}, Westfield Tower, cleared to enter the control zone, join left hand downwind runway two seven, "
        "Q N H one zero one three, report downwind"
    )


def test_downwind_behind_traffic_and_a_low_approach(world):
    db, engine = world
    tower = db.station(118100, 51.0, -1.0)
    downwind = at(50.97, -1.0, feet=1100, heading=90)
    engine.handle(Intent("inbound", flight_rules="VFR"), tower, ISSUE, downwind)
    ahead = (51.0, -0.93, 700.0, 270.0, False)  # two miles out for 27
    assert engine.handle(Intent("position_report", position="downwind"), tower, ISSUE, with_traffic(downwind, ahead)).text == (
        f"{CALLSIGN}, number two, follow the traffic on a two mile final, report final runway two seven"
    )
    final = at(51.0, -0.94, feet=500, heading=270)
    low = engine.handle(Intent("request_low_approach"), tower, ISSUE, final)
    assert low.text == f"{CALLSIGN}, runway two seven, cleared low approach, wind two seven zero degrees one zero knots"
    assert engine.flight.low_approach is False  # one low approach, not every approach from now on


def test_vfr_departures_circuits_a_direction_and_the_frequency_change(world):
    db, engine = world
    tower, here = db.station(118100, 51.0, -1.0), parked(51.0, -1.0)
    assert "Request circuits" in labels(engine, tower, here)
    circuits = engine.handle(Intent("request_circuits"), tower, ISSUE, here)
    assert circuits.text == (
        f"{CALLSIGN}, Westfield Tower, V F R circuits approved, departure runway two seven, left hand, "
        "squawk seven zero zero zero, Q N H one zero one three"
    )
    engine.flight.pending = None
    assert engine.handle(Intent("ready_for_departure"), tower, ISSUE, here).text.endswith(
        "cleared for takeoff, wind two seven zero degrees one zero knots, report downwind"
    )
    engine._observe({**here, "on_ground": 0, "agl_m": 300.0})
    engine.flight.pending = None
    assert engine.tick(tower, ISSUE, at(51.3, -1.0, feet=2000)) is None  # in the circuit: nobody sends you away

    _, engine = world[0], Engine(db, seed=1, airspace=engine.airspace)
    north = engine.handle(Intent("request_clearance", flight_rules="VFR", compass="north", atis="C"), tower, ISSUE, here)
    assert north.text == (
        f"{CALLSIGN}, Westfield Tower, V F R departure approved, departure runway two seven, leave the control zone to the north, "
        "not above altitude two thousand feet, squawk seven zero zero zero, Q N H one zero one three"
    )
    engine.flight.pending = None
    engine._observe({**here, "on_ground": 0, "agl_m": 300.0})
    assert engine.tick(tower, ISSUE, at(51.05, -1.0, feet=1400, heading=360)) is None  # still inside the zone
    gone = engine.tick(tower, ISSUE, at(51.15, -1.0, feet=1400, heading=360))
    assert gone.text == f"{CALLSIGN}, leaving the control zone, frequency change approved"
    assert engine.tick(tower, ISSUE, at(51.2, -1.0, feet=1400, heading=360)) is None  # said once

    american = db.station(118300, 40.0, -100.0)
    engine = Engine(db, seed=1, airspace=engine.airspace)
    assert engine.handle(
        Intent("request_clearance", flight_rules="VFR", compass="west", atis="C"), american, ISSUE, parked(40.0, -100.0)
    ).text == (
        f"{CALLSIGN}, Deltaville Tower, V F R departure to the west approved, departure runway two seven, "
        "squawk one two zero zero, altimeter two niner niner two"
    )
    assert engine.handle(Intent("request_circuits"), american, ISSUE, parked(40.0, -100.0)).text.startswith(
        f"{CALLSIGN}, closed traffic approved, right traffic runway two seven"
    )


def test_special_vfr_when_the_weather_is_below_vfr_minima(world):
    db, engine = world
    tower, here = db.station(118100, 51.0, -1.0), parked(51.0, -1.0)
    assert not vfr.vmc(MURK, False) and vfr.vmc(ISSUE, False) and vfr.vmc(None, True)
    assert "Request special VFR" in labels(engine, tower, here, MURK) and "Request special VFR" not in labels(engine, tower, here)
    refused = engine.handle(Intent("request_clearance", flight_rules="VFR"), tower, MURK, here)
    assert refused.text == (
        f"{CALLSIGN}, Westfield Tower, the weather is below V F R minima, special V F R is available, say intentions"
    )
    special = engine.handle(Intent("request_special_vfr"), tower, MURK, here)
    assert special.text == (
        f"{CALLSIGN}, special V F R clearance, leave the control zone special V F R, "
        "not above altitude two thousand feet, Q N H one zero zero five"
    )
    assert special.expect == {"altitude": 2000} and engine.flight.special_vfr
    assert engine.handle(Intent("request_special_vfr"), tower, FOG, here).text.endswith(
        "unable special V F R, visibility is below minima"
    )

    american = db.station(118300, 40.0, -100.0)
    engine = Engine(db, seed=1, airspace=engine.airspace)
    arriving = at(40.15, -100.0, feet=2500, heading=180)
    assert engine.handle(Intent("inbound", flight_rules="VFR"), american, MURK, arriving).text == (
        f"{CALLSIGN}, Deltaville Tower, Deltaville is I F R, say intentions"
    )
    assert engine.handle(Intent("request_special_vfr"), american, MURK, arriving).text == (
        f"{CALLSIGN}, cleared into the class Delta airspace, maintain special V F R at or below two thousand five hundred, "
        "altimeter two niner six eight"
    )


def test_a_zone_transit_is_cleared_and_released_when_you_leave(world):
    db, engine = world
    tower = db.station(118100, 51.0, -1.0)
    south = at(50.85, -1.0, feet=1800, heading=360)
    assert "Request zone transit" in labels(engine, tower, south)
    cleared = engine.handle(Intent("request_transit"), tower, ISSUE, south)
    assert cleared.text == (
        f"{CALLSIGN}, Westfield Tower, cleared to transit the control zone, V F R, not above altitude two thousand feet, "
        "Q N H one zero one three, report leaving"
    )
    assert cleared.expect == {"altitude": 2000}
    engine.flight.pending = None
    assert engine.tick(tower, ISSUE, south) is None  # not in yet
    assert engine.tick(tower, ISSUE, at(51.0, -1.0, feet=1800, heading=360)) is None  # crossing
    out = engine.tick(tower, ISSUE, at(51.15, -1.0, feet=1800, heading=360))
    assert out.text == f"{CALLSIGN}, leaving the control zone, frequency change approved"
    assert engine.flight.transit is None

    american = db.station(118300, 40.0, -100.0)
    engine = Engine(db, seed=1, airspace=engine.airspace)
    assert engine.handle(Intent("request_transit"), american, ISSUE, at(40.15, -100.0, feet=2500, heading=180)).text == (
        f"{CALLSIGN}, Deltaville Tower, transition approved at or below two thousand five hundred, "
        "altimeter two niner niner two, report clear of the class Delta airspace"
    )
    assert engine.handle(Intent("report_leaving"), american, ISSUE, at(39.9, -100.0, feet=2500, heading=180)).text == (
        f"{CALLSIGN}, frequency change approved"
    )


# ---- radar services ----------------------------------------------------------------


def test_the_radar_service_is_what_the_country_calls_it(world):
    db, engine = world
    cruising = at(51.4, -1.0, feet=3000, heading=90)
    radar = db.station(124300, 51.0, -1.0)
    assert labels(engine, radar, cruising)[1:4] == ["Request basic service", "Request traffic service", "Request zone transit"]
    assert "Request hold" not in labels(engine, radar, cruising)
    traffic_service = engine.handle(Intent("request_flight_following", service="traffic"), radar, ISSUE, cruising)
    code = say_code(engine)
    assert (
        traffic_service.text
        == f"{CALLSIGN}, Westfield Radar, squawk {code}, identified, traffic service, Q N H one zero one three"
    )
    assert engine.summary()["service"] == "traffic"

    engine = Engine(db, seed=1, airspace=engine.airspace)
    french = db.station(126000, 49.0, 3.0)
    assert engine.handle(Intent("request_flight_following"), french, ISSUE, at(49.3, 3.0, feet=3000)).text == (
        f"{CALLSIGN}, Eastfield Approach, squawk {say_code(engine)}, identified, flight information service, Q N H one zero one three"
    )
    engine = Engine(db, seed=1, airspace=engine.airspace)
    american = db.station(125000, 40.0, -100.0)
    assert "Request flight following" in labels(engine, american, at(40.3, -100.0, feet=4500))
    assert engine.handle(Intent("request_flight_following"), american, ISSUE, at(40.3, -100.0, feet=4500)).text == (
        f"{CALLSIGN}, Deltaville Approach, squawk {say_code(engine)}, radar contact, flight following, altimeter two niner niner two"
    )


def say_code(engine) -> str:
    from proatc import phraseology as say

    return say.digits(engine.flight.squawk)


def test_a_basic_service_gives_no_traffic_information_and_a_traffic_service_does(world):
    db, engine = world
    radar = db.station(124300, 51.0, -1.0)
    own = at(51.4, -1.0, feet=3000, heading=90)
    other = (51.4, -0.9, 3300.0, 270.0, False)
    engine.handle(Intent("request_flight_following", service="basic"), radar, ISSUE, own)
    engine.flight.pending = None
    assert engine.tick(radar, ISSUE, with_traffic(own, other)) is None
    engine.handle(Intent("request_flight_following", service="traffic"), radar, ISSUE, own)
    engine.flight.pending = None
    assert "traffic, " in engine.tick(radar, ISSUE, with_traffic(own, other)).text


def test_a_flight_under_a_radar_service_is_warned_before_controlled_or_restricted_airspace(world):
    db, engine = world
    radar = db.station(124300, 51.0, -1.0)
    southbound = at(51.18, -1.0, feet=2000, heading=180)
    assert engine.tick(radar, ISSUE, southbound) is None  # no service yet: nobody is watching
    engine.handle(Intent("request_flight_following", service="traffic"), radar, ISSUE, southbound)
    engine.flight.pending = None
    warned = engine.tick(radar, ISSUE, southbound)
    assert warned.text == (
        f"{CALLSIGN}, control zone five miles ahead, for entry contact Westfield Tower one one eight decimal one, "
        "otherwise remain outside"
    )
    assert warned.expect == {"frequency": "118.100"}
    engine.flight.pending = None
    assert engine.tick(radar, ISSUE, southbound) is None  # once
    northbound = at(51.25, -1.0, feet=3000, heading=360)
    assert engine.tick(radar, ISSUE, northbound).text == (
        f"{CALLSIGN}, caution, restricted area three miles ahead on your present track"
    )
    assert engine.tick(radar, ISSUE, northbound) is None


# ---- chatter ------------------------------------------------------------------------


def test_chatter_at_quiet_airfields_is_other_pilots_not_a_controller(world):
    db, engine = world
    rng = random.Random(5)
    for _ in range(15):
        callsign, said, answered = chatter.exchange(db.station(135475, 52.0, 1.0), ISSUE, engine, rng, CALLSIGN)
        assert said.startswith(f"Grassfield traffic, {callsign}, ") and said.endswith(", Grassfield") and answered == ""
        assert callsign.startswith("Golf ") and callsign != CALLSIGN
    assert chatter.light_callsign("KXAA", rng).startswith("November ") and chatter.light_callsign("LFXX", rng).startswith(
        "Foxtrot "
    )
    said = [chatter.exchange(db.station(119100, 60.0, 15.0), ISSUE, engine, rng, CALLSIGN) for _ in range(15)]
    assert all("cleared" not in line[1] for line in said) and any("at your discretion" in line[1] for line in said)
