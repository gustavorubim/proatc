"""The sound check: a short guided test of what only a person can judge, with what the machine can measure.

Some things have never been heard by anyone: two radios sounding at once on a given sound card, the
Phonon voice on a slow processor, the Mac and Linux builds. The check plays each in turn and asks the
person a yes/no question after each. Next to every answer it records what can be measured without
ears: whether each output stream opened, how much sound was handed to the sound card, the underruns
PortAudio reported, the device's name, and whether speech was made faster than it is spoken.

The result goes into the journal as a `sound_check` note and into `logs/sound-check.json`, which a
problem report includes. The check never starts by itself, and it is refused while it would talk over
a flight: it runs with the sim not connected, or with the aircraft parked on the ground.
"""

from __future__ import annotations

import json
import math
import platform
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from .audio_io import MIC_RATE, Meter, device_name, play_underruns
from .radio import SECOND_RADIO_GAIN, transmit
from .radio_fx import radio_effect

SENTENCE = "Radio check. This is the Pro ATC sound check. How do you read?"
ATIS_TEXT = (
    "Testfield information Alpha, one two zero zero zulu. Wind two seven zero degrees one zero knots, "
    "visibility one zero kilometres, Q N H one zero one three. Advise you have information Alpha."
)
MIC_SECONDS = 2.0
MIC_HEARD = 0.01  # the peak below which the radio loop, too, calls the microphone silent
PARKED_MS = 0.5  # ground speed in metres a second (about one knot) under which the aircraft is parked
LATE_S = 0.05  # a piece of speech this much later than the sound before it ran out is heard as a break
RESULT_FILE = "sound-check.json"
ANSWER_WAIT_S = 300  # a question nobody answers for five minutes ends the check

Ask = Callable[[int, int, str, str, str], "bool | None"]  # (step, total, title, measured, question) -> answer
Tell = Callable[[int, int, str, str], None]  # (step, total, title, what is happening)


def refusal(connected: bool, state: dict, busy: bool, running: bool = False) -> str | None:
    """Why the sound check may not start now, or None. It must never talk over a flight."""
    if running:
        return "A sound check is already running."
    if busy:
        return "A controller is talking. Try again when the radio is quiet."
    if not connected:
        return None
    parked = state.get("on_ground", False) and abs(state.get("groundspeed_ms", 0.0)) < PARKED_MS
    if not parked:
        return "The sound check would talk over your flight. Run it parked on the ground, or with X-Plane closed."
    return None


class Timed:
    """Stands between a voice engine and the radio, noting how long each piece of speech took to make."""

    def __init__(self, tts, clock=time.perf_counter):
        self.tts, self.clock = tts, clock
        self.arrivals: list[tuple[float, float]] = []  # (when it was ready, seconds of sound in it)
        self.making = 0.0  # seconds spent waiting for the engine

    @property
    def streams(self) -> bool:
        return bool(getattr(self.tts, "streams", False))

    def stream(self, text: str, voice: str | None = None, speed: float | None = None):
        pieces, rate = self.tts.stream(text, voice, speed)

        def noted():
            try:
                while True:
                    asked = self.clock()
                    piece = next(pieces, None)
                    if piece is None:
                        return
                    self._arrived(asked, len(piece) / rate)
                    yield piece
            finally:
                close = getattr(pieces, "close", None)
                if close:
                    close()  # a cut-off stops the engine, as it does without the check

        return noted(), rate

    def speak(self, text: str, voice: str | None = None, speed: float | None = None):
        asked = self.clock()
        samples, rate = self.tts.speak(text, voice, speed)
        self._arrived(asked, len(samples) / rate)
        return samples, rate

    def _arrived(self, asked: float, seconds: float) -> None:
        now = self.clock()
        self.making += now - asked
        self.arrivals.append((now, seconds))

    def pace(self, started: float, first_sound: float | None) -> dict:
        """When the first sound came, and whether speech was made faster than it is spoken."""
        sound = sum(seconds for _, seconds in self.arrivals)
        breaks, played = 0, 0.0
        for at, seconds in self.arrivals:
            # Playback starts with the first piece; a piece that comes after the sound before it ran out is a break.
            if self.arrivals and at - self.arrivals[0][0] > played + LATE_S:
                breaks += 1
            played += seconds
        speed = round(sound / self.making, 1) if self.making > 0 else None
        return {
            "first_sound_s": round(first_sound - started, 2) if first_sound is not None else None,
            "sound_s": round(sound, 2),
            "making_s": round(self.making, 2),
            "times_faster": speed,
            "breaks": breaks,
            "faster_than_real_time": bool(sound) and breaks == 0 and (speed is None or speed >= 1.0),
        }


