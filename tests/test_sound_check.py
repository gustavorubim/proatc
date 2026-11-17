"""The sound check: every step asks the person, records what the machine measured, and never talks over a
flight. Nothing here plays through the speakers or records from the microphone: the sound card is stood in
for, as in test_speech.py."""

import json
import threading
import time

import numpy as np

from proatc import audio_io, soundcheck
from proatc import config as config_module
from proatc.atis import AtisBook
from proatc.audio_io import Player, Recorder
from proatc.config import Config, Tts
from proatc.radio import SECOND_RADIO_GAIN, SecondRadio
from proatc.server import AppServer
from proatc.tts import TextToSpeech
from proatc.voice_loop import Session
from tests.test_radio_loop import FakeBridge
from tests.test_realism import APRON, ground_world, taxiing
from tests.test_speech import RATE, Backend


class FakeCard:
    """Stands in for `sounddevice`: whole sounds played with `play`, streams written piece by piece, and a
    microphone at a chosen loudness. It notes when each sound plays, to show two of them overlap."""

    def __init__(self, speed=400.0, underflow_every=0, mic_level=0.2, play_underflow=False):
        self.speed, self.underflow_every, self.mic_level = speed, underflow_every, mic_level
        self.play_underflow = play_underflow
        self.played: list[tuple[float, float, int]] = []  # (start, end, frames) of each whole sound
        self.streamed: list[tuple[float, float, int]] = []  # (start, end, frames) of each output stream
        self.writes = 0
        self.lock = threading.Lock()

    # whole sounds: the first radio with a voice engine that makes a whole sentence
    def play(self, samples, rate, device=None):
        now = time.monotonic()
        with self.lock:
            self.played.append((now, now + len(samples) / rate / self.speed, len(samples)))

    def stop(self):
        pass

    def get_status(self):
        return type("Flags", (), {"output_underflow": self.play_underflow})()

    def query_devices(self, device=None, kind=None):
        if kind is None:
            return [{"name": "Desk speakers", "max_input_channels": 0, "max_output_channels": 2},
                    {"name": "Headset microphone", "max_input_channels": 1, "max_output_channels": 0}]  # fmt: skip
        return {"name": "Desk speakers" if kind == "output" else "Headset microphone"}

    # streams: the second radio, and a voice engine that speaks while it is still working
    def OutputStream(self, **settings):
        return FakeStream(self, settings["samplerate"])

    def InputStream(self, samplerate, callback, **settings):
        return FakeMicrophone(self.mic_level, samplerate, callback)


class FakeStream:
    def __init__(self, card, rate):
        self.card, self.rate, self.frames, self.opened = card, rate, 0, time.monotonic()

    def __enter__(self):
        return self

    def __exit__(self, *error):
        with self.card.lock:
            self.card.streamed.append((self.opened, time.monotonic(), self.frames))
        return False

    def write(self, block):
        time.sleep(len(block) / self.rate / self.card.speed)
        self.frames += len(block)
        with self.card.lock:
            self.card.writes += 1
            every = self.card.underflow_every
            return bool(every and self.card.writes % every == 0)

    def abort(self):
        pass


