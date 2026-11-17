"""The problem report never carries a secret, hides the callsign and positions when asked, and the checks of
this computer say the right thing on systems this suite does not run on (they are stood in for)."""

import json
import zipfile
from datetime import datetime

from proatc import config as config_module
from proatc import report, soundcheck, system_check
from proatc.config import Config
from proatc.server import AppServer

KEY = "sk-or-v1-0000placeholder0000test0000key"
SIMBRIEF = "771234"
CODE = "QZXW7K2M"


def data_folder(tmp_path, monkeypatch):
    """A data folder holding every secret the app knows: the key in .env, the SimBrief ID and the pairing code
    in settings.json, and all three quoted in the journal, as an error message might have done."""
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("SIMBRIEF_USER", raising=False)
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={KEY}\n", encoding="utf-8")
    settings = {"simbrief_user": SIMBRIEF, "remote_code": CODE, "config": {"llm": {"local_url": "http://me:pw@pc:11434/v1"}}}
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    journal = tmp_path / "logs" / "journal"
    journal.mkdir(parents=True)
    raw = {"tail_number": "G-ABCD", "lat": 51.47123, "lon": -0.45678, "tcas_lat": [51.5555, 51.6666], "on_ground": 1}
    entries = [
        {"t": 1.0, "type": "session", "version": "0.14.0"},
        {"t": 2.0, "type": "error", "text": f"SimBrief could not be reached: https://x/api?username={SIMBRIEF}"},
        {"t": 3.0, "type": "info", "text": f"key {KEY} code {CODE.lower()}"},
        {"t": 4.0, "type": "atc", "station": "Heathrow Ground", "text": "Golf Alfa Bravo Charlie Delta, taxi to holding point",
         "context": {"tuned": "121.900", "aircraft": {"callsign": "G-ABCD"}, "raw": raw}},
        {"t": 5.0, "type": "pilot", "text": "Speedbird one two three, request taxi", "context": {"flight": {}}},
        {"t": 6.0, "type": "plan", "plan": {"callsign": "BAW123", "origin": "EGLL", "destination": "LFPG"}},
        {"t": 7.0, "type": "trace", "context": {"raw": {"lat": 51.47123, "lon": -0.45678}}},
    ]  # fmt: skip
    (journal / "session-20261117-100000.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return tmp_path


def contents(path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


def test_the_report_never_holds_the_key_the_simbrief_id_or_the_pairing_code(tmp_path, monkeypatch):
    home = data_folder(tmp_path, monkeypatch)
    soundcheck.keep({"verdict": f"Sound check passed {CODE}", "steps": []}, home / "logs")
    path = report.write(home, config_module.load_config(), False, [], now=datetime(2026, 11, 17, 12, 0, 0))
    assert path.parent == home / "logs" / "reports" and path.name == "proatc-report-20261117-120000.zip"
    files = contents(path)
    assert set(files) == {"README.txt", "about.json", "journal/session-20261117-100000.jsonl", "sound-check.json"}
    everything = "\n".join(files.values())
    for secret in (KEY, SIMBRIEF, CODE, CODE.lower(), "me:pw"):
        assert secret not in everything
    assert everything.count(report.REMOVED) >= 4 and ".env" not in everything

    about = json.loads(files["about.json"])
    assert about["settings"]["llm"]["local_url"] == "http://pc:11434/v1" and about["python"] and about["system"]
    assert "simbrief_user" not in files["about.json"] and "remote_code" not in files["about.json"]
    # Without hiding, the flight is there as it was: the README says so.
    assert "G-ABCD" in files["journal/session-20261117-100000.jsonl"] and "51.47123" in everything
    assert "callsign, the aircraft's positions, and what was said" in files["README.txt"]
    assert report.ISSUE_URL in files["README.txt"]


def test_the_secret_comes_out_wherever_it_was_written(tmp_path, monkeypatch):
    data_folder(tmp_path, monkeypatch)
    hidden = report.secrets()
    assert {KEY, SIMBRIEF, CODE} <= set(hidden)
    assert report.scrub(f"x{KEY}y id={SIMBRIEF}, ts 1763{SIMBRIEF}.5", hidden) == "x[removed]y id=[removed], ts 1763[removed].5"
    assert report.scrub("runway 27 at 12:00", ["27"]) == "runway [removed] at 12:00"  # a short one: whole words only
    assert report.scrub("runway 127", ["27"]) == "runway 127"


def test_hiding_masks_the_callsign_and_rounds_positions_in_the_copy_only(tmp_path, monkeypatch):
    home = data_folder(tmp_path, monkeypatch)
    journal = home / "logs" / "journal" / "session-20261117-100000.jsonl"
    before = journal.read_text(encoding="utf-8")
    path = report.write(home, config_module.load_config(), True, [], now=datetime(2026, 11, 17, 12, 0, 0))
    copy = contents(path)["journal/session-20261117-100000.jsonl"]
    for name in ("G-ABCD", "Golf Alfa Bravo Charlie Delta", "BAW123", "Speedbird one two three", "51.47123", "-0.45678"):
        assert name not in copy
    lines = [json.loads(line) for line in copy.splitlines()]
    assert lines[3]["text"] == "[callsign], taxi to holding point" and lines[4]["text"] == "[callsign], request taxi"
    assert lines[3]["context"]["raw"]["lat"] == 51.5 and lines[3]["context"]["raw"]["lon"] == -0.5
    assert lines[3]["context"]["raw"]["tcas_lat"] == [51.6, 51.7] and lines[3]["context"]["raw"]["on_ground"] == 1
    assert lines[5]["plan"]["origin"] == "EGLL" and lines[3]["station"] == "Heathrow Ground"  # airports stay
    assert SIMBRIEF not in copy and KEY not in copy
    assert "replaced by [callsign]" in contents(path)["README.txt"]
    assert journal.read_text(encoding="utf-8") == before  # the journal on disk is untouched


def test_the_run_before_is_included_when_the_newest_has_just_started(tmp_path):
    folder = tmp_path / "journal"
    folder.mkdir()
    for name in ("session-20261117-090000", "session-20261117-100000", "session-20261117-115950"):
        (folder / f"{name}.jsonl").write_text("{}\n", encoding="utf-8")
    names = [path.stem for path in report.journals(folder, datetime(2026, 11, 17, 12, 0, 0))]
    assert names == ["session-20261117-100000", "session-20261117-115950"]
    names = [path.stem for path in report.journals(folder, datetime(2026, 11, 17, 12, 5, 0))]
    assert names == ["session-20261117-115950"]
    assert report.journals(tmp_path / "nothing") == []


def test_a_report_with_no_journal_and_no_sound_check_still_says_what_it_is(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("SIMBRIEF_USER", raising=False)
    checks = [system_check.item("keyboard", "Push-to-talk key", False, "Wayland.", "Use the button.")]
    files = contents(report.write(tmp_path, Config(), False, checks))
    assert set(files) == {"README.txt", "about.json"} and json.loads(files["about.json"])["this_computer"] == checks


def test_the_app_command_writes_the_report_before_the_engine_is_ready(tmp_path, monkeypatch):
    home = data_folder(tmp_path, monkeypatch)
    monkeypatch.setattr("proatc.voice_loop.PROJECT_DIR", home)
    monkeypatch.setattr("proatc.server.PROJECT_DIR", home)
    app = AppServer(Config(), 0, "token")
    seen: list[dict] = []
    app.session._sink = seen.append
    app.session.computer = [system_check.item("speakers", "Speakers or headset", True, "1 found.")]
    app._command({"cmd": "report", "hide": True})
    saved = seen[-1]
    assert saved["type"] == "report_saved" and saved["issue"] == report.ISSUE_URL and saved["path"].endswith(".zip")
    files = contents(saved["path"])
    assert json.loads(files["about.json"])["callsign_and_positions_hidden"] is True and "G-ABCD" not in "".join(files.values())


# ---- this computer: macOS and Linux stood in for -----------------------------------------


def devices(outputs=1, inputs=1):
    class Card:
        @staticmethod
        def query_devices():
            return [{"name": "out", "max_output_channels": 2, "max_input_channels": 0}] * outputs + [
                {"name": "in", "max_output_channels": 0, "max_input_channels": 1}
            ] * inputs

    return lambda: Card


def by_id(checks):
    return {check["id"]: check for check in checks}


def test_a_mac_without_input_monitoring_is_told_how_to_allow_it():
    for access, ok in (("denied", False), ("unknown", None), ("granted", True)):
        checks = by_id(system_check.this_computer(True, "darwin", {}, lambda access=access: access, devices()))
        assert checks["keyboard"]["ok"] is ok
        assert ("Input Monitoring" in checks["keyboard"]["fix"]) is (ok is not True)
    notes = system_check.startup_notes(system_check.this_computer(True, "darwin", {}, lambda: "denied", devices()), "darwin")
    assert len(notes) == 1 and notes[0].startswith("This computer: macOS does not let Pro ATC hear keys")
    # Asking macOS on another system finds no IOKit and says it cannot tell.
    assert system_check.input_monitoring() in ("unknown", "granted", "denied")


def test_linux_under_wayland_is_told_the_key_cannot_work_and_what_to_use():
    for env in ({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}, {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}):
        key = by_id(system_check.this_computer(True, "linux", env, load=devices()))["keyboard"]
        assert key["ok"] is False and "Wayland" in key["text"] and "Hold to talk" in key["fix"] and "joystick" in key["fix"]
    x11 = by_id(system_check.this_computer(True, "linux", {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, load=devices()))
    assert x11["keyboard"]["ok"] is True and x11["keyboard"]["fix"] == ""
    bare = by_id(system_check.this_computer(True, "linux", {}, load=devices()))
    assert bare["keyboard"]["ok"] is False and "No X11 display" in bare["keyboard"]["text"]


def test_a_missing_sound_library_or_device_is_named_with_its_fix():
    def missing():
        raise OSError("PortAudio library not found")

    checks = by_id(system_check.this_computer(True, "linux", {"DISPLAY": ":0"}, load=missing))
    assert checks["sound_library"]["ok"] is False and "libportaudio2" in checks["sound_library"]["fix"]
    assert checks["speakers"]["ok"] is None and checks["microphone"]["ok"] is None
    none = by_id(system_check.this_computer(True, "darwin", {}, lambda: "granted", devices(outputs=0, inputs=0)))
    assert none["speakers"]["ok"] is False and none["microphone"]["ok"] is False
    assert "Privacy & Security, Microphone" in none["microphone"]["fix"]
    windows = system_check.this_computer(True, "win32", {}, load=devices())
    assert all(check["ok"] for check in windows) and system_check.startup_notes(windows, "win32") == []
    assert system_check.startup_notes([system_check.item("x", "X", False, "Broken.", "Fix it.")], "win32") == []
    stopped = by_id(system_check.this_computer(False, "win32", {}, load=devices()))
    assert stopped["keyboard"]["ok"] is False and "did not start" in stopped["keyboard"]["text"]
