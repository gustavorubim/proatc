"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import time


def cmd_check(config) -> int:
    from .xpbridge import SimUnavailable, XPBridge, describe

    bridge = XPBridge(config.sim.host, config.sim.port)
    try:
        caps = bridge.capabilities()
        print(f"X-Plane {caps['x-plane']['version']}, Web API {', '.join(caps['api']['versions'])}")
        bridge.resolve()
        if bridge.missing:
            print("Datarefs not found:", *bridge.missing, sep="\n  ")
        print("REST read:  ", json.dumps(describe(bridge.read_once())))
        bridge.start()
        ok = bridge.wait_for_data(5)
        time.sleep(0.5)
        raw = bridge.snapshot()
        print("Live stream:", json.dumps(describe(raw)) if ok else "NO DATA within 5 s")
        bridge.stop()
        if ok:
            from .navdata import AirportDB
            from .radio import current_station
            from .weather import from_snapshot

            db = AirportDB.open(config.sim.xplane_path)
            khz, station = current_station(db, raw)
            tuned = f"{khz / 1000:.3f}" if khz else "none"
            print(
                f"Tuned {tuned}:",
                f"{station.callsign} ({station.airport.ident}, {station.distance_nm:.0f} NM)"
                if station
                else "no station in range",
            )
            for dist, airport in db.nearest(raw["lat"], raw["lon"], 40, 3):
                print(f"  nearby: {airport.ident} {airport.name}, {dist:.0f} NM")
            print("Weather:", from_snapshot(raw, describe(raw).get("altitude_ft", 0) - describe(raw).get("height_agl_ft", 0)))
        return 0 if ok else 1
    except SimUnavailable as error:
        print(error)
        return 1


def cmd_devices(config) -> int:
    from .audio_io import list_devices

    print(list_devices())
    return 0


def cmd_setup_models(config) -> int:
    from .tts import download_models

    download_models()
    return 0


def cmd_say(config, text: str) -> int:
    from .audio_io import play
    from .radio_fx import radio_effect
    from .tts import TextToSpeech

    samples, rate = TextToSpeech(config.tts).speak(text)
    if config.radio.enabled:
        samples = radio_effect(samples, rate, config.radio.noise)
    play(samples, rate, config.audio.output_device)
    return 0


def _live_state(config) -> dict:
    """Raw sim values, or {} when the sim is not running."""
    from .xpbridge import SimUnavailable, XPBridge

    try:
        return XPBridge(config.sim.host, config.sim.port).read_once()
    except SimUnavailable:
        return {}


def cmd_airport(config, ident: str) -> int:
    from .navdata import AirportDB

    airport = AirportDB.open(config.sim.xplane_path).airport(ident)
    if not airport:
        print(f"No airport '{ident}' in X-Plane's data.")
        return 1
    print(f"{airport.ident}  {airport.name}  ({airport.lat:.4f}, {airport.lon:.4f})  elevation {airport.elev_ft:.0f} ft")
    for runway in airport.runways:
        (a, heading_a), (b, heading_b) = runway.ends()
        print(f"  runway {a}/{b}  {runway.length_m:.0f} m  true headings {heading_a:03.0f}/{heading_b:03.0f}")
    for freq in sorted(airport.freqs, key=lambda f: (f.kind, f.khz)):
        print(f"  {freq.khz / 1000:.3f}  {freq.kind:<10} {freq.label}")
    return 0


def cmd_atis(config, ident: str, speak: bool) -> int:
    from .atis import AtisBook
    from .navdata import AirportDB
    from .weather import SAMPLE, from_snapshot
    from .xpbridge import magnetic_variation

    airport = AirportDB.open(config.sim.xplane_path).airport(ident)
    if not airport:
        print(f"No airport '{ident}' in X-Plane's data.")
        return 1
    state = _live_state(config)
    weather = from_snapshot(state, airport.elev_ft) if state else None
    if weather is None:
        print("(sim not running: using sample weather)")
        weather = SAMPLE
    else:
        print("(weather read from the sim at the aircraft's position)")
    zulu = state.get("zulu_seconds", 12 * 3600)
    from .navdata import Station

    atis_freq = next((f for f in airport.freqs if f.kind == "atis"), None)
    name = Station(airport, "atis", atis_freq.khz, atis_freq.label, 0).place if atis_freq else None
    _, text = AtisBook().text(airport, weather, zulu, magnetic_variation(state), name)
    print(text)
    if speak:
        from .audio_io import play
        from .radio_fx import radio_effect
        from .tts import TextToSpeech

        samples, rate = TextToSpeech(config.tts).speak(text, voice=config.tts.atis_voice, speed=config.tts.atis_speed)
        play(
            radio_effect(samples, rate, config.radio.noise) if config.radio.enabled else samples, rate, config.audio.output_device
        )
    return 0


def cmd_keytest(config) -> int:
    from pynput import keyboard

    print(f"Press any key to see its name (configured push-to-talk key: {config.ptt.key}). Esc to quit.")

    def on_press(key):
        name = key.char if isinstance(key, keyboard.KeyCode) else key.name
        print(f"  key name: {name}")
        return key != keyboard.Key.esc

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()
    return 0


def cmd_run(config) -> int:
    from .voice_loop import run

    run(config)
    return 0


