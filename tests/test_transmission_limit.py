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
