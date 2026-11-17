"""Who is on the tuned frequency, what they know, and the continuous ATIS broadcast."""

from __future__ import annotations

import random
import threading

import numpy as np

from . import chatter, navdata
from . import phraseology as say
from .atis import AtisBook, runways_in_use, uses_faa_style
from .clock import REAL, Clock
from .events import Emit, console
from .navdata import AirportDB, Station
from .radio_fx import radio_effect, radio_stream
from .weather import Weather, from_snapshot
from .xpbridge import magnetic_variation, other_radio, tuned_khz


class SimClock:
    """Seconds of simulated flight: it stops while the sim is paused, so a hold or a reminder does not run out."""

    def __init__(self, clock: Clock = REAL):
        self.running = True
        self._time = clock
        self._total = 0.0
        self._since = clock.monotonic()

    def __call__(self) -> float:
        now = self._time.monotonic()
        if self.running:
            self._total += now - self._since
        self._since = now
        return self._total


def current_station(db: AirportDB, state: dict) -> tuple[int | None, Station | None]:
    """(tuned frequency in kHz, station in range on it)."""
    khz = tuned_khz(state)
    if not khz or "lat" not in state or "lon" not in state:
        return khz, None
    return khz, station_on(db, khz, state)


def station_on(db: AirportDB, khz: int, state: dict) -> Station | None:
    """Who answers on a frequency where the aircraft is. An airport's station and a centre can share one:
    the station already being talked to stays; otherwise the airport of the flight's plan, then an airport
    station the aircraft is low enough for, then the centre."""
    feet = state["elevation_m"] / 0.3048 if "elevation_m" in state and not state.get("on_ground", True) else None
    local = db.station(khz, state["lat"], state["lon"], feet)
    centre = db.centers.on_frequency(khz, state["lat"], state["lon"]) if db.centers else None
    found = [station for station in (local, centre) if station is not None]
    if not found:
        navdata.TALKING_TO.pop(khz, None)
        return None
    kept = next((station for station in found if station.key == navdata.TALKING_TO.get(khz)), None)
    planned = local if local is not None and navdata.is_preferred(local.airport.ident) else None
    chosen = kept or planned or found[0]
    navdata.TALKING_TO[khz] = chosen.key
    return chosen


def observe(db: AirportDB, airport, state: dict) -> Weather | None:
    """The airport's weather: its own METAR while the sim is on real weather, else the sim's weather at the aircraft."""
    metars = getattr(db, "metars", None)
    if metars is not None and state.get("wx_source") == 1:
        reported = metars.weather(airport.ident, airport.elev_ft)
        if reported:
            return reported
    return from_snapshot(state, airport.elev_ft)


def prime(pieces):
    """Wait for the first piece of sound, then give all of them: what follows starts playing at once."""
    first = next(pieces, None)

    def all_of_them():
        try:
            if first is not None:
                yield first
            yield from pieces
        finally:
            close = getattr(pieces, "close", None)
            if close:
                close()

    return all_of_them()


def transmit(tts, player, text: str, voice: dict, radio, on_sound=None) -> int:
    """Say `text` on the frequency. With a voice engine that gives sound in pieces, it is played as it is made:
    the controller starts speaking before the sentence is finished. Returns the player's ticket."""
    if getattr(tts, "streams", False) and hasattr(player, "play_stream"):
        pieces, rate = tts.stream(text, **voice)
        if radio.enabled:
            pieces = radio_stream(pieces, rate, radio.noise)
        pieces = prime(pieces)
        if on_sound:
            on_sound()
        return player.play_stream(pieces, rate)
    samples, rate = tts.speak(text, **voice)
    if radio.enabled:
        samples = radio_effect(samples, rate, radio.noise)
    if on_sound:
        on_sound()
    return player.play(samples, rate)


def station_voice(tts, station) -> dict:
    """The arguments for `tts.speak` that give this station its own voice."""
    pick = getattr(tts, "for_station", None)
    if station is None or pick is None:
        return {}
    airport = getattr(station, "airport", None)
    ident = airport.ident if airport else getattr(getattr(station, "center", None), "facility", None)
    return pick(station.key, ident)


