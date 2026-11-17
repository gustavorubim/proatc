"""The ATIS written down for the side panel: the same facts as the recording, in the same units."""

from dataclasses import replace
from types import SimpleNamespace

from proatc import config as config_module
from proatc.atis import AtisBook, compose, decoded
from proatc.config import Config
from proatc.server import build_state
from proatc.voice_loop import Session
from proatc.weather import Cloud, Weather
from tests.test_radio_loop import FakeBridge
from tests.test_realism import APRON, ground_world, taxiing


def rows(found):
    return {row["label"]: row["value"] for row in found}


def test_a_fine_day_in_europe_and_in_the_united_states(tmp_path):
    db, _, _ = ground_world(tmp_path)
    airport = db.airport("EGXX")
    weather = Weather(wind_dir_true=268, wind_kt=12, visibility_sm=10, clouds=[Cloud(0.2, 3020)], temperature_c=14.6,
                      dewpoint_c=9.2, qnh_hpa=1013.4)  # fmt: skip
    europe = decoded(airport, weather, "K", 12 * 3600 + 50 * 60, variation=1.0)
    assert [row["label"] for row in europe] == ["Information", "Runways", "Wind", "Visibility", "Clouds", "Temperature", "QNH"]
    assert rows(europe) == {
        "Information": "K · 1250Z",
        "Runways": "Landing 27L · departing 27R",
        "Wind": "270° / 12 kt",
        "Visibility": "10 km or more",
        "Clouds": "FEW 3,000 ft",
        "Temperature": "15°C · dew point 9°C",
        "QNH": "1013 hPa",
    }
    assert not any(row.get("alert") for row in europe)

    states = decoded(replace(airport, ident="KXXX"), weather, "B", 0.0)
    assert rows(states)["Altimeter"] == "29.93 inHg" and rows(states)["Visibility"] == "10 SM" and "QNH" not in rows(states)
    assert rows(states)["Information"] == "B · 0000Z"
    clear = decoded(replace(airport, ident="KXXX"), replace(weather, clouds=[], wind_kt=0.4), "B", 0.0)
    assert rows(clear)["Clouds"] == "Sky clear" and rows(clear)["Wind"] == "Calm"
    assert rows(decoded(airport, replace(weather, clouds=[Cloud(0.9, 30000)]), "K", 0.0))["Clouds"] == "No significant cloud"


def test_bad_weather_is_marked(tmp_path):
    db, _, _ = ground_world(tmp_path)
    airport = db.airport("EGXX")
    weather = Weather(wind_dir_true=90, wind_kt=18, gust_kt=31, visibility_sm=0.3, rain=0.8, thunderstorm=True, fog=True,
                      clouds=[Cloud(0.6, 180, cumulonimbus=True), Cloud(0.95, 4200)], temperature_c=-2.4, dewpoint_c=-3,
                      qnh_hpa=987.9)  # fmt: skip
    found = decoded(airport, weather, "Z", 86399.0)
    values = rows(found)
    assert values["Information"] == "Z · 2359Z" and values["Wind"] == "090° / 18 kt, gusting 31"
    assert values["Visibility"] == "500 m" and values["Weather"] == "Thunderstorm, Rain, Fog"
    assert values["Clouds"] == "BKN 200 ft CB · OVC 4,200 ft" and values["Temperature"] == "-2°C · dew point -3°C"
    assert values["QNH"] == "987 hPa" and values["Procedures"] == "Low visibility procedures in force"
    assert {row["label"] for row in found if row.get("alert")} == {"Visibility", "Weather", "Clouds", "Procedures"}
    # A gust only a little above the wind is not reported, as in the recording.
    assert rows(decoded(airport, replace(weather, gust_kt=20), "Z", 0.0))["Wind"] == "090° / 18 kt"
    assert rows(decoded(airport, replace(weather, visibility_sm=4.0), "Z", 0.0))["Visibility"] == "6 km"
    assert rows(decoded(replace(airport, ident="KXXX"), replace(weather, visibility_sm=0.4), "Z", 0.0))["Visibility"] == (
        "Less than 1 SM"
    )


def test_the_written_atis_says_what_the_recording_says(tmp_path):
    db, _, _ = ground_world(tmp_path)
    airport = db.airport("EGXX")
    for weather in (
        Weather(wind_dir_true=268, wind_kt=12, clouds=[Cloud(0.4, 2500)], qnh_hpa=1009.0),
        Weather(wind_dir_true=80, wind_kt=7, visibility_sm=2.0, rain=0.3, clouds=[Cloud(0.7, 900)], qnh_hpa=998.0),
    ):
        spoken = compose(airport, weather, "C", 43200.0)
        written = rows(decoded(airport, weather, "C", 43200.0))
        assert ("hectopascals" in spoken) == (int(written["QNH"].split()[0]) < 1000)
        assert ("Light rain." in spoken) == ("Weather" in written)
        landing = written["Runways"].split(" · ")[0].removeprefix("Landing ")
        from proatc import phraseology as say

        assert f"Runway {say.runway(landing)} in use for landing" in spoken


def test_the_app_gets_the_written_atis_with_the_airport(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    db, engine, _ = ground_world(tmp_path)
    state = {**taxiing(*APRON), "com1_khz": 121700, "zulu_seconds": 45000.0, "wx_qnh_pas": 101300.0,
             "wx_wind_alt_m": [30.0], "wx_wind_kt": [10.0], "wx_wind_dir": [270.0], "wx_visibility_sm": 10.0}  # fmt: skip
    session = Session(Config(), emit=lambda event: None)
    session.bridge, session.db, session.book, session.engine = FakeBridge(state), db, AtisBook(), engine
    session.stt, session.ready = SimpleNamespace(device="cpu"), True
    airport = build_state(session, db)["airport"]
    assert airport["ident"] == "EGXX" and airport["atis_letter"]
    written = rows(airport["atis"])
    assert written["Information"].startswith(f"{airport['atis_letter']} · ") and written["QNH"] == "1013 hPa"
    assert written["Wind"].endswith("/ 10 kt") and "Runways" in written
