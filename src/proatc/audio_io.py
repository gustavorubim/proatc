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


STREAM_STEP_S = 0.1


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

    def play_stream(self, pieces, sample_rate: int) -> int:
        """Play sound that is still being made: each piece as it arrives. Returns a ticket for `wait`."""
        with self._lock:
            self._generation += 1
            ticket = self._generation
            sd.stop()
            self._ends_at = float("inf")  # not known until the last piece has come
        threading.Thread(target=self._feed, args=(ticket, pieces, sample_rate), name="speech", daemon=True).start()
        return ticket

    def _feed(self, ticket: int, pieces, sample_rate: int) -> None:
        import time

        step = max(int(sample_rate * STREAM_STEP_S), 1)  # written in short steps, so a cut-off is heard at once
        try:
            with sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32", device=self.device) as out:
                for piece in pieces:
                    piece = np.ascontiguousarray(piece, dtype=np.float32).reshape(-1, 1)
                    for start in range(0, len(piece), step):
                        if self._generation != ticket:
                            out.abort()
                            return
                        out.write(piece[start : start + step])
        except Exception as error:
            print(f"Audio output failed: {type(error).__name__}: {error}", flush=True)
        finally:
            close = getattr(pieces, "close", None)
            if close:
                close()  # stops the voice engine if it was cut off
            with self._lock:
                if self._generation == ticket:
                    self._ends_at = time.monotonic()

    @property
    def playing(self) -> bool:
        import time

        return time.monotonic() < self._ends_at

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
