"""ATIS: runway selection and the broadcast text, built from templates (no language model)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import phraseology as say
from .navdata import Airport
from .weather import Weather

CALM_KT = 3
RUNWAY_TAILWIND_KT = 5  # a runway asked for, or named in the flight plan, is used with no more tailwind than this
RUNWAY_CROSSWIND_KT = 25  # nor with more crosswind than this
IN_USE_FRACTION = 0.6  # a runway shorter than this part of the airport's longest is not one of the runways in use
# apt.dat surface codes of runways an airport does not send its traffic to while it has a paved one: grass, dirt,
# gravel, dry lakebed, water, snow or ice. Such a runway is still given to a pilot who asks for it.
SOFT_SURFACES = frozenset((3, 4, 5, 12, 13, 14))

# The runways the flight plan names, by airport. Dispatch picks them from the wind and from how the airport is really
# run, so where the wind allows it the airport is taken to be using that runway's direction: its ATIS, every
# controller and the other traffic on frequency then agree with the plan. Set by the engine with the plan.
_PLANNED_DEPARTURE: dict[str, str] = {}
_PLANNED_ARRIVAL: dict[str, str] = {}
# Runway ends no taxiway reaches in the scenery, by airport, as the engine found them in the taxi network: left out of
# the runways in use, so that the ATIS does not give a runway no controller will.
_CLOSED: dict[str, frozenset[str]] = {}
# What the airport's real digital ATIS says of its runways (datis.RealRunways), by airport: the runways landing
# and departing there now, and the ones closed. Where it is known it comes before the wind and the flight plan:
# an airport is not always run the way the wind suggests. Set by the engine, on real weather only.
_REAL: dict[str, object] = {}


class RunwaysInUse(list):
    """The runways in use where it is known which land and which depart: the list of all of them, with the two
    uses kept. `split_runways` gives them back as they are, not by halves."""

    def __init__(self, landing, departing):
        super().__init__([*landing, *(end for end in departing if end not in landing)])
        self.landing, self.departing = list(landing), list(departing)


def uses_faa_style(ident: str) -> bool:
    """US and Canada report inches of mercury and statute miles."""
    return len(ident) == 4 and ident[0] in "KC" or ident[:2] in ("PA", "PH", "PG", "TJ")


def plan_runways(origin: str | None, departure: str | None, destination: str | None, arrival: str | None) -> None:
    """The flight plan's runways: its departure runway at the origin, its arrival runway at the destination."""
    _PLANNED_DEPARTURE.clear()
    _PLANNED_ARRIVAL.clear()
    if origin and departure:
        _PLANNED_DEPARTURE[origin.upper()] = departure.strip().upper()
    if destination and arrival:
        _PLANNED_ARRIVAL[destination.upper()] = arrival.strip().upper()


def close_runways(ident: str, ends) -> None:
    """The runway ends at this airport no taxiway reaches."""
    _CLOSED[ident.upper()] = frozenset(ends)


def real_runways(ident: str, found) -> None:
    """What the airport's real ATIS says of its runways, or None when nothing is known (or it is not to be used)."""
    if found is None:
        _REAL.pop(ident.upper(), None)
    else:
        _REAL[ident.upper()] = found


def really_in_use(ident: str) -> frozenset:
    """The runways the airport's real ATIS gives as landing or departing; empty when it is not known."""
    found = _REAL.get(ident.upper())
    return found.in_use if found else frozenset()


def really_closed(ident: str) -> frozenset:
    """The runways the airport's real ATIS gives as closed."""
    found = _REAL.get(ident.upper())
    return found.closed if found else frozenset()


def forget_runways() -> None:
    """A new session, perhaps with other scenery: nothing is known of any airport's runways or of a plan."""
    _CLOSED.clear()
    _REAL.clear()
    plan_runways(None, None, None, None)


def wind_on_runway(heading_true: float, wind_dir_true: float, wind_kt: float) -> tuple[float, float]:
    """(tailwind, crosswind) in knots on a runway of this true heading. A headwind is a negative tailwind."""
    angle = math.radians(wind_dir_true - heading_true)
    return -wind_kt * math.cos(angle), abs(wind_kt * math.sin(angle))


def wind_allows(heading_true: float, wind_dir_true: float, wind_kt: float) -> bool:
    """Whether a runway of this true heading can be used in this wind."""
    tail, cross = wind_on_runway(heading_true, wind_dir_true, wind_kt)
    return wind_kt < 1 or (tail <= RUNWAY_TAILWIND_KT and cross <= RUNWAY_CROSSWIND_KT)


