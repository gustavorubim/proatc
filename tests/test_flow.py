"""STAR level restrictions, holds, terrain and weather."""

import pytest

from proatc.atis import AtisBook, Issue, compose, low_visibility, significantly_different
from proatc.centers import CenterIndex, parse_atc
from proatc.engine import Engine
from proatc.intents import Intent
from proatc.metar import parse_metar
from proatc.navdata import AirportDB, build_index
from proatc.navfix import Hold, NavFix, parse_fixes, parse_holds, parse_mora, parse_msa
from proatc.procedures import parse_cifp
from proatc.radio import observe
from proatc.vectors import locate, next_vector
from proatc.weather import Cloud, Weather
from tests.test_ifr import APT, ATC, CALLSIGN, CIFP_EAST, CIFP_WEST, ISSUE, PLAN, at

NAVFIX = NavFix(
    fixes={"DELTA": [(51.0, 3.5)], "ECHOX": [(51.0, 4.9), (10.0, 10.0)]},
    holds={"ECHOX": [Hold("ECHOX", "LFXX", 270.0, "R", 1.0, 5000, 14000, 220)]},
    msa={"LFXX": 4200},
    mora={(51, 5): 3000, (51, 0): 2300},
)


class Reports:
    """Stands in for the METAR store."""

    def __init__(self, weather):
        self._weather = weather

    def weather(self, ident, field_elev_ft=0.0):
        return self._weather if ident == "LFXX" else None


def make(tmp_path, **options):
    apt = tmp_path / "apt.dat"
    apt.write_text("\n".join(APT), encoding="utf-8")
    if not (tmp_path / "a.sqlite").exists():
        build_index(apt, tmp_path / "a.sqlite")
    db = AirportDB(tmp_path / "a.sqlite")
    procedures = {"EGXX": parse_cifp(CIFP_WEST), "LFXX": parse_cifp(CIFP_EAST)}
    centers = CenterIndex(parse_atc(ATC))
    clock = [0.0]
    engine = Engine(db, seed=1, procedures=lambda ident: procedures.get(ident, parse_cifp([])), centers=centers,
                    clock=lambda: clock[0], **options)  # fmt: skip
    engine.set_plan(PLAN)
    engine.flight.has_been_airborne, engine.flight.origin_ident = True, "EGXX"
    return db, engine, centers, clock


# ---- data -------------------------------------------------------------------


def test_navigation_data_parsers():
    fixes = parse_fixes(
        [" 54.876538889   -2.896138889  INPIP ENRT EG 4530263 INPIP", "garbage", " 10.0 20.0 INPIP ENRT XX 1 INPIP"],
        ["3  51.749736111  -0.106736111  378  11705  130  -2.0  BPK ENRT EG BROOKMANS PARK VOR/DME", "4 1 2 3 4 5 6 ILS X Y Z"],
    )
    assert fixes["INPIP"] == [(54.876538889, -2.896138889), (10.0, 20.0)] and fixes["BPK"] == [(51.749736111, -0.106736111)]
    nav = NavFix(fixes=fixes)
    assert nav.position("INPIP", 55.9, -3.3) == (54.876538889, -2.896138889)  # the nearer of two with the same name
    assert nav.position("INPIP", -40.0, 100.0) is None and nav.position("NOPE", 0, 0) is None

    holds = parse_holds([
        "TARTN EG EGPH 11     14.0      0.0      3.5 L     7000    14000      230",
        "  LAM EG ENRT  3    262.0      1.5      0.0 L    14000    17000        0",
        "  LAM EG ENRT  3    262.0      1.0      0.0 L     7000    14000      220",
        "1140 Version",
    ])  # fmt: skip
    nav = NavFix(holds=holds)
    assert nav.hold("TARTN", "EGPH").turn == "L" and nav.hold("TARTN", "EGPH").inbound_course == 14.0
    assert nav.hold("LAM", "EGLL", 8000).min_ft == 7000 and nav.hold("LAM", "EGLL", 15000).min_ft == 14000
    assert nav.hold("NOPE") is None

    msa = parse_msa([
        " 1  LOWI LO LOWI M 180 107 25 270 135 25 015 143 25 100 113 25 000 000  0",
        " 2   RTT LO LOWI M 270 142 25 045 116 25 120 103 25 000 000  0",
        " 1  DAAE DA DAAE M 180 078 25 000 000  0",
    ])  # fmt: skip
    assert msa == {"LOWI": 14300, "DAAE": 7800}  # the highest sector, across every centre point
    mora = parse_mora(["+47 +010 080 143 UNK 090", "-01 -010 010 020", "I", "1150 Version"])
    assert mora[(47, 10)] == 8000 and mora[(47, 11)] == 14300 and (47, 12) not in mora and mora[(-1, -9)] == 2000
    assert NavFix(mora=mora).grid_safe_ft(47.2, 11.3) == 14300 and NavFix(mora=mora).grid_safe_ft(0, 0) is None


