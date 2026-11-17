"""Real flights kept as regression tests: keep, check, accept, list, and a replay faithful enough to trust.

The journals here are synthetic: flown through the real engine on a small invented airport, written
the way the app writes them (flightlog.py), with the placeholder registration G-ABCD.
"""

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace

from proatc import centers, cli, flightlog, journals, navdata
from proatc import engine as engine_module
from proatc.atis import AtisBook
from proatc.config import Config
from proatc.engine import Engine
from proatc.flightlog import FlightLog
from proatc.flightplan import FlightPlan
from proatc.intents import Intent
from proatc.procedures import parse_cifp
from proatc.radio import current_station, observe
from proatc.replay import load, replay, snapshot
from tests.test_realism import APRON, ground_world, taxiing

WEATHER = {"wx_qnh_pas": 101320.0, "wx_wind_alt_m": [30.0], "wx_wind_kt": [10.0], "wx_wind_dir": [270.0],
           "wx_visibility_sm": 10.0, "zulu_seconds": 43200.0, "com1_khz": 121700}  # fmt: skip
SETTINGS = {"holding": "never", "traffic": True}


class Recorder:
    """Flies calls through an engine and writes the journal as the app does: the call, what it was understood
    as, the answer; every second the controller may speak unprompted; every twenty seconds a trace."""

    def __init__(self, tmp_path, monkeypatch, ticks=True, seed=5):
        self.db, _, _ = ground_world(tmp_path)
        self.clock = 1_800_000_000.0
        monkeypatch.setattr(flightlog, "time", SimpleNamespace(time=lambda: self.clock))
        self.engine = Engine(self.db, seed=seed, procedures=lambda ident: parse_cifp([]), clock=lambda: self.clock)
        self.ticks = ticks  # False: a controller that never spoke unprompted, as an older version might
        self.log = FlightLog()
        self.log.started = datetime(2026, 11, 17, 10, 0, 0)
        self.journal = self.log.keep_journal(tmp_path / "journal")
        self.log.note("session", seed=seed, version="test")
        self.raw = {**taxiing(*APRON), **WEATHER}

    def plan(self, plan: FlightPlan | None) -> None:
        self.engine.set_plan(plan)
        self.log.note("plan", plan=plan.summary() if plan else None)

    def station(self):
        _, station = current_station(self.db, self.raw)
        issue = AtisBook().current(station.airport, observe(self.db, station.airport, self.raw), 43200.0)
        return station, issue

    def context(self) -> dict:
        return {"tuned": f"{self.raw['com1_khz'] / 1000:.3f}", "raw": snapshot(self.raw)}

    def call(self, name: str, text: str = "", source: str = "clicked", **fields):
        station, issue = self.station()
        intent = Intent(name, **fields)
        self.log.record({"type": "pilot", "text": text or name, "source": source}, self.context())
        sent = {key: value for key, value in asdict(intent).items() if value not in (None, False)}
        self.log.record({"type": "understood", "intent": name, "fields": sent}, self.context())
        reply = self.engine.handle(intent, station, issue, self.raw)
        answer = {"type": "atc", "station": station.callsign, "text": reply.text, "unprompted": False}
        self.log.record(answer if reply.text else {"type": "silent"}, self.context())
        return reply

    def wait(self, seconds: int, **changes) -> None:
        self.raw = {**self.raw, **changes}
        for second in range(1, seconds + 1):
            self.clock += 1
            if self.ticks:
                station, issue = self.station()
                said = self.engine.tick(station, issue, self.raw)
                if said is not None and said.text:
                    event = {"type": "atc", "station": station.callsign, "text": said.text, "unprompted": True}
                    self.log.record(event, self.context())
            if second % 20 == 0:
                self.log.note("trace", self.context())


def taxi_flight(recorder: Recorder, read_back=True) -> Recorder:
    recorder.plan(FlightPlan("EGXX", "LFXX", [], 6000))
    recorder.call("request_taxi", "Testfield Ground, G-ABCD, request taxi")
    if read_back:
        recorder.call("readback", "holding point runway zero niner right, G-ABCD", runway="09R")
    recorder.wait(60)
    recorder.raw = {**recorder.raw, **taxiing(50.999)}
    recorder.call("request_crossing")
    recorder.wait(20)
    return recorder


