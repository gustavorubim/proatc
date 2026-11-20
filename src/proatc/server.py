"""Local WebSocket server that lets the desktop app drive the engine.

Listens on 127.0.0.1 only and requires the token the app passed in PROATC_TOKEN, so
another program or a web page on this machine cannot key the microphone or change the key.

Engine -> app: every event from events.py, plus a "state" message about once a second.
App -> engine: {"cmd": "ptt", "down": bool}, {"cmd": "say", "text": str},
               {"cmd": "click", "text": str, "intent": {...}}, {"cmd": "set_key", "key": str},
               {"cmd": "sound_check"}, {"cmd": "sound_check_answer", "heard": bool | None}, {"cmd": "report", "hide": bool}

The view for other devices (a phone or tablet on the same network) is a second listener, off
until it is switched on in the settings. It serves the app's own page over plain HTTP: what it
shows can be seen by anyone watching the network, so it shows and allows less than the app.
- Pairing: a code shown on the computer for a few minutes, made new each time and good for one
  device. That device is then given a token of its own, kept by its browser; the computer can
  forget every paired device at once. A source that keeps guessing is refused for a while.
- A paired device sends calls (typed, clicked, asked for), tunes, loads a plan and changes the ATC
  and voice settings. It never keys the computer's microphone, captures a key, runs the sound
  check, writes files, or changes the key, the model, push-to-talk, audio devices or its own
  switch. It is not sent the SimBrief ID, the settings or the paths of saved files.
"""

from __future__ import annotations

import hashlib
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
from .atis import decoded, runways_in_use, uses_faa_style
from .config import PROJECT_DIR, Config, editable_values, load_settings, private, save_settings
from .flightplan import PlanError, fetch_simbrief, manual
from .intents import Intent
from .navdata import AirportDB, distance_nm
from .radio import current_station, monitored, observe
from .voice_loop import Session
from .xpbridge import SimUnavailable, describe, magnetic_variation, same_channel

BACKLOG = 200
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I: the code is typed on a phone
MAX_BAD_CODES = 5  # wrong pairing codes from one source within LOCKOUT_S before it is refused for a while
LOCKOUT_S = 300
MAX_BAD_PAIRING = 40  # wrong codes from anywhere while a pairing window is open before it closes
PAIRING_WINDOW_S = 300  # how long a pairing code shown on the computer is good for
MAX_DEVICES = 8  # paired devices kept; the oldest is forgotten first
# What a paired device may do. Everything else is the computer's: the microphone, the settings that are not
# about ATC or voices, the key and the model, files, the sound check.
REMOTE_COMMANDS = frozenset(("say", "click", "phrase", "explain", "what_next", "debrief", "tune", "chart", "simbrief",
                             "plan", "clear_plan", "new_flight", "settings"))  # fmt: skip
REMOTE_SETTINGS = frozenset(("atc", "tts", "radio"))
# Calls that may cost the user's OpenRouter credit: a paired device makes no more than this many a minute.
PAID_COMMANDS = frozenset(("say", "click", "phrase", "explain", "what_next", "debrief"))
REMOTE_PAID_PER_MINUTE = 10
# Events for the computer's own window only: they name files on it.
PRIVATE_EVENTS = frozenset(("log_saved", "report_saved"))
# Parts of the state a paired device is not sent: the SimBrief ID, the settings (a local model's address may
# carry a password), this computer's checks, the pairing code, the sound check.
PRIVATE_STATE = ("simbrief_user", "settings", "computer", "remote", "sound_check")
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


def page_files() -> dict[str, Path]:
    """The page's files by the name a browser asks for them: 'index.html', 'assets/app.js'."""
    root = web_dir()
    if root is None:
        return {}
    root = root.resolve()
    if root != _PAGE_FILES[0]:
        files = {}
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in CONTENT_TYPES:
                files[path.relative_to(root).as_posix()] = path
        _PAGE_FILES[:] = [root, files]
    return _PAGE_FILES[1]


_PAGE_FILES: list = [None, {}]  # (the folder, its files): the page does not change while the app runs


def for_remote(state: dict) -> dict:
    """The state as a paired device is sent it."""
    return {key: value for key, value in state.items() if key not in PRIVATE_STATE}


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