def test_metar_parsing():
    plain = parse_metar("EGPH 161220Z 25005KT 220V280 CAVOK 04/M02 Q1005")
    assert (plain.wind_dir_true, plain.wind_kt, plain.visibility_sm, plain.clouds) == (250.0, 5.0, 10.0, [])
    assert (plain.temperature_c, plain.dewpoint_c, plain.qnh_hpa, plain.gust_kt) == (4.0, -2.0, 1005.0, None)

    american = parse_metar("KSEA 161153Z 00000KT 10SM SCT046 BKN070 07/04 A3004 RMK AO2 SLP182 T00670044")
    assert american.wind_kt == 0 and american.visibility_sm == 10.0 and round(american.qnh_hpa) == 1017
    assert [(c.coverage, c.base_ft_agl) for c in american.clouds] == [(0.45, 4600.0), (0.75, 7000.0)]

    storm = parse_metar("EGXX 161220Z 27015G28KT 0400 R27/0500 +TSRA FG BKN001 OVC010CB M02/M03 Q0998 TEMPO 9999 NSW")
    assert storm.gust_kt == 28.0 and storm.thunderstorm and storm.fog and storm.rain == 0.8 and storm.clouds[1].cumulonimbus
    assert round(storm.visibility_sm, 2) == 0.25 and storm.temperature_c == -2.0 and low_visibility(storm)

    snow = parse_metar("KXXX 161153Z VRB03KT 1 1/2SM -SN BR OVC008 M05/M07 A2992")
    assert snow.snow and snow.visibility_sm == 1.5 and snow.wind_dir_true == 0.0 and not low_visibility(snow)
    metres = parse_metar("LFPG 161200Z AUTO 24012KT 9000 -RA BKN018 11/09 Q1007 NOSIG")
    assert 5.5 < metres.visibility_sm < 5.7 and metres.rain == 0.2
    assert parse_metar("EGXX 161220Z /////KT //// Q////") is None and parse_metar("") is None


def test_atis_mentions_gusts_storms_and_low_visibility(tmp_path):
    db, *_ = make(tmp_path)
    airport = db.airport("EGXX")
    weather = Weather(wind_dir_true=270, wind_kt=15, gust_kt=28, visibility_sm=0.25, thunderstorm=True, fog=True,
                      clouds=[Cloud(0.75, 100)], qnh_hpa=998)  # fmt: skip
    text = compose(airport, weather, "A", 0)
    assert "Wind two seven zero degrees one five gusting two eight knots." in text
    assert "Thunderstorm." in text and "Fog." in text and "Low visibility procedures in force." in text
    assert "gusting" not in compose(airport, Weather(wind_dir_true=270, wind_kt=15, gust_kt=18), "A", 0)  # not worth a mention
    assert significantly_different(airport, Weather(), Weather(thunderstorm=True))


def test_airport_weather_prefers_its_own_report_on_real_weather(tmp_path):
    db, *_ = make(tmp_path)
    east = db.airport("LFXX")
    at_aircraft = {"wx_qnh_pas": 101300.0, "wx_wind_alt_m": [100.0], "wx_wind_kt": [3.0], "wx_wind_dir": [90.0]}
    db.metars = Reports(Weather(wind_dir_true=250, wind_kt=20, qnh_hpa=995))
    assert observe(db, east, {**at_aircraft, "wx_source": 1}).wind_kt == 20  # the airport's own report
    assert observe(db, east, {**at_aircraft, "wx_source": 0}).wind_kt == 3.0  # preset weather: what the sim shows
    assert observe(db, db.airport("EGXX"), {**at_aircraft, "wx_source": 1}).wind_kt == 3.0  # no report for this airport
    db.metars = None
    assert observe(db, east, {**at_aircraft, "wx_source": 1}).wind_kt == 3.0


