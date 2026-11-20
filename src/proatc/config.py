"""Settings: defaults, then config.toml, then what was changed in the app (settings.json).

The API key is not a setting: it is read from the environment or the .env file.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Where the key, settings, speech models and airport index live. From a source checkout that is the
# project folder; the installed app points PROATC_HOME at its own data folder.
PROJECT_DIR = Path(os.environ["PROATC_HOME"]) if os.environ.get("PROATC_HOME") else Path(__file__).resolve().parents[2]
MODELS_DIR = PROJECT_DIR / "models"

HOLDING = ("never", "sometimes", "always")
COPILOT = ("off", "readbacks", "full")
CHATTER = ("off", "some", "busy")
PROVIDERS = ("openrouter", "local")


@dataclass
class Sim:
    host: str = "localhost"
    port: int = 8086
    xplane_path: str | None = None  # only needed if the X-Plane folder is not found automatically
    allow_tuning: bool = True  # let a click on a frequency in the app tune the COM radio in the sim
    auto_tune: bool = False  # tune a hand-off frequency without being asked, once it has been read back


@dataclass
class Ptt:
    key: str = "ctrl_r"  # keyboard key, by its pynput name
    joystick: str | None = None  # name of the joystick or yoke that carries the push-to-talk button
    joystick_button: int | None = None


@dataclass
class Audio:
    input_device: int | None = None
    output_device: int | None = None


@dataclass
class Stt:
    model: str = "small.en"
    device: str = "auto"  # GPU when available, otherwise CPU
    compute_type: str = "int8"
    beam_size: int = 1


@dataclass
class Llm:
    model: str = "anthropic/claude-haiku-5.5"
    fallback_models: list[str] = field(default_factory=lambda: ["google/gemini-2.5-flash-lite"])
    max_tokens: int = 200
    # "openrouter" (your key, any model) or "local": a model on this machine or network behind an
    # OpenAI-compatible address, such as Ollama or LM Studio. No key, no cost, nothing leaves the machine.
    provider: str = "openrouter"
    local_url: str = "http://localhost:11434/v1/chat/completions"
    local_model: str = "llama3.1"


@dataclass
class Tts:
    engine: str = "kokoro"  # or "phonon": on trial; it starts speaking sooner (see tts.py)
    voice: str = "am_michael"
    speed: float = 1.15
    atis_voice: str = "af_sarah"
    atis_speed: float = 1.25
    pilot_voice: str = "am_adam"  # reads typed, clicked and copilot calls aloud
    speak_pilot_text: bool = True
    vary_voices: bool = True  # every controller has a voice of their own; off: they all use `voice`


@dataclass
class Radio:
    enabled: bool = True
    noise: float = 0.012


@dataclass
class Atc:
    # Holding before the approach: "never", "sometimes" (about one arrival in four) or "always".
    # A hold can always be requested by the pilot.
    holding: str = "never"
    # The copilot on the radio: "off", "readbacks" (reads instructions back for you) or "full"
    # (also tunes hand-off frequencies and checks in with the next controller).
    copilot: str = "off"
    traffic: bool = True  # traffic calls and runway sequencing against X-Plane's AI aircraft
    chatter: str = "some"  # other aircraft heard on the frequency: "off", "some" or "busy". Sound only.
    # The COM radio you are not transmitting on is heard too when the aircraft's audio panel has it on:
    # an ATIS on COM2 while you work a controller on COM1.
    second_radio: bool = True
    # On real weather, use the runways the real airport is using now, where it publishes a digital ATIS (about
    # seventy-five airports in the United States). The airport's code is sent to atis.info, and nothing else.
    real_atis: bool = True


@dataclass
class Remote:
    """The app's screen in a browser on another device on your network (a phone or a tablet)."""

    enabled: bool = False
    port: int = 8787


@dataclass
class Config:
    sim: Sim = field(default_factory=Sim)
    ptt: Ptt = field(default_factory=Ptt)
    audio: Audio = field(default_factory=Audio)
    stt: Stt = field(default_factory=Stt)
    llm: Llm = field(default_factory=Llm)
    tts: Tts = field(default_factory=Tts)
    radio: Radio = field(default_factory=Radio)
    atc: Atc = field(default_factory=Atc)
    remote: Remote = field(default_factory=Remote)


# What the app's settings screen may change, and what each value must look like.
EDITABLE: dict[str, dict[str, object]] = {
    "audio": {"input_device": (int, type(None)), "output_device": (int, type(None))},
    "ptt": {"key": str, "joystick": (str, type(None)), "joystick_button": (int, type(None))},
    "tts": {
        "engine": ("kokoro", "phonon"),
        "voice": str,
        "atis_voice": str,
        "pilot_voice": str,
        "speed": (int, float),
        "speak_pilot_text": bool,
        "vary_voices": bool,
    },  # fmt: skip
    "llm": {"model": str, "provider": PROVIDERS, "local_url": str, "local_model": str},
    "radio": {"enabled": bool},
    "atc": {"holding": HOLDING, "copilot": COPILOT, "traffic": bool, "chatter": CHATTER, "second_radio": bool, "real_atis": bool},
    "sim": {"allow_tuning": bool, "auto_tune": bool},
    "remote": {"enabled": bool},
}