def briefing(station: Station, weather: Weather | None, letter: str | None, variation: float) -> dict:
    """Facts the controller is allowed to use. Everything here comes from the sim's own data."""
    airport = station.airport
    if airport is None:
        return {"you_are": station.callsign, "station_type": "en-route centre"}
    facts: dict = {
        "you_are": station.callsign,
        "station_type": station.kind,
        "airport": airport.ident,
        "airport_name": airport.spoken_name,
        "field_elevation_ft": round(airport.elev_ft),
        "aircraft_distance_nm": round(station.distance_nm, 1),
        "all_runways": sorted(ident for runway in airport.runways for ident, _ in runway.ends()),
        "frequencies": {
            f"{freq.kind} ({freq.label})" if freq.label else freq.kind: f"{freq.khz / 1000:.3f}"
            for freq in sorted(airport.freqs, key=lambda f: (f.kind, f.khz))
        },
    }
    if weather:
        facts["runway_in_use"] = runways_in_use(airport, weather.wind_dir_true, weather.wind_kt)
        direction = round(((weather.wind_dir_true + variation) % 360) / 10) * 10 or 360
        facts["surface_wind"] = "calm" if weather.wind_kt < 1 else f"{direction:03d} degrees {round(weather.wind_kt)} knots"
        if uses_faa_style(airport.ident):
            facts["altimeter_inhg"] = f"{weather.qnh_hpa * 0.02953:.2f}"
        else:
            facts["qnh_hpa"] = int(weather.qnh_hpa)
    if letter:
        facts["current_atis"] = say.letter(letter)
    return facts


SECOND_RADIO_GAIN = 0.5  # the second radio is heard under the first, as with its volume turned down


def monitored(db: AirportDB, state: dict) -> tuple[dict, Station | None]:
    """(the radio that is not transmitted on, the station in range on it). The same station as on the
    transmitting radio counts as none: it is already heard."""
    other = other_radio(state)
    if not other["khz"] or "lat" not in state or other["khz"] == tuned_khz(state):
        return other, None
    return other, station_on(db, other["khz"], state)


class SecondRadio(threading.Thread):
    """The COM radio the pilot is not transmitting on, when the audio panel has it on: its ATIS is heard
    under whatever happens on the first radio. Listening only: nobody on it is talking to this aircraft."""

    def __init__(self, bridge, db: AirportDB, tts, player, book: AtisBook, config, emit: Emit = console, clock: Clock = REAL):
        super().__init__(name="second-radio", daemon=True)
        self.bridge, self.db, self.tts, self.player, self.book, self.config = bridge, db, tts, player, book, config
        self.emit = emit
        self.clock = clock
        self._halt = threading.Event()
        self.hold = threading.Event()  # set while the sound check uses the second radio itself
        self._text = ""
        self._audio = None
        self._again_at = 0.0

    def stop(self) -> None:
        self._halt.set()
        self.player.stop()

    def broadcast(self, samples, rate: int, gain: float) -> int:
        """Play a recording on this radio, under whatever the first radio is saying. Returns the player's ticket."""
        return self.player.play_stream(iter([samples * gain]), rate)

    def run(self) -> None:
        while not self.clock.wait(self._halt, 0.5):
            try:
                self.tick()
            except Exception as error:
                self.emit({"type": "error", "text": f"Second radio: {error}"})
                self.clock.wait(self._halt, 5)

    def heard(self, state: dict):
        """(frequency, ATIS station, loudness) the second radio is playing now, or None."""
        if not self.config.atc.second_radio or state.get("paused"):
            return None
        other, station = monitored(self.db, state)
        if not other["listening"] or station is None or station.kind != "atis":
            return None
        return other["khz"], station, SECOND_RADIO_GAIN * min(1.0, other["volume"] / 0.55)

    def tick(self) -> None:
        if self.hold.is_set():
            return  # the sound check is playing its own recording on this radio: leave it alone
        state = self.bridge.snapshot() if self.bridge.connected else {}
        heard = self.heard(state)
        if heard is None:
            if self.player.playing:
                self.player.stop()  # tuned away, turned down, or switched off
            self._again_at = 0.0
            return
        khz, station, gain = heard
        if self.player.playing or self.clock.monotonic() < self._again_at:
            return
        weather = observe(self.db, station.airport, state)
        if weather is None:
            return
        variation = round(magnetic_variation(state))
        _, text = self.book.text(station.airport, weather, state.get("zulu_seconds", 0.0), variation, station.place)
        if text != self._text:
            samples, rate = self.tts.speak(text, voice=self.config.tts.atis_voice, speed=self.config.tts.atis_speed)
            if self.config.radio.enabled:
                samples = radio_effect(samples, rate, self.config.radio.noise)
            self._text, self._audio = text, (samples, rate)
            radio = other_radio(state)["radio"]
            self.emit(
                {"type": "atis", "airport": station.airport.ident, "mhz": f"{khz / 1000:.3f}", "text": text, "radio": radio}
            )
        samples, rate = self._audio
        self.broadcast(samples, rate, gain)
        self._again_at = self.clock.monotonic() + len(samples) / rate + 2  # a pause between repeats


