"""The glue between sim state, the tuned frequency, the ATIS broadcast and the controller, with fakes for audio and the sim."""

import threading

import numpy as np

from proatc import voice_loop
from proatc.atis import AtisBook
from proatc.config import Config
from proatc.navdata import AirportDB, build_index
from proatc.radio import AtisBroadcaster
from tests.test_phase2 import APT

NEAR_LUTON = {
    "lat": 51.88,
    "lon": -0.37,
    "elevation_m": 160.0,
    "tail_number": "G-TEST",
    "com1_khz": 120580,
    "heading_mag": 250.0,
    "heading_true": 250.0,
    "zulu_seconds": 14 * 3600.0,
    "wx_qnh_pas": 101300.0,
    "wx_visibility_sm": 10.0,
    "wx_wind_alt_m": [150.0],
    "wx_wind_kt": [12.0],
    "wx_wind_dir": [260.0],
}


class FakeBridge:
    def __init__(self, state, connected=True):
        self.state, self.connected = state, connected

    def snapshot(self):
        return dict(self.state)


class FakeTts:
    def __init__(self):
        self.spoken = []

    def speak(self, text, voice=None, speed=None):
        self.spoken.append(text)
        return np.zeros(240, dtype=np.float32), 24000


class FakePlayer:
    def __init__(self):
        self.played = 0

    def play(self, samples, rate):
        self.played += 1
        return self.played

    def wait(self, ticket, should_stop=lambda: False):
        return not should_stop()

    def stop(self):
        pass


class FakeStt:
    def __init__(self, text):
        self.text, self.calls = text, 0

    def transcribe(self, audio, hints=""):
        self.calls += 1
        return self.text


class FakeController:
    def __init__(self):
        self.calls = []

    def reply(self, transcript, context, station_key="free"):
        self.calls.append((transcript, context, station_key))
        return "G-TEST, roger."

    def remember(self, station_key, transcript, answer):
        pass


class FakeRecorder:
    def start(self):
        pass

    def stop(self):
        return np.full(16000, 0.2, dtype=np.float32)


class FakePtt:
    released = threading.Event()
    pressed = threading.Event()


FakePtt.released.set()


def make_db(tmp_path):
    apt = tmp_path / "apt.dat"
    db_path = tmp_path / "a.sqlite"
    if not db_path.exists():
        apt.write_text("\n".join(APT), encoding="utf-8")
        build_index(apt, db_path)
    return AirportDB(db_path)


def broadcaster(tmp_path, state, busy=None):
    tts, player = FakeTts(), FakePlayer()
    caster = AtisBroadcaster(FakeBridge(state), make_db(tmp_path), tts, player, AtisBook(), Config(), busy or threading.Event())
    caster._halt.set()  # no pause between repeats in tests
    return caster, tts, player


def test_atis_plays_when_atis_frequency_is_tuned_and_is_synthesised_once(tmp_path, capsys):
    caster, tts, player = broadcaster(tmp_path, NEAR_LUTON)
    caster._tick()
    caster._tick()
    assert player.played == 2 and len(tts.spoken) == 1
    assert tts.spoken[0].startswith("London Luton information Oscar")
    assert "Runway two five in use." in tts.spoken[0]
    assert "ATIS  (EGGW 120.580)" in capsys.readouterr().out


def test_atis_silent_on_other_frequencies_when_busy_or_disconnected(tmp_path):
    caster, _, player = broadcaster(tmp_path, {**NEAR_LUTON, "com1_khz": 132555})
    caster._tick()
    assert player.played == 0

    busy = threading.Event()
    busy.set()
    caster, _, player = broadcaster(tmp_path, NEAR_LUTON, busy)
    caster._tick()
    assert player.played == 0

    caster, _, player = broadcaster(tmp_path, NEAR_LUTON)
    caster.bridge.connected = False
    caster._tick()
    assert player.played == 0


def exchange(tmp_path, state, connected=True):
    stt, controller, player, tts = FakeStt("Luton Tower, G-TEST, ready for departure"), FakeController(), FakePlayer(), FakeTts()
    voice_loop._exchange(
        Config(),
        FakeBridge(state, connected),
        make_db(tmp_path),
        AtisBook(),
        stt,
        tts,
        controller,
        player,
        FakePtt,
        FakeRecorder(),
    )
    return stt, controller, player


def test_tower_answers_with_real_airport_facts(tmp_path):
    stt, controller, player = exchange(tmp_path, {**NEAR_LUTON, "com1_khz": 132555})
    assert player.played == 1
    transcript, context, key = controller.calls[0]
    assert key == "EGGW:tower" and context["callsign"] == "G-TEST"
    station = context["station"]
    assert station["you_are"] == "Luton Tower" and station["runway_in_use"] == ["25"]
    assert station["surface_wind"] == "260 degrees 12 knots" and station["qnh_hpa"] == 1013
    assert station["current_atis"] == "Oscar"


def test_nobody_answers_on_empty_or_atis_frequency(tmp_path, capsys):
    stt, controller, player = exchange(tmp_path, {**NEAR_LUTON, "com1_khz": 123450})
    assert (stt.calls, controller.calls, player.played) == (0, [], 0)
    assert "nobody answers" in capsys.readouterr().out

    stt, controller, player = exchange(tmp_path, NEAR_LUTON)
    assert (stt.calls, controller.calls, player.played) == (0, [], 0)
    assert "ATIS" in capsys.readouterr().out


def test_without_sim_the_controller_talks_freely(tmp_path):
    stt, controller, player = exchange(tmp_path, {}, connected=False)
    assert player.played == 1 and controller.calls[0][2] == "free" and "station" not in controller.calls[0][1]
