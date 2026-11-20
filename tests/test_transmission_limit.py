"""How long the microphone records. A review found that the limit on a transmission counted from when the radio's
worker got to the call, not from when the microphone started: with the worker busy (a reply being spoken, the
copilot tuning), a key left down kept the microphone recording past the limit."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
import sounddevice as sd

from proatc import config as config_module
from proatc import voice_loop
from proatc.audio_io import Recorder
from proatc.config import Config
from proatc.ptt import PushToTalk
from proatc.voice_loop import Session


class Microphone:
    """The recorder as the session uses it, without a sound card."""

    def __init__(self):
        self.recording, self.halted = False, 0

    def start(self):
        self.recording = True

    def halt(self):
        self.recording = False
        self.halted += 1

    def stop(self):
        self.halt()
        return np.zeros(0, dtype=np.float32)


def key(on_down, on_up) -> PushToTalk:
    """The push-to-talk key without its keyboard listener, which needs a desktop."""
    ptt = PushToTalk.__new__(PushToTalk)
    ptt.on_down, ptt.on_up = on_down, on_up
    ptt.pressed, ptt.released = threading.Event(), threading.Event()
    ptt.released.set()
    ptt.lock, ptt.presses = threading.RLock(), 0
    return ptt


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    events: list[dict] = []
    session = Session(Config(), emit=events.append)
    session.events = events
    session.recorder = Microphone()
    session.ptt = key(session._key_down, session._key_up)
    return session


def test_a_key_left_down_while_the_radio_is_busy_stops_the_microphone_at_the_limit(session, monkeypatch):
    monkeypatch.setattr(voice_loop, "MAX_TRANSMISSION_S", 0.3)
    session.ptt.press()  # nothing takes the job: the radio's worker is busy
    assert session.recorder.recording
    for _ in range(100):
        if not session.recorder.recording:
            break
        time.sleep(0.02)
    assert not session.recorder.recording and session.ptt.released.is_set()
    assert any("transmission ended" in event.get("text", "") for event in session.events)


def test_letting_go_of_the_key_stops_the_microphone_before_the_radio_gets_to_the_call(session):
    session.ptt.press()
    session.ptt.release()
    assert not session.recorder.recording and session.recorder.halted == 1
    assert session._tx_timer is None  # and the limit's timer is gone with it


def test_the_recorder_keeps_no_more_than_its_limit_and_then_stops_the_microphone():
    recorder = Recorder(limit_s=0.001)  # 16 samples
    recorder._room = int(recorder.limit_s * 16000)
    recorder._callback(np.ones((10, 1), dtype=np.float32), 10, None, None)
    with pytest.raises(sd.CallbackStop):
        recorder._callback(np.ones((10, 1), dtype=np.float32), 10, None, None)
    assert recorder.stop().size == 16


def test_a_microphone_that_failed_at_key_down_is_not_opened_after_the_key_is_let_go(session, monkeypatch):
    class Broken(Microphone):
        def start(self):
            raise OSError("no input device")

    session.recorder = Broken()
    session.ptt.press()  # the microphone fails here, and that is reported
    session.ptt.release()  # let go before the radio gets to the call
    exchanged = []
    monkeypatch.setattr(voice_loop, "_exchange", lambda *args, **kwargs: exchanged.append(kwargs))

    class Jobs:
        def __init__(self, *jobs):
            self.jobs = list(jobs)

        def get(self):
            if not self.jobs:
                raise KeyboardInterrupt  # the end of the run
            return self.jobs.pop(0)

    session.jobs = Jobs(("voice", session.ptt.presses, None))
    session.player = type("Player", (), {"stop": lambda self: None})()
    session._after_transmission = session.close = lambda: None
    for part in ("bridge", "db", "book", "stt", "tts", "controller", "engine", "parser"):
        setattr(session, part, None)  # passed along to the exchange, which is stood in for
    session.run_forever()
    assert exchanged == []  # the job did not try the microphone again
    assert not any(event["type"] == "error" and "Microphone" not in event["text"] for event in session.events)
    assert any("Microphone" in event.get("text", "") for event in session.events)


def test_a_key_let_go_while_the_microphone_is_tried_again_never_leaves_it_opened_after(session, monkeypatch):
    # The worker checked that the key was held, then opened the microphone: let go in between, the microphone
    # opened after the key was up. A release now waits for the attempt, and then stops it.
    class Late(Microphone):
        def __init__(self):
            super().__init__()
            self.tries, self.opened_after_release = 0, False

        def start(self):
            self.tries += 1
            if self.tries == 1:
                raise OSError("busy")  # at key-down
            letting_go = threading.Thread(target=session.ptt.release)
            letting_go.start()
            letting_go.join(0.5)  # the pilot lets go now
            self.opened_after_release = not session.ptt.pressed.is_set()
            self.recording = True

    session.recorder = Late()
    session.ptt.press()

    class Jobs:
        def __init__(self, *jobs):
            self.jobs = list(jobs)

        def get(self):
            if not self.jobs:
                raise KeyboardInterrupt
            return self.jobs.pop(0)

    session.jobs = Jobs(("voice", session.ptt.presses, None))
    session.player = type("Player", (), {"stop": lambda self: None})()
    session._after_transmission = session.close = lambda: None
    session.bridge = type("Bridge", (), {"connected": False})()
    for part in ("db", "book", "stt", "tts", "controller", "engine", "parser"):
        setattr(session, part, None)
    session.run_forever()
    assert session.recorder.tries == 2 and not session.recorder.opened_after_release
    assert not session.recorder.recording  # stopped once the key was let go
