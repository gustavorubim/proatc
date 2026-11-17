"""Replay a saved flight log through the engine.

"Save log" writes what the pilot was understood to say, the sim's state at that moment, and what
the controller answered; the journal (flightlog.py) writes the same and, every twenty seconds, a
trace of the sim's state. Replaying feeds the same calls and the same states to a fresh engine, in
time order, and compares the answers. Two uses:

- a bug report: replay the reporter's log before and after a change, and see the answer change;
- a regression check: a log replayed on unchanged code gives no differences (journals.py keeps
  real flights for this).

What makes a replay faithful:

- the engine is started with the seed the log was recorded with, so squawk codes, stands and
  delays come out the same;
- the engine's clock runs on the log's own times (stopped while the sim was paused), so a
  readback reminder or a heading grace period runs out where it did in flight;
- the controller is given a chance to speak unprompted at every record that holds the sim's state
  (the trace above all), as the app's radio thread does, so a controller that now speaks where it
  was silent, or is silent where it spoke, is seen;
- the station lookup's memory, which is shared by the whole process, is cleared first, so one
  replay does not decide who answers in the next;
- a readback the copilot made is made again from what the engine now expects, as the copilot would.

What it cannot reproduce: the moment between trace records (the app looks every half second, the
trace every twenty seconds, so an unprompted call can come up to twenty seconds early or late:
such a call is matched, not reported); other aircraft between calls (the trace leaves them out);
the reported weather the sim had (the journal does not hold the METAR); and anything the language
model said, which is not the controller's rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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

# Records of a pilot's exchange: while it runs the app's radio thread is held back (busy), so the
# controller makes no unprompted call at these moments.
EXCHANGE = ("pilot", "understood", "silent")
# A logged unprompted call and a replayed one this far apart are the same call, said a little earlier or later.
SHIFT_S = 30.0
NOTHING = "(nothing)"
BUILT_READBACK = ("copilot", "clicked")  # sources whose readback the app writes from the pending instruction


def snapshot(raw: dict) -> dict:
    """The part of a sim snapshot that goes into the log."""
    return {key: raw[key] for key in RAW_KEYS if key in raw}


@dataclass
class Difference:
    index: int  # position of the record in the log
    said: str  # what the pilot said, or "(unprompted)"
    logged: str
    now: str
    at: float = 0.0  # seconds from the start of the log
    tuned: str | None = None  # the frequency, "121.700"
    kind: str = "call"  # "call", "unprompted" (logged then), "new" (said now where nothing was logged)


@dataclass
class Result:
    calls: int = 0  # pilot calls replayed
    unprompted: int = 0  # logged unprompted controller calls replayed
    skipped: int = 0  # calls the log does not hold enough of to replay (logs written before 0.8)
    differences: list[Difference] | None = None
    ticks: int = 0  # moments the controller was given to speak unprompted
    new: int = 0  # unprompted calls made now where none was logged
    shifted: int = 0  # unprompted calls made now a little earlier or later than logged: not reported
    answers: list[Difference] = field(default_factory=list)  # every replayed answer, logged or not, in order

    def summary(self) -> str:
        differences = self.differences or []
        lines = [
            f"{self.calls} calls and {self.unprompted} unprompted transmissions replayed, "
            f"{self.skipped} could not be, {len(differences)} differ"
        ]
        if self.ticks:
            lines[0] += f" ({self.ticks} moments the controller could speak, {self.new} new unprompted calls)"
        for diff in differences:
            lines += [f"  #{diff.index} {diff.said}", f"     logged: {diff.logged}", f"     now:    {diff.now}"]
        return "\n".join(lines)


def load(path) -> dict:
    """A log from disk: "Save log"'s .json file, or a journal (.jsonl), in the shape `replay` takes."""
    import json
    from pathlib import Path

    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix != ".jsonl":
        return json.loads(text)
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line cut short when the app was closed
        if isinstance(event, dict):
            events.append(event)
    session = next((event for event in events if event.get("type") == "session"), {})
    return {"seed": session.get("seed"), "events": events}


