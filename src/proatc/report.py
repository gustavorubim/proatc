"""A problem report: one zip a tester can attach to an issue, instead of finding the journal by hand.

It holds the newest journal (and the one before, when the newest is only a minute old: the app was
probably restarted to report), the versions, the settings without secrets, the last sound check and
the checks of this computer. It never holds the OpenRouter key, the SimBrief ID or the pairing code:
the settings are copied from a list of what is safe, and every file is scrubbed of those values on its
way into the zip, wherever they might have been written (an error message that quoted an address, say).

With "hide my callsign and positions", the copy in the zip has the aircraft's callsign masked and every
latitude and longitude rounded to one decimal (about 10 km). The journal on disk is not touched.
"""

from __future__ import annotations

import json
import platform
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import phraseology as say
from .config import editable_values, load_settings, openrouter_key, safe_url
from .soundcheck import last

ISSUE_URL = "https://github.com/gustavorubim/proatc/issues/new"
RECENT_S = 60  # a journal younger than this is from a restart: the one before it is the flight
REMOVED = "[removed]"
MASK = "[callsign]"
CALLSIGN_KEYS = ("tail_number", "callsign", "callsign_spoken")
POSITION_KEYS = ("lat", "lon", "tcas_lat", "tcas_lon")

README = """Pro ATC problem report
======================

Made by "Report a problem" in Pro ATC on {when}. Attach this file to an issue at
{issue}

What is inside
--------------
about.json         Pro ATC's version, the operating system and Python versions, the settings
                   (without secrets), and the checks of this computer from the settings window.
journal/           The record of the newest run of the app{previous}: every radio call, what was
                   understood, every reply, and every twenty seconds the aircraft's position and the
                   tuned frequency.
sound-check.json   The last sound check, if one was run: the answers given and what was measured.

What it holds about you
-----------------------
The journal holds the flight's callsign, the aircraft's positions, and what was said on the radio
(as text: there is no audio). {hidden}

It never holds your OpenRouter key, your SimBrief ID or the pairing code for other devices.
"""
HIDDEN = (
    "For this file the callsign was replaced by [callsign] and every position rounded to one decimal "
    "(about 10 km). Airports and routes are still there."
)
SHOWN = 'To leave out the callsign and exact positions, make the report again with "Hide my callsign and positions".'


# ---- secrets ---------------------------------------------------------------------


def secrets() -> list[str]:
    """The values that must never leave the computer: the key, the SimBrief ID, the pairing code, a password or
    token in the local model's address, and the user's home folder (which holds their name), as errors write it."""
    settings = load_settings()  # includes SIMBRIEF_USER from .env when no other account was used
    found = {str(value).strip() for value in (openrouter_key(), settings.get("simbrief_user"), settings.get("remote_code"))
             if value and str(value).strip()}  # fmt: skip
    local = str((settings.get("config") or {}).get("llm", {}).get("local_url") or "")
    try:
        parts = urlsplit(local)
        found |= {value for value in (parts.username, parts.password) if value}
        found |= {value for pair in parts.query.split("&") for value in pair.split("=")[1:] if len(value) >= 4}
        if parts.username or parts.password or parts.query:
            found.add(local)
    except ValueError:
        pass
    home = str(Path.home())
    if len(home) > 3:  # not a bare drive or "/"
        found |= {home, home.replace("\\", "/")}
    found |= {json.dumps(value)[1:-1] for value in found}  # as it is written inside the journal's JSON
    return sorted(found, key=len, reverse=True)


def scrub(text: str, hidden: list[str]) -> str:
    """Remove every secret from a text, wherever it appears, even inside a longer word or number: a
    mangled timestamp costs less than a leaked ID. Only a value under four characters, which would
    match all over the journal and identify nobody, is removed as a whole word alone."""
    for value in hidden:
        pattern = re.escape(value) if len(value) >= 4 else rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])"
        text = re.sub(pattern, REMOVED, text, flags=re.IGNORECASE)
    return text