# ---- STAR restrictions ------------------------------------------------------


def test_descent_follows_the_stars_level_restriction(tmp_path):
    db, engine, centers, _ = make(tmp_path, navfix=NAVFIX)
    engine.flight.cleared_ft = 36000
    west = centers.on_frequency(127500, 51.0, 0.0)
    engine.handle(Intent("check_in"), west, None, at(51.0, 0.5, feet=36000, heading=90))
    # DELTA is to be crossed at FL100: (36000 - 10000) / 300 + 10 = about 97 NM before it.
    assert engine.tick(west, None, at(51.0, 0.8, feet=36000, heading=90)) is None
    descent = engine.tick(west, None, at(51.0, 1.0, feet=36000, heading=90))
    assert descent.text == f"{CALLSIGN}, descend flight level one zero zero, level by Delta, Delta one Echo arrival"
    assert descent.expect == {"altitude": 10000} and engine.flight.star == "DELT1E"
    assert engine.tick(west, None, at(51.0, 1.5, feet=30000, heading=90)) is None  # no generic step on top of it

    east = centers.on_frequency(132100, 51.0, 2.3)
    engine.tick(west, None, at(51.0, 2.3, feet=20000, heading=90))  # hand-off to the next sector
    engine.handle(Intent("check_in"), east, None, at(51.0, 2.3, feet=20000, heading=90))
    late = engine.tick(east, None, at(51.0, 3.45, feet=12000, heading=90))
    assert late.text == f"{CALLSIGN}, you were to be level flight level one zero zero by Delta, expedite descent"
    assert engine.tick(east, None, at(51.0, 3.6, feet=10500, heading=90)) is None
    assert all(c.passed for c in engine.flight.constraints)


def test_a_restriction_met_on_time_draws_no_comment(tmp_path):
    db, engine, centers, _ = make(tmp_path, navfix=NAVFIX)
    engine.flight.cleared_ft = 36000
    east = centers.on_frequency(132100, 51.0, 2.3)
    engine.handle(Intent("check_in"), east, None, at(51.0, 2.3, feet=36000, heading=90))
    assert "level by Delta" in engine.tick(east, None, at(51.0, 2.3, feet=36000, heading=90)).text
    assert engine.tick(east, None, at(51.0, 3.45, feet=10200, heading=90)) is None


# ---- holds ------------------------------------------------------------------


def test_approach_holds_an_arrival_then_vectors_it(tmp_path):
    db, engine, _, clock = make(tmp_path, navfix=NAVFIX, holding="always")
    engine.flight.cleared_ft = 13000
    approach = db.station(124300, 51.0, 5.0)
    inbound = at(51.05, 4.3, feet=13000, heading=90)
    hold = engine.handle(Intent("check_in"), approach, ISSUE, inbound)
    minutes = engine.flight.arrival.hold.minutes
    assert minutes in (5, 8, 10)
    assert hold.text == (
        f"{CALLSIGN}, Eastfield Approach, identified, hold at Echox as published, maintain flight level one three zero, "
        f"expect approach in {' '.join(['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'niner'][int(d)] for d in str(minutes))} minutes"
    )
    assert hold.expect == {"altitude": 13000} and engine.summary()["holding"] == "ECHOX"
    assert [s["label"] for s in engine.suggestions(approach, ISSUE, inbound)][:3] == [
        "Read back",
        "Check in",
        "Ready for approach",
    ]

    engine.handle(Intent("readback", altitude=13000), approach, ISSUE, inbound)
    clock[0] += 3600
    assert engine.tick(approach, ISSUE, inbound) is None  # not at the fix yet: the clock has not started
    at_fix = at(51.0, 4.95, feet=13000, heading=270)
    assert engine.tick(approach, ISSUE, at_fix) is None  # entering
    clock[0] += minutes * 60 - 5
    assert engine.tick(approach, ISSUE, at_fix) is None
    clock[0] += 10
    release = engine.tick(approach, ISSUE, at_fix)
    assert release.text.startswith(
        f"{CALLSIGN}, leave the hold, turn right heading zero seven zero, descend altitude four thousand two hundred feet"
    )
    assert engine.flight.arrival.hold is None and engine.summary().get("holding") is None
    assert engine.handle(Intent("check_in"), approach, ISSUE, at_fix) is None  # one hold per arrival


