"""Real flights kept as regression tests.

A journal (flightlog.py) holds a whole flight: every call, every answer and, every twenty seconds,
the sim's state. Kept in a corpus folder, it is replayed (replay.py) before a release, and the
answers the engine gives now are compared with the answers a person last accepted for it. A change
in any of them is either a fault or an intended change, and someone has to read it to say which.

The corpus is private: a journal holds the pilot's callsign and every position of the flight. It is
kept in the app's data folder (`logs/regression/`), never in the repository. Beside each journal
`<name>.jsonl` is `<name>.accepted.json`: one entry per replayed answer (where in the journal, what
the pilot said, what the controller says now), written so that a diff of it reads as a list of
changed answers.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import private_folder
from .replay import NOTHING, Result, load

ACCEPTED = ".accepted.json"
NOT_REPLAYED = "(not replayed)"
STAMP = re.compile(r"session-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})")

# Replays a payload (replay.load) with the given settings (holding, traffic) and returns its result.
Runner = Callable[[dict, dict], Result]


def default_folder() -> Path:
    from . import config

    return config.PROJECT_DIR / "logs" / "regression"


def journal_folder() -> Path:
    from . import config

    return config.PROJECT_DIR / "logs" / "journal"


def newest_journal(folder: Path) -> Path | None:
    # The file names hold the time they were started, so the last name is the newest flight.
    found = sorted(folder.glob("session-*.jsonl")) if folder.is_dir() else []
    return found[-1] if found else None


def kept(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []


def accepted_path(journal: Path) -> Path:
    return journal.with_name(journal.stem + ACCEPTED)


# ---- names ------------------------------------------------------------------


def _started(journal: Path, events: list[dict]) -> str:
    match = STAMP.search(journal.name)
    if match:
        year, month, day, hour, minute, _ = match.groups()
        return f"{year}-{month}-{day}-{hour}{minute}"
    first = next((e["t"] for e in events if isinstance(e.get("t"), int | float)), None)
    return f"{datetime.fromtimestamp(first):%Y-%m-%d-%H%M}" if first else "undated"


def _route(events: list[dict]) -> str:
    plan = next((e["plan"] for e in events if e.get("type") == "plan" and e.get("plan")), None)
    if not plan:
        return "no-plan"
    ends = [str(plan.get(key) or "").upper() for key in ("origin", "destination")]
    return "-".join(re.sub(r"[^A-Z0-9]", "", end) or "XXXX" for end in ends)


def kept_name(journal: Path, events: list[dict]) -> str:
    """A name a person can find a flight by: when it started, and from where to where."""
    return f"{_started(journal, events)}-{_route(events)}"


# ---- the accepted result -----------------------------------------------------


def as_record(result: Result, settings: dict) -> dict:
    """The accepted result as it is written to disk: counts, then one entry per replayed answer."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        release = version("proatc")
    except PackageNotFoundError:
        release = "source"
    answers = []
    for answer in result.answers:
        entry = {"index": answer.index, "at": answer.at, "tuned": answer.tuned, "kind": answer.kind,
                 "said": answer.said, "now": answer.now}  # fmt: skip
        if answer.logged != answer.now and answer.kind != "new":
            entry["logged"] = answer.logged  # what the controller said in the real flight, where it differs
        answers.append(entry)
    return {"accepted_with": release, "settings": settings, "counts": counts(result), "answers": answers}


def counts(result: Result) -> dict:
    return {
        "calls": result.calls, "unprompted": result.unprompted, "skipped": result.skipped,
        "ticks": result.ticks, "new_unprompted": result.new, "shifted": result.shifted,
        "differ_from_log": len(result.differences or []),
    }  # fmt: skip