def _set_plan(engine, plan: dict | None) -> None:
    from .flightplan import FlightPlan

    if not plan:
        engine.set_plan(None)
        return
    known = {key: plan[key] for key in FlightPlan.__dataclass_fields__ if key in plan}
    known["route"] = str(plan.get("route", "")).split()
    engine.set_plan(FlightPlan(**known))


def forget_stations() -> None:
    """Clear what the station lookup remembers between calls. It lives in module globals, shared by the
    process, so without this the frequencies of one replayed flight would decide who answers in the next."""
    from . import centers, navdata

    navdata.prefer_airports([])  # also forgets the station held and the station talked to on each frequency
    centers._HELD.clear()


def in_time_order(events: list[dict]) -> list[tuple[int, dict]]:
    """(position in the log, record), sorted by time. The trace is written from its own thread, so a trace
    can land on disk just after a record it was made before; a record without a time keeps its place."""
    timed, last = [], 0.0
    for index, event in enumerate(events):
        last = event["t"] if isinstance(event.get("t"), int | float) else last
        timed.append((last, index, event))
    timed.sort(key=lambda item: (item[0], item[1]))
    return [(index, event) for _, index, event in timed]


class _Clock:
    """The engine's clock, run on the log's times. It stops while the sim is paused, as the app's does."""

    def __init__(self):
        self.seconds = 0.0
        self._last: float | None = None
        self._paused = False

    def advance(self, event: dict, raw: dict | None) -> None:
        t = event.get("t")
        if isinstance(t, int | float):
            if self._last is not None and not self._paused:
                self.seconds += max(0.0, t - self._last)
            self._last = t
        if raw:
            self._paused = bool(raw.get("paused"))

    def __call__(self) -> float:
        return self.seconds


