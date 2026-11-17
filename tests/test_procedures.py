"""Procedures as they are published: the legs flown for a runway and a route, every kind of altitude
restriction, speed limits, initial approach fixes, and a watch on the cleared track."""

from proatc import restrictions
from proatc.atis import Issue
from proatc.flightplan import FlightPlan
from proatc.intents import Intent
from proatc.navfix import NavFix
from proatc.procedures import parse_cifp
from proatc.weather import Weather
from tests.test_ifr import CALLSIGN, ISSUE, at
from tests.test_realism import make


def leg(kind, seq, route_type, name, transition, fix, descr="", alt="", alt2="", speed="", role=" ", path="TF"):
    """One CIFP line. Only the fields the engine reads are filled in."""
    f = [""] * 38
    f[0], f[1], f[2], f[3], f[4] = f"{seq:03d}", route_type, name, transition, fix
    f[8], f[11], f[22], f[23], f[24], f[27] = f"E  {role}", path, descr, alt, alt2, speed
    return f"{kind}:{','.join(f)};"


# A SID from runway 27 with two en-route transitions, and one from runway 09.
SID = [
    leg("SID", 10, "1", "WEST2A", "RW27", "W27A", "+", "02000"),
    leg("SID", 20, "1", "WEST2A", "RW27", "WMID", "-", "05000", speed="220"),
    leg("SID", 10, "1", "WEST2A", "RW09", "W09A", "+", "01500"),
    leg("SID", 20, "1", "WEST2A", "RW09", "WMID", "", "04000"),
    leg("SID", 10, "2", "WEST2A", "ALL", "WMID"),
    leg("SID", 20, "2", "WEST2A", "ALL", "WCOM", "B", "09000", "07000"),
    leg("SID", 10, "3", "WEST2A", "ALFAX", "WCOM"),
    leg("SID", 20, "3", "WEST2A", "ALFAX", "ALFAX"),
    leg("SID", 10, "3", "WEST2A", "OTHER", "WCOM"),
    leg("SID", 20, "3", "WEST2A", "OTHER", "OTHER", "+", "FL120"),
]
# A STAR with two ways in, a common part, and a part for each runway; and an R NAV approach with an initial fix.
STAR = [
    leg("STAR", 10, "1", "EAST1B", "DELTA", "DELTA", "B", "FL200", "FL160", "280"),
    leg("STAR", 20, "1", "EAST1B", "DELTA", "EJOIN"),
    leg("STAR", 10, "1", "EAST1B", "NORTH", "NORTH", "", "FL180"),
    leg("STAR", 20, "1", "EAST1B", "NORTH", "EJOIN"),
    leg("STAR", 10, "2", "EAST1B", "ALL", "EJOIN", "-", "FL120"),
    leg("STAR", 20, "2", "EAST1B", "ALL", "ESLOW", speed="250"),
    leg("STAR", 10, "3", "EAST1B", "RW27", "ESLOW"),
    leg("STAR", 20, "3", "EAST1B", "RW27", "E27", "", "06000"),
    leg("STAR", 10, "3", "EAST1B", "RW09", "ESLOW"),
    leg("STAR", 20, "3", "EAST1B", "RW09", "E09", "", "08000"),
    leg("APPCH", 10, "A", "R27", "IAFNE", "IAFNE", "+", "04000", role="A", path="IF"),
    leg("APPCH", 20, "A", "R27", "IAFNE", "IF27", "+", "03000"),
    leg("APPCH", 10, "R", "R27", "", "IF27", "+", "03000", role="I", path="IF"),
    leg("APPCH", 20, "R", "R27", "", "FF27", "+", "02500", role="F"),
    leg("APPCH", 30, "R", "R27", "", "RW27", "", "00350", role="M"),
    leg("APPCH", 40, "R", "R27", "", "MISSD", "+", "04000"),
]
# Positions: the arrival comes in from the east along 51 north; the initial fix is north east of the field.
FIXES = NavFix(
    fixes={
        "DELTA": [(51.0, 7.6)], "NORTH": [(52.2, 6.6)], "EJOIN": [(51.0, 6.9)], "ESLOW": [(51.0, 6.4)], "E27": [(51.0, 6.0)],
        "E09": [(50.6, 5.0)], "IAFNE": [(51.2, 5.75)], "IF27": [(51.0, 5.65)], "FF27": [(51.0, 5.5)],
        "W27A": [(51.0, -1.1)], "W09A": [(51.0, -0.9)], "WMID": [(51.0, -1.3)], "WCOM": [(51.0, -1.6)], "ALFAX": [(51.0, -2.0)],
        "OTHER": [(51.6, -1.6)],
    }
)  # fmt: skip
PLAN = FlightPlan("EGXX", "LFXX", "ALFAX UL9 BRAVO UM605 DELTA".split(), 36000, arrival_runway="27")
WESTERLY = Issue("C", Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1013.2), 12 * 3600)