class AtisBroadcaster(threading.Thread):
    """Plays the ATIS in a loop for as long as an ATIS frequency is tuned."""

    def __init__(
        self,
        bridge,
        db: AirportDB,
        tts,
        player,
        book: AtisBook,
        config,
        busy: threading.Event,
        engine=None,
        emit: Emit = console,
        after=None,
        clock: Clock = REAL,
    ):
        super().__init__(name="atis", daemon=True)
        self.bridge, self.db, self.tts, self.player, self.book, self.config = bridge, db, tts, player, book, config
        self.emit = emit
        self.clock = clock
        self.after = after  # called after an unprompted transmission (the copilot may want to answer)
        self.engine = engine  # rules engine, for calls the controller makes unprompted
        self.busy = busy  # set while the pilot is transmitting or the controller is replying
        self._halt = threading.Event()
        self._text = ""
        self._audio = None
        self._rng = random.Random()
        self._chatter_key: str | None = None  # the station whose frequency the chatter is on
        self._chatter_at = 0.0  # when the next exchange is due
        self._chatter_until = 0.0  # while an exchange is playing
        self._tuned: int | None = None  # the frequency at the last tick

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        while not self.clock.wait(self._halt, 0.5):
            try:
                self._tick()
            except Exception as error:  # never let the broadcast thread die silently
                self.emit({"type": "error", "text": f"ATIS error: {error}"})
                self.clock.wait(self._halt, 5)

    def _tick(self) -> None:
        if not self.bridge.connected or self.busy.is_set():
            return
        state = self.bridge.snapshot()
        clock = getattr(self.engine, "clock", None) if self.engine else None
        if clock is not None:
            clock.running = not state.get("paused")
        if state.get("paused"):
            return  # the sim is paused: controllers do not carry on without the aircraft
        khz, station = current_station(self.db, state)
        if khz != self._tuned:
            self._tuned = khz
            if self.after:
                self.after()  # a new frequency: the copilot may have a call to make on it
        if station and station.kind != "atis" and self.engine:
            if not self._unprompted(station, state):
                self._chatter(station, state)
        elif self._chatter_key:
            self._quiet()
        if not station or station.kind != "atis":
            return
        weather = observe(self.db, station.airport, state)
        if weather is None:
            return
        variation = round(magnetic_variation(state))
        _, text = self.book.text(station.airport, weather, state.get("zulu_seconds", 0.0), variation, station.place)
        if text != self._text:
            samples, rate = self.tts.speak(text, voice=self.config.tts.atis_voice, speed=self.config.tts.atis_speed)
            if self.config.radio.enabled:
                samples = radio_effect(samples, rate, self.config.radio.noise)
            self._text, self._audio = text, (samples, rate)
            self.emit({"type": "atis", "airport": station.airport.ident, "mhz": f"{khz / 1000:.3f}", "text": text})
        ticket = self.player.play(*self._audio)
        self.player.wait(
            ticket, should_stop=lambda: self._halt.is_set() or self.busy.is_set() or tuned_khz(self.bridge.snapshot()) != khz
        )
        self.clock.wait(self._halt, 2)  # pause between repeats

    def _unprompted(self, station: Station, state: dict) -> bool:
        """Say what the controller has to say without being asked. True when something was said."""
        weather = observe(self.db, station.airport, state) if station.airport else None
        issue = self.book.current(station.airport, weather, state.get("zulu_seconds", 0.0)) if weather else None
        reply = self.engine.tick(station, issue, state)
        if not reply or not reply.text:
            return False
        self._chatter_until = 0.0
        event = {"type": "atc", "station": station.callsign, "text": reply.text, "unprompted": True}
        if reply.event:
            event["event"] = reply.event
        self.emit(event)
        ticket = transmit(self.tts, self.player, reply.text, station_voice(self.tts, station), self.config.radio)
        # Tuned away mid-sentence: the rest is not heard on the new frequency.
        khz = tuned_khz(state)
        self.player.wait(ticket, should_stop=lambda: self._halt.is_set() or self.busy.is_set() or self._retuned(khz))
        if self.after:
            self.after()
        return True

    def _retuned(self, khz: int | None) -> bool:
        return tuned_khz(self.bridge.snapshot()) != khz

    # ---- other aircraft on the frequency (sound only) -----------------------

    def _quiet(self) -> None:
        """The frequency was left: an exchange still playing belongs to it and stops."""
        if self.clock.monotonic() < self._chatter_until:
            self.player.stop()
        self._chatter_key, self._chatter_until = None, 0.0

    def _chatter(self, station: Station, state: dict) -> None:
        mode = self.config.atc.chatter
        now = self.clock.monotonic()
        if self._chatter_key != station.key:
            self._quiet()
            self._chatter_key = station.key
            self._chatter_at = now + self._rng.uniform(*chatter.FIRST_AFTER_S)
            return
        if mode not in ("some", "busy"):
            if now < self._chatter_until:  # switched off in the settings: an exchange that is playing stops too
                self.player.stop()
                self._chatter_until = 0.0
            return
        if now < self._chatter_at or now < self._chatter_until:
            return
        flight = self.engine.flight
        if (flight.pending is not None and flight.pending_key == station.key) or getattr(self.player, "playing", False):
            self._chatter_at = now + 4  # the controller is waiting for your readback, or someone is talking
            return
        weather = observe(self.db, station.airport, state) if station.airport else None
        issue = self.book.current(station.airport, weather, state.get("zulu_seconds", 0.0)) if weather else None
        self._chatter_at = now + chatter.gap_seconds(station.kind, mode, self._rng)
        found = chatter.exchange(station, issue, self.engine, self._rng, self.engine._callsign(state))
        if not found:
            return
        callsign, said, answered = found
        voice = station_voice(self.tts, station)
        first, rate = self.tts.speak(said, **voice) if answered else (None, 0)
        others = [n for n in getattr(self.tts, "voices", list)() if n != voice.get("voice") and "santa" not in n]
        other = {"voice": self._rng.choice(others)} if others else {}
        if answered:
            second, _ = self.tts.speak(answered, **other)
            samples = np.concatenate([first, np.zeros(int(rate * 0.35), dtype=np.float32), second])
        else:
            samples, rate = self.tts.speak(said, **other)  # a blind call: it is another pilot speaking, not the station
        if self.config.radio.enabled:
            samples = radio_effect(samples, rate, self.config.radio.noise * 1.5)
        if self.busy.is_set() or self._halt.is_set() or getattr(self.player, "playing", False):
            return  # you keyed the microphone, or a controller started talking, while this was being prepared
        self.emit({"type": "chatter", "station": station.callsign, "callsign": callsign, "said": said, "answered": answered})
        self.player.play(samples * 0.6, rate)
        self._chatter_until = self.clock.monotonic() + len(samples) / rate
