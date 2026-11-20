"""The radio session: pilot transmissions in (voice, typed or clicked), controller transmissions out.

A controller only answers when the tuned frequency belongs to a station in range, and the
ATIS plays while an ATIS frequency is tuned. Without a sim connection the session falls
back to a free-talking controller so the audio chain can still be tested.

Everything the session does is reported through `emit` (see events.py), so the same code
drives the terminal and the desktop app.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from contextlib import nullcontext
from dataclasses import asdict

from . import phrasing, questions, resolver, soundcheck, system_check
from . import report as problem_report
from .airspace import open_airspace
from .airways import open_airways
from .atis import AtisBook
from .audio_io import MIC_RATE, Player, Recorder
from .centers import CenterIndex
from .clock import REAL, Clock
from .config import PROJECT_DIR, Config, _apply, openrouter_key, save_overrides, validate
from .datis import RealAtis
from .debrief import as_text, review
from .engine import Engine
from .events import Emit, console
from .flightlog import WITH_CONTEXT, FlightLog
from .health import check, for_app
from .intents import Intent, IntentParser
from .joystick import JoystickButtons
from .llm import Controller, LlmError
from .metar import MetarStore
from .navdata import AirportDB
from .navfix import open_navfix
from .procedures import NavaidNames, route_fixes
from .ptt import PushToTalk
from .radio import AtisBroadcaster, SecondRadio, SimClock, briefing, current_station, observe, station_voice, transmit
from .replay import load as load_log
from .replay import snapshot
from .stt import SpeechToText
from .tts import TextToSpeech, download_models, models_present
from .xpbridge import SimUnavailable, XPBridge, describe, magnetic_variation, same_channel, transmit_radio, tuned_khz

log = logging.getLogger(__name__)
MAX_TRANSMISSION_S = 90  # the longest a transmission records: a full clearance readback takes about twenty

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
TRACE_EVERY_S = 20
TUNE_WAIT_S = 5.0  # how long a written frequency may take to be reported: real sims have taken over 2 s
# Calls on offer that are never "what to say next": questions, and ways out of an instruction.
GENERIC_CALLS = (
    "Say again", "Radio check", "Deviate left", "Deviate right", "Destination weather", "Clear of weather", "Unable",
    "Confirm level", "Verify altitude", "Expected approach", "Wind check", "Return to stand", "Return to gate",
)  # fmt: skip


def what_to_say(suggestions: list[dict]) -> list[dict]:
    """The calls worth suggesting next: the answers to the controller's question first, never a generic one."""
    answers = [call for call in suggestions if call.get("answers")]
    return answers + [call for call in suggestions if call["label"] not in GENERIC_CALLS and not call.get("answers")]


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

    def __init__(self, config: Config, emit: Emit = console, clock: Clock = REAL):
        self.config, self._sink = config, emit
        self.clock = clock  # every wait and sleep of the radio goes through it (tests/sim.py runs it simulated)
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
        self._traced, self._traced_plan = None, False  # the plan is noted the first time, even when there is none
        self._last_atc: tuple[str, str] | None = None
        self._read_back: tuple | None = None  # (instruction, reminded) the copilot has already read back
        self._followed: tuple | None = None  # (frequency, hand-off number) the copilot has already tuned
        self._keyed_khz: int | None = None  # the frequency the microphone was last keyed on
        self.computer: list[dict] = []  # what could be checked about this computer (system_check.py)
        self._checking = False  # a sound check is queued or running
        self._answers: queue.Queue = queue.Queue()  # the person's answers to the sound check's questions
        checked = soundcheck.last(PROJECT_DIR / "logs")
        self.last_sound_check: str = checked.get("verdict", "") if isinstance(checked, dict) else ""
        self._recording = False  # the microphone was started at key-down
        self._keying = threading.Lock()  # between the key going down and a finished job letting others talk
        self._tx_timer: threading.Timer | None = None  # ends a transmission MAX_TRANSMISSION_S after key-down
        self._missed: tuple | None = None  # a readback of the copilot's that nobody heard, tried once more

    # ---- events -------------------------------------------------------------

    def emit(self, event: dict) -> None:
        """Report an event to the terminal or the app, and keep it in the flight log."""
        if event.get("type") == "unsaid":
            # A controller's call that was decided and not heard: kept in the journal, not shown or spoken.
            try:
                self.log.note("unsaid", None, **{key: value for key, value in event.items() if key != "type"})
            except Exception:
                pass  # the log must never break the radio
            return
        if event.get("type") == "atc":
            self._last_atc = (event["station"], event["text"])
        try:
            self.log.record(event, self._context() if event.get("type") in WITH_CONTEXT else None)
        except Exception:
            pass  # the log must never break the radio
        self._sink(event)

    # ---- the journal on disk -----------------------------------------------

    def _start_journal(self) -> None:
        """From here on the flight is written to disk as it happens (see flightlog.py)."""
        from importlib.metadata import PackageNotFoundError, version

        try:
            release = version("proatc")
        except PackageNotFoundError:
            release = "source"
        if self.log.keep_journal(PROJECT_DIR / "logs" / "journal") is None:
            return
        self.log.note("session", version=release, seed=self.engine.seed, model=self.config.llm.model,
                      voice_engine=self.tts.engine_name, copilot=self.config.atc.copilot)  # fmt: skip
        threading.Thread(target=self._trace_loop, name="journal-trace", daemon=True).start()

    def _trace_loop(self) -> None:
        stop = threading.Event()
        while not self.clock.wait(stop, TRACE_EVERY_S):
            try:
                self.trace()
            except Exception:
                pass  # the journal must never break the radio

    def trace(self) -> None:
        """Where the aircraft is and who answers on the tuned frequency; the plan when it changes."""
        plan = self.engine.flight.plan.summary() if self.engine.flight.plan else None
        if plan != self._traced_plan:
            self._traced_plan = plan
            self.log.note("plan", plan=plan)
        context = self._context()
        if context is None:
            return
        context["raw"] = {key: value for key, value in context["raw"].items() if not key.startswith("tcas_")}
        mark = (context["tuned"], context["station"], context["raw"].get("lat"), context["raw"].get("lon"),
                context["raw"].get("elevation_m"), json.dumps(context["flight"], sort_keys=True, default=str))  # fmt: skip
        if mark != self._traced:  # a parked aircraft is not written every twenty seconds
            self._traced = mark
            self.log.note("trace", context)

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
            "raw": snapshot(raw),  # what a replay of this log feeds back to the engine
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
        try:
            self.tts = TextToSpeech(config.tts)
        except Exception as error:
            if config.tts.engine == "kokoro":
                raise
            # The trial engine could not be loaded (its model is fetched the first time): the usual one is used.
            emit({"type": "error", "text": f"The Phonon voice engine could not be loaded ({error}). Using Kokoro."})
            config.tts.engine = "kokoro"
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
            holding=config.atc.holding, traffic=config.atc.traffic, airspace=open_airspace(root), clock=SimClock(self.clock),
            airways=open_airways(root), real_atis=RealAtis() if config.atc.real_atis else None,
        )  # fmt: skip
        self.engine.clock = self.engine.now
        radio_db = AirportDB()  # the background thread's own connection
        radio_db.centers, radio_db.metars = self.centers, self.metars
        self.broadcaster = AtisBroadcaster(
            self.bridge, radio_db, self.tts, self.player, self.book, config, self.busy, self.engine, emit,
            after=lambda: self.jobs.put(("after", None, None)), clock=self.clock,
        )  # fmt: skip
        self.broadcaster.start()
        second_db = AirportDB()  # each background thread has its own connection
        second_db.centers, second_db.metars = self.centers, self.metars
        self.second_radio = SecondRadio(
            self.bridge, second_db, self.tts, Player(config.audio.output_device, alongside=True), self.book, config, emit,
            clock=self.clock,
        )  # fmt: skip
        self.second_radio.start()
        self.ptt = PushToTalk(config.ptt.key, on_down=self._key_down, on_up=self._key_up)
        self.ptt.start()
        self.computer = system_check.this_computer(self.ptt.listening())
        for note in system_check.startup_notes(self.computer):
            emit({"type": "info", "text": note})
        self.joystick = JoystickButtons(self._joystick_button, config.ptt.joystick, config.ptt.joystick_button)
        self.joystick.start()
        self.recorder = Recorder(config.audio.input_device, limit_s=MAX_TRANSMISSION_S)
        try:
            self.options.update(audio_options())
            self.options["voices"] = self.tts.voices()
        except Exception:
            pass  # the settings screen then offers no choices; everything else works
        self.ready = True
        self._start_journal()
        emit({"type": "status", "text": f"\nReady. Hold [{config.ptt.key}] to talk, release to send. Ctrl+C to quit.\n"})

    def _key_down(self) -> None:
        """The pilot keyed the microphone. From now nobody else talks, the microphone records, and the call is
        for whoever is on this frequency, even while the job that will answer it is still waiting its turn
        (the copilot may be tuning, or a reply still being spoken)."""
        with self._keying:
            self.busy.set()
        # The limit counts from now, when the microphone starts, not from when the radio gets to the call.
        timer = threading.Timer(MAX_TRANSMISSION_S, self._too_long, args=(None,))
        timer.args = (timer,)
        timer.daemon = True
        self._tx_timer = timer
        timer.start()
        self._keyed_khz = tuned_khz(self.bridge.snapshot()) if self.ready and self.bridge.connected else None
        recorder = getattr(self, "recorder", None)
        self._recording = False
        if recorder is not None:
            try:
                recorder.start()
                self._recording = True
            except Exception as error:  # the job tries again, and reports what is wrong
                self.emit({"type": "error", "text": f"Microphone: {error}"})
        self.jobs.put(("voice", getattr(self.ptt, "presses", None), None))  # for this press

    def _key_up(self) -> None:
        """The transmission ended: the microphone stops now, even while the job for the call is still waiting
        its turn. What it recorded waits for that job."""
        timer, self._tx_timer = self._tx_timer, None
        if timer is not None:
            timer.cancel()
        recorder = getattr(self, "recorder", None)
        if recorder is not None and hasattr(recorder, "halt"):
            try:
                recorder.halt()
            except Exception:
                pass  # the job's own stop reports a microphone that misbehaves

    def _microphone_again(self, press) -> bool:
        """The microphone did not start when the key went down (that was reported): try once more, for that same
        press while it is still held. Under the key's lock, so a release waits for this and then stops it."""
        with getattr(self.ptt, "lock", None) or nullcontext():
            if press is None or press != getattr(self.ptt, "presses", None) or not self.ptt.pressed.is_set():
                return False
            try:
                self.recorder.start()
                return True
            except Exception as error:
                self.emit({"type": "error", "text": f"Microphone: {error}"})
                return False

    def _too_long(self, timer) -> None:
        """A key left down (a window or a device gone, a key the system never let go): the transmission ends."""
        if self._tx_timer is not timer or not self.ptt.pressed.is_set():
            return
        self.ptt.release()  # and so _key_up: the microphone stops
        self.emit(
            {"type": "info", "text": f"transmission ended after {MAX_TRANSMISSION_S} seconds: release the key between calls"}
        )

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
        if "engine" in cleaned.get("tts", {}):
            self._use_voice_engine(cleaned["tts"]["engine"])
        if "input_device" in audio:
            self.recorder.device = audio["input_device"]
        if "output_device" in audio:
            self.player.device = audio["output_device"]
            self.second_radio.player.device = audio["output_device"]
        if "key" in ptt:
            self.ptt.set_key(ptt["key"])
        if "joystick" in ptt or "joystick_button" in ptt:
            self.joystick.bind(self.config.ptt.joystick, self.config.ptt.joystick_button)
        if "holding" in atc:
            self.engine.holding = atc["holding"]
        if "traffic" in atc:
            self.engine.traffic_enabled = atc["traffic"]
        if "real_atis" in atc and bool(atc["real_atis"]) != (self.engine.real_atis is not None):
            self.engine.real_atis = RealAtis() if atc["real_atis"] else None

    def _use_voice_engine(self, name: str) -> None:
        """Change the voice engine now. If the new one cannot be loaded, the one in use stays."""
        previous = self.tts.engine_name
        if name == previous:
            return
        self.emit({"type": "info", "text": f"Loading the {name.title()} voices (the first time, they are downloaded)..."})
        try:
            self.tts.use(name)
            self.tts.speak("Radio check.")
        except Exception as error:
            self.tts.use(previous)
            self.config.tts.engine = previous
            save_overrides({"tts": {"engine": previous}})
            self.emit({"type": "error", "text": f"The {name.title()} voices could not be loaded: {error}"})
            return
        self.options["voices"] = self.tts.voices()
        self.emit({"type": "info", "text": f"Voices: {name.title()}."})

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

    def tune(self, mhz: str, other: bool = False) -> None:
        """Set the COM radio selected for transmitting to this frequency, at the pilot's request; with
        `other`, the radio that is not: the one to listen on."""
        if not self.config.sim.allow_tuning:
            raise SimUnavailable("Tuning from the app is switched off (sim.allow_tuning in config.toml).")
        khz = int(round(float(mhz) * 1000))
        if not 118000 <= khz <= 136990:
            raise SimUnavailable(f"{mhz} is not a voice frequency.")
        radio = transmit_radio(self.bridge.snapshot())
        radio = 3 - radio if other else radio
        key = f"com{radio}_khz"
        self.bridge.write(key, khz)
        deadline = self.clock.monotonic() + TUNE_WAIT_S
        while True:  # looked at once more when the wait is over: a report that came just then is not a failure
            reported = self.bridge.snapshot().get(key)
            if same_channel(khz, reported):  # the sim may report the name of the 8.33 kHz channel it snapped to
                self.emit({"type": "info", "text": f"COM{radio} tuned to {int(reported) / 1000:.3f}"})
                return
            if self.clock.monotonic() >= deadline:
                break
            self.clock.sleep(0.1)
        raise SimUnavailable(
            f"The aircraft did not take {khz / 1000:.3f} on COM{radio}. Some add-on aircraft manage their own radios; tune it in the cockpit."
        )

    def submit(self, text: str, intent: Intent | None = None) -> None:
        """Queue a transmission that was typed, or clicked from the suggested calls (which carry their intent)."""
        self.jobs.put(("clicked" if intent else "typed", text, intent))

    def ask(self, what: str) -> None:
        """Queue a question for the instructor: 'explain' the last instruction, or 'what_next'."""
        self.jobs.put((what, None, None))

    def phrase(self, wanted: str) -> None:
        """Word what the pilot typed as the proper call. Answered with a "phrase" event; nothing is transmitted."""
        threading.Thread(target=self._phrase, args=(wanted,), name="phrase", daemon=True).start()

    def _phrase(self, wanted: str) -> None:
        result: dict = {"note": "Tune a controller's frequency first; then I can word your call."}
        try:
            if self.bridge.connected:
                raw, station, issue = self._station_and_issue()
                if station is not None and station.kind != "atis":
                    calls = self.engine.suggestions(station, issue, raw)
                    ask = None
                    if self.controller:
                        ask = lambda system, user: self.controller.complete(system, user, max_tokens=160, temperature=0.0)  # noqa: E731
                    said = self.engine.flight.last_said.get(station.key, "")
                    opening = self.engine.opening(station, raw)
                    result = phrasing.phrase(wanted, station.callsign, opening, calls, ask, said)
        except LlmError as error:
            result = {"note": str(error)}
        except Exception as error:
            result = {"note": f"That could not be worded: {type(error).__name__}: {error}"}
        self.emit({"type": "phrase", "said": wanted.strip()[:500], "text": result.get("text", ""),
                   "intent": result.get("intent"), "note": result.get("note", "")})  # fmt: skip

    def debrief(self) -> dict:
        """Review the radio work so far; with a language model, add the instructor's remarks."""
        result = review(self.log.entries())
        if self.controller and result["transmissions"] >= 5:  # too little to say anything useful about
            try:
                facts = {key: result[key] for key in ("minutes", "transmissions", "readbacks", "say_again", "stations")}
                facts["events"] = [f"{f['time']} {f['text']}" for f in result["findings"]][:30]
                result["notes"] = self.controller.complete(DEBRIEFER, json.dumps(facts), max_tokens=260, temperature=0.3)
            except LlmError:
                pass  # the counts stand on their own
        result["health"] = self.health()
        self.emit({"type": "debrief", **result})
        return result

    def health(self) -> list[dict]:
        """What went wrong on the radio that the pilot did not cause (health.py), also noted in the journal."""
        try:
            records = list(self.log.records)
            if self.log.journal is not None:
                records = load_log(self.log.journal)["events"]  # the trace is only on disk
            findings = check(records, self._locate if self.ready else None)
        except Exception as error:  # the report must never break the debrief
            self.emit({"type": "error", "text": f"health report: {type(error).__name__}: {error}"})
            return []
        self.log.note("health", findings=findings)
        return for_app(findings)

    def _locate(self, ident: str) -> tuple[float, float] | None:
        airport = self.db.airport(ident)
        return (airport.lat, airport.lon) if airport else None

    def save_log(self) -> None:
        extra = {"flight": self.engine.summary() if self.ready else None, "openrouter_usage": dict(self.usage),
                 "model": self.config.llm.model}  # fmt: skip
        if self.ready:
            extra["seed"] = self.engine.seed
        extra["debrief"] = review(self.log.entries())
        extra["debrief_text"] = "\n" + as_text(extra["debrief"])
        if self.ready and self.engine.flight.plan:
            extra["plan"] = self.engine.flight.plan.summary()
        path = self.log.save(PROJECT_DIR / "logs", extra)
        self.emit({"type": "log_saved", "path": str(path)})

    # ---- the sound check and the problem report -----------------------------

    def _sound_check_refused(self, busy: bool, running: bool) -> str | None:
        connected = self.ready and self.bridge.connected
        state = self.bridge.snapshot() if connected else {}
        if busy and not self.busy.is_set() and connected:
            # The ATIS repeating on the tuned frequency is not someone talking: the check may cut it off.
            _, station = current_station(self.db, state)
            busy = station is not None and station.kind != "atis"
        return soundcheck.refusal(connected, state, busy, running)

    def start_sound_check(self) -> None:
        """Queue the sound check, unless it would talk over a flight. Only ever started by the person."""
        reason = self._sound_check_refused(self.busy.is_set() or getattr(self.player, "playing", False), self._checking)
        if reason:
            self.emit({"type": "sound_check", "state": "refused", "text": reason})
            return
        self._checking = True
        while not self._answers.empty():
            self._answers.get_nowait()  # an answer left from a check that was stopped
        self.jobs.put(("sound_check", None, None))

    def answer_sound_check(self, heard: bool | None) -> None:
        """The person's yes or no; None stops the check."""
        if self._checking:
            self._answers.put(heard)

    def _ask(self, step: int, total: int, title: str, shown: str, question: str) -> bool | None:
        moving = self._sound_check_refused(busy=False, running=False)
        if moving:  # the aircraft began to move during the check: stop before the next sound
            self.emit({"type": "info", "text": f"Sound check stopped. {moving}"})
            return None
        self.emit({"type": "sound_check", "state": "question", "step": step, "total": total, "title": title,
                   "text": shown, "question": question})  # fmt: skip
        try:
            return self._answers.get(timeout=soundcheck.ANSWER_WAIT_S)
        except queue.Empty:
            return None

    def _tell(self, step: int, total: int, title: str, text: str) -> None:
        self.emit({"type": "sound_check", "state": "running", "step": step, "total": total, "title": title, "text": text})

    def _sound_check(self) -> None:
        """Run the check (from the job queue, so nothing else talks meanwhile) and record what it found."""
        try:
            reason = self._sound_check_refused(busy=False, running=False)  # the aircraft may have moved since
            if reason:
                self.emit({"type": "sound_check", "state": "refused", "text": reason})
                return
            self.second_radio.hold.set()
            self.second_radio.player.stop()
            kit = soundcheck.Kit(self.tts, self.player, self.second_radio, self.recorder, self.config,
                                 lambda: soundcheck.load_engines(self.tts, self.config.tts))  # fmt: skip
            outcome = soundcheck.run(kit, self._ask, self._tell)
        except Exception as error:
            self.emit(
                {"type": "sound_check", "state": "done", "verdict": f"The sound check failed: {type(error).__name__}: {error}"}
            )
            return
        finally:
            self.second_radio.hold.clear()
            self._checking = False
        self.log.note("sound_check", result=outcome)
        soundcheck.keep(outcome, PROJECT_DIR / "logs")
        self.last_sound_check = outcome["verdict"]
        self.emit({"type": "sound_check", "state": "done", "verdict": outcome["verdict"],
                   "steps": [{"title": step["title"], "text": step["shown"], "answer": step["answer"],
                              "problems": step["problems"]} for step in outcome["steps"]]})  # fmt: skip

    def write_report(self, hide: bool) -> None:
        """Write the problem report and tell the window where it is."""
        computer = self.computer or system_check.this_computer(None)
        path = problem_report.write(PROJECT_DIR, self.config, hide, computer)
        self.emit({"type": "report_saved", "path": str(path), "issue": problem_report.ISSUE_URL})

    def run_forever(self) -> None:
        try:
            while True:
                kind, text, intent = self.jobs.get()
                if kind == "copilot" and not self._wait_for_quiet():
                    continue  # the pilot keyed the microphone or made a call meanwhile: that comes first
                if kind != "after":
                    self.busy.set()
                    self.player.stop()
                    try:
                        if kind == "voice":
                            recording, self._recording = self._recording, False  # a key going down again sets it anew
                            if recording or self._microphone_again(text):
                                _exchange(self.config, self.bridge, self.db, self.book, self.stt, self.tts,
                                          self.controller, self.player, self.ptt, self.recorder, self.engine,
                                          self.parser, self.emit, on_khz=self._keyed_khz, recording=True)  # fmt: skip
                        elif kind in ("explain", "what_next"):
                            self._instructor(kind)
                        elif kind == "debrief":
                            self.debrief()
                        elif kind == "sound_check":
                            self._sound_check()
                        elif kind == "copilot":
                            self._copilot_speaks()
                        else:
                            self._say(text, kind, intent)
                    except Exception as error:  # one bad exchange must not end the session
                        self.emit({"type": "error", "text": f"{type(error).__name__}: {error}"})
                    finally:
                        with self._keying:  # so that a key going down now is not undone
                            if not self.ptt.pressed.is_set():  # keyed again meanwhile: still nobody else talks
                                self.busy.clear()
                try:
                    self._after_transmission()
                except Exception as error:
                    self.emit({"type": "error", "text": f"copilot: {type(error).__name__}: {error}"})
        except KeyboardInterrupt:
            self.emit({"type": "status", "text": "\nBye."})
        finally:
            self.close()

    def _say(self, text: str, kind: str, intent: Intent | None) -> bool:
        """A typed, clicked or copilot call: spoken in a pilot voice, then answered by whoever was listening.
        True if a station heard it."""
        on_khz = tuned_khz(self.bridge.snapshot()) if self.bridge.connected else None
        self._spoken_for_you(text)
        return _respond(text, kind, intent, time.perf_counter(), self.config, self.bridge, self.db, self.book, self.tts,
                        self.controller, self.player, self.ptt, self.engine, self.parser, self.emit, on_khz=on_khz)  # fmt: skip

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

    # The copilot looks at the flight afresh each time something happens (a call was made, a controller has
    # spoken, the tuned frequency has changed) and does the one thing that is due, in this order:
    #   1. read back the instruction the controller on frequency is waiting for;
    #   2. tune the hand-off to follow (the engine keeps the latest one: flight.next_frequency);
    #   3. check in on that frequency.
    # Its calls are decided again when it is about to speak: by then a controller may have said something new.

    def _after_transmission(self) -> None:
        """What the copilot does once something has happened on the radio."""
        if not self.ready or not self.bridge.connected or not self.jobs.empty():
            return
        call = self._copilot_call()
        if call is not None:
            self.jobs.put(("copilot", call["text"], Intent.from_dict(call["intent"])))
            return
        self._follow_handoff()

    def _copilot_call(self) -> dict | None:
        """The call the copilot would make now: the readback the controller on frequency is waiting for, or (in
        full) the check-in on the frequency the flight was handed to. None when there is nothing to say."""
        mode, flight = self.config.atc.copilot, self.engine.flight
        if mode == "off":
            return None
        raw, station, issue = self._station_and_issue()
        if station is None or station.kind == "atis":
            return None
        with self.engine.lock:
            pending, reminded, handoff = flight.pending, flight.reminded, flight.next_frequency
            waiting = self.engine.waiting_on(station, raw)
        if waiting:
            if self._read_back == (pending, reminded):
                return None  # read back already; a controller who says it again is read back again
            return self._suggested("Read back", station, issue, raw)
        if mode == "full" and handoff and _same_frequency(handoff, tuned_khz(raw)):
            return self._suggested("Check in", station, issue, raw)
        return None

    def _suggested(self, label: str, station, issue, raw) -> dict | None:
        return next((call for call in self.engine.suggestions(station, issue, raw) if call["label"] == label), None)

    def _follow_handoff(self) -> None:
        """Tune the hand-off to follow, once for each hand-off given. Not before the controller on frequency has
        had the readback it is waiting for; but a hand-off from a controller no longer tuned is followed at once."""
        mode, flight = self.config.atc.copilot, self.engine.flight
        if not (self.config.sim.allow_tuning and (self.config.sim.auto_tune or mode == "full")):
            return
        raw, station, _ = self._station_and_issue()
        with self.engine.lock:
            handoff, pending, given = flight.next_frequency, flight.pending, flight.handoffs
            # A hand-off the controller has decided and not said yet is not followed: nobody has heard it.
            waiting = (station is not None and self.engine.waiting_on(station, raw)) or self.engine.not_yet_said()
        if not handoff or _same_frequency(handoff, tuned_khz(raw)) or self._followed == (handoff, given):
            return
        if waiting or (pending is not None and mode != "full"):
            # Read back first. Only the full copilot, which has heard the hand-off, follows one at once that a
            # controller no longer tuned gave: auto-tune alone waits for the pilot's readback, wherever the pilot
            # has since turned the knob.
            return
        if self._keyed():
            return  # not under the pilot's call: looked at again once it is over
        self._followed = (handoff, given)  # tried once: a failure is reported once, and the pilot is not fought
        try:
            self.tune(handoff)
        except (SimUnavailable, ValueError) as error:
            self.emit({"type": "error", "text": str(error)})
            return
        if mode == "full":
            self.clock.sleep(1.5)  # a moment on the new frequency before calling
            self.jobs.put(("after", None, None))  # then look again: something may have been said meanwhile

    def _keyed(self) -> bool:
        ptt = getattr(self, "ptt", None)  # none before load()
        return ptt is not None and ptt.pressed.is_set()

    def _wait_for_quiet(self, longest: float = 30.0) -> bool:
        """The copilot does not talk over a controller or another aircraft. False if the pilot came first."""
        deadline = self.clock.monotonic() + longest
        while getattr(self.player, "playing", False) and self.clock.monotonic() < deadline:
            if not self.jobs.empty() or self._keyed():
                return False
            self.clock.sleep(0.1)
        return self.jobs.empty() and not self._keyed()

    def _copilot_speaks(self) -> None:
        """Make the call that is due now, which may not be the one that was queued."""
        call = self._copilot_call()
        if call is None:
            return
        with self.engine.lock:
            instruction = (self.engine.flight.pending, self.engine.flight.reminded)
        heard = self._say(call["text"], "copilot", Intent.from_dict(call["intent"]))
        if call["label"] != "Read back":
            return
        # A readback nobody heard (the radio was retuned during it) is tried once more, when the controller who
        # is waiting for it is on frequency again; after that, when the controller says it again.
        if heard or self._missed == instruction:
            self._read_back = instruction
        else:
            self._missed = instruction

    def _instructor(self, kind: str) -> None:
        """'What does that mean?' and 'what do I say now?', answered in the log, not on the radio."""
        if kind == "what_next":
            calls, asked = [], None
            if self.bridge.connected:
                raw, station, issue = self._station_and_issue()
                if station is not None and station.kind != "atis":
                    calls = what_to_say(self.engine.suggestions(station, issue, raw))
                    asked = questions.open_question(self.engine, station)
            if calls:
                text = calls[0]["text"].replace("{destination}", "your destination")
                if asked and calls[0].get("answers"):
                    said = f"The controller asked: “{asked.text}”. You could answer: “{text}”"
                    self.emit({"type": "coach", "text": said})
                else:
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
        self.second_radio.stop()
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
              emit: Emit = console, on_khz: int | None = None, recording: bool = False) -> None:  # fmt: skip
    """One voice transmission: record while the key is held, transcribe, answer. `on_khz`: the frequency the
    key was pressed on; `recording`: the microphone has recorded since then. Both when the key went down
    before this job began."""
    if on_khz is None and bridge.connected:
        on_khz = tuned_khz(bridge.snapshot())
    if not recording:
        recorder.start()
    emit({"type": "tx", "on": True})
    if not ptt.released.wait(MAX_TRANSMISSION_S):
        # A key left down (a window or a device gone, a key the system never let go): the microphone is not left
        # recording, and the radio is not kept waiting.
        ptt.release()
        emit({"type": "info", "text": f"transmission ended after {MAX_TRANSMISSION_S} seconds: release the key between calls"})
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
    _respond(transcript, "voice", None, started, config, bridge, db, book, tts, controller, player, ptt, engine, parser, emit,
             on_khz=on_khz)  # fmt: skip