def _apply(target, values: dict) -> None:
    known = {f.name for f in fields(target)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"Unknown setting '{key}' in config.toml")
        current = getattr(target, key)
        if is_dataclass(current):
            _apply(current, value)
        else:
            setattr(target, key, value)


def validate(changes: dict) -> dict:
    """Check changes coming from the app. Returns them cleaned; raises ValueError on anything not allowed."""
    cleaned: dict = {}
    for section, values in changes.items():
        if section not in EDITABLE or not isinstance(values, dict):
            raise ValueError(f"'{section}' cannot be changed from the app.")
        for key, value in values.items():
            rule = EDITABLE[section].get(key)
            if rule is None:
                raise ValueError(f"'{section}.{key}' cannot be changed from the app.")
            if isinstance(rule, tuple) and all(isinstance(option, str) for option in rule):
                ok = value in rule
            else:
                numeric = rule in (int, (int, float), (int, type(None)))
                ok = isinstance(value, rule) and not (isinstance(value, bool) and numeric)
            if ok and isinstance(value, str) and not value.strip():
                ok = False
            if ok and (section, key) == ("tts", "speed"):
                ok = 0.7 <= value <= 1.6
            if ok and (section, key) == ("llm", "local_url"):
                ok = value.startswith(("http://", "https://"))
            if ok and (section, key) == ("ptt", "key"):
                ok = push_to_talk_key(value)
            if not ok:
                raise ValueError(f"'{value}' is not a valid value for {section}.{key}.")
            cleaned.setdefault(section, {})[key] = value
    return cleaned


def push_to_talk_key(name: str) -> bool:
    """Whether a push-to-talk key name is one the keyboard listener knows: one character, or a key's name such
    as ctrl_r or f13. A name it does not know would stop the engine from starting, every time."""
    if len(name) == 1:
        return name.isprintable()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,15}", name):
        return False
    try:
        from pynput import keyboard
    except Exception:
        return True  # no keyboard listener here to ask: the name has the right form
    return hasattr(keyboard.Key, name)


def private(path: Path) -> None:
    """Make a file readable by this user only. Windows keeps a user's AppData to that user already; elsewhere a
    new file is often readable by every user of the machine."""
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass  # a file system without permissions: nothing more can be done


def safe_url(url: str) -> str:
    """An address without a user name, password or query, which could carry a token: for logs and reports."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        return urlunsplit((parts.scheme, f"{host}:{parts.port}" if parts.port else host, parts.path, "", ""))
    except ValueError:
        return "(not shown)"


def settings_path() -> Path:
    return PROJECT_DIR / "settings.json"


def load_settings() -> dict:
    """What the app remembers between runs (not secrets): setting changes and the SimBrief username."""
    path = settings_path()
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        settings = {}
    # SIMBRIEF_USER in .env is the default until another account is used in the app.
    if not settings.get("simbrief_user") and env_value("SIMBRIEF_USER"):
        settings["simbrief_user"] = env_value("SIMBRIEF_USER")
    return settings


def save_settings(changes: dict) -> None:
    path = settings_path()
    try:
        stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        stored = {}
    path.write_text(json.dumps({**stored, **changes}, indent=2), encoding="utf-8")
    private(path)  # the SimBrief ID, the paired devices


def save_overrides(changes: dict) -> None:
    """Remember setting changes made in the app, on top of earlier ones."""
    stored = load_settings().get("config", {})
    for section, values in changes.items():
        stored.setdefault(section, {}).update(values)
    save_settings({"config": stored})


def load_config(path: Path | None = None) -> Config:
    config = Config()
    path = path or PROJECT_DIR / "config.toml"
    if path.exists():
        _apply(config, tomllib.loads(path.read_text(encoding="utf-8")))
    try:
        _apply(config, validate(load_settings().get("config", {})))
    except ValueError:
        pass  # a settings file from another version: ignore what no longer fits
    return config


def editable_values(config: Config) -> dict:
    """The current value of everything the settings screen shows."""
    return {section: {key: getattr(getattr(config, section), key) for key in keys} for section, keys in EDITABLE.items()}


def env_value(name: str) -> str | None:
    """A setting from the environment or the .env file. Values are never logged."""
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip().strip('"').strip("'")
    return None


def openrouter_key() -> str | None:
    return env_value("OPENROUTER_API_KEY")
