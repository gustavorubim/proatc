"""Two R NAV approaches to one runway, the Y and the Z: the one asked for is the one cleared, by its letter, from the
initial fix the arrival leads to. Found on a real flight into Spokane: the pilot asked for the R NAV Z on the ZOOMR
arrival and was cleared "R NAV approach runway two one via Hilie", which is on the R NAV Y only, 69 miles away
on the other side of the field, when the arrival ended at ZOOMR, a fix of both."""

from proatc.flightplan import FlightPlan
from proatc.intents import Intent
from proatc.navfix import NavFix
from proatc.procedures import choose_approach, parse_cifp
from tests.test_ifr import CALLSIGN, at
from tests.test_procedures import WESTERLY, leg
from tests.test_realism import make

# Runway 27 lands westward, its threshold at 51.0 N 5.37 E. The arrival comes from the west, beyond the runway, and
# ends at WSIDE. Both R NAV approaches start there too, and their legs from it curve round to final.
CIFP = [
    leg("STAR", 10, "2", "WEST1A", "ALL", "WENTR", "", "FL150"),
    leg("STAR", 20, "2", "WEST1A", "ALL", "WSIDE", "", "08000"),
    leg("APPCH", 10, "A", "I27", "IAFNE", "IAFNE", "+", "04000", role="A", path="IF"),
    leg("APPCH", 20, "A", "I27", "IAFNE", "IF27", "+", "03000"),
    leg("APPCH", 10, "I", "I27", "", "IF27", "+", "03000", role="I", path="IF"),
    leg("APPCH", 20, "I", "I27", "", "FF27", "+", "02500", role="F"),
    leg("APPCH", 30, "I", "I27", "", "RW27", "", "00350", role="M"),
]
for letter, other, where in (("Y", "IAFNE", "north"), ("Z", "IAFSE", "south")):
    CIFP += [
        leg("APPCH", 10, "A", f"R27-{letter}", other, other, "+", "04000", role="A", path="IF"),
        leg("APPCH", 20, "A", f"R27-{letter}", other, "IF27", "+", "03000"),
        leg("APPCH", 10, "A", f"R27-{letter}", "WSIDE", "WSIDE", "+", "05000", role="A", path="IF"),
        leg("APPCH", 20, "A", f"R27-{letter}", "WSIDE", f"W{where[0].upper()}1", "+", "05000"),
        leg("APPCH", 30, "A", f"R27-{letter}", "WSIDE", f"W{where[0].upper()}2", "+", "04000", path="RF"),
        leg("APPCH", 40, "A", f"R27-{letter}", "WSIDE", "IF27", "+", "03000", path="RF"),
        leg("APPCH", 10, "R", f"R27-{letter}", "", "IF27", "+", "03000", role="I", path="IF"),
        leg("APPCH", 20, "R", f"R27-{letter}", "", "FF27", "+", "02500", role="F"),
        leg("APPCH", 30, "R", f"R27-{letter}", "", "RW27", "", "00350", role="M"),
    ]
FIXES = NavFix(
    fixes={
        "WENTR": [(51.0, 4.2)], "WSIDE": [(51.0, 4.75)],
        "WN1": [(51.25, 5.0)], "WN2": [(51.25, 5.6)], "WS1": [(50.75, 5.0)], "WS2": [(50.75, 5.6)],
        "IAFNE": [(51.2, 5.75)], "IAFSE": [(50.8, 5.75)], "IF27": [(51.0, 5.65)], "FF27": [(51.0, 5.5)],
    }
)  # fmt: skip
PLAN = FlightPlan("EGXX", "LFXX", ["WENTR"], 36000, star="WEST1A", arrival_runway="27")
ON_THE_ARRIVAL = at(51.0, 4.6, feet=9000, heading=90)  # west of the field, short of WSIDE


def world(tmp_path, cifp=CIFP):
    return make(tmp_path, east=cifp, navfix=FIXES, plan=PLAN)


def test_the_approach_asked_for_by_its_letter_is_the_one_chosen():
    procedures = parse_cifp(CIFP)
    assert choose_approach(procedures, "27", "RNAV").ident == "R27-Y"
    assert choose_approach(procedures, "27", "RNAV", "Z").ident == "R27-Z"
    assert choose_approach(procedures, "27", "RNAV", "X").ident == "R27-Y"  # none of that letter: the family's best
    assert choose_approach(procedures, "27").ident == "I27"  # nothing asked for: the most precise
    assert procedures.approaches["R27-Z"].variant == "Z" and procedures.approaches["I27"].variant is None
    # Of two, the one the arrival leads into.
    only_z = [line for line in CIFP if "R27-Y" not in line or "IAFNE" in line]
    assert choose_approach(parse_cifp(only_z), "27", "RNAV", arrival_fixes={"WENTR", "WSIDE"}).ident == "R27-Z"


