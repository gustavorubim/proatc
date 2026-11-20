"""Local text-to-speech. Two engines: Kokoro (ONNX), the default, and Phonon, which is on trial.

Kokoro makes a whole sentence before any of it can be played. Phonon hands over sound in pieces
while it is still working, so a controller starts to speak a fraction of a second after the reply
is known (see `stream`, and `Player.play_stream`).

Voices are named the same way for both: accent (a American, b British), then f or m, then a name:
`am_michael`, `bf_Maeve`. A voice the current engine does not have is replaced by one of the same
accent and sex, so the settings survive a change of engine.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator

import numpy as np

from .config import MODELS_DIR, Tts
from .voices import language, voice_for

KOKORO_DIR = MODELS_DIR / "kokoro"
MODEL_FILE = KOKORO_DIR / "kokoro-v1.0.onnx"
VOICES_FILE = KOKORO_DIR / "voices-v1.0.bin"
NOVELTY = ("am_santa",)  # installed, selectable by hand, but never picked for a controller
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
DOWNLOADS = {MODEL_FILE: f"{RELEASE}/kokoro-v1.0.onnx", VOICES_FILE: f"{RELEASE}/voices-v1.0.bin"}
# What each file must be. The release publishes no checksum: these are the files as that release serves them
# (325,532,387 and 28,214,398 bytes). A download that does not match is thrown away, not loaded.
SHA256 = {
    MODEL_FILE: "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    VOICES_FILE: "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}

ENGINES = ("kokoro", "phonon")
PHONON_MODEL = "hf://gradium/phonon-31m-en"  # Apache 2.0; the larger Phonon models are non-commercial
# The catalogue's English voices, by accent and sex. Its character voices (pirates, dragons) are left out.
PHONON_VOICES = {
    "af": ("Delia", "Everly", "Hadley", "Harper", "Juniper", "Zoey"),
    "am": ("Easton", "Micah", "Miles", "Preston", "Russell", "Trevor"),
    "bf": ("Elodie-Rose", "Freya", "Imogen", "Lottie", "Maeve", "Pippa"),
    "bm": ("Alfie", "Edwin", "Hugh", "Spencer", "Teddy", "Toby"),
}
# Described by their makers as playful or girlish: selectable by hand, never picked for a controller.
PHONON_NOVELTY = ("af_Zoey", "bf_Freya", "bf_Elodie-Rose", "am_Micah")
FALLBACK = {
    "kokoro": {"af": "af_sarah", "am": "am_michael", "bf": "bf_emma", "bm": "bm_george"},
    "phonon": {"af": "af_Hadley", "am": "am_Preston", "bf": "bf_Maeve", "bm": "bm_Edwin"},
}
PACE_LIMIT = 0.5  # Phonon's duration control, either way


class ModelsMissing(RuntimeError):
    pass


class Kokoro:
    name = "kokoro"
    streams = False
    novelty = NOVELTY

    def __init__(self):
        if not MODEL_FILE.exists() or not VOICES_FILE.exists():
            raise ModelsMissing("Kokoro voice files not found. Run: uv run proatc setup-models")
        from kokoro_onnx import Kokoro as Engine

        self.engine = Engine(str(MODEL_FILE), str(VOICES_FILE))
        self.sample_rate = 24000

    def voices(self) -> list[str]:
        """The English voices installed: American (a) and British (b), women (f) and men (m)."""
        return sorted(voice for voice in self.engine.get_voices() if voice[:2] in ("af", "am", "bf", "bm"))

    def pieces(self, text: str, voice: str, speed: float) -> Iterator[np.ndarray]:
        samples, rate = self.engine.create(text, voice=voice, speed=speed, lang=language(voice))
        self.sample_rate = int(rate)
        yield np.asarray(samples, dtype=np.float32)


class Phonon:
    name = "phonon"
    streams = True
    novelty = PHONON_NOVELTY

    def __init__(self):
        try:
            import ptts
        except ImportError as error:
            raise ModelsMissing("The Phonon voice engine is not installed.") from error
        # The text is already written as it is spoken ("two seven left", "Q N H"): Phonon's own rewriting
        # of numbers would turn a pressure into "one thousand thirteen".
        self.engine = ptts.TTS(PHONON_MODEL, lang="en", quant="q8", rewrites="none")
        self.sample_rate = int(self.engine.sample_rate)

    def voices(self) -> list[str]:
        return sorted(f"{group}_{name}" for group, names in PHONON_VOICES.items() for name in names)

    def pieces(self, text: str, voice: str, speed: float) -> Iterator[np.ndarray]:
        self.engine.set_conditions({"duration_delta": round(pace(speed), 3)})
        with self.engine.stream(text, voice=voice.split("_", 1)[1]) as audio:
            for chunk in audio:
                yield np.asarray(chunk, dtype=np.float32).reshape(-1)


def pace(speed: float) -> float:
    """Phonon's duration change for a speaking speed: 1.25 times as fast is a fifth shorter."""
    return max(-PACE_LIMIT, min(PACE_LIMIT, 1.0 / max(speed, 0.1) - 1.0))