def new_code() -> str:
    """A pairing code: eight characters a person can read off one screen and type on another."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def paired_devices() -> list[str]:
    """The paired devices, as hashes of their tokens: what is kept on disk lets nobody in."""
    found = load_settings().get("remote_devices")
    return [entry for entry in found if isinstance(entry, str)] if isinstance(found, list) else []


def save_key(key: str) -> None:
    """Store the OpenRouter key in .env, replacing any existing one. Readable by this user only."""
    if any(c in key for c in "\r\n\0") or not key.isprintable():
        raise ValueError("That does not look like an OpenRouter key.")
    env = PROJECT_DIR / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [line for line in lines if not line.strip().startswith("OPENROUTER_API_KEY")]
    lines.append(f"OPENROUTER_API_KEY={key}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    private(env)


def to_intent(data: dict) -> Intent:
    return Intent.from_dict(data)


LIST_WITHIN_NM = 8  # the airport the aircraft is at
DESTINATION_WITHIN_NM = 80  # further out, the destination's frequencies are the ones wanted
CONTROLLED_WITHIN_NM = 40  # with no plan: the nearest airport that has a controller


def airport_to_list(session: Session, db: AirportDB, raw: dict):
    """The airport whose frequencies the app lists while no airport's station is tuned."""
    lat, lon = raw["lat"], raw["lon"]
    close = [airport for _, airport in db.nearest(lat, lon, LIST_WITHIN_NM, 6) if airport.freqs]
    if close:
        return close[0]
    plan = session.engine.flight.plan
    if plan and plan.destination and not raw.get("on_ground", True):
        destination = db.airport(plan.destination)
        if destination and destination.freqs and distance_nm(lat, lon, destination.lat, destination.lon) <= DESTINATION_WITHIN_NM:
            return destination
    for _, airport in db.nearest(lat, lon, CONTROLLED_WITHIN_NM, 40):
        if any(freq.kind in ("tower", "approach") for freq in airport.freqs):
            return airport
    return None


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
        "centre": None,
        "map": None,
        "settings": {"values": editable_values(config), "options": session.options, "capturing": session.capturing},
        "cost": {**session.usage, "cost": round(session.usage["cost"], 5)},
        "computer": session.computer,
        "sound_check": {"running": session._checking, "last": session.last_sound_check},
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
    other, listened = monitored(db, raw)
    if other["khz"]:
        state["radio"]["second"] = {
            "radio": other["radio"],
            "mhz": f"{other['khz'] / 1000:.3f}",
            "listening": other["listening"] and config.atc.second_radio,
            "station": listened.callsign if listened else None,
            "kind": listened.kind if listened else None,
        }
    airport = station.airport if station else None
    if airport is None and "lat" in raw:
        airport = airport_to_list(session, db, raw)
    centre = getattr(db, "centers", None)
    if centre and "lat" in raw and not raw.get("on_ground", True):
        # The centre for where the aircraft is: the way back for a pilot left on a frequency nobody answers.
        here = centre.at(raw["lat"], raw["lon"], raw.get("elevation_m", 0.0) / 0.3048)
        if here and here.freqs_khz:
            working = next((f for f in here.freqs_khz if khz and abs(f - khz) <= 5), here.freqs_khz[0])
            state["centre"] = {
                "mhz": f"{working / 1000:.3f}",
                "callsign": here.callsign,
                "tuned": bool(khz and abs(working - khz) <= 5),
            }
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
                atis=decoded(airport, w, issue.letter, issue.zulu_seconds, magnetic_variation(raw)),
            )
        state["airport"] = info
    state["flight"] = session.engine.summary()
    state["map"] = session.engine.taxi_map(raw)
    index = session.engine.airspace
    if index and "lat" in raw:
        feet = raw.get("elevation_m", 0.0) / 0.3048
        inside = [space for space in index.at(raw["lat"], raw["lon"], feet) if space.kind in CONTROLLED or space.area <= 1.0]
        state["airspace"] = [space.describe() for space in inside[:2]]
    # A hand-off frequency stops being offered once it is tuned. The engine keeps it until a call is made on
    # it (the copilot checks in on it): this thread only reads it.
    pending = state["flight"].get("next_frequency")
    if pending and khz and same_channel(int(round(float(pending) * 1000)), khz):
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
        self.remote_clients: set = set()  # connections from paired devices
        self._bad_codes: dict[str, deque] = {}  # source address -> when its wrong codes came
        self._bad_pairing = 0  # wrong codes from anywhere while the pairing window is open
        self._code = ""  # the pairing code while a pairing window is open
        self._pairing_until = 0.0
        self._paid: dict = {}  # paired connection -> when its paid calls came
        self._ptt_owner = None  # the connection that keyed the microphone, while it is keyed
        self._last_remote_state = ""
        self._address = lan_address()

    # ---- outgoing -----------------------------------------------------------

    def emit(self, event: dict) -> None:
        if event.get("type") != "tx":
            self.backlog.append(event)
        self._broadcast(json.dumps(event), None if event.get("type") in PRIVATE_EVENTS else json.dumps(event))
        if event.get("type") in ("status", "error", "info"):
            print(event["text"].strip(), flush=True)  # also in the engine's own log

    def _broadcast(self, message: str, remote_message: str | None = None) -> None:
        """`message` to the computer's window, `remote_message` to paired devices (None: not to them)."""
        with self.lock:
            clients = [(client, client in self.remote_clients) for client in self.clients]
        for client, remote in clients:
            text = remote_message if remote else message
            if text is None:
                continue
            try:
                client.send(text)
            except Exception:
                with self.lock:
                    self.clients.discard(client)
                    self.remote_clients.discard(client)

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
                remote_message = json.dumps(for_remote(state))
            except Exception as error:
                message = remote_message = json.dumps({"type": "error", "text": f"state: {type(error).__name__}"})
            if message != self._last_state or remote_message != self._last_remote_state:
                self._last_state, self._last_remote_state = message, remote_message
                self._broadcast(message, remote_message)

    # ---- incoming -----------------------------------------------------------

    def _allowed(self, token: str, remote: bool, source: str = "") -> bool | str:
        """Whether a connection may come in: the app's token from this computer; from another device, its
        own token, or the pairing code while the window for it is open ('paired': give it a token)."""
        if not remote:
            return hmac.compare_digest(token.encode(), self.token.encode())
        digest = _digest(token)
        if token and any(hmac.compare_digest(digest, known) for known in paired_devices()):
            return "device"
        now = time.monotonic()
        bad = self._bad_codes.setdefault(source, deque(maxlen=MAX_BAD_CODES))
        if len(bad) == MAX_BAD_CODES and now - bad[0] < LOCKOUT_S:
            return False  # this source keeps guessing: refused for a while; other devices are not
        if self.pairing_open() and hmac.compare_digest(token.upper().encode(), self._code.encode()):
            self.close_pairing()  # one device per code
            return "paired"
        bad.append(now)
        if self.pairing_open():
            self._bad_pairing += 1
            if self._bad_pairing >= MAX_BAD_PAIRING:
                self.close_pairing()  # guessed at from many places: open it again on the computer
        return False

    @staticmethod
    def _same_origin(connection) -> bool:
        """A browser says which page opened a connection: only the page this server gave may connect."""
        headers = connection.request.headers
        origin = headers.get("Origin")
        return origin is None or urlparse(origin).netloc == headers.get("Host", "")

    def _handle(self, connection, remote: bool = False) -> None:
        query = parse_qs(urlparse(connection.request.path).query)
        source = str((connection.remote_address or ("",))[0])
        granted = (self._same_origin(connection) or not remote) and self._allowed(query.get("token", [""])[0], remote, source)
        if not granted:
            connection.close(1008, "bad token")
            return
        if granted == "paired":
            connection.send(json.dumps({"type": "paired", "device": self._new_device()}))
        with self.lock:
            self.clients.add(connection)
            if remote:
                self.remote_clients.add(connection)
        try:
            for event in list(self.backlog):
                if not (remote and event.get("type") in PRIVATE_EVENTS):
                    connection.send(json.dumps(event))
            last = self._last_remote_state if remote else self._last_state
            if last:
                connection.send(last)
            for message in connection:
                try:
                    self._command(json.loads(message), remote, connection)
                except (PlanError, SimUnavailable, ValueError) as error:
                    self.emit({"type": "error", "text": str(error) or "That did not work."})
                except Exception as error:
                    self.emit({"type": "error", "text": f"command failed: {type(error).__name__}: {error}"})
        finally:
            with self.lock:
                self.clients.discard(connection)
                self.remote_clients.discard(connection)
                self._paid.pop(connection, None)
            if self._ptt_owner is connection and getattr(self.session, "ptt", None) is not None:
                # The window that keyed the microphone has gone: the transmission ends, the microphone stops.
                self._ptt_owner = None
                self.session.ptt.release()

    # ---- pairing --------------------------------------------------------------

    def pairing_open(self) -> bool:
        return bool(self._code) and time.monotonic() < self._pairing_until

    def open_pairing(self) -> None:
        """A new code, good for one device for a few minutes."""
        self._code, self._pairing_until, self._bad_pairing = new_code(), time.monotonic() + PAIRING_WINDOW_S, 0

    def close_pairing(self) -> None:
        self._code, self._pairing_until = "", 0.0

    @staticmethod
    def _new_device() -> str:
        """A token for a device that has just paired. Only its hash is kept."""
        token = secrets.token_urlsafe(24)
        save_settings({"remote_devices": [*paired_devices(), _digest(token)][-MAX_DEVICES:]})
        return token

    def forget_devices(self) -> None:
        """Every paired device must pair again; those connected now are cut off."""
        save_settings({"remote_devices": []})
        self.close_pairing()
        with self.lock:
            connected = list(self.remote_clients)
        for client in connected:
            try:
                client.close(1008, "forgotten")
            except Exception:
                pass  # gone already

    def _paid_allowed(self, connection) -> bool:
        """A paired device's calls that may cost credit: no more than REMOTE_PAID_PER_MINUTE a minute."""
        now = time.monotonic()
        with self.lock:
            times = self._paid.setdefault(connection, deque(maxlen=REMOTE_PAID_PER_MINUTE))
            if len(times) == REMOTE_PAID_PER_MINUTE and now - times[0] < 60:
                return False
            times.append(now)
        return True

    def _command(self, command: dict, remote: bool = False, connection=None) -> None:
        session, name = self.session, command.get("cmd")
        if remote and name == "set_key":
            raise ValueError("The key can only be changed in the app on the computer.")
        if remote and name not in REMOTE_COMMANDS:
            raise ValueError("That can only be done in the app on the computer.")
        if remote and name in PAID_COMMANDS and not self._paid_allowed(connection):
            raise ValueError("Too many requests from this device: wait a minute.")
        if name == "pair_device":
            self.open_pairing()
            return
        if name == "forget_devices":
            self.forget_devices()
            return
        if name == "settings":
            # Before the engine is ready too: the model and the view for other devices do not need it.
            changes = dict(command.get("changes") or {})
            if remote:
                changes = {section: values for section, values in changes.items() if section in REMOTE_SETTINGS}
            switched_on = bool((changes.get("remote") or {}).get("enabled")) and not self.remote
            session.apply_settings(changes)
            if "remote" in changes:
                self.set_remote(session.config.remote.enabled)
                if switched_on and self.remote:
                    self.open_pairing()  # switched on now: a code to pair the first device with
            return
        if name == "set_key":
            key = str(command.get("key", "")).strip()
            if key:
                save_key(key)
                session.set_key(key)
                self.emit({"type": "status", "text": "OpenRouter key saved."})
            return
        if name == "report":
            # Before the engine is ready too: a report is most wanted when something went wrong at start.
            session.write_report(command.get("hide") is True)
            return
        if not session.ready:
            return
        if name == "sound_check":
            session.start_sound_check()
        elif name == "sound_check_answer":
            heard = command.get("heard")
            session.answer_sound_check(heard if isinstance(heard, bool) else None)
        elif name == "simbrief":
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
            session.tune(str(command.get("mhz", "")), other=command.get("radio") == "other")
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
            if command.get("down"):
                session.ptt.press()
                self._ptt_owner = connection
            else:
                self._ptt_owner = None
                session.ptt.release()
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
            "code": self._code if config.enabled and self.pairing_open() else "",
            "pairing_s": max(0, round(self._pairing_until - time.monotonic())) if self.pairing_open() else 0,
            "devices": len(paired_devices()),
        }

    def set_remote(self, on: bool) -> None:
        """Start or stop listening for other devices on the network."""
        if not on:
            self.close_pairing()
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
        """Serve the app's page to a browser; let WebSocket requests through. Only the files of the page are
        served, looked up by name in a list made from its folder: the request never reaches the disk, so a name
        like \\\\host\\share (which Windows would go and open, sending the user's login) is simply not found."""
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        name = urlparse(request.path).path.lstrip("/") or "index.html"
        target = page_files().get(name)
        if target is None:
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