@dataclass
class Kit:
    """What the sound check plays and records with: the app's own sound parts, or the terminal's."""

    tts: object  # the TextToSpeech in use
    player: object  # the first radio's Player
    second: object  # the SecondRadio: its player plays alongside the first
    recorder: object
    config: object
    engines: Callable[[], list[tuple[str, object]]]  # (name, a TextToSpeech, or why it could not be loaded)
    clock: Callable[[], float] = time.perf_counter
    sleep: Callable[[float], None] = time.sleep


def meter_text(meter: Meter) -> dict:
    return {"opened": meter.opened, "frames": meter.frames, "underruns": meter.underruns, "failed": meter.failed}


def controller(kit: Kit, tts) -> dict:
    """Say the sentence on the first radio, the way a controller's reply is said, and measure it."""
    meter, timed = Meter(), Timed(tts, kit.clock)
    kit.player.meter = meter
    first_sound: list[float] = []
    started = kit.clock()
    try:
        ticket = transmit(timed, kit.player, SENTENCE, {}, kit.config.radio, lambda: first_sound.append(kit.clock()))
        kit.player.wait(ticket)
    except Exception as error:
        meter.failed = f"{type(error).__name__}: {error}"
    finally:
        kit.player.meter = None
    if not timed.streams:
        meter.underruns += play_underruns()  # a whole sentence goes through sd.play, which reports it there
    device = getattr(kit.player, "device", None)
    return {
        "engine": getattr(tts, "engine_name", "unknown"),
        "device": device_name(device, "output"),
        **meter_text(meter),
        **timed.pace(started, first_sound[0] if first_sound else None),
    }


def both_radios(kit: Kit) -> dict:
    """An ATIS on the second radio and, over it, the controller on the first: two output streams at once."""
    settings = kit.config.tts
    atis, rate = kit.tts.speak(ATIS_TEXT, voice=settings.atis_voice, speed=settings.atis_speed)
    if kit.config.radio.enabled:
        atis = radio_effect(atis, rate, kit.config.radio.noise)
    second = Meter()
    kit.second.player.meter = second
    try:
        ticket = kit.second.broadcast(atis, rate, SECOND_RADIO_GAIN)
        first = controller(kit, kit.tts)
        kit.second.player.wait(ticket)
    except Exception as error:
        second.failed = second.failed or f"{type(error).__name__}: {error}"
        first = {"failed": "not played", "opened": 0, "frames": 0, "underruns": 0}
    finally:
        kit.second.player.meter = None
    return {"first": first, "second": {"sound_s": round(len(atis) / rate, 2), **meter_text(second)}}


def microphone(kit: Kit) -> dict:
    """Record a couple of seconds and say how loud it was."""
    try:
        kit.recorder.start()
        kit.sleep(MIC_SECONDS)
        audio = np.asarray(kit.recorder.stop(), dtype=np.float32)
    except Exception as error:
        return {"device": device_name(kit.recorder.device, "input"), "failed": f"{type(error).__name__}: {error}",
                "seconds": 0.0, "peak": 0.0, "level_db": None, "heard": False}  # fmt: skip
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    rms = float(np.sqrt(np.mean(audio**2))) if audio.size else 0.0
    return {
        "device": device_name(kit.recorder.device, "input"),
        "failed": "",
        "seconds": round(audio.size / MIC_RATE, 2),
        "peak": round(peak, 3),
        "level_db": round(20 * math.log10(rms), 1) if rms > 0 else None,
        "heard": peak >= MIC_HEARD,
    }


# ---- what the window shows, and what counts as a problem ---------------------


def played_text(measured: dict) -> str:
    if measured.get("failed") and not measured.get("frames"):
        return f"The sound card did not play it ({measured['failed']})."
    breaks = measured.get("underruns", 0)
    text = f"Played on {measured.get('device', 'the speakers')}: " + (
        "the sound card reported no gaps." if not breaks else f"the sound card ran out of sound {breaks} times."
    )
    if measured.get("first_sound_s") is not None:
        text += f" First sound {measured['first_sound_s']:.2f} s after the reply was known"
        if measured.get("times_faster"):
            text += f"; speech made {measured['times_faster']}× faster than it is spoken"
        text += "." if measured.get("faster_than_real_time") else ", too slowly to keep up: expect breaks."
    return text