# What a free-form reply must not say: an instruction, or a permission in any words ("you're good to land",
# "you may depart", "affirmative"). Nothing else checks a reply the model makes up, so this errs on refusing:
# refused, the pilot hears "unable, say again" and a way to ask that the rules engine answers.
INSTRUCTION = re.compile(
    r"\b(cleared|clearance|climb|descend|turn (left|right)|fly heading|heading \w+ \w+ \w+|taxi|cross|line up|take ?off|"
    r"cleared to land|contact|squawk|hold|maintain|approved|proceed|direct|vectors?|reduce speed|"
    r"increase speed|expect|push ?back|start ?up|go around|enter|join|report|"
    r"land|depart|wait|stop|expedite|slow|speed|remain|stay|leave|resume|vacate|follow|orbit|extend|monitor|"
    r"change to|switch to|set|make (a |an )?(left|right|three sixty|360|orbit|short)|number (one|two|three|four|\d)|"
    r"go ahead|permission|authori[sz]ed|granted|affirm(ative)?|you('| a)re (good|ok|okay|fine|free|clear|set|able|all set)|"
    r"you (may|can|should|must|need to|will|are to)|use runway|assigned)\b",
    re.I,
)


def informational(text: str) -> bool:
    """Whether a free-form reply only informs. Anything that reads like a clearance or an instruction is refused."""
    return not INSTRUCTION.search(text)