def write_accepted(journal: Path, record: dict) -> None:
    # One key per line inside each entry: a diff of two accepted files then names the answers that changed.
    accepted_path(journal).write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def read_accepted(journal: Path) -> dict | None:
    try:
        return json.loads(accepted_path(journal).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---- comparing ---------------------------------------------------------------


@dataclass
class Change:
    index: int
    at: float
    tuned: str | None
    said: str
    accepted: str
    now: str


def compare(accepted: list[dict], now: list[dict]) -> list[Change]:
    """The answers that are not what was accepted, in the order of the journal. An answer replayed only
    before, or only now, is a change too: a controller that falls silent, or starts speaking."""
    before = {entry["index"]: entry for entry in accepted}
    after = {entry["index"]: entry for entry in now}
    changes = []
    for index in sorted(before.keys() | after.keys()):
        old, new = before.get(index), after.get(index)
        was = old["now"] if old else _absent(new)
        is_now = new["now"] if new else _absent(old)
        if was != is_now:
            entry = new or old
            changes.append(Change(index, entry.get("at", 0.0), entry.get("tuned"), entry.get("said", ""), was, is_now))
    return changes


def _absent(other: dict) -> str:
    # A pilot call that is not replayed any more is a different thing from a controller saying nothing.
    return NOT_REPLAYED if other.get("kind") == "call" else NOTHING


@dataclass
class Check:
    name: str
    result: Result
    changes: list[Change]
    accepted: bool  # False: nothing was ever accepted for this journal
    events: list[dict]

    @property
    def changed(self) -> bool:
        return bool(self.changes) or not self.accepted


def check_one(journal: Path, runner: Runner, settings: dict) -> Check:
    payload = load(journal)
    record = read_accepted(journal)
    result = runner(payload, (record or {}).get("settings") or settings)
    now = as_record(result, settings)["answers"]
    changes = compare(record.get("answers", []), now) if record else []
    return Check(journal.stem, result, changes, record is not None, payload.get("events", []))


def check(folder: Path, runner: Runner, settings: dict) -> list[Check]:
    return [check_one(journal, runner, settings) for journal in kept(folder)]


# ---- keeping and accepting -----------------------------------------------------


def keep(journal: Path, folder: Path, runner: Runner, settings: dict) -> Path:
    """Copy a journal into the corpus and accept what the engine answers to it now."""
    events = load(journal).get("events", [])
    private_folder(folder)
    name = kept_name(journal, events)
    target = folder / f"{name}.jsonl"
    number = 2
    while target.exists() and target.read_bytes() != journal.read_bytes():
        target = folder / f"{name}-{number}.jsonl"
        number += 1
    if not target.exists():
        shutil.copyfile(journal, target)
    accept_one(target, runner, settings)
    return target


def accept_one(journal: Path, runner: Runner, settings: dict) -> Result:
    record = read_accepted(journal)
    settings = (record or {}).get("settings") or settings  # the settings it was kept with stay
    result = runner(load(journal), settings)
    write_accepted(journal, as_record(result, settings))
    return result


def find(folder: Path, name: str) -> Path | None:
    """A kept journal by its name, with or without the extension, or by the start of its name if only one fits."""
    stem = name.removesuffix(".jsonl")
    exact = folder / f"{stem}.jsonl"
    if exact.exists():
        return exact
    matches = [journal for journal in kept(folder) if journal.stem.startswith(stem)]
    return matches[0] if len(matches) == 1 else None


def listing(folder: Path) -> list[str]:
    lines = []
    for journal in kept(folder):
        record = read_accepted(journal)
        if record is None:
            lines.append(f"{journal.stem}: never accepted")
            continue
        n = record.get("counts", {})
        lines.append(
            f"{journal.stem}: {n.get('calls', 0)} calls, {n.get('unprompted', 0)} unprompted, "
            f"{n.get('skipped', 0)} could not be replayed; accepted with {record.get('accepted_with', '?')}"
        )
    return lines


# ---- the report ------------------------------------------------------------------


def callsigns(events: list[dict]) -> list[str]:
    """Every way the flight's callsign can be written or spoken in it, longest first."""
    from .phraseology import airline_callsign, spoken_callsign

    found: set[str] = set()
    for event in events:
        raw = (event.get("context") or {}).get("raw") or {}
        plan = event.get("plan") if event.get("type") == "plan" else None
        for written in (raw.get("tail_number"), (plan or {}).get("callsign")):
            if not written or not str(written).strip():
                continue
            written = str(written).strip()
            found |= {written, written.replace("-", ""), spoken_callsign(written)}
            found.add(airline_callsign(written) or "")
    return sorted((name for name in found if len(name) >= 3), key=len, reverse=True)


def mask(text: str, names: list[str]) -> str:
    for name in names:
        text = re.sub(re.escape(name), "CALLSIGN", text, flags=re.I)
    return text


def _clock(seconds: float) -> str:
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes // 60}:{minutes % 60:02d}:{rest:02d}"


def _where(events: list[dict], index: int, anonymous: bool) -> str:
    raw = ((events[index] if 0 <= index < len(events) else {}).get("context") or {}).get("raw") or {}
    if "lat" not in raw or "lon" not in raw:
        return ""
    digits = 1 if anonymous else 3  # a tenth of a degree is a few miles: the area, not the aircraft
    feet = f" {round(raw['elevation_m'] / 0.3048, -2):.0f} ft" if "elevation_m" in raw else ""
    return f" near {raw['lat']:.{digits}f},{raw['lon']:.{digits}f}{feet}"


def report(found: Check, anonymous: bool = False) -> str:
    """What a person needs to judge one kept flight: the counts, then each changed answer, where and when."""
    names = callsigns(found.events) if anonymous else []
    n = counts(found.result)
    lines = [
        f"{found.name}: {n['calls']} calls and {n['unprompted']} unprompted calls replayed, "
        f"{n['skipped']} could not be; {n['ticks']} moments the controller could speak, "
        f"{n['new_unprompted']} new unprompted calls; {n['differ_from_log']} answers differ from the flight as logged"
    ]
    if not found.accepted:
        lines.append("  never accepted: read `proatc replay` of it, then `proatc journals accept`")
    elif not found.changes:
        lines.append("  no change since accepted")
    else:
        lines.append(f"  {len(found.changes)} changed since accepted:")
    for change in found.changes:
        where = _where(found.events, change.index, anonymous)
        lines += [
            f"  +{_clock(change.at)} {change.tuned or '---.---'} #{change.index}{where} {mask(change.said, names)}",
            f"      accepted: {mask(change.accepted, names)}",
            f"      now:      {mask(change.now, names)}",
        ]
    return "\n".join(lines)
