"""ATIS: runway selection and the broadcast text, built from templates (no language model)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import phraseology as say
from .navdata import Airport
from .weather import Weather

CALM_KT = 3


def uses_faa_style(ident: str) -> bool:
    """US and Canada report inches of mercury and statute miles."""
    return len(ident) == 4 and ident[0] in "KC" or ident[:2] in ("PA", "PH", "PG", "TJ")


def runways_in_use(airport: Airport, wind_dir_true: float, wind_kt: float) -> list[str]:
    """Runway ends with the best headwind, including parallels. Short runways are ignored."""
    if not airport.runways:
        return []
    longest = max(r.length_m for r in airport.runways)
    ends = [(ident, heading, r.length_m) for r in airport.runways if r.length_m >= 0.6 * longest for ident, heading in r.ends()]
    if wind_kt < CALM_KT:
        # Calm: the longest runway, in the direction that appears first in the sim's data.
        best = max(ends, key=lambda end: end[2])
    else:
        best = max(ends, key=lambda end: (round(math.cos(math.radians(wind_dir_true - end[1])), 3), end[2]))
    parallel = [ident for ident, heading, _ in ends if abs((heading - best[1] + 180) % 360 - 180) < 5]
    return sorted(parallel, key=lambda ident: (ident[:2], "LCR".find(ident[-1])))


def split_runways(active: list[str]) -> tuple[list[str], list[str]]:
    """(landing, departing). With parallel runways in use, arrivals and departures are kept apart:
    the first half lands, the second half departs, the middle one of three does both."""
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

    def current(self, airport: Airport, weather: Weather, zulu_seconds: float) -> Issue:
        issue = self._issues.get(airport.ident)
        if issue is None:
            # The first letter of a session follows the hour, like a real ATIS cycle.
            issue = Issue(chr(ord("A") + int(zulu_seconds // 3600) % 26), weather, zulu_seconds)
        elif significantly_different(airport, issue.weather, weather):
            issue = Issue(chr((ord(issue.letter) - ord("A") + 1) % 26 + ord("A")), weather, zulu_seconds)
        self._issues[airport.ident] = issue
        return issue

    def text(
        self, airport: Airport, weather: Weather, zulu_seconds: float, variation: float = 0.0, name: str | None = None
    ) -> tuple[Issue, str]:
        issue = self.current(airport, weather, zulu_seconds)
        return issue, compose(airport, issue.weather, issue.letter, issue.zulu_seconds, variation, name)