def _fields(intent: Intent) -> dict:
    """An intent as plain data, without what was not said: what the flight log keeps, to replay the call."""
    return {key: value for key, value in asdict(intent).items() if value is not None and value is not False}


def known_names(engine) -> list[str]:
    """Identifiers this flight is likely to mention: its airports, its route's waypoints, its procedures."""
    flight, plan = engine.flight, engine.flight.plan
    names: list[str] = []
    if plan:
        names += [name for name in (plan.origin, plan.destination, plan.alternate) if name]
        names += route_fixes(plan.route)
        names += [name for name, _, _ in engine.route()]
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


def _same_frequency(mhz: str, khz: int | None) -> bool:
    return khz is not None and same_channel(int(round(float(mhz) * 1000)), khz)


def _same_khz(one: int | None, other: int | None) -> bool:
    """Within 5 kHz: a sim may report 118.425 for the 118.420 that was written, or the other way round."""
    return one is not None and other is not None and abs(one - other) <= 5


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
             engine=None, parser=None, emit: Emit = console, on_khz: int | None = None) -> bool:  # fmt: skip
    """Answer one pilot transmission, whatever way it arrived. `on_khz`: the frequency it was begun on.
    True if a station heard it (whether or not it answered)."""
    if on_khz is not None and bridge.connected and not _same_khz(tuned_khz(bridge.snapshot()), on_khz):
        # Retuned in the middle of the call: the station it was meant for heard half of it, the new one the rest.
        emit({"type": "info", "text": "the frequency was changed during the call: nobody heard all of it"})
        return False
    if source != "voice" and not _someone_listening(bridge, db, emit):
        return False
    raw = bridge.snapshot() if bridge.connected else {}
    context = dict(describe(raw))
    station_key = "free"
    station = issue = None
    if raw:
        _, station = current_station(db, raw)
        if station is None or station.kind == "atis":
            return False  # retuned while transmitting
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
        return True
    thought = time.perf_counter()
    if not answer:
        note = engine.flight.last_note if engine is not None else ""
        emit({"type": "silent", "text": note} if note else {"type": "silent"})
        return True
    label = context["station"]["you_are"] if "station" in context else "ATC"
    event = {"type": "atc", "station": label, "text": answer, "unprompted": False}
    if context.get("fallback"):
        event["fallback"] = True  # the rules engine had no answer: the health report counts these (health.py)
    if engine is not None and engine.flight.last_event:
        event["event"] = engine.flight.last_event
        engine.flight.last_event = ""
    emit(event)
    if context.get("way_out"):
        emit({"type": "coach", "text": f"The controller has no instruction for that. Here you could say: “{context['way_out']}”"})

    def timings() -> None:  # when the first sound is ready to play
        voiced = time.perf_counter()
        emit({"type": "timings", "listen": heard - started, "think": thought - heard, "voice": voiced - thought,
              "total": voiced - started})  # fmt: skip

    ticket = transmit(tts, player, answer, station_voice(tts, station), config.radio, timings)
    player.wait(ticket, should_stop=ptt.pressed.is_set)  # pressing the key again cuts the controller off
    return True