def runways_in_use(airport: Airport, wind_dir_true: float, wind_kt: float) -> list[str]:
    """The runways the airport's real ATIS gives, where that is known. Otherwise the runway ends with the best
    headwind, including parallels; the flight plan's runway and its parallels instead, where the wind allows it.
    Short runways are ignored, and so are grass, dirt and gravel ones where the airport has a paved runway. Runways
    no taxiway reaches, or closed in the real ATIS, are left out while another runway long enough remains."""
    if not airport.runways:
        return []
    real = _REAL.get(airport.ident)
    if real and real.in_use:
        return RunwaysInUse(real.landing, real.departing)
    hard = [r for r in airport.runways if r.surface not in SOFT_SURFACES] or airport.runways
    longest = max(r.length_m for r in hard)
    ends = [(ident, heading, r.length_m) for r in hard if r.length_m >= IN_USE_FRACTION * longest for ident, heading in r.ends()]
    # The longest runway counts even when closed: a scenery that leaves the main runway out of its taxi network
    # does not make a short strip beside it the runway in use.
    closed = _CLOSED.get(airport.ident, frozenset()) | (real.closed if real else frozenset())
    ends = [end for end in ends if end[0] not in closed] or ends
    departure, arrival = _PLANNED_DEPARTURE.get(airport.ident), _PLANNED_ARRIVAL.get(airport.ident)
    planned = [end for end in ends if end[0] in (departure, arrival) and wind_allows(end[1], wind_dir_true, wind_kt)]
    if planned:
        best = min(planned, key=lambda end: end[0] != departure)  # leaving and landing at one airport: leaving first
    elif wind_kt < CALM_KT:
        # Calm: the longest runway, in the direction that appears first in the sim's data.
        best = max(ends, key=lambda end: end[2])
    else:
        best = max(ends, key=lambda end: (round(math.cos(math.radians(wind_dir_true - end[1])), 3), end[2]))
    parallel = [ident for ident, heading, _ in ends if abs((heading - best[1] + 180) % 360 - 180) < 5]
    parallel.sort(key=lambda ident: (ident[:2], "LCR".find(ident[-1])))
    # Of parallel runways the first ones land and the last ones depart (split_runways): the plan's departure runway
    # goes last and its arrival runway first, so that the ATIS gives each to the use the plan has for it.
    if departure in parallel:
        parallel = [ident for ident in parallel if ident != departure] + [departure]
    if arrival in parallel:
        parallel = [arrival] + [ident for ident in parallel if ident != arrival]
    return parallel


