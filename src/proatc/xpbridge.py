"""Bridge to the X-Plane 12 Web API.

Resolves dataref names to session ids over REST, then keeps a live snapshot
updated from a WebSocket subscription running on a background thread.
It reads the sim's state. The only thing it ever writes is a COM frequency, through `write`,
when the pilot clicks a frequency in the app.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass

import httpx
from websockets.sync.client import connect

from .phraseology import spoken_callsign

# key -> dataref name. All verified against X-Plane 12.4.4.
DATAREFS = {
    "lat": "sim/flightmodel/position/latitude",
    "lon": "sim/flightmodel/position/longitude",
    "elevation_m": "sim/flightmodel/position/elevation",
    "agl_m": "sim/flightmodel/position/y_agl",
    "groundspeed_ms": "sim/flightmodel/position/groundspeed",
    "ias_kt": "sim/flightmodel/position/indicated_airspeed",
    "heading_mag": "sim/flightmodel/position/mag_psi",
    "vs_fpm": "sim/flightmodel/position/vh_ind_fpm",
    "on_ground": "sim/flightmodel/failures/onground_any",
    "com1_khz": "sim/cockpit2/radios/actuators/com1_frequency_hz_833",
    "com2_khz": "sim/cockpit2/radios/actuators/com2_frequency_hz_833",
    "squawk": "sim/cockpit2/radios/actuators/transponder_code",
    "tail_number": "sim/aircraft/view/acf_tailnum",
    "aircraft_type": "sim/aircraft/view/acf_ICAO",
    "paused": "sim/time/paused",
    "heading_true": "sim/flightmodel/position/true_psi",
    "com_selection": "sim/cockpit2/radios/actuators/audio_com_selection",  # 6 = COM1, 7 = COM2
    # The audio panel: which radios are being listened to, and how loud.
    "com1_listen": "sim/cockpit2/radios/actuators/audio_selection_com1",
    "com2_listen": "sim/cockpit2/radios/actuators/audio_selection_com2",
    "com1_volume": "sim/cockpit2/radios/actuators/audio_volume_com1",
    "com2_volume": "sim/cockpit2/radios/actuators/audio_volume_com2",
    "zulu_seconds": "sim/time/zulu_time_sec",
    # Weather at the aircraft (names from the sim's DataRefs.txt).
    "wx_qnh_pas": "sim/weather/aircraft/qnh_pas",
    "wx_visibility_sm": "sim/weather/aircraft/visibility_reported_sm",
    "wx_wind_alt_m": "sim/weather/aircraft/wind_altitude_msl_m",
    "wx_wind_kt": "sim/weather/aircraft/wind_speed_kts",
    "wx_wind_dir": "sim/weather/aircraft/wind_direction_degt",
    "wx_cloud_cover": "sim/weather/aircraft/cloud_coverage_percent",
    "wx_cloud_base_m": "sim/weather/aircraft/cloud_base_msl_m",
    "wx_cloud_type": "sim/weather/aircraft/cloud_type",
    "wx_dewpoint_c": "sim/weather/aircraft/dewpoint_deg_c",
    "wx_oat_c": "sim/weather/aircraft/temperature_ambient_deg_c",
    "wx_sealevel_temp_c": "sim/weather/region/sealevel_temperature_c",
    "wx_rain": "sim/weather/region/rain_percent",
    # Other aircraft, as the sim tracks them for TCAS. Slot 0 is the user's own.
    "tcas_lat": "sim/cockpit2/tcas/targets/position/lat",
    "tcas_lon": "sim/cockpit2/tcas/targets/position/lon",
    "tcas_ele_m": "sim/cockpit2/tcas/targets/position/ele",
    "tcas_heading": "sim/cockpit2/tcas/targets/position/psi",
    "tcas_vs_fpm": "sim/cockpit2/tcas/targets/position/vertical_speed",
    "tcas_on_ground": "sim/cockpit2/tcas/targets/position/weight_on_wheels",
    "wx_source": "sim/weather/region/weather_source",  # 0 preset, 1 real weather, 2 controlpad, 3 plugin
}


def tuned_khz(state: dict) -> int | None:
    """Frequency of the COM radio selected for transmitting."""
    key = "com2_khz" if state.get("com_selection") == 7 else "com1_khz"
    value = state.get(key)
    return int(value) if value else None


def transmit_radio(state: dict) -> int:
    """The COM radio selected for transmitting: 1 or 2."""
    return 2 if state.get("com_selection") == 7 else 1


def other_radio(state: dict) -> dict:
    """The COM radio that is not transmitted on: its number, its frequency, and whether it is being listened to.

    It is listened to when the aircraft's audio panel has its reception on and its volume up. An aircraft
    that does not report its audio panel is taken as not listening."""
    number = 3 - transmit_radio(state)
    khz = state.get(f"com{number}_khz")
    volume = state.get(f"com{number}_volume")
    listening = bool(khz) and bool(state.get(f"com{number}_listen")) and (volume is None or volume > 0.05)
    return {"radio": number, "khz": int(khz) if khz else None, "listening": listening,
            "volume": 1.0 if volume is None else max(0.0, min(1.0, float(volume)))}  # fmt: skip


def magnetic_variation(state: dict) -> float:
    """Magnetic minus true heading, in degrees."""
    if "heading_mag" in state and "heading_true" in state:
        return (state["heading_mag"] - state["heading_true"] + 180) % 360 - 180
    return 0.0


class SimUnavailable(RuntimeError):
    pass


@dataclass
class _Ref:
    key: str
    id: int
    value_type: str


def decode_value(value_type: str, raw):
    """Turn a Web API value into a plain Python value."""
    if value_type == "data":
        if not isinstance(raw, str):
            return raw
        return base64.b64decode(raw).split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    if isinstance(raw, list) and len(raw) == 1:
        return raw[0]
    return raw


def describe(state: dict) -> dict:
    """Convert raw sim units into the units pilots and controllers use."""
    out: dict = {}
    if state.get("tail_number"):
        out["callsign"] = state["tail_number"]
        out["callsign_spoken"] = spoken_callsign(state["tail_number"])
    if "aircraft_type" in state:
        out["aircraft_type"] = state["aircraft_type"]
    if "lat" in state and "lon" in state:
        out["lat"] = round(state["lat"], 5)
        out["lon"] = round(state["lon"], 5)
    if "elevation_m" in state:
        out["altitude_ft"] = round(state["elevation_m"] * 3.28084 / 10) * 10
    if "agl_m" in state:
        out["height_agl_ft"] = round(state["agl_m"] * 3.28084)
    if "ias_kt" in state:
        out["airspeed_kt"] = round(state["ias_kt"])
    if "groundspeed_ms" in state:
        out["groundspeed_kt"] = round(state["groundspeed_ms"] * 1.94384)
    if "heading_mag" in state:
        out["heading_mag"] = round(state["heading_mag"]) % 360
    if "vs_fpm" in state:
        out["vertical_speed_fpm"] = round(state["vs_fpm"] / 50) * 50
    if "on_ground" in state:
        out["on_ground"] = bool(state["on_ground"])
    for key in ("com1_khz", "com2_khz"):
        if key in state:
            out[key.replace("_khz", "_mhz")] = f"{state[key] / 1000:.3f}"
    if "squawk" in state:
        out["squawk"] = f"{int(state['squawk']):04d}"
    return out


class XPBridge:
    def __init__(self, host: str = "localhost", port: int = 8086):
        self.rest = f"http://{host}:{port}/api/v3"
        self.ws_url = f"ws://{host}:{port}/api/v3"
        self.capabilities_url = f"http://{host}:{port}/api/capabilities"
        self._refs: dict[int, _Ref] = {}
        self._state: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.missing: list[str] = []
        self.last_update = 0.0
        self.connected = False

    # ---- REST -------------------------------------------------------------

    def capabilities(self) -> dict:
        try:
            response = httpx.get(self.capabilities_url, timeout=3)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as error:
            raise SimUnavailable(
                "X-Plane Web API not reachable. Is X-Plane 12.1.1+ running, and is "
                "Settings > Network not set to 'Disable Incoming Traffic'?"
            ) from error

    def resolve(self) -> None:
        """Look up the session id of every dataref we need. Ids change each sim launch."""
        self._refs.clear()
        self.missing.clear()
        with httpx.Client(timeout=5, headers={"Accept": "application/json"}) as client:
            for key, name in DATAREFS.items():
                try:
                    response = client.get(f"{self.rest}/datarefs", params={"filter[name]": name})
                except httpx.HTTPError as error:
                    raise SimUnavailable(str(error)) from error
                if response.status_code != 200 or not response.json().get("data"):
                    self.missing.append(name)
                    continue
                item = response.json()["data"][0]
                self._refs[item["id"]] = _Ref(key, item["id"], item["value_type"])
        if not self._refs:
            raise SimUnavailable("No datarefs found. X-Plane lists none until a flight is loaded.")

    def write(self, key: str, value) -> None:
        """Set one dataref. Used only to tune a COM radio when the pilot asks for it in the app."""
        ref_id = next((ref.id for ref in self._refs.values() if ref.key == key), None)
        if ref_id is None:
            raise SimUnavailable("X-Plane is not connected.")
        try:
            response = httpx.patch(f"{self.rest}/datarefs/{ref_id}/value", json={"data": value}, timeout=5,
                                   headers={"Accept": "application/json"})  # fmt: skip
        except httpx.HTTPError as error:
            raise SimUnavailable(f"X-Plane did not answer: {error}") from error
        if response.status_code != 200:
            raise SimUnavailable(f"X-Plane refused the change ({response.status_code}).")

    def read_once(self) -> dict:
        """One REST read of every dataref; used by `proatc check`."""
        if not self._refs:
            self.resolve()
        state = {}
        with httpx.Client(timeout=5, headers={"Accept": "application/json"}) as client:
            for ref in self._refs.values():
                response = client.get(f"{self.rest}/datarefs/{ref.id}/value")
                if response.status_code == 200:
                    state[ref.key] = decode_value(ref.value_type, response.json()["data"])
        return state

    # ---- WebSocket ---------------------------------------------------------

    def start(self) -> None:
        """Start streaming. Safe to call before the sim is up: it keeps trying in the background."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="xpbridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def wait_for_data(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.last_update:
                return True
            time.sleep(0.05)
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._refs:
                    self.resolve()
                with connect(self.ws_url, open_timeout=5) as socket:
                    socket.send(
                        json.dumps(
                            {
                                "req_id": 1,
                                "type": "dataref_subscribe_values",
                                "params": {"datarefs": [{"id": ref_id} for ref_id in self._refs]},
                            }
                        )
                    )
                    while not self._stop.is_set():
                        try:
                            message = json.loads(socket.recv(timeout=1))
                        except TimeoutError:
                            continue
                        self._handle(message)
            except Exception:
                # Sim closed, restarted or not up yet: ids are per session, so look them up again.
                self.connected = False
                self._refs.clear()
                with self._lock:
                    self._state.clear()
                if self._stop.wait(3):
                    return

    def _handle(self, message: dict) -> None:
        if message.get("type") != "dataref_update_values":
            return
        with self._lock:
            for ref_id, raw in message.get("data", {}).items():
                ref = self._refs.get(int(ref_id))
                if ref:
                    self._state[ref.key] = decode_value(ref.value_type, raw)
        self.last_update = time.monotonic()
        self.connected = True
