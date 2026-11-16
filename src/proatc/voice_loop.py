"""The radio session: pilot transmissions in (voice, typed or clicked), controller transmissions out.

A controller only answers when the tuned frequency belongs to a station in range, and the
ATIS plays while an ATIS frequency is tuned. Without a sim connection the session falls
back to a free-talking controller so the audio chain can still be tested.

Everything the session does is reported through `emit` (see events.py), so the same code
drives the terminal and the desktop app.
"""

from __future__ import annotations

import queue
import threading
import time

from .atis import AtisBook
from .audio_io import MIC_RATE, Player, Recorder
from .centers import CenterIndex
from .config import Config, openrouter_key
from .engine import Engine
from .events import Emit, console
from .intents import Intent, IntentParser
from .llm import Controller, LlmError
from .metar import MetarStore
from .navdata import AirportDB
from .navfix import open_navfix
from .procedures import NavaidNames
from .ptt import PushToTalk
from .radio import AtisBroadcaster, briefing, current_station, observe
from .radio_fx import radio_effect
from .stt import SpeechToText
from .tts import TextToSpeech, download_models, models_present
from .xpbridge import SimUnavailable, XPBridge, describe, magnetic_variation

MIN_SECONDS = 0.4


class Session:
    """Owns the models, the sim connection and the rules engine for one run of the app."""

    def __init__(self, config: Config, emit: Emit = console):
        self.config, self.emit = config, emit
        self.jobs: queue.Queue = queue.Queue()
        self.busy = threading.Event()
        self.ready = False
        self.controller = self.parser = None
        self.download_models = False  # the desktop app fetches missing voice files itself

    def load(self) -> None:
        config, emit = self.config, self.emit
        emit({"type": "status", "text": "Reading X-Plane airport data..."})
        self.db = AirportDB.open(config.sim.xplane_path)
        emit({"type": "status", "text": "Loading speech models (first run downloads the speech-to-text model)..."})
        self.stt = SpeechToText(config.stt)
        if self.download_models and not models_present():
            download_models(lambda text: emit({"type": "status", "text": text}))
        self.tts = TextToSpeech(config.tts)
        self.tts.speak("Radio check.")  # the first synthesis is several seconds slower than the rest
        emit({"type": "status", "text": f"Speech-to-text on {self.stt.device.upper()}."})

        self.set_key(openrouter_key())
        if not self.controller:
            emit({"type": "status", "text": "No OPENROUTER_API_KEY found: echo mode, the controller will read your words back."})

        self.bridge = XPBridge(config.sim.host, config.sim.port)
        self.bridge.start()
        if self.bridge.wait_for_data(4):
            emit({"type": "status", "text": f"Connected to X-Plane. Aircraft: {describe(self.bridge.snapshot())}"})
        else:
            emit({"type": "status", "text": "X-Plane not connected yet. I will connect when a flight is loaded; "
                                            "until then the controller talks freely."})  # fmt: skip

        self.player = Player(config.audio.output_device)
        self.book = AtisBook()
        root = self.db.xplane_root
        self.centers = CenterIndex.open(root)
        self.db.centers = self.centers
        self.metars = MetarStore(root)
        self.navfix = open_navfix(root)
        self.db.metars = self.metars
        self.engine = Engine(
            self.db, centers=self.centers, navaids=NavaidNames(root), navfix=self.navfix, metars=self.metars,
            holding=config.atc.holding,
        )  # fmt: skip
        radio_db = AirportDB()  # the background thread's own connection
        radio_db.centers, radio_db.metars = self.centers, self.metars
        self.broadcaster = AtisBroadcaster(
            self.bridge, radio_db, self.tts, self.player, self.book, config, self.busy, self.engine, emit
        )
        self.broadcaster.start()
        self.ptt = PushToTalk(config.ptt.key, on_down=lambda: self.jobs.put(("voice", None, None)))
        self.ptt.start()
        self.recorder = Recorder(config.audio.input_device)
        self.ready = True
        emit({"type": "status", "text": f"\nReady. Hold [{config.ptt.key}] to talk, release to send. Ctrl+C to quit.\n"})

    def set_key(self, key: str | None) -> None:
        """Use this OpenRouter key from now on (None: echo mode)."""
        self.controller = Controller(self.config.llm, key) if key else None
        self.parser = IntentParser(self.controller) if self.controller else None

    def tune(self, mhz: str) -> None:
        """Set the COM radio selected for transmitting to this frequency, at the pilot's request."""
        if not self.config.sim.allow_tuning:
            raise SimUnavailable("Tuning from the app is switched off (sim.allow_tuning in config.toml).")
        khz = int(round(float(mhz) * 1000))
        if not 118000 <= khz <= 136990:
            raise SimUnavailable(f"{mhz} is not a voice frequency.")
        radio = 2 if self.bridge.snapshot().get("com_selection") == 7 else 1
        key = f"com{radio}_khz"
        self.bridge.write(key, khz)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if abs(self.bridge.snapshot().get(key, 0) - khz) <= 5:
                self.emit({"type": "info", "text": f"COM{radio} tuned to {khz / 1000:.3f}"})
                return
            time.sleep(0.1)
        raise SimUnavailable(
            f"The aircraft did not take {khz / 1000:.3f} on COM{radio}. Some add-on aircraft manage their own radios; tune it in the cockpit."
        )

    def submit(self, text: str, intent: Intent | None = None) -> None:
        """Queue a transmission that was typed, or clicked from the suggested calls (which carry their intent)."""
        self.jobs.put(("clicked" if intent else "typed", text, intent))

    def run_forever(self) -> None:
        try:
            while True:
                source, text, intent = self.jobs.get()
                self.busy.set()
                self.player.stop()
                try:
                    if source == "voice":
                        _exchange(self.config, self.bridge, self.db, self.book, self.stt, self.tts, self.controller,
                                  self.player, self.ptt, self.recorder, self.engine, self.parser, self.emit)  # fmt: skip
                    else:
                        self._spoken_for_you(text)
                        _respond(text, source, intent, time.perf_counter(), self.config, self.bridge, self.db, self.book,
                                 self.tts, self.controller, self.player, self.ptt, self.engine, self.parser, self.emit)  # fmt: skip
                except Exception as error:  # one bad exchange must not end the session
                    self.emit({"type": "error", "text": f"{type(error).__name__}: {error}"})
                finally:
                    self.busy.clear()
        except KeyboardInterrupt:
            self.emit({"type": "status", "text": "\nBye."})
        finally:
            self.close()

    def _spoken_for_you(self, text: str) -> None:
        """A clicked or typed call is read out in a pilot voice, so the exchange still sounds like radio."""
        if not self.config.tts.speak_pilot_text:
            return
        samples, rate = self.tts.speak(text, voice=self.config.tts.pilot_voice)
        ticket = self.player.play(samples, rate)
        self.player.wait(ticket, should_stop=self.ptt.pressed.is_set)

    def close(self) -> None:
        if not self.ready:
            return
        self.broadcaster.stop()
        self.player.stop()
        self.ptt.stop()
        self.bridge.stop()


