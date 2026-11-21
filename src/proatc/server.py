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
import queue
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
MAX_SOURCES = 256  # addresses whose wrong codes are remembered; older ones are forgotten first
MAX_REMOTE_CONNECTIONS = 16  # connections from other devices at once, counted from the moment one is accepted
REMOTE_OPEN_TIMEOUT_S = 5  # a connection from another device that has not finished its handshake by then is closed
PAIRING_WINDOW_S = 300  # how long a pairing code shown on the computer is good for
MAX_DEVICES = 8  # paired devices kept; the oldest is forgotten first
DEVICE_DAYS = 30  # a paired device must pair again after this: its token crosses the network in the clear
DEVICE_RECHECK_S = 5  # how often connected devices are checked against the paired ones, on a thread of its own
OUTBOX = BACKLOG + 64  # messages waiting for one connection: a client this far behind is cut off
CLOSE_WAIT_S = 5  # a connection being cut off that does not close by then is dropped
# What a paired device may do. Everything else is the computer's: the microphone, the settings that are not
# about ATC or voices, the key and the model, files, the sound check.
REMOTE_COMMANDS = frozenset(("say", "click", "phrase", "explain", "what_next", "debrief", "tune", "chart", "simbrief",
                             "plan", "clear_plan", "new_flight", "settings"))  # fmt: skip
REMOTE_SETTINGS = frozenset(("atc", "tts", "radio"))
# Calls that may cost the user's OpenRouter credit: a paired device makes no more than this many a minute.
PAID_COMMANDS = frozenset(("say", "click", "phrase", "explain", "what_next", "debrief"))
REMOTE_PAID_PER_MINUTE = 10  # for each paired device, however often it reconnects
REMOTE_PAID_PER_HOUR = 120  # for all paired devices together: what they can spend of the key's credit
# Free to the user but work for the computer (a download, a chart, a settings write): a budget for each device,
# and the downloads and charts one at a time for all of them.
REMOTE_BUSY_COMMANDS = frozenset(("chart", "simbrief", "tune", "settings", "plan"))
REMOTE_BUSY_PER_MINUTE = 30
REMOTE_SLOW_COMMANDS = frozenset(("chart", "simbrief"))
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
    """The state as a paired device is sent it: without what is the computer's own, such as its keyboard's
    push-to-talk key (a device cannot key the computer's microphone)."""
    shown = {key: value for key, value in state.items() if key not in PRIVATE_STATE}
    if isinstance(shown.get("status"), dict):
        shown["status"] = {key: value for key, value in shown["status"].items() if key != "ptt_key"}
    return shown


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


def _device_entries(now: float | None = None) -> list[dict]:
    """The paired devices still allowed in: the hash of each one's token (what is kept on disk lets nobody in)
    and when it paired. A device paired before pairing had an age (a bare hash) counts from now."""
    now = time.time() if now is None else now
    found = load_settings().get("remote_devices")
    entries = []
    for entry in found if isinstance(found, list) else []:
        if isinstance(entry, str):
            entry = {"hash": entry, "paired": now}
        if isinstance(entry, dict) and isinstance(entry.get("hash"), str) and isinstance(entry.get("paired"), (int, float)):
            if now - entry["paired"] < DEVICE_DAYS * 86400:
                entries.append({"hash": entry["hash"], "paired": entry["paired"]})
    return entries


def paired_devices() -> list[str]:
    """The hashes of the paired devices' tokens that still let them in."""
    return [entry["hash"] for entry in _device_entries()]


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


class Outbox:
    """What waits to be sent to one connection, sent by a thread of its own: a client that stops reading holds up
    only itself, never the others or the checks of paired devices, and is cut off once OUTBOX messages wait."""

    def __init__(self, connection):
        self.connection = connection
        self.queue: queue.Queue = queue.Queue(maxsize=OUTBOX)
        threading.Thread(target=self._send, name="app-outbox", daemon=True).start()

    def _send(self) -> None:
        while (text := self.queue.get()) is not None:
            try:
                self.connection.send(text)
            except Exception:
                return  # closed: its handler tidies up

    def put(self, text: str) -> bool:
        try:
            self.queue.put_nowait(text)
            return True
        except queue.Full:
            return False

    def end(self) -> None:
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass  # stuck on a client that does not read: dropping the connection ends it


