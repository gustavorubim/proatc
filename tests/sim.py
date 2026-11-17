"""The radio's threads run together, in simulated time, against stand-ins for everything outside the program.

Unit tests call the engine one step at a time. In the app three things run at once: the session's job loop
(the pilot's calls and the copilot), the ATIS broadcaster (which also makes the controllers' unprompted
calls) and the second radio. A fault that needs two of them at the same moment cannot be seen one step at
a time. Here they run as real threads on the real `Session`, `AtisBroadcaster` and `SecondRadio`, with:

- `SimTime`: the clock every wait and sleep goes through (see proatc/clock.py). Only one thread runs at a
  time; time moves on only when every thread is waiting, and the next one to run is always the same one.
  A run therefore gives the same result every time, and a minute of flight takes a fraction of a second.
- `SimBridge`: the sim. An aircraft flying a straight line, descending to the level it was cleared to,
  with COM radios that report a new frequency some time after it is written (or never, or refuse it).
- `SimPlayer`, `SimTts`, `SimStt`, `SimRecorder`, `SimPtt`: the sound card, the voices, the microphone and
  the push-to-talk key. A sentence lasts as long as it would take to say.
- `SimParser`, `SimModel`: the language model. The parser knows what each scripted sentence means.

`Rig` puts them together on a small world (tests/test_ifr.py's airports, centres and procedures) and is
what a scenario drives: `rig.run_until(t)`, `rig.say(...)`, `rig.tune_by_hand(...)`, `rig.controller_says(...)`.
See docs/testing.md, "Timing tests".
"""

from __future__ import annotations

import math
import queue
import random
import threading
import traceback
from dataclasses import dataclass, field

import numpy as np

from proatc import ifr
from proatc.atis import AtisBook
from proatc.centers import CenterIndex, parse_atc
from proatc.config import Config
from proatc.engine import Engine
from proatc.intents import Intent
from proatc.navdata import AirportDB, build_index, prefer_airports
from proatc.procedures import parse_cifp
from proatc.radio import AtisBroadcaster, SecondRadio, SimClock, current_station
from proatc.voice_loop import Session
from proatc.xpbridge import SimUnavailable
from tests.test_ifr import APT, ATC, CIFP_EAST, CIFP_WEST, PLAN

WATCHDOG_S = 20.0  # real seconds a thread may wait for its turn before the run is declared stuck
WORDS_PER_S = 2.7  # how fast the stand-in voices speak
SOUND_RATE = 1000  # samples per second of the stand-in voices: only the length matters

# The centres of tests/test_ifr.py, with their boundary 60 miles short of the destination (as in
# tests/test_arrival_handoff.py): near it both centres answer, and approach is not far off.
NEAR_BOUNDARY = [line.replace(" 2.0", " 3.8") for line in ATC]
WEST, EAST, EAST_2, APPROACH = 127500, 132100, 128600, 124300


class SimStopped(BaseException):
    """Raised in the radio's threads when a run ends. Not an Exception, so their own error handling lets it through."""


class SimStuck(RuntimeError):
    """Every thread is waiting for something that cannot happen, or one has not given up its turn."""


# ---- time -------------------------------------------------------------------


