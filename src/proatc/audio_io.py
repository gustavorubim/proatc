"""Microphone capture while push-to-talk is held, and blocking playback."""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

MIC_RATE = 16000  # what speech-to-text models expect


class Recorder:
    def __init__(self, device: int | None = None, limit_s: float | None = None):
        self.device = device
        self.limit_s = limit_s  # the most one recording keeps: then the microphone stops by itself
        self._chunks: list[np.ndarray] = []
        self._room: int | None = None  # samples still to record before the limit
        self._lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._stream: sd.InputStream | None = None

    def _callback(self, indata, frames, time_info, status) -> None:
        with self._lock:
            take = indata[:, 0]
            if self._room is not None:
                take = take[: max(self._room, 0)]
                self._room -= take.size
            if take.size:
                self._chunks.append(take.copy())
            full = self._room is not None and self._room <= 0
        if full:
            raise sd.CallbackStop  # at the limit the microphone stops, whatever else the app is doing

    def start(self) -> None:
        with self._lock:
            self._chunks = []
            self._room = int(self.limit_s * MIC_RATE) if self.limit_s else None
        with self._stream_lock:
            self._close()  # one left open
            self._stream = sd.InputStream(
                samplerate=MIC_RATE,
                channels=1,
                dtype="float32",
                device=self.device,
                callback=self._callback,
            )
            self._stream.start()

    def halt(self) -> None:
        """Stop the microphone now. What it recorded is kept for stop()."""
        with self._stream_lock:
            self._close()

    def _close(self) -> None:
        stream, self._stream = self._stream, None
        if stream:
            stream.stop()
            stream.close()

    def stop(self) -> np.ndarray:
        self.halt()
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._chunks)


def play(samples: np.ndarray, sample_rate: int, device: int | None = None) -> None:
    sd.play(samples, sample_rate, device=device)
    sd.wait()


STREAM_STEP_S = 0.1


@dataclass
class Meter:
    """What a player can tell about its own sound without anyone listening: for the sound check."""

    opened: int = 0  # output streams (or plays) that started
    frames: int = 0  # samples handed to the sound card
    underruns: int = 0  # times the sound card ran out of sound to play
    failed: str = ""  # why a stream could not be opened or written, if one could not


def play_underruns() -> int:
    """Whether the last `sd.play` ran out of sound, as PortAudio reports it (1 or 0)."""
    try:
        return int(bool(sd.get_status().output_underflow))
    except Exception:
        return 0  # nothing was played yet, or a stand-in sound card that does not report it


def device_name(device: int | None, kind: str) -> str:
    """The name of a sound device ("input" or "output"); the system's default when `device` is None."""
    try:
        return str(sd.query_devices(device, kind)["name"])
    except Exception:
        return "unknown"


class Player:
    """One voice at a time on the speakers; a new transmission cuts off the previous one."""

    def __init__(self, device: int | None = None, alongside: bool = False):
        self.device = device
        # A second radio plays alongside the first: its sound has its own stream and stops nobody else's.
        self.alongside = alongside
        self._lock = threading.Lock()
        self._generation = 0
        self._ends_at = 0.0
        self.meter: Meter | None = None  # set by the sound check to count what is played

    def play(self, samples: np.ndarray, sample_rate: int) -> int:
        """Start playing and return a ticket for `wait`."""
        import time

        with self._lock:
            self._generation += 1
            sd.stop()
            sd.play(samples, sample_rate, device=self.device)
            if self.meter is not None:
                self.meter.opened += 1
                self.meter.frames += len(samples)
            self._ends_at = time.monotonic() + len(samples) / sample_rate
            return self._generation

    def play_stream(self, pieces, sample_rate: int) -> int:
        """Play sound that is still being made: each piece as it arrives. Returns a ticket for `wait`."""
        with self._lock:
            self._generation += 1
            ticket = self._generation
            if not self.alongside:
                sd.stop()
            self._ends_at = float("inf")  # not known until the last piece has come
        threading.Thread(target=self._feed, args=(ticket, pieces, sample_rate), name="speech", daemon=True).start()
        return ticket

    def _feed(self, ticket: int, pieces, sample_rate: int) -> None:
        import time

        step = max(int(sample_rate * STREAM_STEP_S), 1)  # written in short steps, so a cut-off is heard at once
        meter = self.meter
        try:
            with sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32", device=self.device) as out:
                if meter is not None:
                    meter.opened += 1
                for piece in pieces:
                    piece = np.ascontiguousarray(piece, dtype=np.float32).reshape(-1, 1)
                    for start in range(0, len(piece), step):
                        if self._generation != ticket:
                            out.abort()
                            return
                        underflowed = out.write(piece[start : start + step])
                        if meter is not None:
                            meter.frames += len(piece[start : start + step])
                            meter.underruns += bool(underflowed)  # PortAudio filled a gap with silence
        except Exception as error:
            if meter is not None:
                meter.failed = f"{type(error).__name__}: {error}"
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
            if not self.alongside:
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