class _Replay:
    """One run of a log through one engine."""

    def __init__(self, payload: dict, db, make_engine):
        self.db = db
        self.events = payload.get("events", [])
        self.order = in_time_order(self.events)
        self.position = {index: place for place, (index, _) in enumerate(self.order)}
        self.engine = make_engine(payload.get("seed"))
        self.clock = _Clock()
        self.engine.now = self.clock
        self.book = AtisBook()
        self.result = Result(differences=[])
        self.start = next((e["t"] for _, e in self.order if isinstance(e.get("t"), int | float)), 0.0)
        self.pilot: dict = {}
        self.early: set[int] = set()  # logged unprompted calls already made now, a little earlier
        self.missed: dict[str, Difference] = {}  # logged unprompted calls not made now (yet), since the last call
        if payload.get("plan"):
            _set_plan(self.engine, payload["plan"])

    def run(self) -> Result:
        for index, event in self.order:
            kind = event.get("type")
            raw = (event.get("context") or {}).get("raw")
            self.clock.advance(event, raw)
            if kind == "pilot":
                self.pilot, self.missed = event, {}
            elif kind == "plan":  # a journal holds the plan where it was loaded or changed
                _set_plan(self.engine, event.get("plan"))
            elif kind == "understood":
                self.call(index, event, raw)
            elif kind == "atc" and event.get("unprompted"):
                self.logged_unprompted(index, event, raw)
            elif raw and kind not in EXCHANGE and not (kind == "atc" and not event.get("unprompted")):
                self.tick(index, event, raw)
        return self.result

    # ---- helpers ------------------------------------------------------------

    def station_and_issue(self, raw: dict):
        _, station = current_station(self.db, raw)
        if station is None or station.kind == "atis":
            return None, None
        weather = observe(self.db, station.airport, raw) if station.airport else None
        issue = self.book.current(station.airport, weather, raw.get("zulu_seconds", 0.0)) if weather else None
        return station, issue

    def entry(self, index: int, event: dict, said: str, logged: str, now: str, kind: str) -> Difference:
        context = event.get("context") or {}
        tuned = context.get("tuned")
        at = round(event["t"] - self.start, 1) if isinstance(event.get("t"), int | float) else 0.0
        answer = Difference(index, said, logged, now, at, tuned, kind)
        self.result.answers.append(answer)
        return answer

    def differ(self, answer: Difference) -> None:
        self.result.differences.append(answer)

    # ---- the three kinds of moment ------------------------------------------

    def call(self, index: int, event: dict, raw: dict | None) -> None:
        if not raw or "fields" not in event:
            self.result.skipped += 1
            return
        station, issue = self.station_and_issue(raw)
        if station is None:
            self.result.skipped += 1
            return
        reply = self.engine.handle(self.intent(event), station, issue, raw, magnetic_variation(raw))
        self.result.calls += 1
        now = reply.text if reply is not None else "(no rule: free-form reply)"
        logged = _answer_after(self.events, index)
        said = self.pilot.get("text", "")
        answer = self.entry(index, event, said, "(silent)" if logged == "" else logged or "", now or "(silent)", "call")
        if reply is not None and logged is not None and now != logged:
            self.differ(answer)

    def intent(self, event: dict) -> Intent:
        """What the pilot said. A readback made by the copilot or by the app's "Read back" button is made
        again from what the engine expects now: both are built from the instruction, not chosen by the pilot."""
        fields = event["fields"]
        pending = self.engine.flight.pending
        made = self.pilot.get("source") in BUILT_READBACK
        if made and fields.get("intent") == "readback" and pending and pending.expect:
            return Intent.from_dict(self.engine._readback_intent(pending.expect))
        return Intent.from_dict(fields)

    def say_now(self, raw: dict) -> str | None:
        """What the controller says unprompted at this moment; None when nobody could speak here."""
        if raw.get("paused"):
            return None  # the app's radio thread waits while the sim is paused
        station, issue = self.station_and_issue(raw)
        if station is None:
            return None
        self.result.ticks += 1
        said = self.engine.tick(station, issue, raw)
        return said.text if said is not None and said.text else ""

    def logged_unprompted(self, index: int, event: dict, raw: dict | None) -> None:
        if not raw:
            self.result.skipped += 1
            return
        now = self.say_now(raw)
        if now is None:
            self.result.skipped += 1
            return
        self.result.unprompted += 1
        logged = event.get("text", "")
        if index in self.early and not now:
            return  # said a little earlier, at the trace before
        answer = self.entry(index, event, "(unprompted)", logged, now or NOTHING, "unprompted")
        if now != logged:
            self.differ(answer)
            if not now:
                self.missed[logged] = answer

    def tick(self, index: int, event: dict, raw: dict) -> None:
        now = self.say_now(raw)
        if not now:
            return
        answer = self.entry(index, event, "(unprompted)", NOTHING, now, "new")
        late = self.missed.pop(now, None)
        if late is not None and answer.at - late.at <= SHIFT_S:
            self.result.differences.remove(late)
            self.result.shifted += 1
            return
        coming = self.logged_soon(index, now, answer.at)
        if coming is not None:
            self.early.add(coming)
            self.result.shifted += 1
            return
        self.result.new += 1
        self.differ(answer)

    def logged_soon(self, index: int, text: str, at: float) -> int | None:
        """The logged unprompted call with this text that comes next, before any pilot call and soon enough."""
        for later, event in self.order[self.position[index] + 1 :]:
            if event.get("type") in ("pilot", "understood"):
                return None
            t = event.get("t")
            if isinstance(t, int | float) and t - self.start - at > SHIFT_S:
                return None
            if event.get("type") == "atc" and event.get("unprompted") and event.get("text") == text:
                return later
        return None


def replay(payload: dict, db, make_engine) -> Result:
    """Run a saved log again. `make_engine(seed)` builds a fresh engine on `db`."""
    forget_stations()
    return _Replay(payload, db, make_engine).run()


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