class SimTime:
    """A `proatc.clock.Clock` in simulated seconds that runs one thread at a time.

    A thread runs until it waits (sleep, wait for an event, wait for a job). Then the next thread whose wait
    is over runs: the one that has been waiting longest. When none is ready, time jumps to the earliest
    deadline. The thread that creates the clock (the test) holds the first turn."""

    def __init__(self, start: float = 0.0):
        self._now = start
        self._cond = threading.Condition()
        self._turn: threading.Thread | None = threading.current_thread()
        self._waiting: dict[threading.Thread, tuple] = {}  # thread -> (ready, deadline, order)
        self._order = 0
        self._stopped = False
        self.threads: list[threading.Thread] = []
        self.errors: list[str] = []

    # -- the Clock interface --

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.wait_until(lambda: False, self._now + max(0.0, seconds))

    def wait(self, event: threading.Event, timeout: float | None = None) -> bool:
        self.wait_until(event.is_set, None if timeout is None else self._now + max(0.0, timeout))
        return event.is_set()

    # -- scheduling --

    def wait_until(self, ready, deadline: float | None = None) -> None:
        """Give up the turn until `ready()` is true or the deadline has come."""
        me = threading.current_thread()
        with self._cond:
            if self._stopped:
                raise SimStopped
            if self._turn is not me:
                raise SimStuck(f"{me.name} waited without its turn: start threads with SimTime.spawn")
            self._order += 1
            self._waiting[me] = (ready, deadline, self._order)
            self._hand_on()
            self._await_turn(me)

    def _hand_on(self) -> None:
        """Pass the turn to the thread that has waited longest among those that can go on."""
        while True:
            ready = [
                (order, thread)
                for thread, (can_go, deadline, order) in self._waiting.items()
                if (deadline is not None and deadline <= self._now) or can_go()
            ]
            if ready:
                _, chosen = min(ready, key=lambda pair: pair[0])
                del self._waiting[chosen]
                self._turn = chosen
                self._cond.notify_all()
                return
            deadlines = [deadline for _, deadline, _ in self._waiting.values() if deadline is not None]
            if not deadlines:
                self._stop_all()
                raise SimStuck("every thread is waiting for something that cannot happen")
            self._now = min(deadlines)

    def _await_turn(self, me: threading.Thread) -> None:
        while self._turn is not me and not self._stopped:
            if not self._cond.wait(WATCHDOG_S):
                self._stop_all()
                raise SimStuck(f"{me.name} waited {WATCHDOG_S} s (real) for {self._turn and self._turn.name}")
        if self._stopped:
            raise SimStopped

    def _stop_all(self) -> None:
        self._stopped = True
        self._cond.notify_all()

    def spawn(self, run, name: str) -> threading.Thread:
        """Start a thread that takes turns with the others. It first runs when the caller next waits."""
        thread = threading.Thread(target=self._thread, args=(run,), name=name, daemon=True)
        with self._cond:
            self._order += 1
            self._waiting[thread] = (lambda: True, None, self._order)
        self.threads.append(thread)
        thread.start()
        return thread

    def _thread(self, run) -> None:
        me = threading.current_thread()
        try:
            with self._cond:
                self._await_turn(me)
            run()
        except SimStopped:
            pass
        except BaseException:
            self.errors.append(f"{me.name}: {traceback.format_exc()}")
        finally:
            with self._cond:
                if self._turn is me and not self._stopped:
                    self._turn = None
                    try:
                        self._hand_on()
                    except SimStuck as error:
                        self.errors.append(str(error))

    def stop(self) -> None:
        """End the run: every thread's next wait raises SimStopped."""
        with self._cond:
            self._stop_all()
        for thread in self.threads:
            thread.join(WATCHDOG_S)


class SimEvent(threading.Event):
    """A threading.Event whose wait goes through the simulated clock."""

    def __init__(self, clock: SimTime):
        super().__init__()
        self.clock = clock

    def wait(self, timeout: float | None = None) -> bool:
        return self.clock.wait(self, timeout)


class SimQueue(queue.Queue):
    """The session's job queue, waited on through the simulated clock."""

    def __init__(self, clock: SimTime):
        super().__init__()
        self.clock = clock

    def get(self, block: bool = True, timeout: float | None = None):
        if block:
            deadline = None if timeout is None else self.clock.monotonic() + timeout
            self.clock.wait_until(lambda: not self.empty(), deadline)
        return super().get(block=False)


# ---- the sim ----------------------------------------------------------------