def split_runways(active: list[str]) -> tuple[list[str], list[str]]:
    """(landing, departing). With parallel runways in use, arrivals and departures are kept apart:
    the first half lands, the second half departs, the middle one of three does both. Where the real ATIS says
    which land and which depart, those."""
    if isinstance(active, RunwaysInUse):
        return list(active.landing), list(active.departing)
    count = len(active)
    if count < 2:
        return active, active
    return active[: (count + 1) // 2], active[count // 2 :]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _wind(weather: Weather, variation: float) -> str:
    if weather.wind_kt < 1:
        return "Wind calm."
    direction = round(((weather.wind_dir_true + variation) % 360) / 10) * 10 or 360
    gust = f" gusting {say.digits(round(weather.gust_kt))}" if weather.gust_kt and weather.gust_kt >= weather.wind_kt + 5 else ""
    return f"Wind {say.digits(f'{direction:03d}')} degrees {say.digits(round(weather.wind_kt))}{gust} knots."


def _visibility(weather: Weather, faa: bool) -> str:
    if faa:
        miles = weather.visibility_sm
        if miles >= 10:
            return "Visibility one zero."
        return f"Visibility {say.digits(max(1, round(miles)))}." if miles >= 1 else "Visibility less than one."
    metres = weather.visibility_sm * 1609.34
    if metres >= 9999:
        return "Visibility one zero kilometres or more."
    if metres >= 5000:
        return f"Visibility {say.digits(int(metres // 1000))} kilometres."
    return f"Visibility {say.altitude(max(100, metres))} metres."


def _clouds(weather: Weather, faa: bool) -> str:
    layers = [c for c in weather.clouds if c.base_ft_agl < 25000]
    if not layers:
        return "Sky clear." if faa else "No significant cloud."
    parts = []
    for cloud in layers:
        amount = (
            "few"
            if cloud.coverage < 0.3
            else "scattered"
            if cloud.coverage < 0.55
            else "broken"
            if cloud.coverage < 0.9
            else "overcast"
        )
        text = f"{amount} {say.altitude(max(100, cloud.base_ft_agl))}"
        if not faa:
            text += " feet"
        if cloud.cumulonimbus:
            text += " cumulonimbus"
        parts.append(text)
    joined = ", ".join(parts)
    return f"{joined.capitalize()}." if faa else f"Cloud {joined}."


def _pressure(weather: Weather, faa: bool) -> str:
    if faa:
        return f"Altimeter {say.digits(f'{round(weather.qnh_hpa * 2.953):04d}')}."
    return f"{say.qnh(weather.qnh_hpa)}."


def low_visibility(weather: Weather) -> bool:
    """Below about 600 metres visibility or a 200 ft ceiling."""
    ceiling = min((c.base_ft_agl for c in weather.clouds if c.coverage >= 0.55), default=99999)
    return weather.visibility_sm < 0.38 or ceiling < 200


def compose(
    airport: Airport, weather: Weather, letter: str, zulu_seconds: float, variation: float = 0.0, name: str | None = None
) -> str:
    """The full spoken ATIS. `variation` is magnetic minus true heading, in degrees."""
    faa = uses_faa_style(airport.ident)
    name, info = name or airport.spoken_name, say.letter(letter)
    active = runways_in_use(airport, weather.wind_dir_true, weather.wind_kt)
    landing, departing = split_runways(active)

    def names(idents: list[str]) -> str:
        return f"runway{'s' if len(idents) > 1 else ''} {_join([say.runway(r) for r in idents])}"

    if not active:
        runway_line = ""
    elif landing != departing:
        runway_line = (
            f"Landing {names(landing)}, departing {names(departing)}."
            if faa
            else f"{names(landing).capitalize()} in use for landing, {names(departing)} for departure."
        )
    else:
        runway_line = f"Landing and departing {names(active)}." if faa else f"{names(active).capitalize()} in use."
    closing = (
        f"Advise on initial contact you have information {info}."
        if faa
        else f"Acknowledge receipt of information {info} on first contact."
    )
    lines = [
        f"{name} information {info}, time {say.zulu_time(zulu_seconds)} Zulu.",
        runway_line,
        _wind(weather, variation),
        _visibility(weather, faa),
        "Thunderstorm." if weather.thunderstorm else "",
        "Light rain." if 0.05 < weather.rain <= 0.5 else "Rain." if weather.rain > 0.5 else "",
        "Snow." if weather.snow else "",
        "Fog." if weather.fog and weather.visibility_sm < 0.65 else "",
        _clouds(weather, faa),
        f"Temperature {say.signed(weather.temperature_c)}, dewpoint {say.signed(weather.dewpoint_c)}.",
        _pressure(weather, faa),
        "Low visibility procedures in force." if low_visibility(weather) else "",
        closing,
    ]
    return " ".join(line for line in lines if line)


CLOUD_CODES = ((0.3, "FEW"), (0.55, "SCT"), (0.9, "BKN"), (9.0, "OVC"))


def decoded(airport: Airport, weather: Weather, letter: str, zulu_seconds: float, variation: float = 0.0) -> list[dict]:
    """The same ATIS as `compose`, written down: rows of label and value for the app's side panel.

    Every row says what the recording says, in the same units; a row with "alert" matters for the approach."""
    faa = uses_faa_style(airport.ident)
    hours, minutes = int(zulu_seconds // 3600) % 24, int(zulu_seconds // 60) % 60
    rows: list[dict] = [{"label": "Information", "value": f"{letter} · {hours:02d}{minutes:02d}Z"}]
    active = runways_in_use(airport, weather.wind_dir_true, weather.wind_kt)
    landing, departing = split_runways(active)
    if active and landing != departing:
        rows.append({"label": "Runways", "value": f"Landing {', '.join(landing)} · departing {', '.join(departing)}"})
    elif active:
        rows.append({"label": "Runway" if len(active) == 1 else "Runways", "value": ", ".join(active)})
    if weather.wind_kt < 1:
        wind = "Calm"
    else:
        direction = round(((weather.wind_dir_true + variation) % 360) / 10) * 10 or 360
        gusting = weather.gust_kt and weather.gust_kt >= weather.wind_kt + 5
        wind = f"{direction:03d}° / {round(weather.wind_kt)} kt" + (f", gusting {round(weather.gust_kt)}" if gusting else "")
    rows.append({"label": "Wind", "value": wind})
    if faa:
        miles = weather.visibility_sm
        visibility = "10 SM" if miles >= 10 else f"{max(1, round(miles))} SM" if miles >= 1 else "Less than 1 SM"
    else:
        metres = weather.visibility_sm * 1609.34
        visibility = (
            "10 km or more"
            if metres >= 9999
            else f"{int(metres // 1000)} km"
            if metres >= 5000
            else f"{max(100, round(metres, -2)):.0f} m"
        )
    low = weather.visibility_sm < 3
    rows.append({"label": "Visibility", "value": visibility, **({"alert": True} if low else {})})
    present = [
        "Thunderstorm" if weather.thunderstorm else "",
        "Light rain" if 0.05 < weather.rain <= 0.5 else "Rain" if weather.rain > 0.5 else "",
        "Snow" if weather.snow else "",
        "Fog" if weather.fog and weather.visibility_sm < 0.65 else "",
    ]
    present = [item for item in present if item]
    if present:
        rows.append({"label": "Weather", "value": ", ".join(present), "alert": True})
    layers = [c for c in weather.clouds if c.base_ft_agl < 25000]
    if layers:
        parts = []
        for cloud in layers:
            code = next(code for limit, code in CLOUD_CODES if cloud.coverage < limit)
            parts.append(f"{code} {max(100, round(cloud.base_ft_agl, -2)):,.0f} ft" + (" CB" if cloud.cumulonimbus else ""))
        ceiling = min((c.base_ft_agl for c in layers if c.coverage >= 0.55), default=99999)
        rows.append({"label": "Clouds", "value": " · ".join(parts), **({"alert": True} if ceiling < 1000 else {})})
    else:
        rows.append({"label": "Clouds", "value": "Sky clear" if faa else "No significant cloud"})
    rows.append({"label": "Temperature", "value": f"{round(weather.temperature_c)}°C · dew point {round(weather.dewpoint_c)}°C"})
    if faa:
        rows.append({"label": "Altimeter", "value": f"{round(weather.qnh_hpa * 2.953) / 100:.2f} inHg"})
    else:
        rows.append({"label": "QNH", "value": f"{int(weather.qnh_hpa)} hPa"})
    if low_visibility(weather):
        rows.append({"label": "Procedures", "value": "Low visibility procedures in force", "alert": True})
    return rows


@dataclass
class Issue:
    letter: str
    weather: Weather  # as observed when this information was issued
    zulu_seconds: float


def _cloud_summary(weather: Weather) -> list[tuple[int, int]]:
    return [
        (0 if c.coverage < 0.3 else 1 if c.coverage < 0.55 else 2 if c.coverage < 0.9 else 3, round(c.base_ft_agl / 1000))
        for c in weather.clouds
        if c.base_ft_agl < 25000
    ]


def significantly_different(airport: Airport, old: Weather, new: Weather) -> bool:
    """True when a real ATIS would be re-recorded. Small fluctuations do not count."""
    if runways_in_use(airport, old.wind_dir_true, old.wind_kt) != runways_in_use(airport, new.wind_dir_true, new.wind_kt):
        return True
    if abs(old.wind_kt - new.wind_kt) >= 5:
        return True
    turn = abs((old.wind_dir_true - new.wind_dir_true + 180) % 360 - 180)
    if max(old.wind_kt, new.wind_kt) >= 5 and turn >= 30:
        return True
    if abs(min(old.visibility_sm, 10) - min(new.visibility_sm, 10)) >= 1:
        return True
    if abs(old.qnh_hpa - new.qnh_hpa) >= 1 or abs(old.temperature_c - new.temperature_c) >= 2:
        return True
    if old.thunderstorm != new.thunderstorm or old.snow != new.snow or low_visibility(old) != low_visibility(new):
        return True
    return _cloud_summary(old) != _cloud_summary(new) or (old.rain > 0.05) != (new.rain > 0.05)


class AtisBook:
    """The current ATIS per airport. Like a real one, it keeps broadcasting the weather
    as it was when issued and only moves to the next letter after a significant change."""

    def __init__(self):
        self._issues: dict[str, Issue] = {}
        self._runways: dict[str, list[str]] = {}  # the runways in use each airport's current letter gives

    def current(self, airport: Airport, weather: Weather, zulu_seconds: float) -> Issue:
        issue = self._issues.get(airport.ident)
        runways = runways_in_use(airport, weather.wind_dir_true, weather.wind_kt)
        if issue is None:
            # The first letter of a session follows the hour, like a real ATIS cycle.
            issue = Issue(chr(ord("A") + int(zulu_seconds // 3600) % 26), weather, zulu_seconds)
        elif significantly_different(airport, issue.weather, weather) or runways != self._runways.get(airport.ident):
            # A change of runway is a new letter, also when it comes from a flight plan loaded since, not the wind.
            issue = Issue(chr((ord(issue.letter) - ord("A") + 1) % 26 + ord("A")), weather, zulu_seconds)
        self._issues[airport.ident] = issue
        self._runways[airport.ident] = runways_in_use(airport, issue.weather.wind_dir_true, issue.weather.wind_kt)
        return issue

    def text(
        self, airport: Airport, weather: Weather, zulu_seconds: float, variation: float = 0.0, name: str | None = None
    ) -> tuple[Issue, str]:
        issue = self.current(airport, weather, zulu_seconds)
        return issue, compose(airport, issue.weather, issue.letter, issue.zulu_seconds, variation, name)
