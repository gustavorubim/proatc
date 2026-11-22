"""The filed flight plan: imported from SimBrief or entered by hand."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import httpx

SIMBRIEF_URL = "https://www.simbrief.com/api/xml.fetcher.php"


class PlanError(RuntimeError):
    pass


@dataclass
class FlightPlan:
    origin: str | None = None
    destination: str | None = None
    route: list[str] = field(default_factory=list)
    cruise_ft: int | None = None
    sid: str | None = None
    star: str | None = None
    departure_runway: str | None = None
    arrival_runway: str | None = None
    callsign: str | None = None  # as filed, e.g. BAW123
    alternate: str | None = None
    source: str = "manual"

    def summary(self) -> dict:
        data = asdict(self)
        data["route"] = " ".join(self.route)
        return {key: value for key, value in data.items() if value not in (None, "", [])}


def _text(value) -> str | None:
    """SimBrief gives missing values as {} or empty strings."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def from_simbrief(data: dict) -> FlightPlan:
    """Read the parts of a SimBrief briefing (its JSON form) that ATC needs."""
    general = data.get("general") or {}
    origin, destination = data.get("origin") or {}, data.get("destination") or {}
    if not _text(origin.get("icao_code")) or not _text(destination.get("icao_code")):
        raise PlanError("The SimBrief briefing has no origin or destination.")
    cruise = _text(str(general.get("initial_altitude", "")))
    alternate = data.get("alternate")
    alternate = alternate[0] if isinstance(alternate, list) and alternate else alternate
    alternate = _text(alternate.get("icao_code")) if isinstance(alternate, dict) else None
    return FlightPlan(
        origin=origin["icao_code"].upper(),
        destination=destination["icao_code"].upper(),
        route=(_text(general.get("route")) or "").upper().split(),
        cruise_ft=int(cruise) if cruise and cruise.isdigit() else None,
        sid=_text(general.get("sid_ident")),
        star=_text(general.get("star_ident")),
        departure_runway=_text(origin.get("plan_rwy")),
        arrival_runway=_text(destination.get("plan_rwy")),
        callsign=_text((data.get("atc") or {}).get("callsign")),
        alternate=alternate.upper() if alternate else None,
        source="simbrief",
    )


SIMBRIEF_MAX_BYTES = 16_000_000  # a briefing with its whole navigation log is a few megabytes


def _download(params: dict) -> tuple[int, bytes]:
    """SimBrief's status and the briefing, already decompressed, read no further than SIMBRIEF_MAX_BYTES. The
    bytes are returned as they are: wrapped in a new response with SimBrief's headers, a briefing sent compressed
    (as SimBrief sends it) would be decompressed a second time and fail."""
    with httpx.stream("GET", SIMBRIEF_URL, params=params, timeout=20, follow_redirects=True) as response:
        body = bytearray()
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > SIMBRIEF_MAX_BYTES:
                raise PlanError("SimBrief sent more than a briefing holds.")
        return response.status_code, bytes(body)


def fetch_simbrief(user: str) -> FlightPlan:
    """Download the user's latest briefing. `user` is their SimBrief username or numeric pilot ID."""
    user = user.strip()
    if not user:
        raise PlanError("Enter your SimBrief username or pilot ID.")
    params = {"json": "1", "userid" if user.isdigit() else "username": user}
    try:
        status, body = _download(params)
    # httpx's own messages can quote the request, username and all: what went wrong is said in plain words,
    # since the message is shown, written to the journal and saved with the log.
    except httpx.TimeoutException as error:
        raise PlanError("SimBrief did not answer in time. Try again in a moment.") from error
    except httpx.HTTPError as error:
        raise PlanError("SimBrief could not be reached. Check the internet connection.") from error
    if status != 200:
        raise PlanError(f"SimBrief returned {status}. Check the username, and that a flight is planned.")
    try:
        return from_simbrief(json.loads(body))
    except ValueError as error:
        raise PlanError("SimBrief sent something that is not a briefing.") from error


def manual(destination: str, cruise_fl: str | int | None = None, route: str = "", origin: str | None = None) -> FlightPlan:
    destination = destination.strip().upper()
    if not (3 <= len(destination) <= 4 and destination.isalnum()):
        raise PlanError("The destination must be an airport code such as LFPG.")
    cruise = None
    if cruise_fl not in (None, ""):
        digits = "".join(c for c in str(cruise_fl) if c.isdigit())
        if not digits:
            raise PlanError("The cruise level must be a number such as 360.")
        # 360 means FL360; 36000 means feet.
        cruise = int(digits) * 100 if int(digits) < 1000 else int(digits)
    return FlightPlan(origin=origin, destination=destination, route=route.upper().split(), cruise_ft=cruise)