def both_text(measured: dict) -> str:
    second = measured["second"]
    if second.get("failed") and not second.get("frames"):
        tail = f"The second radio's sound did not play ({second['failed']})."
    else:
        gaps = second.get("underruns", 0)
        tail = f"Second radio: {second['sound_s']:.1f} s of sound, " + ("no gaps." if not gaps else f"{gaps} gaps.")
    return f"{played_text(measured['first'])} {tail}"


def microphone_text(measured: dict) -> str:
    if measured.get("failed"):
        return f"The microphone could not be opened ({measured['failed']})."
    level = f"{measured['level_db']:.0f} dB" if measured.get("level_db") is not None else "silence"
    heard = "It heard you." if measured["heard"] else "It heard nothing."
    return f"Recorded {measured['seconds']:.1f} s from {measured['device']}: loudest {measured['peak']:.2f}, average {level}. {heard}"


def played_ok(measured: dict) -> bool:
    return measured.get("opened", 0) > 0 and measured.get("frames", 0) > 0 and not measured.get("failed")


def problems(step: str, measured: dict, answer: bool | None, name: str = "") -> list[str]:
    """What went wrong in one step, in plain words. Empty when it went right."""
    found: list[str] = []
    if step == "first_radio":
        if not played_ok(measured):
            found.append("the controller's sound did not play")
        elif answer is False:
            found.append("the controller was not heard")
        if measured.get("underruns"):
            found.append("the controller's sound had gaps")
    elif step == "both_radios":
        if not played_ok(measured["first"]) or not played_ok(measured["second"]):
            found.append("the two radios could not play at the same time")
        elif answer is False:
            found.append("the two radios were not heard together")
        if measured["first"].get("underruns") or measured["second"].get("underruns"):
            found.append("the two radios had gaps")
    elif step == "voice":
        if "loaded" in measured:
            return []  # not installed here: nothing to hear, nothing wrong
        if not played_ok(measured):
            found.append(f"the {name} voice did not play")
        elif answer is False:
            found.append(f"the {name} voice was not clear")
        if played_ok(measured) and not measured.get("faster_than_real_time"):
            found.append(f"this computer makes the {name} voice more slowly than it is spoken")
        elif measured.get("underruns"):
            found.append(f"the {name} voice had gaps")
    elif step == "microphone":
        if measured.get("failed"):
            found.append("the microphone could not be opened")
        elif not measured["heard"]:
            found.append("the microphone heard nothing")
        elif answer is False:
            found.append("the microphone heard something, but not you")
    return found


def verdict(steps: list[dict], total: int) -> str:
    """One line for the window."""
    if len(steps) < total or any(step["answer"] is None and step["question"] for step in steps):
        done = sum(1 for step in steps if step["answer"] is not None or not step["question"])
        return f"Sound check stopped after {done} of {total} steps."
    found = [problem for step in steps for problem in step["problems"]]
    if not found:
        return "Sound check passed: everything played, was heard, and the sound card reported no gaps."
    return f"Sound check found {len(found)} problem{'s' if len(found) > 1 else ''}: {'; '.join(found)}."


# ---- the sequence --------------------------------------------------------------


