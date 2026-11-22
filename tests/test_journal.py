"""The flight written to disk as it happens, so that a fault can be looked into after the flight."""

import json
import sys
import threading

import httpx
import pytest

from proatc import config as config_module
from proatc import flightlog
from proatc.atis import AtisBook
from proatc.config import Config
from proatc.engine import Engine
from proatc.flightlog import FlightLog
from proatc.flightplan import FlightPlan, PlanError, fetch_simbrief
from proatc.intents import Intent
from proatc.procedures import parse_cifp
from proatc.replay import load, replay, snapshot
from proatc.voice_loop import Session
from tests.test_ifr import ISSUE
from tests.test_radio_loop import FakeBridge
from tests.test_realism import APRON, ground_world, taxiing

KNOWN_SECRETS = flightlog.known_secrets  # tests/conftest.py replaces it for every other test
USER, KEY = "Placeholder-Pilot", "sk-or-v1-placeholder-0000"  # never real ones


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_every_record_is_on_disk_as_soon_as_it_is_made(tmp_path):
    log = FlightLog()
    log.record({"type": "info", "text": "before the journal was started"})
    journal = log.keep_journal(tmp_path / "journal")
    assert journal.parent == tmp_path / "journal" and journal.name.startswith("session-") and journal.suffix == ".jsonl"
    log.record({"type": "pilot", "text": "request taxi", "source": "voice"}, {"tuned": "121.700"})
    log.record({"type": "tx", "on": True})  # not kept in memory, not kept on disk
    log.note("trace", {"tuned": "121.700"})
    log.note("session", seed=7, version="1.2.3")
    written = lines(journal)  # read while the file is still open: nothing waits in a buffer
    assert [entry["type"] for entry in written] == ["info", "pilot", "trace", "session"]
    assert written[1]["context"] == {"tuned": "121.700"} and written[3]["seed"] == 7 and written[0]["t"] > 0
    assert [entry["type"] for entry in log.records] == ["info", "pilot"]  # notes are for the journal only


def test_old_journals_are_deleted_and_a_folder_that_cannot_be_used_stops_nothing(tmp_path, monkeypatch):
    folder = tmp_path / "journal"
    folder.mkdir()
    for number in range(60):
        (folder / f"session-200001{number // 30 + 1:02d}-0000{number % 30:02d}.jsonl").write_text("{}\n", encoding="utf-8")
    (folder / "notes.txt").write_text("not a journal", encoding="utf-8")
    log = FlightLog()
    log.keep_journal(folder)
    kept = sorted(path.name for path in folder.glob("session-*.jsonl"))
    assert len(kept) == flightlog.JOURNALS_KEPT and kept[-1] == log.journal.name
    assert kept[0] == "session-20000101-000021.jsonl" and (folder / "notes.txt").exists()  # the oldest went, only journals

    blocked = tmp_path / "a file"
    blocked.write_text("in the way", encoding="utf-8")
    silent = FlightLog()
    assert silent.keep_journal(blocked) is None and silent.journal is None
    silent.record({"type": "info", "text": "still recorded"})
    silent.note("trace")
    assert len(silent.records) == 1
    # A disk that fails later is given up on, quietly.
    log._file.close()
    log.record({"type": "info", "text": "after the disk failed"})
    assert log._file is None and log.records[-1]["text"] == "after the disk failed"


def session_with_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr("proatc.voice_loop.PROJECT_DIR", tmp_path)
    db, engine, ground = ground_world(tmp_path)
    session = Session(Config(), emit=lambda event: None)
    state = {**taxiing(*APRON), "com1_khz": 121700, "zulu_seconds": 43200.0, "tcas_lat": [1.0, 2.0], "tcas_lon": [1.0, 2.0]}
    session.bridge, session.db, session.book = FakeBridge(state), db, AtisBook()
    session.engine, session.ready = engine, True
    session.tts = type("Voice", (), {"engine_name": "kokoro"})()
    return session, engine, ground, state


