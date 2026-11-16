"""Airport weather from the METAR reports X-Plane downloads for its real-weather mode.

X-Plane writes them to `Output/real weather/metar-YYYY-MM-DD-HH.MM.txt`, one station per line:

    EGPH 161220Z 25005KT 220V280 CAVOK 04/M02 Q1005
    KSEA 161153Z 00000KT 10SM SCT046 BKN070 07/04 A3004 RMK ...

With these, the ATIS, the runway in use and the pressure at the destination are right from
any distance, not just when the aircraft is there. They are only used while the sim itself
is on real weather; with preset or custom weather the sim's own values at the aircraft are used.
"""

from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path

from .weather import Cloud, Weather

FOLDER = "Output/real weather"
COVER = {"FEW": 0.2, "SCT": 0.45, "BKN": 0.75, "OVC": 1.0, "VV": 1.0}

WIND = re.compile(r"(VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)")
VISIBILITY_SM = re.compile(r"(?:(\d+) )?(M)?(\d+(?:/\d+)?)SM")
CLOUD = re.compile(r"(FEW|SCT|BKN|OVC|VV)(\d{3})(CB|TCU)?")
PRESSURE = re.compile(r"([QA])(\d{4})")
PHENOMENON = re.compile(r"(\+|-|VC)?(MI|BC|PR|DR|BL|SH|TS|FZ)?(DZ|RA|SN|SG|PL|GR|GS|BR|FG|FU|HZ)+|(\+|-|VC)?TS")


def _signed(text: str) -> float:
    return -float(text[1:]) if text.startswith("M") else float(text)


def parse_metar(report: str, field_elev_ft: float = 0.0) -> Weather | None:
    """Surface weather from one METAR. None when it has no usable pressure or wind."""
    body = report.split(" RMK")[0].split(" TEMPO")[0].split(" BECMG")[0]
    weather = Weather(wind_dir_true=0.0, wind_kt=0.0)
    wind = WIND.search(body)
    pressure = PRESSURE.search(body)
    if not wind or not pressure:
        return None
    factor = 1.94384 if wind.group(4) == "MPS" else 1.0
    weather.wind_kt = float(wind.group(2)) * factor
    # METAR winds are true, like the sim's; a variable wind keeps direction 0 and is only light.
    weather.wind_dir_true = 0.0 if wind.group(1) == "VRB" else float(wind.group(1))
    weather.gust_kt = float(wind.group(3)) * factor if wind.group(3) else None
    value = int(pressure.group(2))
    weather.qnh_hpa = float(value) if pressure.group(1) == "Q" else value / 100 * 33.8639

    tokens = body.split()
    if "CAVOK" in tokens:
        weather.visibility_sm = 10.0
    else:
        miles = VISIBILITY_SM.search(body)
        metres = next((t for t in tokens[2:6] if re.fullmatch(r"\d{4}", t)), None)
        if miles:
            weather.visibility_sm = float(int(miles.group(1) or 0) + Fraction(miles.group(3)))
        elif metres:
            weather.visibility_sm = 10.0 if metres == "9999" else int(metres) / 1609.34
    for cover, height, kind in CLOUD.findall(body):
        weather.clouds.append(Cloud(COVER[cover], int(height) * 100.0, kind == "CB"))
    weather.clouds.sort(key=lambda cloud: cloud.base_ft_agl)

    temperature = next((m for m in (re.fullmatch(r"(M?\d{2})/(M?\d{2})?", t) for t in tokens) if m), None)
    if temperature:
        weather.temperature_c = _signed(temperature.group(1))
        weather.dewpoint_c = _signed(temperature.group(2)) if temperature.group(2) else weather.temperature_c

    for token in tokens[2:]:
        if token.endswith("KT") or "/" in token or not PHENOMENON.fullmatch(token):
            continue
        if "TS" in token:
            weather.thunderstorm = True
        if "SN" in token:
            weather.snow = True
        if any(code in token for code in ("RA", "DZ")):
            weather.rain = 0.8 if token.startswith("+") else 0.2 if token.startswith("-") else 0.5
        if any(code in token for code in ("FG", "BR")):
            weather.fog = True
    return weather


class MetarStore:
    """The newest METAR file in X-Plane's folder, reloaded when a newer one appears."""

    def __init__(self, xplane_root: Path | None):
        self.folder = xplane_root / FOLDER if xplane_root else None
        self._file: Path | None = None
        self._reports: dict[str, str] = {}

    def _refresh(self) -> None:
        if not self.folder or not self.folder.exists():
            return
        newest = max(self.folder.glob("metar-*.txt"), key=lambda p: p.stat().st_mtime, default=None)
        if newest is None or newest == self._file:
            return
        reports: dict[str, str] = {}
        for line in newest.read_text(encoding="utf-8", errors="ignore").splitlines():
            ident, _, rest = line.partition(" ")
            if len(ident) == 4 and ident.isalnum() and rest[:6].isdigit():
                reports[ident] = line
        self._file, self._reports = newest, reports

    def report(self, ident: str) -> str | None:
        self._refresh()
        return self._reports.get(ident.upper())

    def weather(self, ident: str, field_elev_ft: float = 0.0) -> Weather | None:
        report = self.report(ident)
        return parse_metar(report, field_elev_ft) if report else None