def runner_on(db, changed=None):
    """Replays on the test airport. `changed(engine)` alters the engine, as a change to the code would."""

    def make(seed):
        engine = Engine(db, seed=seed, procedures=lambda ident: parse_cifp([]))
        if changed:
            changed(engine)
        return engine

    return lambda payload, settings: replay(payload, db, make)


def say_delta(engine):
    """A change to the code: the taxi route is now given via Delta."""
    handle = engine.handle

    def changed(*args, **kwargs):
        reply = handle(*args, **kwargs)
        if reply is not None:
            reply.text = reply.text.replace("via Charlie", "via Delta")
        return reply

    engine.handle = changed


def test_keeping_a_journal_names_it_by_date_and_route_and_accepts_what_it_replays_to(tmp_path, monkeypatch):
    flight = taxi_flight(Recorder(tmp_path, monkeypatch))
    corpus = tmp_path / "regression"
    kept = journals.keep(flight.journal, corpus, runner_on(flight.db), SETTINGS)
    assert kept.name == "2026-11-17-1000-EGXX-LFXX.jsonl" and kept.read_bytes() == flight.journal.read_bytes()
    record = json.loads(journals.accepted_path(kept).read_text(encoding="utf-8"))
    assert record["settings"] == SETTINGS and record["counts"]["calls"] == 3 and record["counts"]["differ_from_log"] == 0
    taxi = record["answers"][0]
    assert taxi["said"] == "Testfield Ground, G-ABCD, request taxi" and "via Charlie" in taxi["now"] and taxi["kind"] == "call"
    assert set(taxi) == {"index", "at", "tuned", "kind", "said", "now"} and taxi["tuned"] == "121.700"
    # Kept twice, it is still one flight; a different journal with the same name is kept beside it.
    assert journals.keep(flight.journal, corpus, runner_on(flight.db), SETTINGS) == kept
    assert [line.split(":")[0] for line in journals.listing(corpus)] == ["2026-11-17-1000-EGXX-LFXX"]
    assert "3 calls" in journals.listing(corpus)[0]
    other = tmp_path / "session-20261117-100000.jsonl"
    other.write_text(flight.journal.read_text(encoding="utf-8") + '{"type": "info", "text": "more"}\n', encoding="utf-8")
    assert journals.keep(other, corpus, runner_on(flight.db), SETTINGS).name == "2026-11-17-1000-EGXX-LFXX-2.jsonl"
    found = journals.check(corpus, runner_on(flight.db), SETTINGS)
    assert [one.changed for one in found] == [False, False] and "no change since accepted" in journals.report(found[0])


def test_a_changed_answer_is_reported_until_a_person_accepts_it(tmp_path, monkeypatch):
    flight = taxi_flight(Recorder(tmp_path, monkeypatch))
    corpus = tmp_path / "regression"
    kept = journals.keep(flight.journal, corpus, runner_on(flight.db), SETTINGS)

    (found,) = journals.check(corpus, runner_on(flight.db, say_delta), SETTINGS)
    assert found.changed and len(found.changes) == 1
    change = found.changes[0]
    assert change.said.endswith("request taxi") and "via Charlie" in change.accepted and "via Delta" in change.now
    text = journals.report(found)
    assert "1 changed since accepted" in text and "+0:00:00 121.700" in text and "near 50.996,-1.000" in text

    journals.accept_one(kept, runner_on(flight.db, say_delta), SETTINGS)
    (again,) = journals.check(corpus, runner_on(flight.db, say_delta), SETTINGS)
    assert not again.changed and not again.changes
    record = json.loads(journals.accepted_path(kept).read_text(encoding="utf-8"))
    assert "via Charlie" in record["answers"][0]["logged"]  # what the real flight heard stays in view


