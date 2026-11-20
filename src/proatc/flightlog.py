"""A record of the flight's radio work, for bug reports and debriefing.

Every event the session emits is kept with the time and, for transmissions, where the
aircraft was and who it was talking to. Nothing secret is recorded: no key, no audio. Saved logs
are attached to public bug reports, so the OpenRouter key and the SimBrief username are taken out
of anything recorded, wherever they appear (`known_secrets`).

It is kept twice. In memory, for "Save log" and the debrief. And on disk as it happens (the
journal): one file for each time the app runs, a line for each event, written at once so that a
crash or a closed window loses nothing. The journal also holds a trace: every twenty seconds,
where the aircraft is and who answers on the tuned frequency. A fault can then be looked into
after the flight without anyone having thought to save a log. The files stay on this computer;
the oldest are deleted.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import config

KEPT = 5000
JOURNALS_KEPT = 40  # runs of the app whose journal is kept
JOURNAL_NAME = "session-{:%Y%m%d-%H%M%S}.jsonl"
WITH_CONTEXT = ("pilot", "atc", "atis", "understood", "silent", "info", "error", "coach")
# The sound check's prompts are not kept: its outcome is written once, as a `sound_check` note.
SKIPPED = ("tx", "timings", "status", "settings", "log_saved", "chatter", "debrief", "capture", "phrase", "sound_check",
           "report_saved")  # fmt: skip
HIDDEN = "[hidden]"
SECRETS_RECHECK_S = 10.0  # the key or the username may be changed in the app


def known_secrets() -> list[str]:
    """What must never be written down: the OpenRouter key and the SimBrief username, as the app has them."""
    found = []
    for get in (lambda: config.openrouter_key(), lambda: config.load_settings().get("simbrief_user")):
        try:
            value = str(get() or "").strip()
        except Exception:
            continue  # a settings file that cannot be read holds nothing to hide
        if len(value) >= 3:  # shorter would hide pieces of ordinary words
            found.append(value)
    return found


class FlightLog:
    def __init__(self):
        self.records: deque = deque(maxlen=KEPT)
        self.started = datetime.now()
        self.journal: Path | None = None  # the file on disk, once `keep_journal` has been called
        self._file = None
        # Records come from the session's thread, the trace thread and the radio threads at once: one lock keeps
        # each line whole on disk, in the order it was kept in memory, and none lost.
        self._lock = threading.RLock()
        self._secrets: re.Pattern | None = None
        self._secrets_at: float | None = None

    def keep_journal(self, folder: Path) -> Path | None:
        """Write every record to a file in `folder` from now on, starting with what is already held."""
        try:
            config.private_folder(folder)
            for old in sorted(folder.glob("session-*.jsonl"))[: -(JOURNALS_KEPT - 1) or None]:
                old.unlink(missing_ok=True)
            with self._lock:  # what is already held goes first, before any record made meanwhile
                self.journal = folder / JOURNAL_NAME.format(self.started)
                self._file = open(self.journal, "a", encoding="utf-8")  # noqa: SIM115  (kept open for the whole run)
                config.private(self.journal)
                for entry in list(self.records):
                    self._write(entry)
        except OSError:
            self.journal = self._file = None  # no journal is not a reason to stop
        return self.journal

    def _write(self, entry: dict) -> None:
        line = json.dumps(entry, default=str) + "\n"
        with self._lock:
            if self._file is None:
                return
            try:
                self._file.write(line)
                self._file.flush()
            except (OSError, ValueError):
                self._file = None

    def entries(self) -> list[dict]:
        """The records kept in memory as they are now; other threads may add to them meanwhile."""
        with self._lock:
            return list(self.records)

    def _hidden(self, value):
        """`value` with every known secret in it replaced: in text, in keys, at any depth."""
        now = time.monotonic()
        if self._secrets_at is None or now - self._secrets_at >= SECRETS_RECHECK_S:
            values = sorted(known_secrets(), key=len, reverse=True)
            self._secrets = re.compile("|".join(map(re.escape, values)), re.IGNORECASE) if values else None
            self._secrets_at = now
        return _without(value, self._secrets) if self._secrets else value

    def note(self, kind: str, context: dict | None = None, **fields) -> None:
        """A record that is not a radio event: the session's start, the plan, the trace. Journal only."""
        entry = {"t": round(time.time(), 2), "type": kind, **fields}
        if context:
            entry["context"] = context
        self._write(self._hidden(entry))

    def record(self, event: dict, context: dict | None = None) -> None:
        kind = event.get("type")
        if kind in SKIPPED:
            return
        entry = {"t": round(time.time(), 2), **event}
        if context and kind in WITH_CONTEXT:
            entry["context"] = context
        entry = self._hidden(entry)
        with self._lock:  # in memory and on disk in the same order
            self.records.append(entry)
            self._write(entry)

    def text(self) -> str:
        """The conversation as a readable transcript."""
        lines = [f"Pro ATC flight log, started {self.started:%Y-%m-%d %H:%M}", ""]
        for entry in self.entries():
            stamp = datetime.fromtimestamp(entry["t"]).strftime("%H:%M:%S")
            kind = entry["type"]
            where = entry.get("context", {})
            tuned = f" [{where['tuned']}]" if where.get("tuned") else ""
            if kind == "pilot":
                lines.append(f"{stamp}{tuned} PILOT ({entry.get('source', 'voice')}): {entry['text']}")
            elif kind == "understood":
                lines.append(f"{stamp}          understood as: {entry['intent']}")
            elif kind == "atc":
                unprompted = " (unprompted)" if entry.get("unprompted") else ""
                lines.append(f"{stamp}{tuned} {entry['station'].upper()}{unprompted}: {entry['text']}")
            elif kind == "atis":
                lines.append(f"{stamp} ATIS {entry['airport']} {entry['mhz']}: {entry['text']}")
            elif kind == "silent":
                lines.append(f"{stamp}          ({entry.get('text') or 'readback correct, controller silent'})")
            elif kind == "coach":
                lines.append(f"{stamp} INSTRUCTOR: {entry['text']}")
            elif kind in ("info", "error"):
                lines.append(f"{stamp} {kind.upper()}: {entry['text']}")
        return "\n".join(lines) + "\n"

    def save(self, folder: Path, extra: dict | None = None) -> Path:
        """Write the transcript (.txt) and the full record (.json). Returns the transcript's path."""
        config.private_folder(folder)
        stem = folder / f"flight-{datetime.now():%Y%m%d-%H%M%S}"
        extra = dict(extra or {})
        text = self._hidden(self.text() + extra.pop("debrief_text", ""))
        stem.with_suffix(".txt").write_text(text, encoding="utf-8")
        payload = self._hidden({"started": self.started.isoformat(timespec="seconds"), **extra, "events": self.entries()})
        stem.with_suffix(".json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        for suffix in (".txt", ".json"):
            config.private(stem.with_suffix(suffix))
        return stem.with_suffix(".txt")


def _without(value, secrets: re.Pattern):
    if isinstance(value, str):
        return secrets.sub(HIDDEN, value)
    if isinstance(value, dict):
        return {_without(key, secrets) if isinstance(key, str) else key: _without(item, secrets) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_without(item, secrets) for item in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return secrets.sub(HIDDEN, str(value))  # as it would be written down