def _second_reading(transcript, station_key, station, issue, raw, controller, engine, emit: Emit):
    """A call the first reading could not place, read again with the whole conversation and the flight's state
    (resolver.py). The engine still decides the answer: the reading only says what the pilot asked for."""
    history = controller.histories.get(station_key, [])
    try:
        about = resolver.situation(engine, station, issue, raw, history)
    except Exception:  # without the situation there is nothing to read the call against: the free answer takes it
        log.exception("could not describe the situation for the second reading")
        return None
    reading = resolver.read(controller, about, transcript)
    if reading.intent is None:
        if reading.why:
            emit({"type": "coach", "text": f"The controller could not tell what you meant ({reading.why})."})
        return None
    # A second "understood" for the same call: what it was taken to mean, and how sure the reading was. A replay
    # of the journal repeats it the same way (replay.py).
    emit({"type": "understood", "intent": reading.intent.intent, "fields": _fields(reading.intent), "resolved": reading.decision})
    if reading.decision == "confirm":
        asked = engine.confirm_request(reading.intent, station, raw)
        if asked is not None or reading.intent.intent not in resolver.HARMLESS:
            return asked  # a doubtful call that cannot be asked about is not made: the free answer takes it
    return engine.handle(reading.intent, station, issue, raw, magnetic_variation(raw))


