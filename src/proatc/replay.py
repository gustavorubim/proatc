"""Replay a saved flight log through the engine.

"Save log" writes what the pilot was understood to say, the sim's state at that moment, and what
the controller answered. Replaying feeds the same calls and the same state to the engine again and
compares the answers. Two uses:

- a bug report: replay the reporter's log before and after a change, and see the answer change;
- a regression check: a log replayed on unchanged code gives no differences.

The engine is started with the seed the log was recorded with, so squawk codes and other choices
come out the same. What cannot be reproduced exactly is timing between calls: unprompted calls are
replayed at the moments they were logged, not continuously.
"""

from __future__ import annotations

from dataclasses import dataclass

from .atis import AtisBook
from .intents import Intent
from .radio import current_station, observe
from .xpbridge import magnetic_variation

# What the log keeps of the sim's state with each call: enough to find the station and decide the answer.
RAW_KEYS = (
    "lat", "lon", "elevation_m", "agl_m", "on_ground", "groundspeed_ms", "ias_kt", "heading_mag", "heading_true", "vs_fpm",
    "squawk", "tail_number", "aircraft_type", "zulu_seconds", "com1_khz", "com2_khz", "com_selection", "paused",
    "wx_qnh_pas", "wx_visibility_sm", "wx_wind_alt_m", "wx_wind_kt", "wx_wind_dir", "wx_cloud_cover", "wx_cloud_base_m",
    "wx_cloud_type", "wx_dewpoint_c", "wx_oat_c", "wx_sealevel_temp_c", "wx_rain", "wx_source",
    "tcas_lat", "tcas_lon", "tcas_ele_m", "tcas_heading", "tcas_vs_fpm", "tcas_on_ground",
)  # fmt: skip


def snapshot(raw: dict) -> dict:
    """The part of a sim snapshot that goes into the log."""
    return {key: raw[key] for key in RAW_KEYS if key in raw}


@dataclass
class Difference:
    index: int  # position of the call in the log
    said: str  # what the pilot said, or "(unprompted)"
    logged: str
    now: str


@dataclass
class Result:
    calls: int = 0  # pilot calls replayed
    unprompted: int = 0  # unprompted controller calls replayed
    skipped: int = 0  # calls the log does not hold enough of to replay (logs written before 0.8)
    differences: list[Difference] | None = None

    def summary(self) -> str:
        differences = self.differences or []
        lines = [
            f"{self.calls} calls and {self.unprompted} unprompted transmissions replayed, "
            f"{self.skipped} could not be, {len(differences)} differ"
        ]
        for diff in differences:
            lines += [f"  #{diff.index} {diff.said}", f"     logged: {diff.logged}", f"     now:    {diff.now}"]
        return "\n".join(lines)


def replay(payload: dict, db, make_engine) -> Result:
    """Run a saved log (the .json file) again. `make_engine(seed)` builds a fresh engine on `db`."""
    events = payload.get("events", [])
    engine = make_engine(payload.get("seed"))
    plan = payload.get("plan")
    if plan:
        from .flightplan import FlightPlan

        known = {key: plan[key] for key in FlightPlan.__dataclass_fields__ if key in plan}
        known["route"] = str(plan.get("route", "")).split()
        engine.set_plan(FlightPlan(**known))
    book = AtisBook()
    result = Result(differences=[])
    pilot_text = ""
    for index, event in enumerate(events):
        kind = event.get("type")
        raw = (event.get("context") or {}).get("raw")
        if kind == "pilot":
            pilot_text = event.get("text", "")
            continue
        if kind == "understood":
            if not raw or "fields" not in event:
                result.skipped += 1
                continue
            _, station = current_station(db, raw)
            if station is None or station.kind == "atis":
                result.skipped += 1
                continue
            weather = observe(db, station.airport, raw) if station.airport else None
            issue = book.current(station.airport, weather, raw.get("zulu_seconds", 0.0)) if weather else None
            reply = engine.handle(Intent.from_dict(event["fields"]), station, issue, raw, magnetic_variation(raw))
            result.calls += 1
            now = reply.text if reply is not None else "(no rule: free-form reply)"
            logged = _answer_after(events, index)
            if reply is not None and logged is not None and now != logged:
                result.differences.append(Difference(index, pilot_text, logged or "(silent)", now or "(silent)"))
        elif kind == "atc" and event.get("unprompted") and raw:
            _, station = current_station(db, raw)
            if station is None or station.kind == "atis":
                result.skipped += 1
                continue
            weather = observe(db, station.airport, raw) if station.airport else None
            issue = book.current(station.airport, weather, raw.get("zulu_seconds", 0.0)) if weather else None
            said = engine.tick(station, issue, raw)
            result.unprompted += 1
            now = said.text if said is not None else "(nothing)"
            if now != event.get("text"):
                result.differences.append(Difference(index, "(unprompted)", event.get("text", ""), now))
    return result


def _answer_after(events: list[dict], index: int) -> str | None:
    """What the controller was logged as answering to the call at `index`: text, "" for silence, None if unknown."""
    for event in events[index + 1 :]:
        kind = event.get("type")
        if kind == "silent":
            return ""
        if kind == "atc" and not event.get("unprompted"):
            return event.get("text", "")
        if kind in ("pilot", "understood"):
            return None
    return None