def run(config: Config) -> None:
    session = Session(config)
    session.load()
    session.run_forever()


def _exchange(config, bridge, db, book, stt, tts, controller, player, ptt, recorder, engine=None, parser=None,
              emit: Emit = console) -> None:  # fmt: skip
    """One voice transmission: record while the key is held, transcribe, answer."""
    recorder.start()
    emit({"type": "tx", "on": True})
    ptt.released.wait()
    audio = recorder.stop()
    emit({"type": "tx", "on": False})
    started = time.perf_counter()
    if audio.size < MIC_RATE * MIN_SECONDS:
        emit({"type": "info", "text": "too short: keep the key held while you speak"})
        return
    level = float(abs(audio).max())
    if level < 0.01:
        emit({"type": "info", "text": f"microphone is silent, peak level {level:.3f}: check the input device"})
        return
    if not _someone_listening(bridge, db, emit):
        return
    raw = bridge.snapshot() if bridge.connected else {}
    transcript = stt.transcribe(audio, hints=describe(raw).get("callsign_spoken", ""))
    if not transcript:
        emit({"type": "info", "text": "nothing heard"})
        return
    _respond(transcript, "voice", None, started, config, bridge, db, book, tts, controller, player, ptt, engine, parser, emit)


def _someone_listening(bridge, db, emit: Emit) -> bool:
    """False, with a note, when the tuned frequency has no controller in range."""
    if not bridge.connected:
        return True  # no sim: free-talk mode
    khz, station = current_station(db, bridge.snapshot())
    tuned = f"{khz / 1000:.3f}" if khz else "no frequency"
    if station is None:
        emit({"type": "info", "text": f"nobody answers: no station in range on {tuned}"})
        return False
    if station.kind == "atis":
        emit({"type": "info", "text": f"{tuned} is the {station.airport.ident} ATIS: nobody is listening on it"})
        return False
    return True