class SimBridge:
    """X-Plane: an aircraft flying east along a parallel, descending to whatever it was last cleared to.

    A written COM frequency is reported `tune_delay` seconds later. `refuse` makes writes fail:
    "error" (the sim refuses them) or "ignored" (accepted and never taken, as some add-on aircraft do)."""

    def __init__(self, clock: SimTime, lat: float, lon: float, feet: float, khz: int, speed_ms: float = 230.0,
                 cleared=None, tune_delay: float = 0.3, refuse: str | None = None):  # fmt: skip
        self.clock, self.connected = clock, True
        self.lat, self.lon0, self.speed_ms = lat, lon, speed_ms
        self.feet, self._feet_at = feet, clock.monotonic()
        self.start = clock.monotonic()
        self.cleared = cleared or (lambda: None)  # the autopilot's target level
        self.radios = {"com1_khz": khz, "com2_khz": 0}
        self.tune_delay, self.refuse = tune_delay, refuse
        self._landing: list[tuple[float, str, int]] = []  # (when the sim reports it, key, value)
        self.written: list[tuple[float, str, int]] = []

    def write(self, key: str, value) -> None:
        now = self.clock.monotonic()
        self.written.append((now, key, value))
        if self.refuse == "error":
            raise SimUnavailable("X-Plane refused the change (403).")
        if self.refuse != "ignored":
            self._landing.append((now + self.tune_delay, key, int(value)))

    def tune_by_hand(self, khz: int, key: str = "com1_khz") -> None:
        """The pilot turns the knob: at once, and ahead of any write still on its way."""
        self._landing = [item for item in self._landing if item[1] != key]
        self.radios[key] = khz

    def _land_writes(self, now: float) -> None:
        for when, key, value in sorted(self._landing):
            if when <= now:
                self.radios[key] = value
        self._landing = [item for item in self._landing if item[0] > now]

    def _fly(self, now: float) -> None:
        target = self.cleared()
        if target is not None:
            step = 25.0 * (now - self._feet_at)  # 1,500 feet a minute
            self.feet = max(target, self.feet - step) if self.feet > target else min(target, self.feet + step)
        self._feet_at = now

    def lon(self, now: float | None = None) -> float:
        elapsed = (self.clock.monotonic() if now is None else now) - self.start
        return self.lon0 + self.speed_ms * elapsed / 1852 / (60 * math.cos(math.radians(self.lat)))

    def snapshot(self) -> dict:
        now = self.clock.monotonic()
        self._land_writes(now)
        self._fly(now)
        metres = self.feet * 0.3048
        return {
            "tail_number": "G-ABCD", "lat": self.lat, "lon": self.lon(now), "on_ground": 0, "elevation_m": metres,
            "agl_m": metres, "groundspeed_ms": self.speed_ms, "heading_mag": 90.0, "heading_true": 90.0, "ias_kt": 240.0,
            "zulu_seconds": 43200.0, "wx_qnh_pas": 101300.0, "wx_visibility_sm": 10.0, "wx_wind_alt_m": [150.0],
            "wx_wind_kt": [8.0], "wx_wind_dir": [270.0], "com_selection": 6, **self.radios,
        }  # fmt: skip

    def stop(self) -> None:
        pass


# ---- sound, voices, microphone, key ------------------------------------------


def seconds_to_say(text: str) -> float:
    return 0.4 + len(text.split()) / WORDS_PER_S


class SimPlayer:
    """The sound card, as audio_io.Player behaves: one sound at a time, a new one cuts the last off."""

    def __init__(self, clock: SimTime, alongside: bool = False):
        self.clock, self.alongside, self.device = clock, alongside, None
        self._generation, self._ends_at = 0, 0.0

    def play(self, samples, rate: int) -> int:
        self._generation += 1
        self._ends_at = self.clock.monotonic() + len(samples) / rate
        return self._generation

    def play_stream(self, pieces, rate: int) -> int:
        return self.play(np.concatenate(list(pieces)), rate)

    @property
    def playing(self) -> bool:
        return self.clock.monotonic() < self._ends_at

    def stop(self) -> None:
        self._generation += 1
        self._ends_at = 0.0

    def wait(self, ticket: int, should_stop=lambda: False) -> bool:
        while self.clock.monotonic() < self._ends_at:
            if self._generation != ticket:
                return False
            if should_stop():
                self.stop()
                return False
            self.clock.sleep(0.05)
        return self._generation == ticket


class SimTts:
    streams = False
    engine_name = "stand-in"

    def __init__(self):
        self.spoken: list[str] = []

    def speak(self, text: str, voice=None, speed=None):
        self.spoken.append(text)
        return np.zeros(int(seconds_to_say(text) * SOUND_RATE), dtype=np.float32), SOUND_RATE

    def voices(self) -> list[str]:
        return ["controller", "pilot"]


