"""The two voice engines behind one interface, sound played while it is still being made, and the radio
effect on speech that arrives in pieces. No voice model is loaded here: the engines are stood in for."""

import threading
import time

import numpy as np
import pytest

from proatc import audio_io, tts
from proatc import config as config_module
from proatc.audio_io import Player
from proatc.config import Config, Radio, Tts, validate
from proatc.radio import prime, transmit
from proatc.radio_fx import RadioStream, radio_effect, radio_stream
from proatc.tts import TextToSpeech, pace, resolve
from proatc.voice_loop import Session
from tests.test_radio_loop import FakePlayer, FakeTts

RATE = 24000


class Backend:
    """A voice engine that makes a quarter of a second of tone per word, a piece per word."""

    def __init__(self, name="phonon", streams=True, voices=("af_Hadley", "am_Preston", "bf_Maeve", "bm_Edwin", "am_Micah")):
        self.name, self.streams, self._voices = name, streams, list(voices)
        self.sample_rate = RATE
        self.novelty = ("am_Micah",)
        self.asked: list[tuple[str, str, float]] = []
        self.finished = self.closed = 0

    def voices(self):
        return sorted(self._voices)

    def pieces(self, text, voice, speed):
        self.asked.append((text, voice, speed))
        try:
            for _ in text.split():
                yield (np.sin(np.arange(RATE // 4) / 20) * 0.5).astype(np.float32)
            self.finished += 1
        finally:
            self.closed += 1


def test_a_voice_the_engine_lacks_becomes_one_of_the_same_accent_and_sex():
    phonon = [f"{group}_{name}" for group, names in tts.PHONON_VOICES.items() for name in names]
    assert len(phonon) == 24 and resolve("bf_Maeve", phonon, "phonon") == "bf_Maeve"
    assert resolve("am_michael", phonon, "phonon") == "am_Preston" and resolve("bf_emma", phonon, "phonon") == "bf_Maeve"
    kokoro = ["af_sarah", "am_michael", "bf_emma", "bm_george"]
    assert resolve("bm_Edwin", kokoro, "kokoro") == "bm_george" and resolve("nonsense", kokoro, "kokoro") == "am_michael"
    assert resolve("am_michael", ["bf_emma"], "kokoro") == "bf_emma" and resolve("am_michael", [], "kokoro") == "am_michael"
    assert set(tts.PHONON_NOVELTY) <= set(phonon)  # the voices kept from controllers exist


def test_speaking_speed_becomes_phonons_duration_change():
    assert pace(1.0) == 0.0 and pace(1.25) == pytest.approx(-0.2) and pace(0.8) == pytest.approx(0.25)
    assert pace(10.0) == -tts.PACE_LIMIT and pace(0.0) == tts.PACE_LIMIT


def test_one_interface_for_both_engines():
    backend = Backend()
    voice = TextToSpeech(Tts(voice="am_michael", speed=1.15), backend)
    assert voice.streams and voice.engine_name == "phonon"
    samples, rate = voice.speak("cleared to land")
    assert rate == RATE and len(samples) == 3 * RATE // 4 and samples.dtype == np.float32
    assert backend.asked == [("cleared to land", "am_Preston", 1.15)]  # the setting's Kokoro voice, as Phonon has it
    voice.speak("roger", voice="bf_Maeve", speed=1.3)
    assert backend.asked[-1] == ("roger", "bf_Maeve", 1.3)

    pieces, rate = voice.stream("one two three four")
    assert len(next(pieces)) == RATE // 4 and backend.finished == 2
    pieces.close()  # cut off: the engine stops, and the lock is free for the next call
    assert backend.closed == 3 and backend.finished == 2
    assert len(voice.speak("after")[0]) == RATE // 4


def test_controllers_are_not_given_the_atis_voice_or_a_novelty_voice():
    voice = TextToSpeech(Tts(atis_voice="af_sarah", pilot_voice="am_adam"), Backend())
    picked = {voice.for_station(f"station {number}", "KJFK")["voice"] for number in range(60)}
    assert picked == {"bf_Maeve", "bm_Edwin"}  # both American voices are taken, and Micah is never used
    voice = TextToSpeech(Tts(atis_voice="bf_emma", pilot_voice="bm_george"), Backend())
    picked = {voice.for_station(f"station {number}", "KJFK")["voice"] for number in range(60)}
    assert picked == {"af_Hadley", "am_Preston"}  # American, and never Micah
    assert TextToSpeech(Tts(vary_voices=False), Backend()).for_station("x", "KJFK") == {}


def test_the_engine_in_use_stays_when_another_cannot_be_loaded(monkeypatch):
    first = Backend("kokoro", streams=False)
    voice = TextToSpeech(Tts(), first)
    monkeypatch.setattr(TextToSpeech, "_load", staticmethod(lambda name: (_ for _ in ()).throw(RuntimeError("no network"))))
    with pytest.raises(RuntimeError):
        voice.use("phonon")
    assert voice.backend is first and not voice.streams
    voice.use("kokoro")  # already in use: nothing is loaded
    second = Backend("phonon")
    monkeypatch.setattr(TextToSpeech, "_load", staticmethod(lambda name: second))
    voice.use("phonon")
    assert voice.backend is second and voice.streams


def test_the_app_can_change_the_engine_and_a_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    assert validate({"tts": {"engine": "phonon"}}) == {"tts": {"engine": "phonon"}}
    with pytest.raises(ValueError):
        validate({"tts": {"engine": "espeak"}})
    events: list[dict] = []
    session = Session(Config(), emit=events.append)
    session.tts = TextToSpeech(session.config.tts, Backend("kokoro", streams=False, voices=("am_michael", "bf_emma")))
    session.options = {"voices": session.tts.voices()}
    phonon = Backend()
    monkeypatch.setattr(TextToSpeech, "_load", staticmethod(lambda name: phonon))
    session._use_voice_engine("phonon")
    assert session.tts.engine_name == "phonon" and "bf_Maeve" in session.options["voices"]
    assert phonon.asked[0][0] == "Radio check." and events[-1]["type"] == "info"

    monkeypatch.setattr(TextToSpeech, "_load", staticmethod(lambda name: (_ for _ in ()).throw(RuntimeError("no network"))))
    session.config.tts.engine = "kokoro"  # as the settings were just changed to
    session._use_voice_engine("kokoro")
    assert session.tts.engine_name == "phonon" and session.config.tts.engine == "phonon"
    assert events[-1]["type"] == "error" and "no network" in events[-1]["text"]
    assert config_module.load_config().tts.engine == "phonon"  # and it is what the next start will use


# ---- the radio effect on pieces ------------------------------------------------


def test_the_radio_effect_carries_on_from_one_piece_to_the_next():
    rng = np.random.default_rng(1)
    speech = (rng.normal(0, 0.2, RATE) * np.sin(np.arange(RATE) / 3000)).astype(np.float32)
    whole = RadioStream(RATE, noise=0.0)
    whole.peak = 1.0
    parts = RadioStream(RATE, noise=0.0)
    parts.peak = 1.0
    together = whole.process(speech)
    in_pieces = np.concatenate([parts.process(piece) for piece in np.array_split(speech, 13)])
    assert np.allclose(together, in_pieces, atol=1e-5)  # no click where two pieces meet
    assert RadioStream(RATE).process(np.zeros(0, dtype=np.float32)).size == 0

    loud = RadioStream(RATE, noise=0.012, seed=3)
    out = [loud.process(piece) for piece in np.array_split(speech * 4, 5)] + [loud.tail()]
    assert all(piece.dtype == np.float32 and np.max(np.abs(piece)) <= 1.0 for piece in out)
    assert len(out[-1]) == int(RATE * 0.09) and abs(out[-1][-1]) < 0.02  # the static fades out
    # About as loud as the effect on a whole sentence.
    one_shot = radio_effect(speech, RATE, 0.012, seed=3)
    streamed = np.concatenate([*radio_stream(iter(np.array_split(speech, 8)), RATE, 0.012, seed=3)])
    assert len(streamed) == len(one_shot)
    assert 0.6 < np.sqrt(np.mean(streamed**2)) / np.sqrt(np.mean(one_shot**2)) < 1.6


def test_stopping_the_sound_stops_the_voice_engine():
    backend = Backend()
    voice = TextToSpeech(Tts(), backend)
    pieces, rate = voice.stream("one two three four five")
    through_radio = prime(radio_stream(pieces, rate))
    assert backend.asked and backend.finished == 0  # the first piece was made before `prime` returned
    assert len(next(through_radio)) == RATE // 4
    through_radio.close()
    assert backend.closed == 1 and backend.finished == 0
    assert list(prime(iter(()))) == []  # nothing to say


# ---- playing while it is made --------------------------------------------------


class FakeSound:
    """Stands in for the sound card: writing takes as long as the sound lasts, divided by `speed`."""

    def __init__(self, speed=200.0):
        self.speed, self.written, self.aborted, self.stopped = speed, [], False, 0
        self.settings = None

    def stop(self):
        self.stopped += 1

    def OutputStream(self, **settings):
        self.settings = settings
        return self

    def __enter__(self):
        return self

    def __exit__(self, *error):
        return False

    def write(self, block):
        time.sleep(len(block) / self.settings["samplerate"] / self.speed)
        self.written.append(block.copy())

    def abort(self):
        self.aborted = True


def test_sound_is_played_as_it_arrives(monkeypatch):
    sound = FakeSound()
    monkeypatch.setattr(audio_io, "sd", sound)
    player = Player(device=7)
    backend = Backend()
    pieces, rate = TextToSpeech(Tts(), backend).stream("one two three")
    ticket = player.play_stream(pieces, rate)
    assert player.playing and player.wait(ticket) is True and not player.playing
    assert sound.settings == {"samplerate": RATE, "channels": 1, "dtype": "float32", "device": 7}
    assert sum(len(block) for block in sound.written) == 3 * RATE // 4
    assert max(len(block) for block in sound.written) <= RATE * audio_io.STREAM_STEP_S and sound.written[0].shape[1] == 1
    assert backend.finished == 1 and not sound.aborted


def test_a_new_transmission_cuts_off_sound_that_is_still_being_made(monkeypatch):
    sound = FakeSound(speed=20.0)
    monkeypatch.setattr(audio_io, "sd", sound)
    player = Player()
    backend = Backend()
    pieces, rate = TextToSpeech(Tts(), backend).stream(" ".join(["word"] * 400))  # a hundred seconds of it
    ticket = player.play_stream(pieces, rate)
    time.sleep(0.05)
    pressed = threading.Event()
    threading.Timer(0.05, pressed.set).start()
    began = time.monotonic()
    assert player.wait(ticket, should_stop=pressed.is_set) is False  # the pilot keyed the microphone
    assert time.monotonic() - began < 1.0
    for _ in range(100):
        if backend.closed:
            break
        time.sleep(0.01)
    assert sound.aborted and backend.closed == 1 and backend.finished == 0 and not player.playing

    # A sound card that fails does not leave the player waiting for ever.
    class Broken(FakeSound):
        def OutputStream(self, **settings):
            raise OSError("no device")

    monkeypatch.setattr(audio_io, "sd", Broken())
    ticket = player.play_stream(iter([np.zeros(10, dtype=np.float32)]), RATE)
    assert player.wait(ticket) is True


def test_a_controller_speaks_in_pieces_only_with_an_engine_that_makes_them():
    backend = Backend()
    voice = TextToSpeech(Tts(), backend)

    class StreamPlayer(FakePlayer):
        def play_stream(self, pieces, rate):
            self.streamed = [piece for piece in pieces]
            return 9

    heard: list[str] = []
    player = StreamPlayer()
    ticket = transmit(
        voice, player, "cleared for takeoff", {"voice": "bm_Edwin"}, Radio(enabled=True), lambda: heard.append("now")
    )
    assert ticket == 9 and heard == ["now"] and backend.asked == [("cleared for takeoff", "bm_Edwin", 1.15)]
    assert len(player.streamed) == 4  # three words and the unkeying static
    assert len(transmit(voice, player, "roger", {}, Radio(enabled=False)) and player.streamed) == 1

    # Kokoro, a player without streaming, or a test double: the whole sentence, as before.
    whole = TextToSpeech(Tts(), Backend("kokoro", streams=False))
    plain = FakePlayer()
    transmit(whole, plain, "cleared for takeoff", {}, Radio(enabled=True), lambda: heard.append("then"))
    transmit(FakeTts(), plain, "roger", {}, Radio(enabled=False))
    transmit(voice, plain, "roger", {}, Radio(enabled=False))
    assert heard[-1] == "then" and not hasattr(plain, "streamed")
