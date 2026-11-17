"""Make clean synthetic speech sound like an aviation VHF radio."""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt


def radio_effect(samples: np.ndarray, sample_rate: int, noise: float = 0.012, seed: int | None = None) -> np.ndarray:
    """Band-limit, compress and add static plus a squelch tail. Returns float32 in [-1, 1]."""
    audio = np.asarray(samples, dtype=np.float32)
    if audio.size == 0:
        return audio
    rng = np.random.default_rng(seed)

    # AM voice radio passes roughly 300-3000 Hz.
    sos = butter(4, [300, 3000], btype="bandpass", fs=sample_rate, output="sos")
    audio = sosfilt(sos, audio).astype(np.float32)

    peak = float(np.max(np.abs(audio))) or 1.0
    audio = np.tanh(audio / peak * 2.5) * 0.8  # compression with mild overdrive

    audio = audio + rng.normal(0, noise, audio.size).astype(np.float32)

    # Short burst of static when the transmitter unkeys.
    tail = rng.normal(0, 0.12, int(sample_rate * 0.09)).astype(np.float32)
    tail *= np.linspace(1, 0, tail.size, dtype=np.float32)
    tail = sosfilt(sos, tail).astype(np.float32)

    return np.clip(np.concatenate([audio, tail]), -1, 1).astype(np.float32)


class RadioStream:
    """The same radio sound for speech that arrives in pieces: the filter carries on from one piece to the next.

    The loudness cannot be measured over the whole sentence before it starts, so it is taken from what has
    been heard so far, starting from a typical level."""

    def __init__(self, sample_rate: int, noise: float = 0.012, seed: int | None = None):
        self.sample_rate, self.noise = sample_rate, noise
        self.rng = np.random.default_rng(seed)
        self.sos = butter(4, [300, 3000], btype="bandpass", fs=sample_rate, output="sos")
        self.state = np.zeros((self.sos.shape[0], 2))
        self.peak = 0.2

    def process(self, samples: np.ndarray) -> np.ndarray:
        audio = np.asarray(samples, dtype=np.float32)
        if audio.size == 0:
            return audio
        audio, self.state = sosfilt(self.sos, audio, zi=self.state)
        self.peak = max(self.peak, float(np.max(np.abs(audio))))
        audio = np.tanh(audio / self.peak * 2.5) * 0.8
        audio = audio + self.rng.normal(0, self.noise, audio.size)
        return np.clip(audio, -1, 1).astype(np.float32)

    def tail(self) -> np.ndarray:
        """The burst of static when the transmitter unkeys."""
        tail = self.rng.normal(0, 0.12, int(self.sample_rate * 0.09)).astype(np.float32)
        tail *= np.linspace(1, 0, tail.size, dtype=np.float32)
        return np.clip(sosfilt(self.sos, tail), -1, 1).astype(np.float32)


def radio_stream(pieces, sample_rate: int, noise: float = 0.012, seed: int | None = None):
    """`pieces` of clean speech, through the radio, as they come."""
    effect = RadioStream(sample_rate, noise, seed)
    try:
        for piece in pieces:
            yield effect.process(piece)
        yield effect.tail()
    finally:
        close = getattr(pieces, "close", None)
        if close:
            close()
