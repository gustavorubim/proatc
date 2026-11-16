"""Local WebSocket server that lets the desktop app drive the engine.

Listens on 127.0.0.1 only and requires the token the app passed in PROATC_TOKEN, so
another program or a web page on this machine cannot key the microphone or change the key.

Engine -> app: every event from events.py, plus a "state" message about once a second.
App -> engine: {"cmd": "ptt", "down": bool}, {"cmd": "say", "text": str},
               {"cmd": "click", "text": str, "intent": {...}}, {"cmd": "set_key", "key": str}
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from dataclasses import fields
from urllib.parse import parse_qs, urlparse

from websockets.sync.server import serve

from . import phraseology as say
from .atis import runways_in_use, uses_faa_style
from .config import PROJECT_DIR, Config, env_value
from .flightplan import PlanError, fetch_simbrief, manual
from .intents import Intent
from .navdata import AirportDB
from .radio import current_station, observe
from .voice_loop import Session
from .xpbridge import SimUnavailable, describe, magnetic_variation

BACKLOG = 200


def save_key(key: str) -> None:
    """Store the OpenRouter key in .env, replacing any existing one."""
    env = PROJECT_DIR / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [line for line in lines if not line.strip().startswith("OPENROUTER_API_KEY")]
    lines.append(f"OPENROUTER_API_KEY={key}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_settings() -> dict:
    """Small things the app remembers between runs (not secrets): the SimBrief username."""
    path = PROJECT_DIR / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        settings = {}
    # SIMBRIEF_USER in .env is the default until another account is used in the app.
    if not settings.get("simbrief_user") and env_value("SIMBRIEF_USER"):
        settings["simbrief_user"] = env_value("SIMBRIEF_USER")
    return settings


def save_settings(changes: dict) -> None:
    settings = {**load_settings(), **changes}
    (PROJECT_DIR / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")


def to_intent(data: dict) -> Intent:
    known = {f.name for f in fields(Intent)}
    return Intent(**{k: v for k, v in data.items() if k in known})


def build_state(session: Session, db: AirportDB) -> dict:
    """Everything the app shows that is not a one-off event."""
    config = session.config
    state: dict = {
        "type": "state",
        "ready": session.ready,
        "status": {
            "sim": bool(session.ready and session.bridge.connected),
            "has_key": session.controller is not None,
            "model": config.llm.model,
            "ptt_key": config.ptt.key,
            "stt_device": session.stt.device if session.ready else None,
        },
        "aircraft": None,
        "radio": None,
        "airport": None,
        "flight": None,
        "plan": None,
        "simbrief_user": load_settings().get("simbrief_user", ""),
        "suggestions": [],
    }
    if session.ready and session.engine.flight.plan:
        state["plan"] = session.engine.flight.plan.summary()
    if not state["status"]["sim"]:
        return state
    raw = session.bridge.snapshot()
    state["aircraft"] = describe(raw)
    khz, station = current_station(db, raw)
    state["radio"] = {
        "mhz": f"{khz / 1000:.3f}" if khz else None,
        "station": {"callsign": station.callsign, "kind": station.kind,
                    "airport": station.airport.ident if station.airport else None,
                    "distance_nm": round(station.distance_nm, 1)} if station else None,
    }  # fmt: skip
    airport = station.airport if station else None
    if airport is None and "lat" in raw:
        near = db.nearest(raw["lat"], raw["lon"], 8, 1)
        airport = near[0][1] if near else None
    issue = None
    if airport:
        weather = observe(db, airport, raw)
        issue = session.book.current(airport, weather, raw.get("zulu_seconds", 0.0)) if weather else None
        faa = uses_faa_style(airport.ident)
        info = {
            "ident": airport.ident,
            "name": airport.name,
            "frequencies": [
                {"mhz": f"{f.khz / 1000:.3f}", "kind": f.kind, "label": f.label, "tuned": bool(khz and abs(f.khz - khz) <= 5)}
                for f in sorted(airport.freqs, key=lambda f: ("atis delivery ground tower departure approach unicom".split().index(f.kind), f.khz))
            ],
        }  # fmt: skip
        if issue:
            w = issue.weather
            direction = round(((w.wind_dir_true + magnetic_variation(raw)) % 360) / 10) * 10 or 360
            info.update(
                atis_letter=issue.letter,
                atis_name=say.letter(issue.letter),
                runways_in_use=runways_in_use(airport, w.wind_dir_true, w.wind_kt),
                wind="calm" if w.wind_kt < 1 else f"{direction:03d}° / {round(w.wind_kt)} kt",
                pressure=f"{w.qnh_hpa * 0.02953:.2f} inHg" if faa else f"QNH {int(w.qnh_hpa)}",
                temperature=f"{round(w.temperature_c)}°C",
            )
        state["airport"] = info
    state["flight"] = session.engine.summary()
    # A hand-off frequency stops being offered once it is tuned.
    pending = session.engine.flight.next_frequency
    if pending and khz and abs(float(pending) * 1000 - khz) <= 5:
        session.engine.flight.next_frequency = None
        state["flight"].pop("next_frequency", None)
    state["status"]["can_tune"] = config.sim.allow_tuning
    if station and station.kind != "atis":
        state["suggestions"] = session.engine.suggestions(station, issue, raw)
    return state


class AppServer:
    def __init__(self, config: Config, port: int, token: str):
        self.port, self.token = port, token
        self.session = Session(config, emit=self.emit)
        self.session.download_models = True
        self.clients: set = set()
        self.backlog: deque = deque(maxlen=BACKLOG)
        self.lock = threading.Lock()
        self._last_state = ""

    # ---- outgoing -----------------------------------------------------------

    def emit(self, event: dict) -> None:
        if event.get("type") != "tx":
            self.backlog.append(event)
        self._broadcast(json.dumps(event))
        if event.get("type") in ("status", "error", "info"):
            print(event["text"].strip(), flush=True)  # also in the engine's own log

    def _broadcast(self, message: str) -> None:
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client.send(message)
            except Exception:
                with self.lock:
                    self.clients.discard(client)

    def _state_loop(self) -> None:
        db = None
        stop = threading.Event()
        while not stop.wait(0.7):
            try:
                if self.session.ready and db is None:
                    db = AirportDB()
                    db.centers, db.metars = self.session.centers, self.session.metars
                message = json.dumps(build_state(self.session, db))
            except Exception as error:
                message = json.dumps({"type": "error", "text": f"state: {type(error).__name__}: {error}"})
            if message != self._last_state:
                self._last_state = message
                self._broadcast(message)

    # ---- incoming -----------------------------------------------------------

    def _handle(self, connection) -> None:
        query = parse_qs(urlparse(connection.request.path).query)
        if query.get("token", [""])[0] != self.token:
            connection.close(1008, "bad token")
            return
        with self.lock:
            self.clients.add(connection)
        try:
            for event in list(self.backlog):
                connection.send(json.dumps(event))
            if self._last_state:
                connection.send(self._last_state)
            for message in connection:
                try:
                    self._command(json.loads(message))
                except (PlanError, SimUnavailable, ValueError) as error:
                    self.emit({"type": "error", "text": str(error) or "That did not work."})
                except Exception as error:
                    self.emit({"type": "error", "text": f"command failed: {type(error).__name__}: {error}"})
        finally:
            with self.lock:
                self.clients.discard(connection)

    def _command(self, command: dict) -> None:
        session, name = self.session, command.get("cmd")
        if name == "set_key":
            key = str(command.get("key", "")).strip()
            if key:
                save_key(key)
                session.set_key(key)
                self.emit({"type": "status", "text": "OpenRouter key saved."})
            return
        if not session.ready:
            return
        if name == "simbrief":
            user = str(command.get("user", "")).strip()
            plan = fetch_simbrief(user)
            save_settings({"simbrief_user": user})
            session.engine.set_plan(plan)
            self.emit({"type": "info", "text": f"Flight plan loaded from SimBrief: {plan.origin} to {plan.destination}."})
        elif name == "plan":
            plan = manual(str(command.get("destination", "")), command.get("cruise"), str(command.get("route", "")))
            session.engine.set_plan(plan)
            self.emit({"type": "info", "text": f"Flight plan set: destination {plan.destination}."})
        elif name == "clear_plan":
            session.engine.set_plan(None)
        elif name == "tune":
            session.tune(str(command.get("mhz", "")))
        elif name == "ptt":
            session.ptt.press() if command.get("down") else session.ptt.release()
        elif name == "say" and str(command.get("text", "")).strip():
            session.submit(str(command["text"]).strip()[:500])
        elif name == "click" and str(command.get("text", "")).strip():
            session.submit(str(command["text"]).strip()[:500], to_intent(command.get("intent") or {}))

    # ---- lifecycle ----------------------------------------------------------

    def _selftest(self) -> None:
        """For the packaged-app test: prove every dependency is installed and loadable, without
        downloading speech models or needing a microphone, then stay up until the app has seen it."""
        import ctranslate2
        import faster_whisper
        import kokoro_onnx
        import onnxruntime
        import pynput
        import scipy
        import sounddevice

        versions = {
            m.__name__: getattr(m, "__version__", "ok") for m in (ctranslate2, faster_whisper, onnxruntime, scipy, sounddevice)
        }
        del kokoro_onnx, pynput
        self.emit({"type": "status", "text": f"SELFTEST OK {json.dumps(versions)}"})
        threading.Event().wait(60)

    def run(self) -> None:
        server = serve(self._handle, "127.0.0.1", self.port)
        threading.Thread(target=server.serve_forever, name="app-server", daemon=True).start()
        threading.Thread(target=self._state_loop, name="app-state", daemon=True).start()
        print(f"SERVING ws://127.0.0.1:{self.port}", flush=True)
        if os.environ.get("PROATC_SELFTEST"):
            self._selftest()
            return
        try:
            self.session.load()
            self.session.run_forever()
        finally:
            server.shutdown()


def serve_app(config: Config, port: int) -> None:
    token = os.environ.get("PROATC_TOKEN", "")
    if not token:
        raise SystemExit("PROATC_TOKEN is not set. The desktop app sets it when it starts the engine.")
    AppServer(config, port, token).run()