class SimStt:
    device = "cpu"

    def __init__(self):
        self.next: list[str] = []  # what the pilot says, transmission by transmission

    def transcribe(self, audio, hints: str = "") -> str:
        return self.next.pop(0) if self.next else ""


class SimRecorder:
    def start(self) -> None:
        pass

    def stop(self):
        return np.full(16000, 0.2, dtype=np.float32)


class SimPtt:
    """The push-to-talk key, as ptt.PushToTalk behaves."""

    def __init__(self, clock: SimTime, on_down):
        self.clock = clock
        self.pressed, self.released = SimEvent(clock), SimEvent(clock)
        self.released.set()
        self.on_down = on_down
        self.held: list[list[float]] = []  # [pressed at, released at]: when the pilot was transmitting

    def press(self) -> None:
        if not self.pressed.is_set():
            self.held.append([self.clock.monotonic(), float("inf")])
            self.released.clear()
            self.pressed.set()
            self.on_down()

    def release(self) -> None:
        if self.pressed.is_set():
            self.held[-1][1] = self.clock.monotonic()
        self.pressed.clear()
        self.released.set()

    def stop(self) -> None:
        pass


class SimParser:
    """The language model's job: what the pilot meant. A scenario says what each sentence means."""

    def __init__(self):
        self.means: dict[str, Intent] = {}

    def parse(self, transcript: str, last_said: str = "", names=None) -> Intent:
        return self.means.get(transcript, Intent("other"))


class SimModel:
    """The language model's free-form fallback: it only ever asks for a repeat."""

    def reply(self, transcript, context, station_key="free", inform_only=False) -> str:
        return "Station calling, say again"

    def remember(self, *args) -> None:
        pass

    def complete(self, *args, **kwargs) -> str:
        return ""


# ---- the rig ----------------------------------------------------------------


@dataclass
class Heard:
    """One event, when it happened, and which station the radio was on."""

    t: float
    type: str
    text: str
    station: str | None
    event: dict = field(default_factory=dict)


@dataclass
class Scripted:
    """An unprompted call the controller on frequency makes at a set time, through the engine's own rules."""

    at: float
    make: object  # (engine, station, raw, callsign) -> Reply | None
    by: str | None = None  # the station that made it, once it has


def handoff_to_next_centre(engine, station, raw, callsign):
    """'Contact <the centre for where the aircraft is>', as ifr.tick says it at a boundary."""
    if station.kind != "center":
        return None
    return ifr._centre_handoff(engine, raw, callsign, exclude=station.center)


def handoff_to_approach(engine, station, raw, callsign):
    """'Contact <destination> Approach', as ifr.tick says it close to the destination."""
    return ifr._to_approach(engine, station, engine.db.airport(engine.flight.plan.destination), raw, callsign)


def descend_to(feet: int):
    def make(engine, station, raw, callsign):
        return ifr._descend(engine, feet, engine.flight.plan.destination, False, callsign, "")

    return make