def test_a_hold_on_request_lasts_until_the_pilot_is_ready(tmp_path):
    db, engine, _, clock = make(tmp_path, navfix=NAVFIX, holding="never")
    engine.flight.cleared_ft = 11000
    approach = db.station(124300, 51.0, 5.0)
    inbound = at(51.05, 4.3, feet=13000, heading=90)
    first = engine.handle(Intent("check_in"), approach, ISSUE, inbound)
    assert "hold" not in first.text and "expect I L S approach" in first.text  # holding is off: straight to the approach
    asked = engine.handle(Intent("request_hold"), approach, ISSUE, inbound)
    assert (
        asked.text
        == f"{CALLSIGN}, hold at Echox as published, descend flight level one one zero, advise when ready for the approach"
    )
    engine.handle(Intent("readback", altitude=11000), approach, ISSUE, inbound)
    clock[0] += 7200
    assert engine.tick(approach, ISSUE, at(51.0, 4.95, feet=11000, heading=270)) is None  # no time limit
    ready = engine.handle(Intent("cancel_hold"), approach, ISSUE, at(51.0, 4.95, feet=11000, heading=270))
    assert ready.text.startswith(f"{CALLSIGN}, leave the hold, turn right heading zero seven zero")
    assert engine.handle(Intent("cancel_hold"), approach, ISSUE, inbound) is None  # not holding any more


def test_no_published_hold_means_no_hold(tmp_path):
    db, engine, _, _ = make(tmp_path, navfix=NavFix(), holding="always")
    approach = db.station(124300, 51.0, 5.0)
    inbound = at(51.05, 4.3, feet=13000, heading=90)
    assert "expect I L S approach" in engine.handle(Intent("check_in"), approach, ISSUE, inbound).text
    assert engine.handle(Intent("request_hold"), approach, ISSUE, inbound).text == f"{CALLSIGN}, unable to hold, expect vectors"


# ---- terrain ----------------------------------------------------------------


def test_vectors_stay_above_the_minimum_safe_altitude():
    threshold, course, platform = (51.0, 5.37), 270.0, 2500
    abeam = locate(51.1, 5.0, threshold, course)
    assert next_vector(abeam, course, platform, 8000).altitude_ft == 3500
    assert next_vector(abeam, course, platform, 8000, safe_ft=4200).altitude_ft == 4200
    assert next_vector(abeam, course, platform, 8000, safe_ft=14250).altitude_ft == 14300
    far = locate(51.0, 4.0, threshold, course)
    assert next_vector(far, course, platform, 20000).altitude_ft == 14000
    assert next_vector(far, course, platform, 20000, safe_ft=16000).altitude_ft == 16000  # no lower, however far out
    on_final = locate(51.04, 5.75, threshold, course)
    assert next_vector(on_final, course, platform, 3000, safe_ft=4200).altitude_ft == 2500  # the published approach altitude


def test_descent_target_and_low_altitude_alert_use_the_terrain(tmp_path):
    from proatc.ifr import _descent_target

    db, engine, centers, _ = make(tmp_path, navfix=NavFix(mora={(51, 5): 15200, (51, 0): 2300}))
    east_airport = db.airport("LFXX")
    assert _descent_target(engine, east_airport) == 16000  # above the 15,200 ft terrain cell around the airport
    assert _descent_target(engine, east_airport, {"lat": 51.0, "lon": 0.5}) == 16000
    engine.navfix = NavFix(mora={(51, 0): 2300})
    assert _descent_target(engine, east_airport) == 13000

    west = centers.on_frequency(127500, 51.0, 0.0)
    engine.flight.cleared_ft = None
    engine.set_plan(None)
    engine.handle(Intent("check_in"), west, None, at(51.0, 0.5, feet=4000, heading=90))
    low = {**at(51.0, 0.5, feet=1900, heading=90), "agl_m": 250.0, "vs_fpm": -1200.0}
    alert = engine.tick(west, None, low)
    assert (
        alert.text
        == f"{CALLSIGN}, low altitude alert, check your altitude immediately, minimum safe altitude two thousand three hundred feet"
    )
    assert engine.tick(west, None, low) is None  # once
    assert engine.tick(west, None, {**low, "agl_m": 900.0, "vs_fpm": 1500.0}) is None
    engine.tick(west, None, {**low, "agl_m": 1000.0, "vs_fpm": 0.0})  # back up: the alert is armed again
    assert engine.tick(west, None, low) is not None
    climbing = {**low, "vs_fpm": 800.0}
    engine.flight.low_altitude_called = False
    assert engine.tick(west, None, climbing) is None  # low but going up


