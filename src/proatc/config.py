"""Settings: defaults, overridden by config.toml, with the API key read from the environment or .env."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

# Where the key, settings, speech models and airport index live. From a source checkout that is the
# project folder; the installed app points PROATC_HOME at its own data folder.
PROJECT_DIR = Path(os.environ["PROATC_HOME"]) if os.environ.get("PROATC_HOME") else Path(__file__).resolve().parents[2]
MODELS_DIR = PROJECT_DIR / "models"


@dataclass
class Sim:
    host: str = "localhost"
    port: int = 8086
    xplane_path: str | None = None  # only needed if the X-Plane folder is not found automatically
    allow_tuning: bool = True  # let a click on a frequency in the app tune the COM radio in the sim


@dataclass
class Ptt:
    key: str = "ctrl_r"


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


@dataclass
class Tts:
    voice: str = "am_michael"
    speed: float = 1.15
    atis_voice: str = "af_sarah"
    atis_speed: float = 1.25
    pilot_voice: str = "am_adam"  # reads typed and clicked calls aloud
    speak_pilot_text: bool = True


@dataclass
class Radio:
    enabled: bool = True
    noise: float = 0.012


@dataclass
class Atc:
    # Holding before the approach: "never", "sometimes" (about one arrival in four) or "always".
    # A hold can always be requested by the pilot.
    holding: str = "never"


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


def load_config(path: Path | None = None) -> Config:
    config = Config()
    path = path or PROJECT_DIR / "config.toml"
    if path.exists():
        _apply(config, tomllib.loads(path.read_text(encoding="utf-8")))
    return config


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