class Rig:
    """A session with its threads, on the world of tests/test_ifr.py, flying east toward Eastfield (LFXX)."""

    def __init__(self, tmp_path, *, miles_out: float = 57, feet: float = 13000, khz: int = WEST, copilot: str = "full",
                 auto_tune: bool = False, allow_tuning: bool = True, tune_delay: float = 0.3, refuse: str | None = None,
                 chatter: str = "off", seed: int = 1, speak_pilot_text: bool = True):  # fmt: skip
        self.clock = SimTime()
        self.db = _world_db(tmp_path)
        self.db.centers = CenterIndex(parse_atc(NEAR_BOUNDARY))
        procedures = {"EGXX": parse_cifp(CIFP_WEST), "LFXX": parse_cifp(CIFP_EAST)}
        engine = Engine(self.db, seed=seed, procedures=lambda ident: procedures.get(ident, parse_cifp([])),
                        centers=self.db.centers, clock=SimClock(self.clock))  # fmt: skip
        engine.clock = engine.now  # as Session.load does: the broadcaster pauses it with the sim
        prefer_airports([])
        engine.set_plan(PLAN)
        engine.flight.has_been_airborne, engine.flight.origin_ident = True, "EGXX"
        self.engine = engine
        destination = self.db.airport("LFXX")
        lon = destination.lon - miles_out / (60 * math.cos(math.radians(51.0)))
        self.bridge = SimBridge(self.clock, 51.0, lon, feet, khz, cleared=lambda: engine.flight.cleared_ft,
                                tune_delay=tune_delay, refuse=refuse)  # fmt: skip

        config = Config()
        config.atc.copilot, config.atc.chatter, config.atc.second_radio = copilot, chatter, True
        config.sim.auto_tune, config.sim.allow_tuning = auto_tune, allow_tuning
        config.radio.enabled = False  # the radio effect's filters mean nothing at the stand-in's sample rate
        config.tts.speak_pilot_text = speak_pilot_text
        self.config = config
        self.heard: list[Heard] = []
        session = Session(config, emit=self._record, clock=self.clock)
        session.jobs = SimQueue(self.clock)
        session.db, session.book, session.engine = self.db, AtisBook(), engine
        session.bridge, session.tts, session.stt = self.bridge, SimTts(), SimStt()
        session.player, session.recorder = SimPlayer(self.clock), SimRecorder()
        session.ptt = SimPtt(self.clock, on_down=session._key_down)
        session.controller, session.parser = SimModel(), SimParser()
        session.centers = self.db.centers
        session.broadcaster = AtisBroadcaster(
            self.bridge, self._thread_db(), session.tts, session.player, session.book, config, session.busy, engine,
            session.emit, after=lambda: session.jobs.put(("after", None, None)), clock=self.clock,
        )  # fmt: skip
        session.broadcaster._rng = random.Random(seed)  # the chatter, the same on every run
        session.second_radio = SecondRadio(self.bridge, self._thread_db(), session.tts, SimPlayer(self.clock, alongside=True),
                                           session.book, config, session.emit, clock=self.clock)  # fmt: skip
        session.ready = True
        self.session = session
        self.script: list[Scripted] = []
        self._real_tick = ifr.tick
        ifr.tick = self._tick  # scripted calls are made where the engine makes its own; put back by stop()
        self._started = False

    def _thread_db(self) -> AirportDB:
        db = AirportDB(self.db_path)
        db.centers = self.db.centers
        return db

    @property
    def db_path(self):
        return self.db.path

    # -- what happened --

    def _record(self, event: dict) -> None:
        station = self.station()
        text = event.get("text") or event.get("said") or ""
        self.heard.append(
            Heard(self.clock.monotonic(), event.get("type", ""), text, station.callsign if station else None, event)
        )

    def station(self):
        return current_station(self.db, self.bridge.snapshot())[1]

    def tuned_mhz(self) -> str:
        return f"{self.bridge.radios['com1_khz'] / 1000:.3f}"

    def said(self, kind: str) -> list[Heard]:
        return [item for item in self.heard if item.type == kind]

    def errors(self) -> list[str]:
        return self.clock.errors + [item.text for item in self.heard if item.type == "error"]

    def transcript(self) -> str:
        """The run as a pilot would tell it, for assertion messages."""
        return "\n".join(f"{item.t:7.2f} [{item.station}] {item.type}: {item.text}" for item in self.heard
                         if item.type in ("pilot", "atc", "error", "info", "silent"))  # fmt: skip

    # -- scripted controller calls --

    def controller_says(self, at: float, make) -> Scripted:
        """At `at` seconds, or the first tick after, the controller on frequency makes this call unprompted."""
        call = Scripted(at, make)
        self.script.append(call)
        return call

    def _tick(self, engine, station, issue, raw, variation, callsign):
        """ifr.tick, with the scripted calls first: Engine.tick's own rules apply to them as to any other."""
        due = next((call for call in self.script if call.by is None and call.at <= self.clock.monotonic()), None)
        if engine is self.engine and due is not None and station.key in engine.flight.contacted and not raw.get("on_ground"):
            reply = due.make(engine, station, raw, callsign)
            if reply is not None:
                due.by = station.callsign
                return reply
        return self._real_tick(engine, station, issue, raw, variation, callsign)

    # -- driving the run --

    def check_in_first(self) -> None:
        """Before the run starts: the flight checked in with the controller on the tuned frequency a while ago,
        a hundred miles out, where Westland Control is the centre."""
        raw = {**self.bridge.snapshot(), "lon": self.db.airport("LFXX").lon - 100 / (60 * math.cos(math.radians(51.0)))}
        station = current_station(self.db, raw)[1]
        self.engine.handle(Intent("check_in"), station, None, raw)
        self.engine.flight.cleared_ft, self.engine.flight.descent_given, self.engine.flight.pending = 13000, True, None

    def start(self) -> None:
        self._started = True
        self.clock.spawn(self.session.run_forever, "jobs")
        self.clock.spawn(self.session.broadcaster.run, "atis")
        self.clock.spawn(self.session.second_radio.run, "second-radio")

    def run_until(self, t: float) -> None:
        if not self._started:
            self.start()
        self.clock.wait_until(lambda: False, t)

    def say(self, text: str, intent: Intent, seconds: float | None = None) -> None:
        """The pilot holds the key, says this, and lets go."""
        self.session.parser.means[text] = intent
        self.session.stt.next.append(text)
        self.session.ptt.press()
        self.clock.sleep(seconds if seconds is not None else seconds_to_say(text))
        self.session.ptt.release()

    def press(self) -> None:
        self.session.ptt.press()

    def release(self, text: str, intent: Intent) -> None:
        self.session.parser.means[text] = intent
        self.session.stt.next.append(text)
        self.session.ptt.release()

    def click(self, text: str, intent: Intent) -> None:
        """A call clicked in the app: it carries its own intent."""
        self.session.submit(text, intent)

    def tune_by_hand(self, khz: int) -> None:
        self.bridge.tune_by_hand(khz)

    def stop(self) -> None:
        self.clock.stop()
        ifr.tick = self._real_tick
        prefer_airports([])

    # -- the end state a pilot needs --

    def settled_on(self, khz: int, callsign: str, errors: int = 0) -> list[str]:
        """What is wrong, if anything, with the flight's state at the end of a run: it must be on this frequency,
        checked in with this station, with no readback owed and no hand-off left to follow; nobody may have talked
        over the pilot, and nothing may have been said twice. `errors`: how many error messages are expected."""
        problems = []
        station, flight = self.station(), self.engine.flight
        if self.bridge.radios["com1_khz"] != khz:
            problems.append(f"tuned to {self.tuned_mhz()}, not {khz / 1000:.3f}")
        if station is None or station.callsign != callsign:
            problems.append(f"talking to {station and station.callsign}, not {callsign}")
        elif station.key not in flight.contacted:
            problems.append(f"never checked in with {callsign}")
        if flight.pending is not None:
            problems.append(f"still waiting for a readback of: {flight.pending.text}")
        if flight.next_frequency is not None:
            problems.append(f"still a hand-off to follow: {flight.next_frequency}")
        found = self.errors()
        if len(found) != errors:
            problems += [f"{len(found)} errors, not {errors}"] + [f"error: {error}" for error in found]
        return problems + self.said_twice() + self.talked_over()

    def talked_over(self) -> list[str]:
        """Controller calls that began while the pilot's key was down."""
        held = self.session.ptt.held
        return [f"{item.station} spoke at {item.t:.2f} while the pilot was transmitting: {item.text}"
                for item in self.said("atc") if any(start < item.t < end for start, end in held)]  # fmt: skip

    def said_twice(self) -> list[str]:
        """The same call made twice running to the same station, or an instruction left unanswered."""
        calls = [(item.station, item.text) for item in self.said("pilot")]
        return [f"said twice to {station}: {text}" for (station, text), (other, again) in zip(calls, calls[1:])
                if station == other and text == again]  # fmt: skip


def _world_db(tmp_path) -> AirportDB:
    path = tmp_path / "a.sqlite"
    if not path.exists():
        apt = tmp_path / "apt.dat"
        apt.write_text("\n".join(APT), encoding="utf-8")
        build_index(apt, path)
    db = AirportDB(path)
    db.path = path
    return db
