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