def _drop(connection) -> None:
    """End a connection at once, without the closing handshake a client that does not read would never finish."""
    try:
        connection.socket.shutdown(socket.SHUT_RDWR)
    except (OSError, AttributeError):
        pass  # gone already


def _cut_off(connection, reason: str) -> None:
    """Close a connection with a reason the page can show, without waiting for it here; dropped if it hangs."""

    def close() -> None:
        closer = threading.Thread(target=_quietly, args=(lambda: connection.close(1008, reason),), daemon=True)
        closer.start()
        closer.join(CLOSE_WAIT_S)
        if closer.is_alive():
            _drop(connection)

    threading.Thread(target=close, name="app-cut-off", daemon=True).start()


def _quietly(action) -> None:
    try:
        action()
    except Exception:
        pass  # a connection gone already


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
        self._remote_slots = threading.BoundedSemaphore(MAX_REMOTE_CONNECTIONS)
        self._busy: dict[str, deque] = {}  # paired device -> when its busy commands came
        self._slow_work = threading.Lock()  # a download or a chart for another device, one at a time
        self._bad_pairing = 0  # wrong codes from anywhere while the pairing window is open
        self._code = ""  # the pairing code while a pairing window is open
        self._pairing_until = 0.0
        self._paid: dict[str, deque] = {}  # paired device (its token's hash) -> when its paid calls came
        self._paid_all: deque = deque(maxlen=REMOTE_PAID_PER_HOUR)  # every paired device's paid calls
        self._device_of: dict = {}  # paired connection -> its device (the hash of its token)
        self._expires: dict = {}  # paired connection -> when its device must pair again (time.time())
        self._outbox: dict = {}  # every connection -> what waits to be sent to it
        self._ptt_owner = None  # the connection that keyed the microphone, while it is keyed
        self._last_remote_state = ""
        self._address = lan_address()
        if load_settings().get("remote_devices"):
            save_settings({"remote_devices": _device_entries()})  # an age for each, and the expired gone
        # The paired devices, held in memory: signing in reads no file. Read again by the check every few seconds
        # (which also notices a change made outside the app) and set when a device pairs or all are forgotten.
        self._entries: list[dict] = _device_entries()

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
            targets = [(client, self._outbox[client], client in self.remote_clients) for client in self.clients]
        behind = []
        for client, outbox, remote in targets:
            text = remote_message if remote else message
            if text is not None and not outbox.put(text):
                behind.append(client)
        for client in behind:  # stopped reading: cut off, without waiting on it
            with self.lock:
                self._unregister(client)
            _drop(client)

    def _unregister(self, connection) -> None:
        """Nothing more is sent to a connection or taken from it as a paired device. Called with the lock held."""
        self.clients.discard(connection)
        self.remote_clients.discard(connection)
        self._device_of.pop(connection, None)
        self._expires.pop(connection, None)
        outbox = self._outbox.pop(connection, None)
        if outbox is not None:
            outbox.end()

    def _sweep_loop(self) -> None:
        """Check connected devices against the paired ones, apart from sending: a client that does not read
        cannot hold it up."""
        stop = threading.Event()
        while not stop.wait(DEVICE_RECHECK_S):
            try:
                self.cut_off_unpaired()
            except Exception as error:
                print(f"device check: {type(error).__name__}", flush=True)

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
        if token and any(hmac.compare_digest(digest, known) for known in self._paired_now()):
            return "device"
        now = time.monotonic()
        if source not in self._bad_codes and len(self._bad_codes) >= MAX_SOURCES:
            self._forget_old_sources(now)
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

    def _paired_now(self) -> list[str]:
        """The paired devices whose month is not up, from memory: an attempt to sign in reads no file."""
        now = time.time()
        return [entry["hash"] for entry in self._entries if now - entry["paired"] < DEVICE_DAYS * 86400]

    def reload_devices(self) -> None:
        """Read the paired devices again from settings.json."""
        self._entries = _device_entries()

    def _forget_old_sources(self, now: float) -> None:
        """Wrong codes from long ago are forgotten, and only the newest addresses are kept."""
        recent = {source: times for source, times in self._bad_codes.items() if times and now - times[-1] < LOCKOUT_S}
        newest = sorted(recent.items(), key=lambda item: item[1][-1])[-(MAX_SOURCES - 1) :]
        self._bad_codes = dict(newest)

    @staticmethod
    def _same_origin(connection) -> bool:
        """A browser says which page opened a connection: only the page this server gave may connect."""
        headers = connection.request.headers
        origin = headers.get("Origin")
        return origin is None or urlparse(origin).netloc == headers.get("Host", "")

    def _handle(self, connection, remote: bool = False) -> None:
        self._serve(connection, remote)

    def _bounded(self, listener) -> None:
        """No more than MAX_REMOTE_CONNECTIONS from other devices at once, counted from the moment one is accepted:
        one over is closed before anything it sends is read, page request and handshake alike. Each accepted
        socket is given to `listener.handler` on a thread of its own (websockets' sync server); a refused one
        is taken off the list of those threads, as websockets' own handler does when it finishes."""
        inner = listener.handler

        def bounded(sock, address) -> None:
            if self._remote_slots.acquire(blocking=False):
                try:
                    inner(sock, address)
                finally:
                    self._remote_slots.release()
                return
            _quietly(sock.close)
            with listener.lock:
                listener.handler_threads.discard(threading.current_thread())

        listener.handler = bounded

    def _serve(self, connection, remote: bool) -> None:
        query = parse_qs(urlparse(connection.request.path).query)
        source = str((connection.remote_address or ("",))[0])
        token, device = query.get("token", [""])[0], None
        with self.lock:
            granted = (self._same_origin(connection) or not remote) and self._allowed(token, remote, source)
            if granted:
                if granted == "paired":
                    device = self._new_device()
                    self.reload_devices()
                outbox = self._outbox[connection] = Outbox(connection)
                # What it has missed goes first, and is in its outbox before anything sent from now on.
                if device:
                    outbox.put(json.dumps({"type": "paired", "device": device}))
                for event in list(self.backlog):
                    if not (remote and event.get("type") in PRIVATE_EVENTS):
                        outbox.put(json.dumps(event))
                last = self._last_remote_state if remote else self._last_state
                if last:
                    outbox.put(last)
                self.clients.add(connection)
                if remote:
                    digest = _digest(device or token)
                    paired = next((e["paired"] for e in _device_entries() if e["hash"] == digest), time.time())
                    self.remote_clients.add(connection)
                    self._device_of[connection] = digest
                    self._expires[connection] = paired + DEVICE_DAYS * 86400
        if not granted:
            connection.close(1008, "bad token")
            return
        try:
            for message in connection:
                try:
                    self._command(json.loads(message), remote, connection)
                except (PlanError, SimUnavailable, ValueError) as error:
                    self.emit({"type": "error", "text": str(error) or "That did not work."})
                except Exception as error:
                    self.emit({"type": "error", "text": f"command failed: {type(error).__name__}: {error}"})
        finally:
            with self.lock:
                self._unregister(connection)
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
        entries = [*_device_entries(), {"hash": _digest(token), "paired": time.time()}]
        save_settings({"remote_devices": entries[-MAX_DEVICES:]})
        return token

    def forget_devices(self) -> None:
        """Every paired device must pair again; those connected now are cut off."""
        with self.lock:  # no device is let in while this runs: each is either cut off below or refused
            save_settings({"remote_devices": []})
            self._entries = []
            self._busy.clear()
            self.close_pairing()
            connected = list(self.remote_clients)
            for client in connected:
                self._unregister(client)
            self._paid.clear()
        for client in connected:
            _cut_off(client, "forgotten")

    def cut_off_unpaired(self) -> None:
        """Close the connections of devices no longer paired: their month is up, or they were forgotten. A device
        is let in while it is paired; it does not stay in after."""
        now = time.time()
        with self.lock:  # the same lock as letting a device in: one pairing now is either in this list or not yet in
            paired = set(paired_devices())
            self.reload_devices()
            gone = [connection for connection, device in self._device_of.items()
                    if device not in paired or now >= self._expires.get(connection, 0)]  # fmt: skip
            for connection in gone:
                self._unregister(connection)
        for connection in gone:
            _cut_off(connection, "pair again")

    def _still_paired(self, connection) -> bool:
        """A paired device's command is taken only while it is paired and its month is not up."""
        with self.lock:
            return connection in self._device_of and time.time() < self._expires.get(connection, 0)

    def _paid_allowed(self, connection) -> bool:
        """A paired device's calls that may cost credit: REMOTE_PAID_PER_MINUTE a minute for each device, counted
        by device so that reconnecting does not start the count again, and REMOTE_PAID_PER_HOUR for all of them."""
        now = time.monotonic()
        with self.lock:
            device = self._device_of.get(connection, "")
            times = self._paid.setdefault(device, deque(maxlen=REMOTE_PAID_PER_MINUTE))
            if len(times) == REMOTE_PAID_PER_MINUTE and now - times[0] < 60:
                return False
            if len(self._paid_all) == REMOTE_PAID_PER_HOUR and now - self._paid_all[0] < 3600:
                return False
            times.append(now)
            self._paid_all.append(now)
        return True

    def _busy_allowed(self, connection) -> bool:
        """A paired device's commands that are work for this computer: REMOTE_BUSY_PER_MINUTE a minute each."""
        now = time.monotonic()
        with self.lock:
            times = self._busy.setdefault(self._device_of.get(connection, ""), deque(maxlen=REMOTE_BUSY_PER_MINUTE))
            if len(times) == REMOTE_BUSY_PER_MINUTE and now - times[0] < 60:
                return False
            times.append(now)
        return True

    def _command(self, command: dict, remote: bool = False, connection=None, slot_held: bool = False) -> None:
        session, name = self.session, command.get("cmd")
        if remote and name in REMOTE_SLOW_COMMANDS and not slot_held:
            # A download or a chart for another device: one at a time, never queued behind another.
            if not self._slow_work.acquire(blocking=False):
                raise ValueError("Busy with another download: try again in a moment.")
            try:
                return self._command(command, remote, connection, slot_held=True)
            finally:
                self._slow_work.release()
        if remote and not self._still_paired(connection):
            if connection is not None:
                _cut_off(connection, "pair again")
            raise ValueError("This device must pair again: on the computer, Settings, Phone or tablet, Pair a device.")
        if remote and name == "set_key":
            raise ValueError("The key can only be changed in the app on the computer.")
        if remote and name not in REMOTE_COMMANDS:
            raise ValueError("That can only be done in the app on the computer.")
        if remote and name in PAID_COMMANDS and not self._paid_allowed(connection):
            raise ValueError("Too many requests from other devices: wait a minute, or use the app on the computer.")
        if remote and name in REMOTE_BUSY_COMMANDS and not self._busy_allowed(connection):
            raise ValueError("Too many requests from this device: wait a minute, or use the app on the computer.")
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
            self.remote = serve(lambda connection: self._handle(connection, True), "0.0.0.0", port,
                                process_request=self._page, open_timeout=REMOTE_OPEN_TIMEOUT_S)  # fmt: skip
            self._bounded(self.remote)
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
                # Never inside another site's frame, where a click could be steered; and no address sent on.
                "Content-Security-Policy": "frame-ancestors 'none'",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
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
        threading.Thread(target=self._sweep_loop, name="app-devices", daemon=True).start()
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
