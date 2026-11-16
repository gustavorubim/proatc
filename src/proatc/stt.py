"""Local speech-to-text with faster-whisper."""

from __future__ import annotations

import numpy as np

from .config import MODELS_DIR, Stt

# Biases the recogniser toward radio phraseology instead of everyday English.
BASE_PROMPT = (
    "Air traffic control radio. Tower, ground, approach, departure, clearance delivery. "
    "Runway two seven left, taxi via alpha, hold short, line up and wait, cleared for takeoff, "
    "cleared to land, squawk, QNH, altimeter, flight level, heading, descend and maintain, "
    "wilco, roger, niner."
)


def _add_nvidia_dlls() -> None:
    """Make the pip-installed NVIDIA libraries (the `gpu` extra) loadable on Windows."""
    import importlib.util
    import os
    from pathlib import Path

    spec = importlib.util.find_spec("nvidia")
    if not spec or not spec.submodule_search_locations:
        return
    for root in spec.submodule_search_locations:
        for bin_dir in Path(root).glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


class SpeechToText:
    def __init__(self, settings: Stt):
        self.settings = settings
        if settings.device in ("auto", "cuda"):
            try:
                _add_nvidia_dlls()
                self._load("cuda", "float16" if settings.device == "auto" else settings.compute_type)
                return
            except Exception as error:
                if settings.device == "cuda":
                    raise
                print(f"GPU speech-to-text unavailable ({str(error)[:80]}); using CPU.")
        self._load("cpu", "int8" if settings.device == "auto" else settings.compute_type)

    def _load(self, device: str, compute_type: str) -> None:
        from faster_whisper import WhisperModel  # imported late: slow to load

        self.model = WhisperModel(
            self.settings.model,
            device=device,
            compute_type=compute_type,
            download_root=str(MODELS_DIR / "whisper"),
        )
        self.device = device
        # Missing GPU libraries only fail on first use, and the first call is slow: do it now.
        self.transcribe(np.zeros(8000, dtype=np.float32))

    def transcribe(self, audio: np.ndarray, hints: str = "") -> str:
        segments, _ = self.model.transcribe(
            audio,
            language="en",
            beam_size=self.settings.beam_size,
            vad_filter=False,
            initial_prompt=f"{BASE_PROMPT} {hints}".strip(),
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()
