"""Surface weather for an airport, derived from the sim's weather at the aircraft.

Accurate when the aircraft is at or near the airport, which is when ATIS and tower matter.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Cloud:
    coverage: float  # 0..1
    base_ft_agl: float
    cumulonimbus: bool = False


@dataclass
class Weather:
    wind_dir_true: float = 0.0
    wind_kt: float = 0.0
    visibility_sm: float = 10.0
    clouds: list[Cloud] = field(default_factory=list)
    temperature_c: float = 15.0
    dewpoint_c: float = 9.0
    qnh_hpa: float = 1013.25
    rain: float = 0.0
    gust_kt: float | None = None
    thunderstorm: bool = False
    snow: bool = False
    fog: bool = False


SAMPLE = Weather(
    wind_dir_true=240, wind_kt=8, visibility_sm=10, clouds=[Cloud(0.2, 3000)], temperature_c=15, dewpoint_c=9, qnh_hpa=1013
)


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def from_snapshot(state: dict, field_elev_ft: float) -> Weather | None:
    """Build surface weather from raw bridge values; None until the weather datarefs have arrived."""
    if "wx_qnh_pas" not in state:
        return None
    weather = Weather(qnh_hpa=state["wx_qnh_pas"] / 100.0)
    field_elev_m = field_elev_ft * 0.3048

    # Wind: the layer closest to field elevation (layers are listed by altitude).
    altitudes = _as_list(state.get("wx_wind_alt_m"))
    speeds = _as_list(state.get("wx_wind_kt"))
    directions = _as_list(state.get("wx_wind_dir"))
    layers = [(abs(alt - field_elev_m), speed, direction) for alt, speed, direction in zip(altitudes, speeds, directions)]
    if layers:
        _, weather.wind_kt, weather.wind_dir_true = min(layers)

    weather.visibility_sm = state.get("wx_visibility_sm", 10.0)

    for coverage, base_m, kind in zip(
        _as_list(state.get("wx_cloud_cover")),
        _as_list(state.get("wx_cloud_base_m")),
        _as_list(state.get("wx_cloud_type")) or [0, 0, 0],
    ):
        base_agl = base_m / 0.3048 - field_elev_ft
        if coverage >= 0.06 and base_agl > 0:
            weather.clouds.append(Cloud(coverage, base_agl, kind >= 2.5))
    weather.clouds.sort(key=lambda cloud: cloud.base_ft_agl)

    # Temperature: outside air when the aircraft is near field level, otherwise a standard lapse from sea level.
    aircraft_elev_ft = state.get("elevation_m", 0.0) / 0.3048
    if "wx_oat_c" in state and abs(aircraft_elev_ft - field_elev_ft) < 1000:
        weather.temperature_c = state["wx_oat_c"]
    elif "wx_sealevel_temp_c" in state:
        weather.temperature_c = state["wx_sealevel_temp_c"] - 1.98 * field_elev_ft / 1000
    dewpoints = _as_list(state.get("wx_dewpoint_c"))
    if dewpoints:
        weather.dewpoint_c = min(dewpoints[0], weather.temperature_c)
    weather.rain = state.get("wx_rain", 0.0)
    return weather