def _answer(transcript, intent, context, station_key, station, issue, raw, controller, engine, parser, emit: Emit) -> str:
    """The controller's words: from the rules engine when it covers the request, else a free-form reply."""
    # A clicked call carries its own intent: it needs no language model at all.
    if station and engine and (intent is not None or parser):
        if intent is None:
            # The first reading is given the situation too: what the flight is doing, the controller's open question,
            # the last few exchanges. "We'll head back" after a mayday is a return, not a request for vectors.
            history = controller.histories.get(station_key, []) if controller is not None else []
            try:
                about = resolver.brief(resolver.situation(engine, station, issue, raw, history))
            except Exception:  # the context is a help, not a condition: the call is still read without it
                log.exception("could not describe the situation for the first reading")
                about = ""
            intent = parser.parse(transcript, engine.flight.last_said.get(station_key, ""), known_names(engine), about)
        emit({"type": "understood", "intent": intent.intent, "fields": _fields(intent)})
        reply = engine.handle(intent, station, issue, raw, magnetic_variation(raw))
        if reply is None and intent.intent == "other" and controller is not None:
            reply = _second_reading(transcript, station_key, station, issue, raw, controller, engine, emit)
        engine.flight.last_note = reply.note if reply is not None else ""
        engine.flight.last_event = reply.event if reply is not None else ""
        if reply is not None:
            if controller:
                controller.remember(station_key, transcript, reply.text)
            return reply.text
        context["flight"] = engine.summary()
    if not controller:
        return f"Readback check. {transcript}"
    if station and engine:
        # The rules engine has no answer. The model may inform or decline; it may not clear or instruct,
        # because nothing it says would be known to the engine or checked against the airport's data.
        text = controller.reply(transcript, context, station_key, inform_only=True)
        context["fallback"] = True  # set after the model has read the context: it is for the journal
        # A controller without an instruction to give is a dead end for the pilot: say what would work here.
        calls = what_to_say(engine.suggestions(station, issue, raw))
        if calls:
            context["way_out"] = calls[0]["text"].replace("{destination}", "your destination")
        return text if informational(text) else f"{engine._callsign(raw)}, unable, say again your request"
    return controller.reply(transcript, context, station_key)