def run(kit: Kit, ask: Ask, tell: Tell) -> dict:
    """Play every step, ask after each, and return the result to record. Stops at a question left unanswered."""
    tell(0, 0, "Getting ready", "Loading the voice engines. The first time, the Phonon voices are downloaded.")
    engines = kit.engines()
    total = 3 + len(engines)
    steps: list[dict] = []

    def record(step_id, title, question, measured, shown, answer, name=""):
        steps.append({"id": step_id, "title": title, "question": question, "answer": answer, "measured": measured,
                      "shown": shown, "problems": problems(step_id, measured, answer, name)})  # fmt: skip
        return answer is not None or not question

    title = "The first radio"
    tell(1, total, title, "A controller is speaking on the first radio…")
    measured = controller(kit, kit.tts)
    question = "Did you hear the controller ask “how do you read”?"
    shown = played_text(measured)
    if not record("first_radio", title, question, measured, shown, ask(1, total, title, shown, question)):
        return result(steps, total)

    title = "Two radios at once"
    tell(2, total, title, "A weather recording on the second radio, and the controller over it…")
    measured = both_radios(kit)
    question = "Did you hear the controller and, more quietly underneath, the weather recording at the same time?"
    shown = both_text(measured)
    if not record("both_radios", title, question, measured, shown, ask(2, total, title, shown, question)):
        return result(steps, total)

    for number, (name, tts) in enumerate(engines, start=3):
        title = f"The {name.title()} voice"
        if isinstance(tts, str):
            measured = {"engine": name, "loaded": False, "why": tts}
            record("voice", title, "", measured, f"Not available on this computer ({tts}).", None, name.title())
            continue
        tell(number, total, title, f"The controller in the {name.title()} voice…")
        measured = controller(kit, tts)
        question = f"Did the {name.title()} voice sound clear, without breaks or stutters?"
        shown = played_text(measured)
        if not record("voice", title, question, measured, shown, ask(number, total, title, shown, question), name.title()):
            return result(steps, total)

    title = "The microphone"
    tell(total, total, title, f"Say a few words now: recording for {MIC_SECONDS:.0f} seconds…")
    measured = microphone(kit)
    question = "Did you say something while it was recording?"
    shown = microphone_text(measured)
    record("microphone", title, question, measured, shown, ask(total, total, title, shown, question))
    return result(steps, total)


def result(steps: list[dict], total: int) -> dict:
    """The outcome as it is written to the journal and to the problem report. No names, no positions."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        release = version("proatc")
    except PackageNotFoundError:
        release = "source"
    return {
        "when": datetime.now().isoformat(timespec="seconds"),
        "version": release,
        "system": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "finished": len(steps) == total and all(step["answer"] is not None or not step["question"] for step in steps),
        "verdict": verdict(steps, total),
        "steps": steps,
    }


def keep(outcome: dict, folder: Path) -> Path | None:
    """Keep the last result next to the journals, for the problem report."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / RESULT_FILE
        path.write_text(json.dumps(outcome, indent=1, default=str), encoding="utf-8")
        return path
    except OSError:
        return None


def last(folder: Path) -> dict | None:
    try:
        return json.loads((folder / RESULT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_engines(current, settings, load=None) -> list[tuple[str, object]]:
    """Every voice engine: the one in use, and the others if they load (Phonon may not be installed)."""
    from .tts import ENGINES, TextToSpeech

    load = load or TextToSpeech._load
    found: list[tuple[str, object]] = []
    for name in ENGINES:
        if name == current.engine_name:
            found.append((name, current))
            continue
        try:
            other = TextToSpeech(settings, backend=load(name))
            other.speak("Radio check.")  # the first sentence is several seconds slower: it is not the one measured
            found.append((name, other))
        except Exception as error:
            found.append((name, f"{type(error).__name__}: {error}"))
    return found


# ---- from the terminal: `proatc sound-check` ------------------------------------


def run_in_terminal(config, state: dict, ask_person=input) -> int:
    """The same check, with questions asked in the terminal. Writes a journal of its own."""
    from .audio_io import Player, Recorder
    from .config import PROJECT_DIR
    from .flightlog import FlightLog
    from .radio import SecondRadio
    from .tts import TextToSpeech

    reason = refusal(bool(state), state, busy=False)
    if reason:
        print(reason)
        return 1
    tts = TextToSpeech(config.tts)
    tts.speak("Radio check.")
    player = Player(config.audio.output_device)
    second = SecondRadio(None, None, tts, Player(config.audio.output_device, alongside=True), None, config)
    kit = Kit(tts, player, second, Recorder(config.audio.input_device), config, lambda: load_engines(tts, config.tts))

    def tell(step, total, title, text):
        print(f"\n[{step}/{total}] {title}: {text}" if total else text, flush=True)

    def ask(step, total, title, shown, question):
        print(f"  {shown}")
        while True:
            try:
                said = ask_person(f"  {question} [y/n] ").strip().lower()
            except EOFError:
                return None
            if said[:1] in ("y", "n"):
                return said[:1] == "y"

    outcome = run(kit, ask, tell)
    log = FlightLog()
    if log.keep_journal(PROJECT_DIR / "logs" / "journal"):
        log.note("session", version=outcome["version"], voice_engine=tts.engine_name, command="sound-check")
        log.note("sound_check", result=outcome)
    keep(outcome, PROJECT_DIR / "logs")
    print(f"\n{outcome['verdict']}")
    return 0 if outcome["finished"] and "passed" in outcome["verdict"] else 1
