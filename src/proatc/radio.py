"""Who is on the tuned frequency, what they know, and the continuous ATIS broadcast."""

from __future__ import annotations

import threading

from . import phraseology as say
from .atis import AtisBook, runways_in_use, uses_faa_style
from .events import Emit, console
from .navdata import AirportDB, Station
from .radio_fx import radio_effect
from .weather import Weather, from_snapshot
from .xpbridge import magnetic_variation, tuned_khz


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
        khz, station = current_station(self.db, state)
        if station and station.kind != "atis" and self.engine:
            self._unprompted(station, state)
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

    def _unprompted(self, station: Station, state: dict) -> None:
        weather = observe(self.db, station.airport, state) if station.airport else None
        issue = self.book.current(station.airport, weather, state.get("zulu_seconds", 0.0)) if weather else None
        reply = self.engine.tick(station, issue, state)
        if not reply or not reply.text:
            return
        self.emit({"type": "atc", "station": station.callsign, "text": reply.text, "unprompted": True})
        samples, rate = self.tts.speak(reply.text)
        if self.config.radio.enabled:
            samples = radio_effect(samples, rate, self.config.radio.noise)
        ticket = self.player.play(samples, rate)
        self.player.wait(ticket, should_stop=lambda: self._halt.is_set() or self.busy.is_set())
        if self.after:
            self.after()
