"""Who is on the tuned frequency, what they know, and the continuous ATIS broadcast."""

from __future__ import annotations

import random
import threading
import time

import numpy as np

from . import chatter
from . import phraseology as say
from .atis import AtisBook, runways_in_use, uses_faa_style
from .events import Emit, console
from .navdata import AirportDB, Station
from .radio_fx import radio_effect, radio_stream
from .weather import Weather, from_snapshot
from .xpbridge import magnetic_variation, tuned_khz


class SimClock:
    """Seconds of simulated flight: it stops while the sim is paused, so a hold or a reminder does not run out."""

    def __init__(self):
        self.running = True
        self._total = 0.0
        self._since = time.monotonic()

    def __call__(self) -> float:
        now = time.monotonic()
        if self.running:
            self._total += now - self._since
        self._since = now
        return self._total


def current_station(db: AirportDB, state: dict) -> tuple[int | None, Station | None]:
    """(tuned frequency in kHz, station in range on it)."""
    khz = tuned_khz(state)
    if not khz or "lat" not in state or "lon" not in state:
        return khz, None
    station = db.station(khz, state["lat"], state["lon"])
    if station is None and db.centers:
        # No airport station on this frequency: an en-route centre whose airspace the aircraft is in.
        station = db.centers.on_frequency(khz, state["lat"], state["lon"])
    return khz, station


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
    ):
        super().__init__(name="atis", daemon=True)
        self.bridge, self.db, self.tts, self.player, self.book, self.config = bridge, db, tts, player, book, config
        self.emit = emit
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

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        while not self._halt.wait(0.5):
            try:
                self._tick()
            except Exception as error:  # never let the broadcast thread die silently
                self.emit({"type": "error", "text": f"ATIS error: {error}"})
                self._halt.wait(5)

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
        self._halt.wait(2)  # pause between repeats

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
        self.player.wait(ticket, should_stop=lambda: self._halt.is_set() or self.busy.is_set())
        if self.after:
            self.after()
        return True

    # ---- other aircraft on the frequency (sound only) -----------------------

    def _quiet(self) -> None:
        """The frequency was left: an exchange still playing belongs to it and stops."""
        if time.monotonic() < self._chatter_until:
            self.player.stop()
        self._chatter_key, self._chatter_until = None, 0.0

    def _chatter(self, station: Station, state: dict) -> None:
        mode = self.config.atc.chatter
        now = time.monotonic()
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
        if flight.pending is not None or getattr(self.player, "playing", False):
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
        self._chatter_until = time.monotonic() + len(samples) / rate
