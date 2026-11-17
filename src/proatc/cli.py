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


def cmd_sound_check(config) -> int:
    from .soundcheck import run_in_terminal

    return run_in_terminal(config, _live_state(config))


def cmd_report(config, hide: bool) -> int:
    from .config import PROJECT_DIR
    from .report import ISSUE_URL, write
    from .system_check import this_computer

    computer = this_computer()
    for check in computer:
        mark = {True: "ok ", False: "NO ", None: "?  "}[check["ok"]]
        print(f"  {mark} {check['label']}: {check['text']} {check['fix']}".rstrip())
    path = write(PROJECT_DIR, config, hide, computer)
    print(f"\nProblem report saved to {path}\nAttach it to an issue: {ISSUE_URL}")
    return 0


def cmd_run(config) -> int:
    from .voice_loop import run

    run(config)
    return 0


def _real_engine(config, db, seed=None, settings: dict | None = None, reported_weather: bool = True):
    """An engine on this machine's X-Plane data, as the app builds it. `settings` overrides the holding and
    traffic settings of the config. Without `reported_weather` every airport has the sim's weather at the
    aircraft: the METAR file in X-Plane's folder changes from day to day, and a kept flight must not."""
    from .airspace import open_airspace
    from .airways import open_airways
    from .centers import CenterIndex
    from .engine import Engine
    from .metar import MetarStore
    from .navfix import open_navfix
    from .procedures import NavaidNames

    root, settings = db.xplane_root, settings or {}
    db.centers, db.metars = CenterIndex.open(root), MetarStore(root) if reported_weather else None
    return Engine(
        db, seed=seed, centers=db.centers, navaids=NavaidNames(root), navfix=open_navfix(root), metars=db.metars,
        holding=settings.get("holding", config.atc.holding), traffic=settings.get("traffic", config.atc.traffic),
        airspace=open_airspace(root), airways=open_airways(root),
    )  # fmt: skip


def cmd_replay(config, path: str) -> int:
    from .navdata import AirportDB
    from .replay import load, replay

    payload = load(path)
    db = AirportDB.open(config.sim.xplane_path)
    result = replay(payload, db, lambda seed: _real_engine(config, db, seed))
    print(result.summary())
    return 1 if result.differences else 0


def _journal_runner(config):
    """Replays a kept journal on this machine's X-Plane data, with the settings it was kept with."""
    from .navdata import AirportDB
    from .replay import replay

    db = AirportDB.open(config.sim.xplane_path)

    def run(payload: dict, settings: dict):
        return replay(payload, db, lambda seed: _real_engine(config, db, seed, settings, reported_weather=False))

    return run


def _settings(config) -> dict:
    return {"holding": config.atc.holding, "traffic": config.atc.traffic}


def cmd_journals(config, args) -> int:
    """Keep real flights and check them before a release: see journals.py and docs/testing.md."""
    from pathlib import Path

    from . import journals

    folder = Path(args.folder) if args.folder else journals.default_folder()
    if args.action == "list":
        lines = journals.listing(folder)
        print("\n".join(lines) if lines else f"No kept flights in {folder}")
        return 0
    if args.action == "keep":
        source = Path(args.journal) if args.journal else journals.newest_journal(journals.journal_folder())
        if source is None or not source.is_file():
            print(f"No journal to keep: {source or journals.journal_folder()}")
            return 1
        target = journals.keep(source, folder, _journal_runner(config), _settings(config))
        counts = journals.read_accepted(target)["counts"]
        print(f"Kept {source.name} as {target.stem} in {folder}")
        print("Accepted as it replays now:", ", ".join(f"{value} {key.replace('_', ' ')}" for key, value in counts.items()))
        return 0
    if args.action == "accept":
        return _accept(config, journals, folder, args)
    found = journals.check(folder, _journal_runner(config), _settings(config))
    if not found:
        print(f"No kept flights in {folder}. Keep one after a flight: proatc journals keep")
        return 0
    for one in found:
        print(journals.report(one, anonymous=args.anonymous))
    changed = [one.name for one in found if one.changed]
    print(f"\n{len(found)} flights checked, {len(changed)} changed" + (": " + ", ".join(changed) if changed else ""))
    return 1 if changed else 0


def _accept(config, journals, folder, args) -> int:
    if args.all == bool(args.name):
        print("Name one kept flight, or --all")
        return 1
    chosen = journals.kept(folder) if args.all else [journals.find(folder, args.name)]
    if None in chosen:
        print(f"No kept flight called {args.name} in {folder}")
        return 1
    runner = _journal_runner(config)
    for journal in chosen:
        journals.accept_one(journal, runner, _settings(config))
        print(f"Accepted {journal.stem}")
    return 0


def _journal_folders() -> list:
    """Where journals are written: this checkout (or PROATC_HOME), then the installed app's data folder."""
    import os
    from pathlib import Path

    from .config import PROJECT_DIR

    if sys.platform == "win32":
        app = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        app = Path.home() / "Library" / "Application Support"
    else:
        app = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return [PROJECT_DIR / "logs" / "journal", app / "Pro ATC" / "engine" / "logs" / "journal"]


def cmd_health(config, path: str | None) -> int:
    from pathlib import Path

    from .health import as_text, check
    from .replay import load

    if path is None:
        found = [journal for folder in _journal_folders() for journal in folder.glob("session-*.jsonl")]
        if not found:
            print("No journal found in:", *_journal_folders(), sep="\n  ")
            return 2
        target = max(found, key=lambda journal: journal.stat().st_mtime)
    else:
        target = Path(path)
    print(f"Journal: {target}\n")
    findings = check(load(target)["events"], _airport_locator(config))
    print(as_text(findings), end="")
    return 1 if findings else 0