def test_a_pilot_who_asks_for_the_rnav_z_is_cleared_for_it_via_the_fix_the_arrival_ends_at(tmp_path):
    _, engine, approach, _ = world(tmp_path)
    first = engine.handle(Intent("check_in"), approach, WESTERLY, ON_THE_ARRIVAL)
    assert "expect I L S approach runway two seven" in first.text
    engine.flight.pending = None
    # The suggested calls name each R NAV approach by its letter, and one click carries the letter.
    offered = {item["label"]: item for item in engine.suggestions(approach, WESTERLY, ON_THE_ARRIVAL)}
    assert "Request RNAV Y approach" in offered and "Request RNAV Z approach" in offered
    clicked = offered["Request RNAV Z approach"]
    assert clicked["text"].endswith("request the R NAV Zulu approach")
    asked = engine.handle(Intent.from_dict(clicked["intent"]), approach, WESTERLY, ON_THE_ARRIVAL)
    assert asked.text == f"{CALLSIGN}, roger, expect R NAV Zulu approach runway two seven"
    engine.flight.pending = None
    cleared = engine.tick(approach, WESTERLY, ON_THE_ARRIVAL)
    assert "route direct Wside" in cleared.text
    assert "cleared R NAV Zulu approach runway two seven via Wside, report established" in cleared.text
    arrival = engine.flight.arrival
    assert (arrival.procedure.ident, arrival.via, engine.flight.direct_to) == ("R27-Z", "WSIDE", "WSIDE")
    labels = [item["label"] for item in engine.suggestions(approach, WESTERLY, ON_THE_ARRIVAL)]
    assert any(item["text"].endswith("established R NAV Zulu runway two seven")
               for item in engine.suggestions(approach, WESTERLY, ON_THE_ARRIVAL) if item["label"] == "Established")  # fmt: skip
    assert "Request RNAV Y approach" not in labels  # cleared already: no switching now


def test_said_in_words_the_letter_is_kept():
    for said in ("RNAV Z", "R NAV Zulu", "RNAV (GPS) Z", "RNP Z", "rnav z"):
        intent = Intent.from_dict({"intent": "request_approach", "approach": said})
        assert (intent.approach, intent.variant) == ("RNAV", "Z"), said
    assert (Intent.from_dict({"approach": "ILS Yankee"}).approach, Intent.from_dict({"approach": "ILS Yankee"}).variant) == (
        "ILS",
        "Y",
    )
    assert Intent.from_dict({"approach": "RNAV"}).variant is None
    assert Intent.from_dict({"approach": "visual", "variant": "Z"}).variant is None  # a letter goes with an instrument one


def test_with_no_letter_asked_the_rnav_is_cleared_via_the_arrivals_fix_not_one_across_the_field(tmp_path):
    # Only R NAV approaches: cleared on first contact. Both R NAV approaches start at WSIDE, where the arrival
    # ends; the Y also starts at IAFNE, on the final approach side. The controller took IAFNE, sending the aircraft
    # round the field to it, because WSIDE lies beyond the runway.
    rnav_only = [line for line in CIFP if "I27" not in line]
    _, engine, approach, _ = world(tmp_path, rnav_only)
    cleared = engine.handle(Intent("check_in"), approach, WESTERLY, ON_THE_ARRIVAL)
    assert "cleared R NAV Yankee approach runway two seven via Wside" in cleared.text and "Iafne" not in cleared.text
    # The centre's expectation names the letter too, and the controller says it whenever the runway has two.
    assert engine.flight.arrival.words() == "R NAV Yankee approach runway two seven"


def test_without_an_arrival_a_fix_beyond_the_runway_is_still_not_used(tmp_path):
    # No STAR to lead there: the long way round to the far side is not given, as before.
    _, engine, approach, _ = make(tmp_path, east=[line for line in CIFP if "I27" not in line and "STAR" not in line],
                                  navfix=FIXES, plan=FlightPlan("EGXX", "LFXX", [], 36000, arrival_runway="27"))  # fmt: skip
    cleared = engine.handle(Intent("check_in"), approach, WESTERLY, ON_THE_ARRIVAL)
    assert "Wside" not in cleared.text


def test_the_legs_from_an_initial_fix_stop_where_the_final_segment_ends(tmp_path):
    # Found flying real data: Fort Yukon's R NAV 04 has no runway fix. Its final segment ends at the missed approach
    # point, and the missed approach that follows goes back out to the fix final began at. Followed on, the flight
    # turned back out over the missed approach.
    from proatc import ifr

    _, engine, _, _ = world(tmp_path)
    airport = engine.db.airport("LFXX")
    no_runway_fix = [
        leg("APPCH", 10, "A", "R27", "WSIDE", "WSIDE", "+", "05000", role="A", path="IF"),
        leg("APPCH", 20, "A", "R27", "WSIDE", "IF27", "+", "03000"),
        leg("APPCH", 10, "R", "R27", "", "IF27", "+", "03000", role="I", path="IF"),
        leg("APPCH", 20, "R", "R27", "", "FF27", "+", "02500", role="M"),
        leg("APPCH", 30, "R", "R27", "", "", "+", "01000", path="CA"),
        leg("APPCH", 40, "R", "R27", "", "IF27", "+", "03000", path="DF"),
    ]
    approach = parse_cifp(no_runway_fix).approaches["R27"]
    assert ifr.procedure_points(engine, approach, "WSIDE", airport) == [(51.0, 4.75), (51.0, 5.65), (51.0, 5.5)]
    # With a runway fix, the legs end before it.
    full = parse_cifp(CIFP).approaches["R27-Z"]
    assert ifr.procedure_points(engine, full, "WSIDE", airport) == [
        (51.0, 4.75), (50.75, 5.0), (50.75, 5.6), (51.0, 5.65), (51.0, 5.5)
    ]  # fmt: skip
