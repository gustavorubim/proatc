import base64

import numpy as np
import pytest

from proatc.config import Config, _apply
from proatc.radio_fx import radio_effect
from proatc.xpbridge import XPBridge, _Ref, decode_value, describe


def test_decode_data_dataref_strips_null_padding():
    raw = base64.b64encode(b"C-GTLT" + b"\x00" * 34).decode()
    assert decode_value("data", raw) == "C-GTLT"


def test_decode_scalar_and_single_item_array():
    assert decode_value("float", 1.5) == 1.5
    assert decode_value("float_array", [2.0]) == 2.0
    assert decode_value("float_array", [1.0, 2.0]) == [1.0, 2.0]


def test_describe_converts_units():
    out = describe(
        {
            "tail_number": "N123AB",
            "elevation_m": 1000.0,
            "agl_m": 304.8,
            "groundspeed_ms": 51.444,
            "heading_mag": 359.7,
            "on_ground": 0,
            "com1_khz": 118505,
            "squawk": 200,
            "vs_fpm": -712.0,
        }
    )
    assert out["callsign"] == "N123AB"
    assert out["callsign_spoken"] == "November one two three Alfa Bravo"
    assert out["altitude_ft"] == 3280
    assert out["height_agl_ft"] == 1000
    assert out["groundspeed_kt"] == 100
    assert out["heading_mag"] == 0
    assert out["on_ground"] is False
    assert out["com1_mhz"] == "118.505"
    assert out["squawk"] == "0200"
    assert out["vertical_speed_fpm"] == -700


def test_bridge_applies_stream_updates_and_ignores_unknown_ids():
    bridge = XPBridge()
    bridge._refs = {7: _Ref("ias_kt", 7, "float")}
    bridge._handle({"type": "dataref_update_values", "data": {"7": 142.0, "99": 1}})
    bridge._handle({"type": "result", "req_id": 1, "success": True})
    assert bridge.snapshot() == {"ias_kt": 142.0}


def test_radio_effect_is_bounded_and_adds_squelch_tail():
    rate = 24000
    t = np.arange(rate) / rate
    voice = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    out = radio_effect(voice, rate, seed=1)
    assert out.dtype == np.float32
    assert out.size > voice.size
    assert np.max(np.abs(out)) <= 1.0


def test_radio_effect_removes_out_of_band_tone():
    rate = 24000
    t = np.arange(rate) / rate
    rumble = np.sin(2 * np.pi * 60 * t).astype(np.float32)
    tone = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    mixed = radio_effect(rumble + tone, rate, noise=0, seed=1)[:rate]
    spectrum = np.abs(np.fft.rfft(mixed))
    assert spectrum[60] < spectrum[1000] * 0.05


def test_config_override_and_unknown_key():
    config = Config()
    _apply(config, {"stt": {"device": "cuda"}, "ptt": {"key": "f13"}})
    assert config.stt.device == "cuda" and config.ptt.key == "f13"
    with pytest.raises(ValueError):
        _apply(config, {"stt": {"modle": "x"}})
