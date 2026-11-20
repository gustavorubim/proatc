"""A fifth security review: the data caches, clicked calls from other devices, the filter on the model's own
replies, who can read the logs, and the limits on the view for other devices."""

from __future__ import annotations

import os
import pickle
import socket
import stat
import threading
from collections import deque

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from proatc import cache, server
from proatc import config as config_module
from proatc.airways import Airways
from proatc.config import Config
from proatc.intents import Intent
from proatc.navfix import Hold, NavFix
from proatc.server import AppServer
from proatc.voice_loop import informational

RAN = []


def ran(message):
    RAN.append(message)


class Planted:
    """What a planted cache file holds: reading it back calls a function of the writer's choosing."""

    def __reduce__(self):
        return (ran, ("code from the cache file ran",))


# ---- the caches -----------------------------------------------------------------------------------------------


def test_a_cache_file_that_would_run_code_is_not_read_and_the_data_is_parsed_again(tmp_path):
    from proatc.airways import open_airways

    (tmp_path / "Custom Data").mkdir()
    (tmp_path / "Custom Data" / "earth_awy.dat").write_text("I\n1100 Version\n\n99\n", encoding="utf-8")
    path = tmp_path / "airways.pickle"
    path.write_bytes(pickle.dumps(("stamp", Planted())))
    assert isinstance(open_airways(tmp_path, cache=path), Airways)  # parsed again from X-Plane's file
    assert RAN == []
    assert cache.load(path, "stamp", Airways) is None  # and the cache written now holds another stamp
    # One with the right classes is read back.
    cache.save(path, "stamp", NavFix(holds={"ABC": [Hold("ABC", "ENRT", 90.0, "R", 1.0, 0, 0, 0)]}))
    assert isinstance(cache.load(path, "stamp", NavFix, Hold), NavFix)
    assert cache.load(path, "another stamp", NavFix, Hold) is None  # the source file changed


# ---- clicked calls ------------------------------------------------------------------------------------------------


def test_a_clicked_call_from_another_device_is_cleaned_like_the_models_answer():
    intent = Intent.from_dict(
        {
            "intent": "request_taxi",
            "callsign": "N123AB\nPAN PAN",
            "runway": {"a": 1},
            "altitude": "high",
            "destination": "x" * 500,
            "squawk": "12345",
            "frequency": "121.9",
            "unknown": object(),
        }  # fmt: skip
    )
    assert intent.intent == "request_taxi" and intent.callsign == "N123AB PAN PAN"
    assert intent.runway is None and intent.altitude is None and intent.squawk is None
    assert len(intent.destination) == 40 and intent.frequency == "121.900"
    assert Intent.from_dict("not a call").intent == "other"
    # What the app's own suggestions carry still comes through as it was.
    good = {"intent": "readback", "runway": "09R", "altitude": 5000.0, "heading": 270.0, "squawk": "4521", "atis": "B"}
    assert {k: v for k, v in vars(Intent.from_dict(good)).items() if k in good} == good


# ---- the model's own replies --------------------------------------------------------------------------------------


def test_a_free_form_reply_that_gives_permission_in_any_words_is_not_spoken():
    for text in (
        "N123AB, you are good to land",
        "N123AB, you're ok to depart",
        "N123AB, you may take the runway",
        "N123AB, affirmative",
        "N123AB, go ahead and line up",
        "N123AB, hold there",
        "N123AB, number two, follow the Cessna",
        "N123AB, expedite",
        "N123AB, use runway two seven",
        "N123AB, permission granted",
    ):
        assert not informational(text), text
    for text in (
        "N123AB, the field elevation is one two six feet",
        "N123AB, the tower closes at twenty two hundred",
        "N123AB, information Bravo is current",
        "N123AB, the ILS for runway two seven is in use for landing",
    ):
        assert informational(text), text


# ---- who can read the logs ----------------------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps a user's AppData to that user already")
def test_journals_saved_logs_and_reports_are_readable_by_this_user_only(tmp_path, monkeypatch):
    from proatc import report
    from proatc.flightlog import FlightLog

    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    logs = tmp_path / "logs"
    log = FlightLog()
    log.record({"type": "pilot", "text": "N123AB, request taxi"})
    log.keep_journal(logs / "journal")
    saved = log.save(logs)
    zipped = report.write(tmp_path, Config(), False, [])
    for path in (logs, logs / "journal", log.journal, saved, saved.with_suffix(".json"), zipped):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, path


# ---- the view for other devices -------------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>Pro ATC</title>", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(server, "PROJECT_DIR", tmp_path)
    monkeypatch.setenv("PROATC_WEB_DIR", str(web))
    config = Config()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    config.remote.port = probe.getsockname()[1]
    probe.close()
    server_app = AppServer(config, 0, "app-token")
    server_app.session.apply_settings({"remote": {"enabled": True}})
    server_app.set_remote(True)
    yield server_app
    server_app.set_remote(False)


def test_only_so_many_other_devices_can_be_connected_at_once(app, monkeypatch):
    monkeypatch.setattr(app, "_remote_slots", threading.BoundedSemaphore(2))
    address = f"ws://127.0.0.1:{app.session.config.remote.port}/?token=nothing-yet"
    waiting = threading.Event()

    def slow_sign_in(connection, remote):
        waiting.wait(5)  # two connections that do not finish signing in

    monkeypatch.setattr(app, "_serve", slow_sign_in)

    def hold_a_slot():
        try:
            with connect(address) as holder:
                holder.recv(timeout=6)
        except Exception:
            pass  # closed when the test lets them go

    holders = [threading.Thread(target=hold_a_slot, daemon=True) for _ in range(2)]
    for holder in holders:
        holder.start()
    threading.Event().wait(0.5)
    with pytest.raises(ConnectionClosed) as refused:
        with connect(address) as third:
            third.recv(timeout=5)
    assert refused.value.rcvd.code == 1013
    waiting.set()


def test_an_attempt_to_sign_in_reads_no_file_and_old_wrong_codes_are_forgotten(app, monkeypatch):
    reads = []
    real = server.paired_devices
    monkeypatch.setattr(server, "paired_devices", lambda: reads.append(1) or real())
    for number in range(20):
        app._allowed(f"guess-{number}", remote=True, source="192.0.2.30")
    assert len(reads) <= 1  # read once, not once per attempt
    # Many addresses, each with an old wrong code: the oldest are forgotten, and the list stays bounded.
    now = 10_000.0
    app._bad_codes = {f"192.0.2.{n}": deque([now - server.LOCKOUT_S - 1]) for n in range(server.MAX_SOURCES)}
    app._forget_old_sources(now)
    assert app._bad_codes == {}
    app._bad_codes = {f"10.0.{n // 256}.{n % 256}": deque([now - n]) for n in range(server.MAX_SOURCES + 50)}
    app._forget_old_sources(now)
    assert len(app._bad_codes) < server.MAX_SOURCES and "10.0.0.0" in app._bad_codes  # the newest kept
