"""The radio session: pilot transmissions in (voice, typed or clicked), controller transmissions out.

A controller only answers when the tuned frequency belongs to a station in range, and the
ATIS plays while an ATIS frequency is tuned. Without a sim connection the session falls
back to a free-talking controller so the audio chain can still be tested.

Everything the session does is reported through `emit` (see events.py), so the same code
drives the terminal and the desktop app.
"""

from __future__ import annotations

import json
import queue
import threading
import time

from .airspace import open_airspace
from .atis import AtisBook
from .audio_io import MIC_RATE, Player, Recorder
from .centers import CenterIndex
from .config import PROJECT_DIR, Config, _apply, openrouter_key, save_overrides, validate
from .debrief import as_text, review
from .engine import Engine
from .events import Emit, console
from .flightlog import WITH_CONTEXT, FlightLog
from .intents import Intent, IntentParser
from .joystick import JoystickButtons
from .llm import Controller, LlmError
from .metar import MetarStore
from .navdata import AirportDB
from .navfix import open_navfix
from .procedures import NavaidNames, route_fixes
from .ptt import PushToTalk
from .radio import AtisBroadcaster, briefing, current_station, observe, station_voice
from .radio_fx import radio_effect
from .stt import SpeechToText
from .tts import TextToSpeech, download_models, models_present
from .xpbridge import SimUnavailable, XPBridge, describe, magnetic_variation

MIN_SECONDS = 0.4


INSTRUCTOR = (
    "You are a flight instructor sitting next to a pilot in a flight simulator. In plain language and at most three "
    "short sentences, say what the controller's instruction means, what the pilot should do, and what to read back. "
    "No markdown, no lists, no lecture on phraseology."
)
DEBRIEFER = (
    "You are a flight instructor debriefing a pilot's radio work after a flight in a flight simulator. You get counts "
    "and a list of events. In at most four short sentences, say what went well, what to work on first, and one concrete "
    "tip. Plain language, no markdown, no lists, no score."
)
GENERIC_CALLS = ("Say again", "Radio check", "Deviate left", "Deviate right", "Destination weather", "Clear of weather")


def audio_options() -> dict:
    """Microphones and speakers to choose from: the devices of Windows' default audio interface."""
    import sounddevice as sd

    try:
        default_api = sd.query_devices(kind="input")["hostapi"]
    except Exception:
        default_api = 0
    inputs, outputs = [], []
    for index, device in enumerate(sd.query_devices()):
        if device["hostapi"] != default_api:
            continue
        if device["max_input_channels"] > 0:
            inputs.append({"id": index, "name": device["name"]})
        if device["max_output_channels"] > 0:
            outputs.append({"id": index, "name": device["name"]})
    return {"inputs": inputs, "outputs": outputs}