def test_the_journal_starts_with_the_session_and_traces_the_flight(tmp_path, monkeypatch):
    monkeypatch.setattr("proatc.voice_loop.TRACE_EVERY_S", 3600)
    session, engine, ground, state = session_with_journal(tmp_path, monkeypatch)
    session._start_journal()
    journal = session.log.journal
    assert journal.parent == tmp_path / "logs" / "journal"
    first = lines(journal)[0]
    assert first["type"] == "session" and first["seed"] == engine.seed and first["voice_engine"] == "kokoro"
    assert "key" not in json.dumps(first).lower()  # nothing secret

    session.trace()
    plan, trace = lines(journal)[1:]
    assert plan == {"t": plan["t"], "type": "plan", "plan": None}
    assert trace["type"] == "trace" and trace["context"]["tuned"] == "121.700"
    assert trace["context"]["station"] == "Testfield Ground" and trace["context"]["raw"]["lat"] == APRON[0]
    assert not any(key.startswith("tcas_") for key in trace["context"]["raw"])  # the other aircraft are left out of it
    session.trace()
    assert len(lines(journal)) == 3  # parked, nothing changed: nothing written

    # Tuned to a frequency nobody answers on, as on the flight that prompted this: the trace shows it.
    session.bridge = FakeBridge({**state, "com1_khz": 118825, "lat": APRON[0] + 0.01})
    engine.set_plan(FlightPlan("EGXX", "LFXX", ["ALFAX"], 36000, callsign="BAW123"))
    session.trace()
    plan, silent = lines(journal)[3:]
    assert plan["plan"]["destination"] == "LFXX" and plan["plan"]["route"] == "ALFAX"
    assert silent["context"]["tuned"] == "118.825" and silent["context"]["station"] is None

    # Events of the flight land in the same file, in order.
    session.emit({"type": "atc", "station": "Testfield Ground", "text": "taxi to holding point", "unprompted": False})
    assert lines(journal)[-1]["type"] == "atc" and lines(journal)[-1]["context"]["tuned"] == "118.825"
    session.bridge = FakeBridge(state, connected=False)
    session.trace()
    assert lines(journal)[-1]["type"] == "atc"  # no sim: nothing to trace


def test_a_journal_can_be_replayed(tmp_path):
    db, _, ground = ground_world(tmp_path)

    def make(seed):
        return Engine(db, seed=seed, procedures=lambda ident: parse_cifp([]))

    engine, log = make(5), FlightLog()
    journal = log.keep_journal(tmp_path / "journal")
    log.note("session", seed=engine.seed)
    log.note("plan", plan=None)
    weather = {"wx_qnh_pas": 101320.0, "wx_wind_alt_m": [30.0], "wx_wind_kt": [10.0], "wx_wind_dir": [270.0],
               "wx_visibility_sm": 10.0, "zulu_seconds": 43200.0}  # fmt: skip

    def call(raw, name, **fields):
        raw = {**raw, **weather, "com1_khz": 121700}
        context = {"raw": snapshot(raw)}
        log.record({"type": "pilot", "text": name, "source": "clicked"}, context)
        log.record({"type": "understood", "intent": name, "fields": {"intent": name, **fields}}, context)
        from proatc.radio import observe

        issue = AtisBook().current(ground.airport, observe(db, ground.airport, raw), raw["zulu_seconds"])
        reply = engine.handle(Intent(name, **fields), ground, issue, raw)
        log.record({"type": "atc", "station": ground.callsign, "text": reply.text, "unprompted": False}, context)
        log.note("trace", context)

    call(taxiing(*APRON), "request_taxi")
    call(taxiing(50.999), "request_crossing")
    with open(journal, "a", encoding="utf-8") as cut:
        cut.write('{"t": 1, "type": "atc", "text": "cut sho')  # the app was closed while writing

    payload = load(journal)
    assert payload["seed"] == 5 and [event["type"] for event in payload["events"]].count("trace") == 2
    result = replay(payload, db, make)
    assert (result.calls, result.skipped, result.differences) == (2, 0, []), result.summary()
    # "Save log"'s file is still read as before.
    saved = log.save(tmp_path / "saved", {"seed": 5})
    assert replay(load(saved.with_suffix(".json")), db, make).calls == 2
    assert ISSUE is not None