def test_a_controller_that_now_speaks_unprompted_is_reported(tmp_path, monkeypatch):
    # Recorded by a controller that never repeated an instruction nobody read back; the engine now does,
    # thirty seconds on, at the trace after it.
    flight = taxi_flight(Recorder(tmp_path, monkeypatch, ticks=False), read_back=False)
    result = replay(
        load(flight.journal), flight.db, lambda seed: Engine(flight.db, seed=seed, procedures=lambda i: parse_cifp([]))
    )
    assert result.new == 1 and result.ticks >= 4, result.summary()
    (new,) = [diff for diff in result.differences if diff.kind == "new"]
    assert "I say again" in new.now and new.logged == "(nothing)" and new.at == 40.0 and new.tuned == "121.700"

    # The same flight with the sim paused all along: the controller's clock stood still, and so did it.
    payload = load(flight.journal)
    for event in payload["events"]:
        if event.get("type") == "trace":
            event["context"]["raw"]["paused"] = 1
    assert replay(payload, flight.db, lambda seed: Engine(flight.db, seed=seed, procedures=lambda i: parse_cifp([]))).new == 0

    # Kept while the engine was silent, checked once it speaks: a change from nothing to the call.
    corpus = tmp_path / "regression"
    monkeypatch.setattr(engine_module, "READBACK_WAIT_S", 3600)
    journals.keep(flight.journal, corpus, runner_on(flight.db), SETTINGS)
    monkeypatch.setattr(engine_module, "READBACK_WAIT_S", 30)
    (found,) = journals.check(corpus, runner_on(flight.db), SETTINGS)
    assert [(c.accepted, "I say again" in c.now) for c in found.changes] == [("(nothing)", True)]


def test_an_unprompted_call_said_a_little_earlier_than_logged_is_matched_not_reported(tmp_path, monkeypatch):
    flight = taxi_flight(Recorder(tmp_path, monkeypatch), read_back=False)
    payload = load(flight.journal)
    events = payload["events"]
    reminder = next(event for event in events if event.get("unprompted"))
    trace = next(event for event in events if event.get("type") == "trace" and event["t"] > reminder["t"])
    # The app said it at the second it was due; here it comes five seconds after the trace that follows.
    events.remove(reminder)
    reminder["t"] = trace["t"] + 5
    events.insert(events.index(trace) + 1, reminder)
    result = replay(payload, flight.db, lambda seed: Engine(flight.db, seed=seed, procedures=lambda i: parse_cifp([])))
    assert (result.shifted, result.new, result.differences) == (1, 0, []), result.summary()


def test_a_readback_the_app_made_is_made_again_from_what_the_engine_expects_now(tmp_path, monkeypatch):
    flight = taxi_flight(Recorder(tmp_path, monkeypatch))

    def with_readback(source):
        payload = load(flight.journal)
        events = payload["events"]
        readback = next(event for event in events if event.get("type") == "understood" and event["intent"] == "readback")
        readback["fields"]["runway"] = "05"  # what the instruction said when the flight was flown
        events[events.index(readback) - 1]["source"] = source
        return replay(payload, flight.db, lambda seed: Engine(flight.db, seed=seed, procedures=lambda i: parse_cifp([])))

    assert with_readback("copilot").differences == [] and with_readback("clicked").differences == []
    spoken = with_readback("voice").differences  # what the pilot said is what the pilot said
    assert spoken[0].kind == "call" and spoken[0].logged == "(silent)" and "negative" in spoken[0].now.lower()


def test_a_journal_cut_short_on_its_last_line_is_kept_and_checked(tmp_path, monkeypatch):
    flight = taxi_flight(Recorder(tmp_path, monkeypatch))
    with open(flight.journal, "a", encoding="utf-8") as cut:
        cut.write('{"t": 1, "type": "atc", "text": "cut sho')  # the app was closed while writing
    corpus = tmp_path / "regression"
    journals.keep(flight.journal, corpus, runner_on(flight.db), SETTINGS)
    (found,) = journals.check(corpus, runner_on(flight.db), SETTINGS)
    assert not found.changed and found.result.calls == 3


def test_an_empty_corpus_has_nothing_to_check_and_says_how_to_start(tmp_path, monkeypatch, capsys):
    assert journals.check(tmp_path / "none", runner_on(None), SETTINGS) == []
    assert journals.listing(tmp_path / "none") == []
    monkeypatch.setattr(cli, "_journal_runner", lambda config: runner_on(None))
    args = argparse.Namespace(action="check", folder=str(tmp_path / "none"), anonymous=False)
    assert cli.cmd_journals(Config(), args) == 0
    assert "No kept flights" in capsys.readouterr().out
    args = argparse.Namespace(action="list", folder=str(tmp_path / "none"))
    assert cli.cmd_journals(Config(), args) == 0