class Session:
    """Owns the models, the sim connection and the rules engine for one run of the app."""

    def __init__(self, config: Config, emit: Emit = console):
        self.config, self._sink = config, emit
        self.log = FlightLog()
        self.usage: dict = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        self.jobs: queue.Queue = queue.Queue()
        self.busy = threading.Event()
        self.ready = False
        self.controller = self.parser = None
        self.download_models = False  # the desktop app fetches missing voice files itself
        self.options: dict = {"inputs": [], "outputs": [], "voices": [], "joysticks": []}
        self.capturing = False
        self.joystick = None
        self._last_atc: tuple[str, str] | None = None
        self._read_back: object | None = None  # the instruction the copilot has already read back
        self._tune_failed: str | None = None

    # ---- events -------------------------------------------------------------

    def emit(self, event: dict) -> None:
        """Report an event to the terminal or the app, and keep it in the flight log."""
        if event.get("type") == "atc":
            self._last_atc = (event["station"], event["text"])
        try:
            self.log.record(event, self._context() if event.get("type") in WITH_CONTEXT else None)
        except Exception:
            pass  # the log must never break the radio
        self._sink(event)

    def _context(self) -> dict | None:
        if not self.ready or not self.bridge.connected:
            return None
        raw = self.bridge.snapshot()
        khz, station = current_station(self.db, raw)
        state = describe(raw)
        keep = ("lat", "lon", "altitude_ft", "height_agl_ft", "heading_mag", "groundspeed_kt", "on_ground", "squawk")
        return {
            "tuned": f"{khz / 1000:.3f}" if khz else None,
            "station": station.callsign if station else None,
            "aircraft": {key: state[key] for key in keep if key in state},
            "flight": self.engine.summary(),
        }

    # ---- start-up -----------------------------------------------------------

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
            holding=config.atc.holding, traffic=config.atc.traffic, airspace=open_airspace(root),
        )  # fmt: skip
        radio_db = AirportDB()  # the background thread's own connection
        radio_db.centers, radio_db.metars = self.centers, self.metars
        self.broadcaster = AtisBroadcaster(
            self.bridge, radio_db, self.tts, self.player, self.book, config, self.busy, self.engine, emit,
            after=lambda: self.jobs.put(("after", None, None)),
        )  # fmt: skip
        self.broadcaster.start()
        self.ptt = PushToTalk(config.ptt.key, on_down=lambda: self.jobs.put(("voice", None, None)))
        self.ptt.start()
        self.joystick = JoystickButtons(self._joystick_button, config.ptt.joystick, config.ptt.joystick_button)
        self.joystick.start()
        self.recorder = Recorder(config.audio.input_device)
        try:
            self.options.update(audio_options())
            self.options["voices"] = self.tts.voices()
        except Exception:
            pass  # the settings screen then offers no choices; everything else works
        self.ready = True
        emit({"type": "status", "text": f"\nReady. Hold [{config.ptt.key}] to talk, release to send. Ctrl+C to quit.\n"})

    def _joystick_button(self, down: bool) -> None:
        if self.capturing:
            return
        self.ptt.press() if down else self.ptt.release()

    def set_key(self, key: str | None) -> None:
        """Use this OpenRouter key from now on (None: echo mode). A local model needs no key."""
        usable = key or self.config.llm.provider == "local"
        self.controller = Controller(self.config.llm, key, self.usage) if usable else None
        self.parser = IntentParser(self.controller) if self.controller else None

    # ---- settings -----------------------------------------------------------

    def apply_settings(self, changes: dict) -> None:
        """Change settings from the app: checked, remembered, and applied now where that is possible."""
        cleaned = validate(changes)
        _apply(self.config, cleaned)
        save_overrides(cleaned)
        if {"provider", "local_url", "local_model"} & set(cleaned.get("llm", {})):
            self.set_key(openrouter_key())
        if not self.ready:
            return
        audio, ptt, atc = cleaned.get("audio", {}), cleaned.get("ptt", {}), cleaned.get("atc", {})
        if "input_device" in audio:
            self.recorder.device = audio["input_device"]
        if "output_device" in audio:
            self.player.device = audio["output_device"]
        if "key" in ptt:
            self.ptt.set_key(ptt["key"])
        if "joystick" in ptt or "joystick_button" in ptt:
            self.joystick.bind(self.config.ptt.joystick, self.config.ptt.joystick_button)
        if "holding" in atc:
            self.engine.holding = atc["holding"]
        if "traffic" in atc:
            self.engine.traffic_enabled = atc["traffic"]

    def capture_ptt(self, seconds: float = 15.0) -> None:
        """Bind push-to-talk to the next keyboard key or joystick button pressed."""
        from pynput import keyboard

        if self.capturing or not self.ready:
            return
        self.capturing = True
        self.emit({"type": "capture", "active": True})
        done = threading.Event()

        def finish(changes: dict | None) -> None:
            if done.is_set():
                return
            done.set()
            listener.stop()
            self.joystick.cancel_capture()
            self.capturing = False
            if changes:
                try:
                    self.apply_settings({"ptt": changes})
                except ValueError as error:
                    self.emit({"type": "error", "text": str(error)})
            self.emit({"type": "capture", "active": False})

        def on_key(key) -> None:
            name = key.char if isinstance(key, keyboard.KeyCode) else key.name
            if name and name != "esc":
                finish({"key": name})
            else:
                finish(None)

        listener = keyboard.Listener(on_press=on_key)
        listener.start()
        self.joystick.capture(lambda device, button: finish({"joystick": device, "joystick_button": button}))
        timer = threading.Timer(seconds, lambda: finish(None))
        timer.daemon = True
        timer.start()

    # ---- the radio ----------------------------------------------------------

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

    def ask(self, what: str) -> None:
        """Queue a question for the instructor: 'explain' the last instruction, or 'what_next'."""
        self.jobs.put((what, None, None))

    def debrief(self) -> dict:
        """Review the radio work so far; with a language model, add the instructor's remarks."""
        result = review(self.log.records)
        if self.controller and result["transmissions"] >= 5:  # too little to say anything useful about
            try:
                facts = {key: result[key] for key in ("minutes", "transmissions", "readbacks", "say_again", "stations")}
                facts["events"] = [f"{f['time']} {f['text']}" for f in result["findings"]][:30]
                result["notes"] = self.controller.complete(DEBRIEFER, json.dumps(facts), max_tokens=260, temperature=0.3)
            except LlmError:
                pass  # the counts stand on their own
        self.emit({"type": "debrief", **result})
        return result

    def save_log(self) -> None:
        extra = {"flight": self.engine.summary() if self.ready else None, "openrouter_usage": dict(self.usage),
                 "model": self.config.llm.model}  # fmt: skip
        extra["debrief"] = review(self.log.records)
        extra["debrief_text"] = "\n" + as_text(extra["debrief"])
        if self.ready and self.engine.flight.plan:
            extra["plan"] = self.engine.flight.plan.summary()
        path = self.log.save(PROJECT_DIR / "logs", extra)
        self.emit({"type": "log_saved", "path": str(path)})

    def run_forever(self) -> None:
        try:
            while True:
                kind, text, intent = self.jobs.get()
                if kind != "after":
                    self.busy.set()
                    self.player.stop()
                    try:
                        if kind == "voice":
                            _exchange(self.config, self.bridge, self.db, self.book, self.stt, self.tts, self.controller,
                                      self.player, self.ptt, self.recorder, self.engine, self.parser, self.emit)  # fmt: skip
                        elif kind in ("explain", "what_next"):
                            self._instructor(kind)
                        elif kind == "debrief":
                            self.debrief()
                        else:
                            self._spoken_for_you(text)
                            _respond(text, kind, intent, time.perf_counter(), self.config, self.bridge, self.db, self.book,
                                     self.tts, self.controller, self.player, self.ptt, self.engine, self.parser, self.emit)  # fmt: skip
                    except Exception as error:  # one bad exchange must not end the session
                        self.emit({"type": "error", "text": f"{type(error).__name__}: {error}"})
                    finally:
                        self.busy.clear()
                try:
                    self._after_transmission()
                except Exception as error:
                    self.emit({"type": "error", "text": f"copilot: {type(error).__name__}: {error}"})
        except KeyboardInterrupt:
            self.emit({"type": "status", "text": "\nBye."})
        finally:
            self.close()

    def _spoken_for_you(self, text: str) -> None:
        """A clicked, typed or copilot call is read out in a pilot voice, so the exchange still sounds like radio."""
        if not self.config.tts.speak_pilot_text:
            return
        samples, rate = self.tts.speak(text, voice=self.config.tts.pilot_voice)
        ticket = self.player.play(samples, rate)
        self.player.wait(ticket, should_stop=self.ptt.pressed.is_set)

    # ---- the copilot --------------------------------------------------------

    def _station_and_issue(self):
        raw = self.bridge.snapshot()
        _, station = current_station(self.db, raw)
        issue = None
        if station is not None and station.airport is not None:
            weather = observe(self.db, station.airport, raw)
            issue = self.book.current(station.airport, weather, raw.get("zulu_seconds", 0.0)) if weather else None
        return raw, station, issue

    def _after_transmission(self) -> None:
        """What the copilot does once a controller has spoken: read back, tune the hand-off, check in."""
        if not self.ready or not self.bridge.connected or not self.jobs.empty():
            return
        mode, flight = self.config.atc.copilot, self.engine.flight
        raw, station, issue = self._station_and_issue()
        if station is None or station.kind == "atis":
            return
        pending = flight.pending
        if mode != "off" and pending is not None and pending is not self._read_back and flight.pending_key == station.key:
            call = next((s for s in self.engine.suggestions(station, issue, raw) if s["label"] == "Read back"), None)
            if call:
                self._read_back = pending
                self.jobs.put(("copilot", call["text"], Intent.from_dict(call["intent"])))
                return
        frequency = flight.next_frequency
        wants_tune = self.config.sim.allow_tuning and (self.config.sim.auto_tune or mode == "full")
        if frequency and flight.pending is None and wants_tune and frequency != self._tune_failed:
            try:
                self.tune(frequency)
            except (SimUnavailable, ValueError) as error:
                self._tune_failed = frequency  # say it once, then leave it to the pilot
                self.emit({"type": "error", "text": str(error)})
                return
            flight.next_frequency = None
            if mode == "full":
                time.sleep(1.5)  # let the sim report the new frequency
                raw, station, issue = self._station_and_issue()
                if station is not None and station.kind != "atis" and station.key not in flight.contacted:
                    call = next((s for s in self.engine.suggestions(station, issue, raw) if s["label"] == "Check in"), None)
                    if call:
                        self.jobs.put(("copilot", call["text"], Intent.from_dict(call["intent"])))

    def _instructor(self, kind: str) -> None:
        """'What does that mean?' and 'what do I say now?', answered in the log, not on the radio."""
        if kind == "what_next":
            calls = []
            if self.bridge.connected:
                raw, station, issue = self._station_and_issue()
                if station is not None and station.kind != "atis":
                    calls = [s for s in self.engine.suggestions(station, issue, raw) if s["label"] not in GENERIC_CALLS]
            if calls:
                text = calls[0]["text"].replace("{destination}", "your destination")
                self.emit({"type": "coach", "text": f"You could say: “{text}”"})
            else:
                self.emit({"type": "coach", "text": "Tune a controller's frequency first; then I can tell you what to say."})
            return
        if not self._last_atc:
            self.emit({"type": "coach", "text": "No controller has said anything yet."})
            return
        if not self.controller:
            self.emit({"type": "coach", "text": "Add an OpenRouter key to have instructions explained."})
            return
        station, said = self._last_atc
        flight = json.dumps(self.engine.summary()) if self.ready else "{}"
        answer = self.controller.complete(
            INSTRUCTOR, f"{station} said: {said}\nThe flight so far: {flight}", max_tokens=220, temperature=0.2
        )
        self.emit({"type": "coach", "text": answer})

    def close(self) -> None:
        if not self.ready:
            return
        self.broadcaster.stop()
        self.player.stop()
        self.ptt.stop()
        if self.joystick:
            self.joystick.stop()
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
    hints = engine._callsign(raw) if engine and raw else describe(raw).get("callsign_spoken", "")
    if engine and raw:
        _, station = current_station(db, raw)
        hints = hearing_hints(engine, hints, station)
    transcript = stt.transcribe(audio, hints=hints)
    if not transcript:
        emit({"type": "info", "text": "nothing heard"})
        return
    _respond(transcript, "voice", None, started, config, bridge, db, book, tts, controller, player, ptt, engine, parser, emit)