def test_records_and_notes_from_several_threads_at_once_are_all_on_disk_each_on_a_whole_line(tmp_path):
    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # the threads change places as often as they can
    log = FlightLog()
    threads, per_thread, long_text = 6, 200, "x" * 2000
    start, failures = threading.Barrier(threads + 1), []

    def work(number):
        start.wait()
        try:
            for index in range(per_thread):
                if index % 3:
                    log.record({"type": "info", "text": f"{number}-{index} {long_text}"})
                else:
                    log.note("trace", {"tuned": "121.700"}, mark=f"{number}-{index}", padding=long_text)
                if number == 0:
                    log.text()  # "Save log" reads the records while they are being added to
        except Exception as error:
            failures.append(error)

    workers = [threading.Thread(target=work, args=(number,)) for number in range(threads)]
    for worker in workers:
        worker.start()
    try:
        start.wait()
        for _ in range(20):
            log.record({"type": "info", "text": "main"})
        journal = log.keep_journal(tmp_path / "journal")  # started while records are being made
        for worker in workers:
            worker.join()
    finally:
        sys.setswitchinterval(switch)
    assert not failures

    written = lines(journal)  # every line parses
    marks = [entry["text"].split()[0] if entry["type"] == "info" else entry["mark"] for entry in written]
    kept = [entry["text"].split()[0] for entry in log.records if entry["text"] != "main"]
    marks = [mark for mark in marks if mark != "main"]
    infos = {f"{n}-{i}" for n in range(threads) for i in range(per_thread) if i % 3}
    assert len(kept) == len(infos) and set(kept) == infos  # none lost in memory
    assert {mark for mark in marks if mark in infos} == infos and len(marks) == len(set(marks))  # none twice
    assert [mark for mark in marks if mark in infos] == kept  # in the order they were kept


# ---- nothing secret in a log that is attached to a public bug report ---------------------


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError, httpx.ReadTimeout, httpx.ProxyError],
)
def test_a_simbrief_fetch_that_fails_says_what_went_wrong_without_the_username_or_the_address(monkeypatch, failure):
    def get(params):
        request = httpx.Request("GET", "https://www.simbrief.com/api/xml.fetcher.php", params=params)
        raise failure(f"failed to reach {request.url}", request=request)

    monkeypatch.setattr("proatc.flightplan._download", get)
    with pytest.raises(PlanError) as failed:
        fetch_simbrief(USER)
    said = str(failed.value)
    assert "SimBrief" in said and USER.lower() not in said.lower() and "http" not in said and "simbrief.com" not in said

    monkeypatch.setattr("proatc.flightplan._download", lambda params: (400, USER.encode()))
    with pytest.raises(PlanError, match="SimBrief returned 400") as refused:
        fetch_simbrief(USER)
    assert USER.lower() not in str(refused.value).lower()


def test_the_simbrief_username_and_the_openrouter_key_are_in_no_journal_saved_log_or_record(tmp_path, monkeypatch):
    monkeypatch.setattr(flightlog, "known_secrets", KNOWN_SECRETS)
    monkeypatch.setattr(config_module, "load_settings", lambda: {"simbrief_user": USER, "config": {}})
    monkeypatch.setattr(config_module, "openrouter_key", lambda: KEY)
    log = FlightLog()
    log.record({"type": "error", "text": f"https://www.simbrief.com/api/xml.fetcher.php?username={USER.lower()} failed"})
    journal = log.keep_journal(tmp_path / "journal")
    log.record({"type": "info", "text": f"key {KEY} in use"}, {"tuned": "121.700", "said": [f"by {USER}"], USER: 1})
    log.note("session", model="anthropic/x", by=USER.upper(), context={"key": KEY})
    log.note("trace", {"raw": {"lat": 51.0, "label": f"{USER}'s aircraft"}})
    saved = log.save(tmp_path / "saved", {"debrief_text": f"notes for {USER}", "debrief": {"notes": KEY}})

    for path in (journal, saved, saved.with_suffix(".json")):
        written = path.read_text(encoding="utf-8")
        assert USER.lower() not in written.lower() and KEY not in written and "[hidden]" in written, path.name
    kept = json.dumps(log.entries())
    assert USER.lower() not in kept.lower() and KEY not in kept and len(log.records) == 2
    assert lines(journal)[1]["context"]["tuned"] == "121.700" and lines(journal)[3]["context"]["raw"]["lat"] == 51.0


def test_an_error_the_session_emits_reaches_the_log_without_the_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(flightlog, "known_secrets", KNOWN_SECRETS)
    monkeypatch.setattr(config_module, "load_settings", lambda: {"simbrief_user": USER})
    monkeypatch.setattr(config_module, "openrouter_key", lambda: KEY)
    shown = []
    session = Session(Config(), emit=shown.append)
    session.emit({"type": "error", "text": f"rejected {KEY} for {USER}"})
    assert USER not in json.dumps(list(session.log.records)) and KEY not in json.dumps(list(session.log.records))
    assert session.log.records[-1]["text"] == "rejected [hidden] for [hidden]"
