"""Downloaded models are checked before they are used."""

from __future__ import annotations

import httpx
import pytest

from proatc import tts


class Served:
    def __init__(self, body: bytes):
        self.body, self.headers = body, {"content-length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self, size):
        yield self.body


def test_a_voice_file_that_does_not_download_as_published_is_thrown_away(tmp_path, monkeypatch):
    model, voices = tmp_path / "kokoro-v1.0.onnx", tmp_path / "voices-v1.0.bin"
    monkeypatch.setattr(tts, "DOWNLOADS", {model: "https://example.invalid/model", voices: "https://example.invalid/voices"})
    monkeypatch.setattr(tts, "SHA256", {model: "0" * 64, voices: "0" * 64})
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: Served(b"not the model"))
    with pytest.raises(RuntimeError, match="did not download as published"):
        tts.download_models(report=lambda line: None)
    assert not model.exists() and not (tmp_path / "kokoro-v1.0.onnx.part").exists()


def test_a_voice_file_that_matches_is_kept(tmp_path, monkeypatch):
    import hashlib

    model = tmp_path / "kokoro-v1.0.onnx"
    body = b"the model"
    monkeypatch.setattr(tts, "DOWNLOADS", {model: "https://example.invalid/model"})
    monkeypatch.setattr(tts, "SHA256", {model: hashlib.sha256(body).hexdigest()})
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: Served(body))
    tts.download_models(report=lambda line: None)
    assert model.read_bytes() == body