def safe_settings(config) -> dict:
    """The settings the settings window shows, which hold no secret; the local model's address cleaned."""
    values = editable_values(config)
    values["llm"]["local_url"] = safe_url(values["llm"]["local_url"])
    return values


# ---- which journals ---------------------------------------------------------------


def started(path: Path) -> datetime:
    try:
        return datetime.strptime(path.stem, "session-%Y%m%d-%H%M%S")
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime)


def journals(folder: Path, now: datetime | None = None) -> list[Path]:
    """The newest journal; and the one before it when the newest began under a minute ago."""
    found = sorted(folder.glob("session-*.jsonl")) if folder.exists() else []
    if not found:
        return []
    now = now or datetime.now()
    if len(found) > 1 and (now - started(found[-1])).total_seconds() < RECENT_S:
        return found[-2:]
    return found[-1:]


# ---- hiding the callsign and positions ---------------------------------------------


def callsigns(entries: list) -> list[str]:
    """Every way the aircraft's callsign is written in the journal: as filed, and as spoken."""
    found: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in CALLSIGN_KEYS and isinstance(inner, str) and len(inner.strip()) >= 2:
                    found.add(inner.strip())
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    walk(entries)
    spoken = {say.spoken_callsign(name) for name in found} | {say.airline_callsign(name) or "" for name in found}
    return sorted({name for name in found | spoken if len(name) >= 2}, key=len, reverse=True)


def mask(value, names: list[str]):
    """A journal entry with the callsign masked and positions rounded to one decimal."""
    if isinstance(value, dict):
        return {key: rounded(inner) if key in POSITION_KEYS else mask(inner, names) for key, inner in value.items()}
    if isinstance(value, list):
        return [mask(inner, names) for inner in value]
    if isinstance(value, str):
        for name in names:
            value = re.sub(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", MASK, value, flags=re.IGNORECASE)
        return value
    return value


def rounded(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 1)
    if isinstance(value, list):
        return [rounded(inner) for inner in value]
    return value


def journal_copy(path: Path, hide: bool, hidden: list[str]) -> str:
    """The journal as it goes into the zip: masked if asked, and always without secrets."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if hide:
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except ValueError:
                entries.append(line)
        names = callsigns([entry for entry in entries if not isinstance(entry, str)])
        lines = [mask(entry, names) if isinstance(entry, str) else json.dumps(mask(entry, names)) for entry in entries]
    return scrub("\n".join(lines) + "\n", hidden)


# ---- the zip ----------------------------------------------------------------------


def version() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed

    try:
        return installed("proatc")
    except PackageNotFoundError:
        return "source"


def write(home: Path, config, hide: bool, computer: list[dict], now: datetime | None = None) -> Path:
    """Write the report to `logs/reports/` in the data folder `home`. Returns the zip's path."""
    now = now or datetime.now()
    logs = home / "logs"
    hidden = secrets()
    chosen = journals(logs / "journal", now)
    about = {
        "pro_atc": version(),
        "system": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "made": now.isoformat(timespec="seconds"),
        "callsign_and_positions_hidden": hide,
        "settings": safe_settings(config),
        "this_computer": computer,
        "journals": [path.name for path in chosen],
    }
    readme = README.format(
        when=f"{now:%Y-%m-%d %H:%M}",
        issue=ISSUE_URL,
        previous=" (and the run before it, which had just ended)" if len(chosen) > 1 else "",
        hidden=HIDDEN if hide else SHOWN,
    )
    folder = logs / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"proatc-report-{now:%Y%m%d-%H%M%S}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", readme)
        archive.writestr("about.json", scrub(json.dumps(about, indent=1, default=str), hidden))
        for journal in chosen:
            archive.writestr(f"journal/{journal.name}", journal_copy(journal, hide, hidden))
        checked = last(logs)
        if checked is not None:
            archive.writestr("sound-check.json", scrub(json.dumps(checked, indent=1, default=str), hidden))
    return path