def test_the_commands_keep_check_accept_and_list(tmp_path, monkeypatch, capsys):
    flight = taxi_flight(Recorder(tmp_path, monkeypatch))
    corpus = str(tmp_path / "regression")
    monkeypatch.setattr(journals, "journal_folder", lambda: flight.journal.parent)  # the newest journal is kept
    monkeypatch.setattr(cli, "_journal_runner", lambda config: runner_on(flight.db))
    assert cli.cmd_journals(Config(), argparse.Namespace(action="keep", folder=corpus, journal=None)) == 0
    assert "as 2026-11-17-1000-EGXX-LFXX" in capsys.readouterr().out
    check = argparse.Namespace(action="check", folder=corpus, anonymous=False)
    assert cli.cmd_journals(Config(), check) == 0
    monkeypatch.setattr(cli, "_journal_runner", lambda config: runner_on(flight.db, say_delta))
    assert cli.cmd_journals(Config(), check) == 1
    assert "1 flights checked, 1 changed: 2026-11-17-1000-EGXX-LFXX" in capsys.readouterr().out
    accept = argparse.Namespace(action="accept", folder=corpus, name=None, all=False)
    assert cli.cmd_journals(Config(), accept) == 1  # a name or --all
    assert cli.cmd_journals(Config(), argparse.Namespace(action="accept", folder=corpus, name="nothing", all=False)) == 1
    assert cli.cmd_journals(Config(), argparse.Namespace(action="accept", folder=corpus, name="2026-11-17", all=False)) == 0
    assert cli.cmd_journals(Config(), check) == 0
    assert cli.cmd_journals(Config(), argparse.Namespace(action="accept", folder=corpus, name=None, all=True)) == 0
    capsys.readouterr()
    assert cli.cmd_journals(Config(), argparse.Namespace(action="list", folder=corpus)) == 0
    assert capsys.readouterr().out.startswith("2026-11-17-1000-EGXX-LFXX: 3 calls")


def test_the_anonymous_report_masks_the_callsign_and_rounds_positions(tmp_path, monkeypatch):
    flight = taxi_flight(Recorder(tmp_path, monkeypatch))
    corpus = tmp_path / "regression"
    journals.keep(flight.journal, corpus, runner_on(flight.db), SETTINGS)
    (found,) = journals.check(corpus, runner_on(flight.db, say_delta), SETTINGS)
    plain, anonymous = journals.report(found), journals.report(found, anonymous=True)
    assert "G-ABCD" in plain and "Golf Alfa Bravo Charlie Delta" in plain and "50.996,-1.000" in plain
    for private in ("G-ABCD", "GABCD", "Golf Alfa Bravo Charlie Delta", "50.996"):
        assert private not in anonymous
    assert "CALLSIGN" in anonymous and "near 51.0,-1.0" in anonymous and "via Delta" in anonymous


def test_one_replay_leaves_nothing_behind_for_the_next(tmp_path, monkeypatch):
    flight = taxi_flight(Recorder(tmp_path, monkeypatch))
    payload = load(flight.journal)
    alone = replay(payload, flight.db, lambda seed: Engine(flight.db, seed=seed, procedures=lambda i: parse_cifp([])))

    # Another flight's station lookup memory, as a replay before this one (or the app) would leave it.
    navdata.prefer_airports(["ZZZZ"])
    navdata.TALKING_TO[121700] = "ZZZZ ground"
    navdata._HELD[121700] = ("ZZZZ", "ground")
    centers._HELD[121700] = "Somewhere Control"
    seen = []

    def make(seed):
        seen.append((dict(navdata.TALKING_TO), dict(navdata._HELD), dict(centers._HELD), navdata.is_preferred("ZZZZ")))
        return Engine(flight.db, seed=seed, procedures=lambda i: parse_cifp([]))

    after = replay(payload, flight.db, make)
    assert seen == [({}, {}, {}, False)]
    assert [(a.index, a.now) for a in after.answers] == [(a.index, a.now) for a in alone.answers]