def known_names(engine) -> list[str]:
    """Identifiers this flight is likely to mention: its airports, its route's waypoints, its procedures."""
    flight, plan = engine.flight, engine.flight.plan
    names: list[str] = []
    if plan:
        names += [name for name in (plan.origin, plan.destination, plan.alternate) if name]
        names += route_fixes(plan.route)
    for procedure in (flight.star_procedure,):
        if procedure:
            names += [fix for fix in procedure.fixes() if fix.isalpha()]
    names += [name for name in (flight.sid, flight.star, flight.direct_to) if name]
    if flight.arrival and flight.arrival.hold:
        names.append(flight.arrival.hold.fix)
    return list(dict.fromkeys(names))


def hearing_hints(engine, callsign: str, station) -> str:
    """Words the speech recogniser should expect: the callsign, who is being called, and the flight's own names.

    Waypoints are five-letter inventions that no recogniser knows; seeing them written makes it far
    more likely to write "BIBAX" than "bee backs"."""
    parts = [callsign]
    if station is not None:
        parts.append(station.callsign)
    plan = engine.flight.plan
    if plan and plan.destination:
        airport = engine.db.airport(plan.destination)
        if airport:
            parts.append(airport.spoken_name)
    names = [name.title() if len(name) > 3 else name for name in known_names(engine) if name.isalpha()][:24]
    if names:
        parts.append("Waypoints: " + ", ".join(names))
    return ". ".join(part for part in parts if part) + "."


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
        note = engine.flight.last_note if engine is not None else ""
        emit({"type": "silent", "text": note} if note else {"type": "silent"})
        return
    label = context["station"]["you_are"] if "station" in context else "ATC"
    emit({"type": "atc", "station": label, "text": answer, "unprompted": False})

    samples, rate = tts.speak(answer, **station_voice(tts, station))
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
            intent = parser.parse(transcript, engine.flight.last_said.get(station_key, ""), known_names(engine))
        emit({"type": "understood", "intent": intent.intent})
        reply = engine.handle(intent, station, issue, raw, magnetic_variation(raw))
        engine.flight.last_note = reply.note if reply is not None else ""
        if reply is not None:
            controller.remember(station_key, transcript, reply.text)
            return reply.text
        context["flight"] = engine.summary()
    return controller.reply(transcript, context, station_key)