def _respond(transcript, source, intent, started, config, bridge, db, book, tts, controller, player, ptt,
             engine=None, parser=None, emit: Emit = console) -> None:  # fmt: skip
    """Answer one pilot transmission, whatever way it arrived."""
    if source != "voice" and not _someone_listening(bridge, db, emit):
        return
    raw = bridge.snapshot() if bridge.connected else {}
    context = dict(describe(raw))
    station_key = "free"
    station = issue = None
    if raw:
        _, station = current_station(db, raw)
        if station is None or station.kind == "atis":
            return  # retuned while transmitting
        weather = observe(db, station.airport, raw) if station.airport else None
        # The controller works from the same observation the ATIS is broadcasting.
        issue = book.current(station.airport, weather, raw.get("zulu_seconds", 0.0)) if weather else None
        context["station"] = briefing(
            station, issue.weather if issue else None, issue.letter if issue else None, magnetic_variation(raw)
        )
        station_key = station.key

    heard = time.perf_counter()
    emit({"type": "pilot", "text": transcript, "source": source})
    try:
        answer = _answer(transcript, intent, context, station_key, station, issue, raw, controller, engine, parser, emit)
    except LlmError as error:
        emit({"type": "error", "text": str(error)})
        return
    thought = time.perf_counter()
    if not answer:
        emit({"type": "silent"})
        return
    label = context["station"]["you_are"] if "station" in context else "ATC"
    emit({"type": "atc", "station": label, "text": answer, "unprompted": False})

    samples, rate = tts.speak(answer)
    if config.radio.enabled:
        samples = radio_effect(samples, rate, config.radio.noise)
    voiced = time.perf_counter()
    emit({"type": "timings", "listen": heard - started, "think": thought - heard, "voice": voiced - thought,
          "total": voiced - started})  # fmt: skip
    ticket = player.play(samples, rate)
    player.wait(ticket, should_stop=ptt.pressed.is_set)  # pressing the key again cuts the controller off


def _answer(transcript, intent, context, station_key, station, issue, raw, controller, engine, parser, emit: Emit) -> str:
    """The controller's words: from the rules engine when it covers the request, else a free-form reply."""
    if not controller:
        return f"Readback check. {transcript}"
    if station and engine and parser:
        if intent is None:
            intent = parser.parse(transcript, engine.flight.last_said.get(station_key, ""))
        emit({"type": "understood", "intent": intent.intent})
        reply = engine.handle(intent, station, issue, raw, magnetic_variation(raw))
        if reply is not None:
            controller.remember(station_key, transcript, reply.text)
            return reply.text
        context["flight"] = engine.summary()
    return controller.reply(transcript, context, station_key)
