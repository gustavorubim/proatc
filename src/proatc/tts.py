"""Local text-to-speech with Kokoro (ONNX)."""

from __future__ import annotations

import numpy as np

from .config import MODELS_DIR, Tts
from .voices import language, voice_for

KOKORO_DIR = MODELS_DIR / "kokoro"
MODEL_FILE = KOKORO_DIR / "kokoro-v1.0.onnx"
VOICES_FILE = KOKORO_DIR / "voices-v1.0.bin"
NOVELTY = ("am_santa",)  # installed, selectable by hand, but never picked for a controller
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
DOWNLOADS = {MODEL_FILE: f"{RELEASE}/kokoro-v1.0.onnx", VOICES_FILE: f"{RELEASE}/voices-v1.0.bin"}


class ModelsMissing(RuntimeError):
    pass


class TextToSpeech:
    def __init__(self, settings: Tts):
        if not MODEL_FILE.exists() or not VOICES_FILE.exists():
            raise ModelsMissing("Kokoro voice files not found. Run: uv run proatc setup-models")
        import threading

        from kokoro_onnx import Kokoro

        self.settings = settings
        self.engine = Kokoro(str(MODEL_FILE), str(VOICES_FILE))
        self._lock = threading.Lock()  # the controller and the ATIS thread share one engine

    def speak(self, text: str, voice: str | None = None, speed: float | None = None) -> tuple[np.ndarray, int]:
        with self._lock:
            samples, sample_rate = self.engine.create(
                text,
                voice=voice or self.settings.voice,
                speed=speed or self.settings.speed,
                lang=language(voice or self.settings.voice),
            )
        return np.asarray(samples, dtype=np.float32), int(sample_rate)

    def voices(self) -> list[str]:
        """The English voices installed: American (a) and British (b), women (f) and men (m)."""
        return sorted(voice for voice in self.engine.get_voices() if voice[:2] in ("af", "am", "bf", "bm"))

    def for_station(self, key: str, ident: str | None) -> dict:
        """The voice and pace of one controller, as arguments for `speak`."""
        if not self.settings.vary_voices:
            return {}
        chosen = voice_for(key, ident, self.voices(), (self.settings.atis_voice, self.settings.pilot_voice, *NOVELTY))
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
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            done, last = 0, -1
            with open(partial, "wb") as handle:
                for chunk in response.iter_bytes(1 << 20):
                    handle.write(chunk)
                    done += len(chunk)
                    percent = done * 100 // total if total else 0
                    if total and percent // 10 != last:
                        last = percent // 10
                        report(f"Downloading {path.name}: {percent}% of {total / 1e6:.0f} MB")
        partial.replace(path)
        report(f"Saved {path.name} ({path.stat().st_size / 1e6:.0f} MB)")