# ---- weather on the radio ---------------------------------------------------


def test_wind_check_destination_weather_and_deviations(tmp_path):
    reported = Weather(wind_dir_true=250, wind_kt=20, gust_kt=32, visibility_sm=3.0, clouds=[Cloud(0.75, 1200)], qnh_hpa=995)
    db, engine, centers, _ = make(tmp_path, metars=Reports(reported))
    tower = db.station(118100, 51.0, -1.0)
    gusty = Issue("C", Weather(wind_dir_true=270, wind_kt=10, gust_kt=22, qnh_hpa=1013.2), 0)
    assert engine.handle(Intent("request_weather"), tower, gusty, at(51.003, -1.0, on_ground=True)).text == (
        f"{CALLSIGN}, Westfield Tower, wind two seven zero degrees one zero gusting two two knots, Q N H one zero one three"
    )

    cruising = {**at(51.0, 0.5, feet=36000, heading=90), "wx_source": 1}
    west = centers.on_frequency(127500, 51.0, 0.5)
    engine.flight.cleared_ft = 36000
    engine.handle(Intent("check_in"), west, None, cruising)
    assert engine.handle(Intent("request_weather"), west, None, cruising).text == (
        f"{CALLSIGN}, Eastfield weather, wind two five zero degrees two zero gusting three two knots, "
        "visibility four thousand eight hundred metres, cloud broken one thousand two hundred feet, Q N H niner niner five hectopascals"
    )
    assert engine.handle(Intent("request_weather"), west, None, {**cruising, "wx_source": 0}) is None  # no report to give

    labels = [s["label"] for s in engine.suggestions(west, None, cruising)]
    assert {"Deviate left", "Deviate right", "Destination weather"} <= set(labels)
    assert engine.handle(Intent("request_deviation", direction="left"), west, None, cruising).text == (
        f"{CALLSIGN}, deviation left of track approved, report clear of weather"
    )
    assert "Clear of weather" in [s["label"] for s in engine.suggestions(west, None, cruising)]
    assert engine.handle(Intent("clear_of_weather"), west, None, cruising).text == f"{CALLSIGN}, roger, resume own navigation"
    assert engine.handle(Intent("clear_of_weather"), west, None, cruising) is None


def test_a_new_atis_is_announced_to_an_aircraft_on_frequency(tmp_path):
    db, engine, _, _ = make(tmp_path)
    engine.flight.has_been_airborne = False
    tower, book = db.station(118100, 51.0, -1.0), AtisBook()
    airport, parked = db.airport("EGXX"), at(51.003, -1.0, on_ground=True)
    first = book.current(airport, Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1013), 12 * 3600)
    engine.handle(Intent("radio_check"), tower, first, parked)
    assert engine.tick(tower, first, parked) is None
    changed = book.current(airport, Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1009), 12 * 3600 + 900)
    assert changed.letter == "N" and first.letter == "M"
    assert (
        engine.tick(tower, changed, parked).text == f"{CALLSIGN}, information November is now current, Q N H one zero zero niner"
    )
    assert engine.tick(tower, changed, parked) is None


@pytest.mark.parametrize("mode, expected", [("never", False), ("always", True)])
def test_holding_setting(tmp_path, mode, expected):
    db, engine, _, _ = make(tmp_path, navfix=NAVFIX, holding=mode)
    engine.flight.cleared_ft = 13000
    approach = db.station(124300, 51.0, 5.0)
    reply = engine.handle(Intent("check_in"), approach, ISSUE, at(51.05, 4.3, feet=13000, heading=90))
    assert ("hold at Echox" in reply.text) is expected
