"""Two radios, as in the aircraft: one to talk on, and the other heard as well when the audio panel has it on."""

import time
from types import SimpleNamespace

import numpy as np

from proatc import audio_io
from proatc import config as config_module
from proatc.atis import AtisBook
from proatc.audio_io import Player
from proatc.config import Config, validate
from proatc.radio import SECOND_RADIO_GAIN, SecondRadio, monitored
from proatc.server import AppServer, build_state
from proatc.voice_loop import Session
from proatc.xpbridge import other_radio, transmit_radio
from tests.test_radio_loop import NEAR_LUTON, FakeBridge, make_db
from tests.test_speech import FakeSound

ATIS, TOWER = 120580, 132555  # Luton's ATIS and tower
# Working the tower on COM1, with the ATIS on COM2 and COM2 switched on in the audio panel.
BOTH = {**NEAR_LUTON, "com1_khz": TOWER, "com2_khz": ATIS, "com_selection": 6, "com1_listen": 1, "com2_listen": 1,
        "com1_volume": 0.55, "com2_volume": 0.55}  # fmt: skip


def test_which_radio_is_talked_on_and_which_is_listened_to():
    assert transmit_radio(BOTH) == 1 and transmit_radio({**BOTH, "com_selection": 7}) == 2
    assert transmit_radio({**BOTH, "com_selection": 0}) == 1  # as the ToLiss A320 reports it
    assert other_radio(BOTH) == {"radio": 2, "khz": ATIS, "listening": True, "volume": 0.55}
    assert other_radio({**BOTH, "com_selection": 7})["radio"] == 1 and other_radio({**BOTH, "com_selection": 7})["khz"] == TOWER
    assert not other_radio({**BOTH, "com2_listen": 0})["listening"]  # reception off in the audio panel
    assert not other_radio({**BOTH, "com2_volume": 0.0})["listening"]  # turned right down
    # An aircraft that reports no audio panel is not listening on its second radio; one without volumes is.
    bare = {key: value for key, value in BOTH.items() if "listen" not in key and "volume" not in key}
    assert not other_radio(bare)["listening"] and other_radio({**bare, "com2_listen": 1})["listening"]
    assert other_radio({"com1_khz": TOWER}) == {"radio": 2, "khz": None, "listening": False, "volume": 1.0}


def test_the_station_on_the_second_radio(tmp_path):
    db = make_db(tmp_path)
    db.centers = None
    other, station = monitored(db, BOTH)
    assert other["radio"] == 2 and station.kind == "atis" and station.airport.ident == "EGGW"
    assert monitored(db, {**BOTH, "com2_khz": TOWER})[1] is None  # the same frequency twice is heard once
    assert monitored(db, {**BOTH, "com2_khz": 119000})[1] is None  # nobody there
    assert monitored(db, {"com1_khz": TOWER, "com2_khz": ATIS})[1] is None  # no position yet


class Tts:
    def __init__(self):
        self.spoken = []

    def speak(self, text, voice=None, speed=None):
        self.spoken.append((text, voice))
        return np.full(2400, 0.8, dtype=np.float32), 24000


class Speaker:
    """Stands in for the second radio's player: remembers what it was given and whether it is playing."""

    def __init__(self):
        self.played, self.stops, self.playing = [], 0, False

    def play_stream(self, pieces, rate):
        self.played.append((np.concatenate(list(pieces)), rate))
        self.playing = True
        return len(self.played)

    def stop(self):
        self.stops += 1
        self.playing = False


def second_radio(tmp_path, state, **settings):
    config = Config()
    config.radio.enabled = False
    for key, value in settings.items():
        setattr(config.atc, key, value)
    events = []
    bridge = FakeBridge(state)
    db = make_db(tmp_path)
    db.centers = None
    radio = SecondRadio(bridge, db, Tts(), Speaker(), AtisBook(), config, events.append)
    return radio, bridge, events


def test_the_atis_on_the_second_radio_is_heard_under_the_first(tmp_path):
    radio, bridge, events = second_radio(tmp_path, BOTH)
    radio.tick()
    assert len(radio.player.played) == 1 and radio.tts.spoken[0][0].startswith("London Luton information Oscar")
    samples, rate = radio.player.played[0]
    assert rate == 24000 and np.allclose(samples, 0.8 * SECOND_RADIO_GAIN)  # quieter than the radio being worked
    assert events == [{"type": "atis", "airport": "EGGW", "mhz": "120.580", "text": radio.tts.spoken[0][0], "radio": 2}]
    radio.tick()
    assert len(radio.player.played) == 1  # still playing: not started again on top of itself
    radio.player.playing = False
    radio.tick()
    assert len(radio.player.played) == 1  # a pause between repeats
    radio._again_at = 0.0
    radio.tick()
    assert len(radio.player.played) == 2 and len(radio.tts.spoken) == 1 and len(events) == 1  # repeated, not made again

    # Turned down in the aircraft, it is quieter here too.
    quiet, _, _ = second_radio(tmp_path, {**BOTH, "com2_volume": 0.275})
    quiet.tick()
    assert np.allclose(quiet.player.played[0][0], 0.8 * SECOND_RADIO_GAIN * 0.5)