class FakeMicrophone:
    def __init__(self, level, rate, callback):
        self.level, self.rate, self.callback = level, rate, callback

    def start(self):
        block = np.full((self.rate // 10, 1), self.level, dtype=np.float32)
        for _ in range(int(soundcheck.MIC_SECONDS * 10)):
            self.callback(block, len(block), None, None)

    def stop(self):
        pass

    def close(self):
        pass


def kit_on(card, monkeypatch, engines=None, voice=None):
    """A sound check wired to the stand-in sound card: the real players, the real second radio."""
    monkeypatch.setattr(audio_io, "sd", card)
    # Short sentences: a whole sentence is waited for as long as it lasts, even on a stand-in card.
    monkeypatch.setattr(soundcheck, "SENTENCE", "Radio check")
    monkeypatch.setattr(soundcheck, "ATIS_TEXT", "Testfield information Alpha wind two seven zero")
    config = Config()
    voice = voice or TextToSpeech(Tts(), Backend("kokoro", streams=False))
    player = Player(device=3)
    second = SecondRadio(None, None, voice, Player(device=3, alongside=True), AtisBook(), config)
    engines = engines if engines is not None else [("kokoro", voice), ("phonon", TextToSpeech(Tts(), Backend("phonon")))]
    return soundcheck.Kit(voice, player, second, Recorder(), config, lambda: engines, sleep=lambda seconds: None)


def answering(*answers):
    """A person who answers the questions in turn, and the questions they were asked."""
    asked: list[tuple[int, int, str, str]] = []
    queue = list(answers)

    def ask(step, total, title, shown, question):
        asked.append((step, total, shown, question))
        return queue.pop(0) if queue else True

    return ask, asked


def test_every_step_asks_the_person_and_records_what_the_sound_card_did(monkeypatch):
    card = FakeCard()
    ask, asked = answering()
    told: list[str] = []
    outcome = soundcheck.run(kit_on(card, monkeypatch), ask, lambda step, total, title, text: told.append(title))

    assert [step["id"] for step in outcome["steps"]] == ["first_radio", "both_radios", "voice", "voice", "microphone"]
    assert [step for step, *_ in asked] == [1, 2, 3, 4, 5] and all(total == 5 for _, total, *_ in asked)
    assert "at the same time" in asked[1][3] and "Phonon" in asked[3][3] and told[0] == "Getting ready"
    assert outcome["finished"] and outcome["verdict"].startswith("Sound check passed")

    first = outcome["steps"][0]["measured"]
    assert first["opened"] == 1 and first["frames"] > 0 and first["underruns"] == 0 and first["device"] == "Desk speakers"
    assert first["engine"] == "kokoro" and first["first_sound_s"] is not None and first["faster_than_real_time"]
    phonon = outcome["steps"][3]["measured"]
    assert phonon["engine"] == "phonon" and phonon["opened"] == 1 and phonon["frames"] > 0 and phonon["breaks"] == 0
    microphone = outcome["steps"][4]["measured"]
    assert microphone["heard"] and microphone["seconds"] == 2.0 and microphone["device"] == "Headset microphone"
    assert "It heard you" in asked[4][2] and "Desk speakers" in asked[0][2]


def test_two_radios_are_two_sounds_playing_at_the_same_time(monkeypatch):
    card = FakeCard(speed=4.0)  # slow enough for the overlap to be seen
    outcome = soundcheck.run(kit_on(card, monkeypatch, engines=[]), answering()[0], lambda *told: None)
    both = outcome["steps"][1]["measured"]
    assert both["second"]["opened"] == 1 and both["second"]["frames"] > 0 and both["first"]["opened"] == 1
    # The weather recording went through the second radio's own stream, turned down, while the controller played.
    atis_start, atis_end, frames = card.streamed[0]
    controller_start = card.played[1][0]  # the first played sound is step one's
    assert atis_start <= controller_start <= atis_end and frames == both["second"]["frames"]
    assert both["second"]["sound_s"] == round(frames / RATE, 2) and SECOND_RADIO_GAIN < 1


def test_gaps_and_slow_speech_are_problems_even_when_the_person_says_yes(monkeypatch):
    class Slow(Backend):
        def pieces(self, text, voice, speed):
            for piece in super().pieces(text, voice, speed):
                time.sleep(0.3)  # a quarter of a second of speech takes longer than that to make
                yield piece

    card = FakeCard(underflow_every=3, play_underflow=True)
    slow = TextToSpeech(Tts(), Slow("phonon"))
    outcome = soundcheck.run(kit_on(card, monkeypatch, engines=[("phonon", slow)]), answering()[0], lambda *t: None)
    voice = outcome["steps"][2]["measured"]
    assert voice["breaks"] > 0 and not voice["faster_than_real_time"] and voice["times_faster"] < 1
    assert "more slowly than it is spoken" in " ".join(outcome["steps"][2]["problems"])
    assert outcome["steps"][0]["measured"]["underruns"] == 1  # what sd.play reported
    assert outcome["steps"][1]["measured"]["second"]["underruns"] > 0
    assert outcome["verdict"].startswith("Sound check found") and "gaps" in outcome["verdict"]


def test_a_voice_engine_that_will_not_load_is_noted_not_asked_about(monkeypatch):
    card = FakeCard()
    voice = TextToSpeech(Tts(), Backend("kokoro", streams=False))
    ask, asked = answering()
    engines = [("kokoro", voice), ("phonon", "ModelsMissing: The Phonon voice engine is not installed.")]
    outcome = soundcheck.run(kit_on(card, monkeypatch, engines=engines, voice=voice), ask, lambda *t: None)
    assert len(asked) == 4 and outcome["steps"][3]["measured"]["loaded"] is False
    assert outcome["finished"] and outcome["verdict"].startswith("Sound check passed")

    # Loading every engine: the one in use is not loaded twice, and one that fails is described.
    def load(name):
        raise RuntimeError("no model here")

    found = soundcheck.load_engines(voice, Tts(), load)
    assert found[0] == ("kokoro", voice) and found[1] == ("phonon", "RuntimeError: no model here")


def test_no_answer_stops_the_check_and_a_no_is_a_problem(monkeypatch):
    card = FakeCard()
    outcome = soundcheck.run(kit_on(card, monkeypatch), answering(True, None)[0], lambda *t: None)
    assert len(outcome["steps"]) == 2 and not outcome["finished"]
    assert outcome["verdict"] == "Sound check stopped after 1 of 5 steps."

    outcome = soundcheck.run(kit_on(FakeCard(), monkeypatch), answering(True, False)[0], lambda *t: None)
    assert outcome["finished"] and "the two radios were not heard together" in outcome["verdict"]


def test_a_silent_or_missing_microphone_is_a_problem(monkeypatch):
    outcome = soundcheck.run(kit_on(FakeCard(mic_level=0.0), monkeypatch), answering()[0], lambda *t: None)
    assert outcome["steps"][-1]["measured"]["heard"] is False and "the microphone heard nothing" in outcome["verdict"]
    assert "It heard nothing" in outcome["steps"][-1]["shown"]

    class NoMicrophone(FakeCard):
        def InputStream(self, **settings):
            raise OSError("no input device")

    outcome = soundcheck.run(kit_on(NoMicrophone(), monkeypatch), answering()[0], lambda *t: None)
    assert "could not be opened" in outcome["verdict"] and "no input device" in outcome["steps"][-1]["shown"]


def test_a_sound_card_that_fails_is_reported_not_raised(monkeypatch):
    class Broken(FakeCard):
        def play(self, samples, rate, device=None):
            raise OSError("device unavailable")

        def OutputStream(self, **settings):
            raise OSError("device unavailable")

    outcome = soundcheck.run(kit_on(Broken(), monkeypatch), answering()[0], lambda *t: None)
    first = outcome["steps"][0]
    assert first["measured"]["opened"] == 0 and "device unavailable" in first["measured"]["failed"]
    assert "did not play" in first["shown"] and "the controller's sound did not play" in first["problems"]
    assert "could not play at the same time" in outcome["verdict"]


def test_the_check_is_refused_while_it_would_talk_over_a_flight():
    moving = {"on_ground": 1, "groundspeed_ms": 8.0}
    flying = {"on_ground": 0, "groundspeed_ms": 120.0}
    parked = {"on_ground": 1, "groundspeed_ms": 0.1}
    assert "talk over your flight" in soundcheck.refusal(True, moving, busy=False)
    assert "talk over your flight" in soundcheck.refusal(True, flying, busy=False)
    assert "talk over your flight" in soundcheck.refusal(True, {}, busy=False)  # nothing known: not parked
    assert soundcheck.refusal(True, parked, busy=False) is None
    assert soundcheck.refusal(False, {}, busy=False) is None  # the sim is not connected
    assert "controller is talking" in soundcheck.refusal(True, parked, busy=True)
    assert "controller is talking" in soundcheck.refusal(False, {}, busy=True)
    assert "already running" in soundcheck.refusal(False, {}, busy=False, running=True)


def parked_session(tmp_path, state):
    db, engine, _ = ground_world(tmp_path)
    events: list[dict] = []
    session = Session(Config(), emit=events.append)
    session.bridge, session.db, session.book, session.engine = FakeBridge(state), db, AtisBook(), engine
    session.ready = True
    session.player = Player()
    return session, events


def test_the_session_refuses_the_check_during_a_flight_and_queues_it_when_parked(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    session, events = parked_session(tmp_path, {**taxiing(*APRON, speed=6.0), "com1_khz": 121700})
    session.start_sound_check()
    assert events[-1]["type"] == "sound_check" and events[-1]["state"] == "refused" and session.jobs.empty()

    session.bridge.state["groundspeed_ms"] = 0.0
    session.busy.set()  # the pilot is transmitting, or a controller is answering
    session.start_sound_check()
    assert "controller is talking" in events[-1]["text"] and session.jobs.empty()

    session.busy.clear()
    session.start_sound_check()
    assert session.jobs.get_nowait()[0] == "sound_check" and session._checking
    session.start_sound_check()
    assert "already running" in events[-1]["text"] and session.jobs.empty()


def test_the_outcome_is_written_to_the_journal_and_kept_for_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr("proatc.voice_loop.PROJECT_DIR", tmp_path)
    card = FakeCard()
    kit = kit_on(card, monkeypatch, engines=[])
    session, events = parked_session(tmp_path, {**taxiing(*APRON), "com1_khz": 121700})
    session.tts, session.player, session.second_radio, session.recorder = kit.tts, kit.player, kit.second, kit.recorder
    session.config = kit.config
    session.log.keep_journal(tmp_path / "logs" / "journal")
    monkeypatch.setattr(soundcheck, "load_engines", lambda current, settings: [])
    monkeypatch.setattr(soundcheck, "MIC_SECONDS", 0.2)
    session.start_sound_check()
    for _ in range(3):
        session.answer_sound_check(True)  # the person answers ahead: the questions take them in turn
    session._sound_check()

    assert [event["state"] for event in events if event["type"] == "sound_check"].count("question") == 3
    done = events[-1]
    assert done["state"] == "done" and done["verdict"].startswith("Sound check passed") and len(done["steps"]) == 3
    written = [json.loads(line) for line in session.log.journal.read_text(encoding="utf-8").splitlines()]
    noted = [entry for entry in written if entry["type"] == "sound_check"]
    assert len(noted) == 1 and noted[0]["result"]["verdict"] == done["verdict"]  # the prompts are not kept, the outcome is
    assert soundcheck.last(tmp_path / "logs")["verdict"] == done["verdict"] and session.last_sound_check == done["verdict"]
    assert not session._checking and not session.second_radio.hold.is_set()

    # Moving again by the time the job runs: refused then, too.
    session.bridge.state["groundspeed_ms"] = 10.0
    session._sound_check()
    assert events[-1]["state"] == "refused"

    # Starting to taxi during the check stops it before the next sound.
    class TaxiAway(FakeBridge):
        """Parked when the check starts; moving by the time the first question would be asked."""

        calls = 0

        def snapshot(self):
            self.calls += 1
            return {**self.state, "groundspeed_ms": 6.0 if self.calls > 1 else 0.0}

    session.bridge.state["groundspeed_ms"] = 0.0
    session.start_sound_check()
    session.bridge = TaxiAway(session.bridge.state)
    session.answer_sound_check(True)
    session._sound_check()
    assert events[-2]["type"] == "info" and events[-2]["text"].startswith("Sound check stopped. The sound check would talk")
    assert events[-1]["state"] == "done" and events[-1]["verdict"] == "Sound check stopped after 0 of 3 steps."


def test_the_second_radio_leaves_the_check_alone_while_it_runs():
    played: list[int] = []

    class Quiet:
        playing = True

        def stop(self):
            played.append(-1)

    radio = SecondRadio(FakeBridge({}, connected=False), None, None, Quiet(), AtisBook(), Config())
    radio.hold.set()
    radio.tick()  # with nothing to listen to it would stop the player: held, it does not touch it
    assert played == []
    radio.hold.clear()
    radio.tick()
    assert played == [-1]


def test_the_app_commands_reach_the_sound_check(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr("proatc.server.PROJECT_DIR", tmp_path)
    app = AppServer(Config(), 0, "token")
    started, answers = [], []
    app.session.start_sound_check = lambda: started.append(True)
    app.session.answer_sound_check = answers.append
    app._command({"cmd": "sound_check"})
    assert started == []  # not before the engine is ready
    app.session.ready = True
    app._command({"cmd": "sound_check"})
    app._command({"cmd": "sound_check_answer", "heard": True})
    app._command({"cmd": "sound_check_answer", "heard": False})
    app._command({"cmd": "sound_check_answer", "heard": "yes"})  # anything else stops it
    app._command({"cmd": "sound_check_answer"})
    assert started == [True] and answers == [True, False, None, None]


def test_the_terminal_check_is_refused_in_flight_and_asks_in_the_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    assert soundcheck.run_in_terminal(Config(), {"on_ground": 0, "groundspeed_ms": 100.0}) == 1
    assert "talk over your flight" in capsys.readouterr().out

    monkeypatch.setattr(audio_io, "sd", FakeCard())
    monkeypatch.setattr(soundcheck, "SENTENCE", "Radio check")
    monkeypatch.setattr(soundcheck, "MIC_SECONDS", 0.2)
    monkeypatch.setattr("proatc.tts.TextToSpeech", lambda settings: TextToSpeech(settings, Backend("kokoro", streams=False)))
    monkeypatch.setattr(soundcheck, "load_engines", lambda current, settings: [("kokoro", current)])
    said = iter(["maybe", "y", "y", "y", "n"])
    assert soundcheck.run_in_terminal(Config(), {}, ask_person=lambda prompt: next(said)) == 1
    out = capsys.readouterr().out
    assert "[1/4] The first radio" in out and "the microphone heard something, but not you" in out
    journal = next((tmp_path / "logs" / "journal").glob("session-*.jsonl"))
    assert '"sound_check"' in journal.read_text(encoding="utf-8") and (tmp_path / "logs" / soundcheck.RESULT_FILE).exists()