def _airport_locator(config):
    """An airport's position from this machine's X-Plane data; None without X-Plane, and then the
    distance to the destination is judged from where the aircraft landed."""
    from .navdata import AirportDB

    try:
        db = AirportDB.open(config.sim.xplane_path)
    except Exception:
        print("(X-Plane's airport data was not found: distances are judged from the landing, if any.)\n")
        return None

    def locate(ident: str):
        airport = db.airport(ident)
        return (airport.lat, airport.lon) if airport else None

    return locate


def cmd_verify_data(config, count: int) -> int:
    from .navdata import AirportDB
    from .verify import pick_airports, sweep

    db = AirportDB.open(config.sim.xplane_path)
    report = sweep(db, pick_airports(db, count), progress=print)
    print(report.summary())
    return 1 if report.failures else 0


def cmd_verify_flights(config, count: int, show: str | None, full: bool = False) -> int:
    from .flights import fly, pick_flights, shared_data, sweep_flights
    from .navdata import AirportDB

    db = AirportDB.open(config.sim.xplane_path)
    if full:
        return _verify_full(db, count, show)
    if show:
        problems, said = fly(db, shared_data(db), *show.upper().split("-")[:2])
        print("\n".join(said))
        print("problems:", problems or "none")
        return 1 if problems else 0
    report = sweep_flights(db, pick_flights(db, count), progress=print)
    print(report.summary().replace(" airports,", " flights,", 1))
    return 1 if report.failures else 0


def _verify_full(db, count: int, show: str | None) -> int:
    """Gate to gate: clearance, taxi, takeoff, the climb, the cruise, the approach, landing and taxi to a stand."""
    from .flights import fly_full, full_summary, pick_flights, shared_data, sweep_full

    sys.stdout.reconfigure(errors="replace")  # station names in any language, to a console that may not have them
    if show:
        problems, said = fly_full(db, shared_data(db), *show.upper().split("-")[:2])
        print("\n".join(said))
        print("problems:" if problems else "problems: none")
        for kind, detail in problems:
            print(f"  {kind}: {detail}")
        return 1 if problems else 0
    # Both ends need a tower: the flight starts and ends on the ground, with someone to talk to there.
    results, seconds = sweep_full(db, pick_flights(db, count, towered=True), progress=print)
    print(full_summary(results, seconds))
    return 1 if any(problems for _, problems, _ in results) else 0


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
    sub.add_parser("sound-check", help="play each sound in turn and ask whether you heard it; records the result")
    report = sub.add_parser("report", help="write a zip to attach to a bug report (no key, no SimBrief ID)")
    report.add_argument("--hide", action="store_true", help="mask the callsign and round positions in the copy")
    replay = sub.add_parser("replay", help="run a saved flight log (.json) through the engine again and show what differs")
    replay.add_argument("log")
    kept = sub.add_parser("journals", help="keep real flights' journals as regression tests and check them (private)")
    kept_sub = kept.add_subparsers(dest="action", required=True)
    for action, text in (
        ("keep", "copy a journal (default: the newest) into the kept flights and accept its answers"),
        ("check", "replay every kept flight and show the answers that changed since they were accepted"),
        ("accept", "accept the answers a kept flight gets now, once a person has read the changes"),
        ("list", "list the kept flights"),
    ):
        one = kept_sub.add_parser(action, help=text)
        one.add_argument("--folder", help="the kept flights' folder (default: logs/regression in the data folder)")
        if action == "keep":
            one.add_argument("journal", nargs="?")
        if action == "check":
            one.add_argument("--anonymous", action="store_true", help="mask the callsign and round positions")
        if action == "accept":
            one.add_argument("name", nargs="?")
            one.add_argument("--all", action="store_true")
    health = sub.add_parser("health", help="report what went wrong on the radio in a flight's journal (default: the newest)")
    health.add_argument("journal", nargs="?")
    speech = sub.add_parser("bench-speech", help="measure speech recognition on spoken radio calls (needs the speech models)")
    speech.add_argument("--quick", action="store_true")
    speech.add_argument("--engine", choices=("kokoro", "phonon"), help="the voice engine to measure (default: the one in use)")
    sub.add_parser("bench-intents", help="measure how well the language model understands a fixed set of calls (uses the model)")
    verify = sub.add_parser("verify-data", help="run the engine over many real airports of this X-Plane and report problems")
    verify.add_argument("--airports", type=int, default=300)
    flights = sub.add_parser("verify-flights", help="fly whole flights between real airports worldwide and report what broke")
    flights.add_argument("--flights", type=int, default=300)
    flights.add_argument("--show", help="print one flight's radio work, e.g. KBOS-KIAD")
    flights.add_argument(
        "--full", action="store_true", help="fly gate to gate: from a stand at the origin to a stand at the destination"
    )
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
        if args.command == "report":
            return cmd_report(config, args.hide)
        if args.command == "replay":
            return cmd_replay(config, args.log)
        if args.command == "journals":
            return cmd_journals(config, args)
        if args.command == "health":
            return cmd_health(config, args.journal)
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
            return cmd_verify_flights(config, args.flights, args.show, args.full)
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
            "sound-check": cmd_sound_check,
        }[args.command](config)
    except ModelsMissing as error:
        print(error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