def names(legs):
    return [leg.fix for leg in legs]


def test_the_legs_flown_depend_on_the_runway_and_the_route():
    sid = parse_cifp(SID).sids["WEST2A"]
    assert names(sid.chain("SID", "27", ["ALFAX"])) == ["W27A", "WMID", "WCOM", "ALFAX"]
    assert names(sid.chain("SID", "09", ["OTHER"])) == ["W09A", "WMID", "WCOM", "OTHER"]
    assert names(sid.chain("SID", "27", ["NOWHERE"])) == ["W27A", "WMID", "WCOM"]  # no transition matches: none is flown
    # Where two parts join, the restriction on the shared fix is kept.
    assert [(leg.fix, leg.alt_descr, leg.altitude_ft, leg.speed_kt) for leg in sid.chain("SID", "27", [])][1] == (
        "WMID",
        "-",
        5000,
        220,
    )

    star = parse_cifp(STAR).stars["EAST1B"]
    assert names(star.chain("STAR", "27", ["DELTA"])) == ["DELTA", "EJOIN", "ESLOW", "E27"]
    assert names(star.chain("STAR", "09", ["NORTH"])) == ["NORTH", "EJOIN", "ESLOW", "E09"]
    first = star.chain("STAR", "27", ["DELTA"])[0]
    assert (first.alt_descr, first.altitude_ft, first.altitude2_ft, first.speed_kt) == ("B", 20000, 16000, 280)

    approach = parse_cifp(STAR).approaches["R27"]
    assert approach.initial_fixes() == [("IAFNE", 4000)] and approach.faf == "FF27"
    assert names(approach.final) == ["IF27", "FF27", "RW27"] and (approach.missed_ft, approach.missed_fix) == (4000, "MISSD")


def test_only_the_restrictions_of_the_part_flown_are_used(tmp_path):
    _, engine, _, _ = make(tmp_path, navfix=FIXES)
    star, east = parse_cifp(STAR).stars["EAST1B"], engine.db.airport("LFXX")
    flown = restrictions.build(engine, star, east, "27", ["DELTA"])
    assert [(c.fix, c.descr, c.altitude_ft, c.floor_ft, c.speed_kt) for c in flown] == [
        ("DELTA", "B", 20000, 16000, 280),
        ("EJOIN", "-", 12000, None, None),
        ("ESLOW", "+", 60000, None, 250),  # a speed limit only
        ("E27", "", 6000, None, None),
    ]
    assert "E09" not in [c.fix for c in flown] and "NORTH" not in [c.fix for c in flown]
    assert [c.fix for c in restrictions.build(engine, star, east, "09", ["NORTH"])] == ["NORTH", "EJOIN", "ESLOW", "E09"]
    assert flown[0].upper and flown[0].lower_ft == 16000 and not flown[2].upper

    sid, west = parse_cifp(SID).sids["WEST2A"], engine.db.airport("EGXX")
    crossings = restrictions.build_sid(engine, sid, west, "27", ["ALFAX"])
    assert [(c.fix, c.descr, c.altitude_ft, c.floor_ft, c.speed_kt) for c in crossings] == [
        ("W27A", "+", 2000, None, None),
        ("WMID", "-", 5000, None, 220),
        ("WCOM", "B", 9000, 7000, None),
    ]
    track = restrictions.path(engine, sid, "SID", west, "27", ["ALFAX"])
    assert track == [(51.0, -1.1), (51.0, -1.3), (51.0, -1.6), (51.0, -2.0)]
    assert restrictions.off_track_nm(track, 51.0, -1.4) < 0.1 and 5.9 < restrictions.off_track_nm(track, 51.1, -1.4) < 6.1
    assert restrictions.off_track_nm(track, 51.0, -3.0) is None  # beyond its end: no longer the SID's business