def _real_engine(config, db, seed=None):
    """An engine on this machine's X-Plane data, as the app builds it."""
    from .airspace import open_airspace
    from .airways import open_airways
    from .centers import CenterIndex
    from .engine import Engine
    from .metar import MetarStore
    from .navfix import open_navfix
    from .procedures import NavaidNames

    root = db.xplane_root
    db.centers, db.metars = CenterIndex.open(root), MetarStore(root)
    return Engine(
        db, seed=seed, centers=db.centers, navaids=NavaidNames(root), navfix=open_navfix(root), metars=db.metars,
        holding=config.atc.holding, traffic=config.atc.traffic, airspace=open_airspace(root),
        airways=open_airways(root),
    )  # fmt: skip


def cmd_replay(config, path: str) -> int:
    from .navdata import AirportDB
    from .replay import load, replay

    payload = load(path)
    db = AirportDB.open(config.sim.xplane_path)
    result = replay(payload, db, lambda seed: _real_engine(config, db, seed))
    print(result.summary())
    return 1 if result.differences else 0


def cmd_verify_data(config, count: int) -> int:
    from .navdata import AirportDB
    from .verify import pick_airports, sweep

    db = AirportDB.open(config.sim.xplane_path)
    report = sweep(db, pick_airports(db, count), progress=print)
    print(report.summary())
    return 1 if report.failures else 0


def cmd_verify_flights(config, count: int, show: str | None) -> int:
    from .flights import fly, pick_flights, shared_data, sweep_flights
    from .navdata import AirportDB

    db = AirportDB.open(config.sim.xplane_path)
    if show:
        problems, said = fly(db, shared_data(db), *show.upper().split("-")[:2])
        print("\n".join(said))
        print("problems:", problems or "none")
        return 1 if problems else 0
    report = sweep_flights(db, pick_flights(db, count), progress=print)
    print(report.summary().replace(" airports,", " flights,", 1))
    return 1 if report.failures else 0


def main() -> int:
    from .config import load_config
    from .tts import ModelsMissing

    parser = argparse.ArgumentParser(prog="proatc", description="Voice ATC for X-Plane 12")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="test the connection to X-Plane and print aircraft state")
    sub.add_parser("devices", help="list audio devices")
    sub.add_parser("setup-models", help="download the local voice model files")
    say = sub.add_parser("say", help="speak a sentence through the radio effect")
    say.add_argument("text")
    airport = sub.add_parser("airport", help="show runways and frequencies of an airport, e.g. EGGW")
    airport.add_argument("ident")
    atis = sub.add_parser("atis", help="print (and with --say, speak) the ATIS for an airport")
    atis.add_argument("ident")
    atis.add_argument("--say", action="store_true")
    sub.add_parser("keytest", help="show the name of each key you press, to pick a push-to-talk key")
    sub.add_parser("run", help="start the push-to-talk voice loop")
    replay = sub.add_parser("replay", help="run a saved flight log (.json) through the engine again and show what differs")
    replay.add_argument("log")
    speech = sub.add_parser("bench-speech", help="measure speech recognition on spoken radio calls (needs the speech models)")
    speech.add_argument("--quick", action="store_true")
    speech.add_argument("--engine", choices=("kokoro", "phonon"), help="the voice engine to measure (default: the one in use)")
    sub.add_parser("bench-intents", help="measure how well the language model understands a fixed set of calls (uses the model)")
    verify = sub.add_parser("verify-data", help="run the engine over many real airports of this X-Plane and report problems")
    verify.add_argument("--airports", type=int, default=300)
    flights = sub.add_parser("verify-flights", help="fly whole flights between real airports worldwide and report what broke")
    flights.add_argument("--flights", type=int, default=300)
    flights.add_argument("--show", help="print one flight's radio work, e.g. KBOS-KIAD")
    server = sub.add_parser("serve", help="run the engine for the desktop app (started by the app itself)")
    server.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    config = load_config()
    try:
        if args.command == "say":
            return cmd_say(config, args.text)
        if args.command == "serve":
            from .server import serve_app

            serve_app(config, args.port)
            return 0
        if args.command == "replay":
            return cmd_replay(config, args.log)
        if args.command == "bench-speech":
            from .bench import bench_speech
            from .stt import SpeechToText
            from .tts import TextToSpeech

            if args.engine:
                config.tts.engine = args.engine
            bench_speech(TextToSpeech(config.tts), SpeechToText(config.stt), args.quick)
            return 0
        if args.command == "bench-intents":
            from .bench import bench_intents
            from .config import openrouter_key
            from .llm import Controller

            key = openrouter_key()
            if not key and config.llm.provider != "local":
                print("No OpenRouter key and no local model: nothing to measure.")
                return 1
            right, total, _ = bench_intents(Controller(config.llm, key))
            return 0 if right >= total * 0.9 else 1
        if args.command == "verify-flights":
            return cmd_verify_flights(config, args.flights, args.show)
        if args.command == "verify-data":
            return cmd_verify_data(config, args.airports)
        if args.command == "airport":
            return cmd_airport(config, args.ident)
        if args.command == "atis":
            return cmd_atis(config, args.ident, args.say)
        return {
            "check": cmd_check,
            "devices": cmd_devices,
            "setup-models": cmd_setup_models,
            "keytest": cmd_keytest,
            "run": cmd_run,
        }[args.command](config)
    except ModelsMissing as error:
        print(error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
