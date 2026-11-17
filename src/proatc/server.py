"""Local WebSocket server that lets the desktop app drive the engine.

Listens on 127.0.0.1 only and requires the token the app passed in PROATC_TOKEN, so
another program or a web page on this machine cannot key the microphone or change the key.

Engine -> app: every event from events.py, plus a "state" message about once a second.
App -> engine: {"cmd": "ptt", "down": bool}, {"cmd": "say", "text": str},
               {"cmd": "click", "text": str, "intent": {...}}, {"cmd": "set_key", "key": str}

The view for other devices (a phone or tablet on the same network) is a second listener, off
until it is switched on in the settings. It serves the app's own page over HTTP and accepts the
same messages, but asks for a pairing code instead of the app's token, may not change the key,
the language model or its own switch, and locks out a device that keeps guessing the code.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.sync.server import serve

from . import phraseology as say
from .airspace import CONTROLLED
from .atis import runways_in_use, uses_faa_style
from .config import PROJECT_DIR, Config, editable_values, load_settings, save_settings
from .flightplan import PlanError, fetch_simbrief, manual
from .intents import Intent
from .navdata import AirportDB
from .radio import current_station, observe
from .voice_loop import Session
from .xpbridge import SimUnavailable, describe, magnetic_variation

BACKLOG = 200
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I: the code is typed on a phone
MAX_BAD_CODES = 10  # wrong pairing codes within LOCKOUT_S before other devices are refused for a while
LOCKOUT_S = 300
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".json": "application/json",
}


def web_dir() -> Path | None:
    """The app's built page, which the desktop app points to. None from a bare `proatc serve`."""
    folder = os.environ.get("PROATC_WEB_DIR")
    return Path(folder) if folder and (Path(folder) / "index.html").exists() else None