def departure(tmp_path):
    db, engine, _, _ = make(tmp_path, navfix=FIXES, plan=PLAN)
    engine._procedures["EGXX"] = parse_cifp(SID)
    engine.flight.has_been_airborne, engine.flight.cleared_ft = False, None
    delivery = db.station(121900, 51.0, -1.0)
    engine.handle(Intent("request_clearance", flight_rules="IFR"), delivery, ISSUE, at(51.0, -1.0, on_ground=True))
    engine.flight.pending = None
    radar = db.station(125500, 51.0, -1.0)
    engine.flight.contacted.add(radar.key)
    engine._observe(at(51.0, -1.05, feet=1500))
    engine.flight.cleared_ft = 24000  # no climb is due: these tests are about the track and the crossings
    return engine, radar


def test_sid_crossings_are_judged_by_their_kind_and_their_speed(tmp_path):
    engine, radar = departure(tmp_path)
    assert engine.flight.sid == "WEST2A" and len(engine.flight.sid_path) == 4  # its waypoints only: no line from the airport
    assert engine.tick(radar, ISSUE, {**at(51.0, -1.1, feet=2400), "ias_kt": 200}) is None  # above 2000: fine
    too_high = engine.tick(radar, ISSUE, {**at(51.0, -1.3, feet=6000), "ias_kt": 210})
    assert too_high.text == f"{CALLSIGN}, you were to cross Wmid at or below altitude five thousand feet"
    assert too_high.event == "restriction_missed"
    engine.flight.pending = None
    low = engine.tick(radar, ISSUE, {**at(51.0, -1.6, feet=6000), "ias_kt": 250})
    assert low.text == f"{CALLSIGN}, you were to cross Wcom at or above flight level zero seven zero, expedite climb"

    engine, radar = departure(tmp_path)
    fast = engine.tick(radar, ISSUE, {**at(51.0, -1.3, feet=4500), "ias_kt": 260})
    assert fast.text == f"{CALLSIGN}, speed restriction two two zero knots at Wmid, reduce speed"


def test_a_flight_that_leaves_its_sid_is_asked_once(tmp_path):
    engine, radar = departure(tmp_path)
    clock = [0.0]
    engine.now = lambda: clock[0]
    off = {**at(51.12, -1.45, feet=7000), "ias_kt": 250}  # seven miles north of the track
    # Found on a real flight out of Boston: the departure began on runway heading, then turned to intercept,
    # and the controller called it "six miles off the departure track" before it had reached the track.
    clock[0] = -500.0
    assert engine.tick(radar, ISSUE, off) is None
    clock[0] = -300.0
    assert engine.tick(radar, ISSUE, off) is None and engine.flight.on_track == set()  # not on it yet: not "off" it
    on_it = {**at(51.005, -1.2, feet=6000), "ias_kt": 250}
    clock[0] = -100.0
    assert engine.tick(radar, ISSUE, on_it) is None and engine.flight.on_track == {"WEST2A"}  # joined: watched from now
    clock[0] = 0.0
    assert engine.tick(radar, ISSUE, off) is None  # only just noticed
    clock[0] = 50.0
    asked = engine.tick(radar, ISSUE, off)
    assert (
        asked.text == f"{CALLSIGN}, you appear to be seven miles off the departure track, confirm you are following the departure"
    )
    assert asked.event == "off_route"
    clock[0] = 200.0
    assert engine.tick(radar, ISSUE, off) is None  # once
    # A flight on a direct routing, or deviating around weather, is not on the SID any more.
    engine, radar = departure(tmp_path)
    engine.now = lambda: clock[0]
    engine.flight.direct_to = "BRAVO"
    clock[0] = 300.0
    engine.tick(radar, ISSUE, off)
    clock[0] = 400.0
    assert engine.tick(radar, ISSUE, off) is None