def resolve(voice: str, available: list[str], engine: str) -> str:
    """The voice to use when `voice` was asked for: itself, or one of the same accent and sex."""
    if voice in available:
        return voice
    wanted = FALLBACK[engine].get(voice[:2], FALLBACK[engine]["am"])
    return wanted if wanted in available or not available else available[0]


class TextToSpeech:
    def __init__(self, settings: Tts, backend=None):
        self.settings = settings
        self._lock = threading.Lock()  # the controller and the ATIS thread share one engine
        self.backend = backend or self._load(settings.engine)

    @staticmethod
    def _load(name: str):
        return Phonon() if name == "phonon" else Kokoro()

    @property
    def engine_name(self) -> str:
        return self.backend.name

    @property
    def streams(self) -> bool:
        """Whether sound comes in pieces while the rest is still being made."""
        return self.backend.streams

    def use(self, name: str) -> None:
        """Change the engine. The one in use stays if the new one cannot be loaded."""
        if name != self.backend.name:
            fresh = self._load(name)
            with self._lock:
                self.backend = fresh

    def stream(self, text: str, voice: str | None = None, speed: float | None = None) -> tuple[Iterator[np.ndarray], int]:
        """(pieces of sound as they are made, sample rate). Close the iterator to stop the work."""
        backend = self.backend
        chosen = resolve(voice or self.settings.voice, backend.voices(), backend.name)

        def pieces() -> Iterator[np.ndarray]:
            with self._lock:
                yield from backend.pieces(text, chosen, speed or self.settings.speed)

        return pieces(), backend.sample_rate

    def speak(self, text: str, voice: str | None = None, speed: float | None = None) -> tuple[np.ndarray, int]:
        pieces, _ = self.stream(text, voice, speed)
        parts = list(pieces)
        samples = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return samples, self.backend.sample_rate

    def voices(self) -> list[str]:
        return self.backend.voices()

    def for_station(self, key: str, ident: str | None) -> dict:
        """The voice and pace of one controller, as arguments for `speak`."""
        if not self.settings.vary_voices:
            return {}
        taken = [
            resolve(name, self.voices(), self.backend.name) for name in (self.settings.atis_voice, self.settings.pilot_voice)
        ]
        chosen = voice_for(key, ident, self.voices(), (*taken, *self.backend.novelty))
        return {"voice": chosen[0], "speed": self.settings.speed + chosen[1]} if chosen else {}


def models_present() -> bool:
    return MODEL_FILE.exists() and VOICES_FILE.exists()


def download_models(report=print) -> None:
    """Fetch the Kokoro voice files that are missing. `report` receives progress lines."""
    import httpx

    for path, url in DOWNLOADS.items():
        if path.exists():
            report(f"Already present: {path.name}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        report(f"Downloading {path.name}...")
        partial = path.with_suffix(path.suffix + ".part")
        digest = hashlib.sha256()
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            done, last = 0, -1
            with open(partial, "wb") as handle:
                for chunk in response.iter_bytes(1 << 20):
                    handle.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    percent = done * 100 // total if total else 0
                    if total and percent // 10 != last:
                        last = percent // 10
                        report(f"Downloading {path.name}: {percent}% of {total / 1e6:.0f} MB")
        if digest.hexdigest() != SHA256[path]:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"{path.name} did not download as published (its checksum differs). Try again later.")
        partial.replace(path)
        report(f"Saved {path.name} ({path.stat().st_size / 1e6:.0f} MB)")