def lan_address() -> str:
    """This machine's address on the local network. Nothing is sent: connecting a UDP socket only picks a route."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def pairing_code() -> str:
    """The code another device must enter. Made once and kept, so a bookmarked phone keeps working."""
    code = load_settings().get("remote_code")
    if not isinstance(code, str) or len(code) != 8:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        save_settings({"remote_code": code})
    return code


def save_key(key: str) -> None:
    """Store the OpenRouter key in .env, replacing any existing one."""
    env = PROJECT_DIR / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [line for line in lines if not line.strip().startswith("OPENROUTER_API_KEY")]
    lines.append(f"OPENROUTER_API_KEY={key}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


def to_intent(data: dict) -> Intent:
    return Intent.from_dict(data)


def build_state(session: Session, db: AirportDB) -> dict:
    """Everything the app shows that is not a one-off event."""
    config = session.config
    state: dict = {
        "type": "state",
        "ready": session.ready,
        "status": {
            "sim": bool(session.ready and session.bridge.connected),
            "has_key": session.controller is not None,
            "model": config.llm.local_model if config.llm.provider == "local" else config.llm.model,
            "provider": config.llm.provider,
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
        "airspace": [],
        "map": None,
        "settings": {"values": editable_values(config), "options": session.options, "capturing": session.capturing},
        "cost": {**session.usage, "cost": round(session.usage["cost"], 5)},
    }
    if session.joystick:
        state["settings"]["options"] = {**session.options, "joysticks": list(session.joystick.names)}
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
    state["map"] = session.engine.taxi_map(raw)
    index = session.engine.airspace
    if index and "lat" in raw:
        feet = raw.get("elevation_m", 0.0) / 0.3048
        inside = [space for space in index.at(raw["lat"], raw["lon"], feet) if space.kind in CONTROLLED or space.area <= 1.0]
        state["airspace"] = [space.describe() for space in inside[:2]]
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
        self.remote = None  # the listener for other devices, while it is switched on
        self._bad_codes: deque = deque(maxlen=MAX_BAD_CODES)
        self._address = lan_address()

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
                state = build_state(self.session, db)
                state["remote"] = self.remote_info()
                message = json.dumps(state)
            except Exception as error:
                message = json.dumps({"type": "error", "text": f"state: {type(error).__name__}: {error}"})
            if message != self._last_state:
                self._last_state = message
                self._broadcast(message)

    # ---- incoming -----------------------------------------------------------

    def _allowed(self, token: str, remote: bool) -> bool:
        if not remote:
            return hmac.compare_digest(token, self.token)
        now = time.monotonic()
        if len(self._bad_codes) == MAX_BAD_CODES and now - self._bad_codes[0] < LOCKOUT_S:
            return False  # too many wrong codes: nobody gets in from outside for a while
        if hmac.compare_digest(token.upper(), pairing_code()):
            return True
        self._bad_codes.append(now)
        return False

    def _handle(self, connection, remote: bool = False) -> None:
        query = parse_qs(urlparse(connection.request.path).query)
        if not self._allowed(query.get("token", [""])[0], remote):
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
                    self._command(json.loads(message), remote)
                except (PlanError, SimUnavailable, ValueError) as error:
                    self.emit({"type": "error", "text": str(error) or "That did not work."})
                except Exception as error:
                    self.emit({"type": "error", "text": f"command failed: {type(error).__name__}: {error}"})
        finally:
            with self.lock:
                self.clients.discard(connection)

    def _command(self, command: dict, remote: bool = False) -> None:
        session, name = self.session, command.get("cmd")
        if remote and name == "set_key":
            raise ValueError("The key can only be changed in the app on the computer.")
        if name == "settings":
            # Before the engine is ready too: the model and the view for other devices do not need it.
            changes = dict(command.get("changes") or {})
            if remote:
                changes.pop("remote", None)
                changes.pop("llm", None)
            session.apply_settings(changes)
            if "remote" in changes:
                self.set_remote(session.config.remote.enabled)
            return
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
        elif name == "new_flight":
            session.engine.reset()
            self.emit({"type": "info", "text": "New flight: clearances and contacts cleared."})
        elif name == "chart":
            found = session.engine.chart(str(command.get("ident", "")).strip().upper()[:8])
            if found:
                self.emit({"type": "chart", **found})
        elif name == "tune":
            session.tune(str(command.get("mhz", "")))
        elif name == "capture_ptt":
            session.capture_ptt()
        elif name == "clear_joystick":
            session.apply_settings({"ptt": {"joystick": None, "joystick_button": None}})
        elif name in ("explain", "what_next", "debrief"):
            session.ask(name)
        elif name == "save_log":
            session.save_log()
        elif name == "reset_cost":
            session.usage.update(calls=0, prompt_tokens=0, completion_tokens=0, cost=0.0)
        elif name == "ptt":
            session.ptt.press() if command.get("down") else session.ptt.release()
        elif name == "phrase" and str(command.get("text", "")).strip():
            session.phrase(str(command["text"]))
        elif name == "say" and str(command.get("text", "")).strip():
            session.submit(str(command["text"]).strip()[:500])
        elif name == "click" and str(command.get("text", "")).strip():
            session.submit(str(command["text"]).strip()[:500], Intent.from_dict(command.get("intent") or {}))

    # ---- the view for other devices ------------------------------------------

    def remote_info(self) -> dict:
        config = self.session.config.remote
        return {
            "available": web_dir() is not None,
            "enabled": config.enabled,
            "running": self.remote is not None,
            "url": f"http://{self._address}:{config.port}",
            "code": pairing_code() if config.enabled else "",
        }

    def set_remote(self, on: bool) -> None:
        """Start or stop listening for other devices on the network."""
        if not on:
            if self.remote:
                self.remote.shutdown()
                self.remote = None
            return
        if self.remote:
            return
        if web_dir() is None:
            raise ValueError("The view for other devices needs the desktop app: its page was not found.")
        port = self.session.config.remote.port
        try:
            self.remote = serve(lambda connection: self._handle(connection, True), "0.0.0.0", port, process_request=self._page)
        except OSError as error:
            raise ValueError(f"Port {port} could not be opened for other devices: {error}") from error
        threading.Thread(target=self.remote.serve_forever, name="remote-view", daemon=True).start()
        self._address = lan_address()

    @staticmethod
    def _page(connection, request):
        """Serve the app's page to a browser; let WebSocket requests through."""
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        root = web_dir()
        path = urlparse(request.path).path
        target = (root / (path.lstrip("/") or "index.html")).resolve() if root else None
        if target is None or not target.is_file() or root.resolve() not in target.parents:
            return connection.respond(404, "Not found\n")
        body = target.read_bytes()
        headers = Headers(
            {
                "Content-Type": CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"),
                "Content-Length": str(len(body)),
                "Cache-Control": "no-cache",
                "X-Content-Type-Options": "nosniff",
            }
        )
        return Response(200, "OK", headers, body)

    # ---- lifecycle ----------------------------------------------------------

    def _selftest(self) -> None:
        """For the packaged-app test: prove every dependency is installed and loadable, without
        downloading speech models or needing a microphone, then stay up until the app has seen it."""
        import ctranslate2
        import faster_whisper
        import kokoro_onnx
        import onnxruntime
        import ptts
        import pygame
        import pynput
        import scipy
        import sounddevice

        versions = {
            m.__name__: getattr(m, "__version__", "ok") for m in (ctranslate2, faster_whisper, onnxruntime, scipy, sounddevice)
        }
        del kokoro_onnx, pynput, pygame, ptts
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
        if self.session.config.remote.enabled:
            try:
                self.set_remote(True)
            except ValueError as error:
                self.emit({"type": "error", "text": str(error)})
        try:
            self.session.load()
            self.session.run_forever()
        finally:
            self.set_remote(False)
            server.shutdown()


def serve_app(config: Config, port: int) -> None:
    token = os.environ.get("PROATC_TOKEN", "")
    if not token:
        raise SystemExit("PROATC_TOKEN is not set. The desktop app sets it when it starts the engine.")
    AppServer(config, port, token).run()
