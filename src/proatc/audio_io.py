"""Microphone capture while push-to-talk is held, and blocking playback."""

from __future__ import annotations

import threading

import numpy as np
import sounddevice as sd

MIC_RATE = 16000  # what speech-to-text models expect


class Recorder:
    def __init__(self, device: int | None = None):
        self.device = device
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None

    def _callback(self, indata, frames, time_info, status) -> None:
        with self._lock:
            self._chunks.append(indata[:, 0].copy())

    def start(self) -> None:
        with self._lock:
            self._chunks = []
        self._stream = sd.InputStream(
            samplerate=MIC_RATE,
            channels=1,
            dtype="float32",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._chunks)


def play(samples: np.ndarray, sample_rate: int, device: int | None = None) -> None:
    sd.play(samples, sample_rate, device=device)
    sd.wait()


class Player:
    """One voice at a time on the speakers; a new transmission cuts off the previous one."""

    def __init__(self, device: int | None = None):
        self.device = device
        self._lock = threading.Lock()
        self._generation = 0
        self._ends_at = 0.0

    def play(self, samples: np.ndarray, sample_rate: int) -> int:
        """Start playing and return a ticket for `wait`."""
        import time

        with self._lock:
            self._generation += 1
            sd.stop()
            sd.play(samples, sample_rate, device=self.device)
            self._ends_at = time.monotonic() + len(samples) / sample_rate
            return self._generation

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._ends_at = 0.0
            sd.stop()

    def wait(self, ticket: int, should_stop=lambda: False) -> bool:
        """Block until this transmission ends. False if it was cut off or `should_stop` fired."""
        import time

        while time.monotonic() < self._ends_at:
            if self._generation != ticket:
                return False
            if should_stop():
                self.stop()
                return False
            time.sleep(0.05)
        return self._generation == ticket


def list_devices() -> str:
    return str(sd.query_devices())