def test_the_star_is_descended_by_its_own_restrictions_and_speeds(tmp_path):
    _, engine, _, _ = make(tmp_path, navfix=FIXES, plan=PLAN)
    engine._procedures["LFXX"] = parse_cifp(STAR)
    centre = engine.centers.on_frequency(132100, 51.0, 7.9)
    engine.flight.contacted.add(centre.key)
    engine.flight.cleared_ft = 36000

    def cruising(lon, feet, ias=280.0):
        return {**at(51.0, lon, feet=feet, heading=270), "ias_kt": ias}

    first = engine.tick(centre, None, cruising(7.9, 36000))  # already within a three degree path of Delta
    assert first.text == (
        f"{CALLSIGN}, descend flight level two zero zero, level by Delta, speed two eight zero knots or less at Delta, "
        "East one Bravo arrival"
    )
    assert [c.fix for c in engine.flight.constraints] == ["DELTA", "EJOIN", "ESLOW", "E27"]
    engine.flight.pending = None
    fast = engine.tick(centre, None, cruising(7.6, 20000, ias=310))
    assert fast.text == f"{CALLSIGN}, speed restriction two eight zero knots at Delta, reduce speed"
    engine.flight.pending = None
    nxt = None
    for lon in (7.5, 7.4, 7.3):
        nxt = nxt or engine.tick(centre, None, cruising(lon, 20000))
    assert nxt.text == f"{CALLSIGN}, descend flight level one two zero, level by Ejoin"
    engine.flight.pending = None
    slow = None
    for lon in (6.9, 6.8, 6.7):
        slow = slow or engine.tick(centre, None, cruising(lon, 12000))
    assert slow.text == f"{CALLSIGN}, cross Eslow at two five zero knots or less"


def test_an_rnav_approach_is_flown_from_its_initial_fix_not_vectored(tmp_path):
    _, engine, approach, tower = make(tmp_path, east=[line for line in STAR if line.startswith("APPCH")], navfix=FIXES)
    inbound = at(51.3, 6.2, feet=7000, heading=230)  # north east of the field, beyond the initial fix
    cleared = engine.handle(Intent("check_in"), approach, WESTERLY, inbound)
    assert cleared.text == (
        f"{CALLSIGN}, Eastfield Approach, identified, route direct Iafne, descend altitude four thousand feet, "
        "Q N H one zero one three, cleared R NAV approach runway two seven via Iafne, report established"
    )
    assert cleared.expect == {"altitude": 4000, "runway": "27"}
    arrival = engine.flight.arrival
    assert (arrival.stage, arrival.via, engine.flight.direct_to) == ("procedure", "IAFNE", "IAFNE")
    engine.flight.pending = None
    assert engine.tick(approach, WESTERLY, at(51.2, 5.75, feet=4000, heading=200)) is None  # no vectors on the way
    assert "Established" in [item["label"] for item in engine.suggestions(approach, WESTERLY, inbound)]
    on_final = engine.tick(approach, WESTERLY, at(51.0, 5.55, feet=2800, heading=270))
    assert on_final.text == f"{CALLSIGN}, contact Eastfield Tower one one niner decimal one"

    # Already inside the initial fix: it is behind the aircraft, so vectors after all.
    _, engine, approach, _ = make(tmp_path, east=[line for line in STAR if line.startswith("APPCH")], navfix=FIXES)
    close = engine.handle(Intent("check_in"), approach, WESTERLY, at(51.1, 5.45, feet=4000, heading=90))
    assert "heading" in close.text and "direct" not in close.text


def test_a_hold_gives_an_expected_approach_time(tmp_path):
    from proatc.holding import HoldState, issue
    from proatc.navfix import Hold

    navfix = NavFix(fixes={"E27": [(51.0, 6.0)]}, holds={"E27": [Hold("E27", "LFXX", 270.0, "R", 1.0, 5000, 14000, 220)]})
    _, engine, approach, _ = make(tmp_path, navfix=navfix)
    raw = {**at(51.0, 6.5, feet=9000, heading=270), "zulu_seconds": 12 * 3600 + 20 * 60}
    engine.handle(Intent("check_in"), approach, ISSUE, raw)
    found = ("E27", (51.0, 6.0), navfix.hold("E27", "LFXX", 9000))
    reply = issue(engine, approach, raw, CALLSIGN, "", found, 10, lambda feet: f"flight level {feet // 100}")
    assert reply.text.endswith("expected approach time one two three zero")
    assert isinstance(engine.flight.arrival.hold, HoldState)