def test_the_second_radio_falls_silent_when_it_should(tmp_path):
    for change in ({"com2_listen": 0}, {"com2_volume": 0.0}, {"com2_khz": 119000}, {"com2_khz": TOWER}, {"paused": 1}):
        radio, bridge, _ = second_radio(tmp_path, BOTH)
        radio.tick()
        assert radio.player.playing
        bridge.state = {**BOTH, **change}
        radio.tick()
        assert radio.player.stops == 1 and not radio.player.playing, change
        radio.tick()
        assert radio.player.stops == 1 and len(radio.player.played) == 1, change

    off, _, events = second_radio(tmp_path, BOTH, second_radio=False)  # switched off in the settings
    off.tick()
    assert off.player.played == [] and events == []
    # A controller on the second radio is not talking to this aircraft: nothing is played.
    tower, _, _ = second_radio(tmp_path, {**BOTH, "com1_khz": ATIS, "com2_khz": TOWER})
    tower.tick()
    assert tower.player.played == []
    gone, bridge, _ = second_radio(tmp_path, BOTH)
    bridge.connected = False
    gone.tick()
    assert gone.player.played == []


def test_a_player_alongside_another_does_not_cut_it_off(monkeypatch):
    sound = FakeSound()
    monkeypatch.setattr(audio_io, "sd", sound)
    first, second = Player(), Player(alongside=True)
    ticket = second.play_stream(iter([np.zeros(2400, dtype=np.float32)]), 24000)
    assert second.wait(ticket) and sound.stopped == 0  # the first radio's sound was left alone
    second.stop()
    assert sound.stopped == 0
    first.play_stream(iter([np.zeros(240, dtype=np.float32)]), 24000)
    first.stop()
    assert sound.stopped == 2
    time.sleep(0.05)


def test_the_app_shows_and_tunes_the_second_radio(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr("proatc.server.PROJECT_DIR", tmp_path)
    assert validate({"atc": {"second_radio": False}}) == {"atc": {"second_radio": False}}
    db = make_db(tmp_path)
    db.centers = None
    session = Session(Config(), emit=lambda event: None)
    session.bridge, session.db, session.book = FakeBridge(BOTH), db, AtisBook()
    session.engine = SimpleNamespace(
        flight=SimpleNamespace(plan=None, next_frequency=None), summary=lambda: {}, taxi_map=lambda raw: None,
        airspace=None, suggestions=lambda *args: [],
    )  # fmt: skip
    session.stt, session.ready = SimpleNamespace(device="cpu"), True
    radio = build_state(session, db)["radio"]
    assert radio["mhz"] == "132.555" and radio["station"]["kind"] == "tower"
    assert radio["second"] == {
        "radio": 2,
        "mhz": "120.580",
        "listening": True,
        "station": "London Luton Information",
        "kind": "atis",
    }
    session.config.atc.second_radio = False
    assert build_state(session, db)["radio"]["second"]["listening"] is False
    session.bridge = FakeBridge({**BOTH, "com2_khz": 0})
    assert "second" not in build_state(session, db)["radio"]

    # "Tune the other radio" writes to the one that is not transmitted on.
    class Bridge(FakeBridge):
        def __init__(self, state):
            super().__init__(state)
            self.writes = []

        def write(self, key, value):
            self.writes.append((key, value))
            self.state[key] = value

    session.bridge = Bridge(dict(BOTH))
    session.tune("120.580", other=True)
    session.tune("121.755")
    assert session.bridge.writes == [("com2_khz", 120580), ("com1_khz", 121755)]
    session.bridge = Bridge({**BOTH, "com_selection": 7})
    session.tune("120.580", other=True)
    assert session.bridge.writes == [("com1_khz", 120580)]

    app = AppServer(Config(), 0, "token")
    asked = []
    app.session.ready = True
    app.session.tune = lambda mhz, other=False: asked.append((mhz, other))
    app._command({"cmd": "tune", "mhz": "120.580", "radio": "other"})
    app._command({"cmd": "tune", "mhz": "118.100"})
    app._command({"cmd": "tune", "mhz": "118.100", "radio": ["other"]})
    assert asked == [("120.580", True), ("118.100", False), ("118.100", False)]
