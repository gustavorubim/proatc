"""A record of the flight's radio work, for bug reports and debriefing.

Every event the session emits is kept with the time and, for transmissions, where the
aircraft was and who it was talking to. Nothing secret is recorded: no key, no audio.

It is kept twice. In memory, for "Save log" and the debrief. And on disk as it happens (the
journal): one file for each time the app runs, a line for each event, written at once so that a
crash or a closed window loses nothing. The journal also holds a trace: every twenty seconds,
where the aircraft is and who answers on the tuned frequency. A fault can then be looked into
after the flight without anyone having thought to save a log. The files stay on this computer;
the oldest are deleted.
"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime
from pathlib import Path

KEPT = 5000
JOURNALS_KEPT = 40  # runs of the app whose journal is kept
JOURNAL_NAME = "session-{:%Y%m%d-%H%M%S}.jsonl"
WITH_CONTEXT = ("pilot", "atc", "atis", "understood", "silent", "info", "error", "coach")
SKIPPED = ("tx", "timings", "status", "settings", "log_saved", "chatter", "debrief", "capture", "phrase")


class FlightLog:
    def __init__(self):
        self.records: deque = deque(maxlen=KEPT)
        self.started = datetime.now()
        self.journal: Path | None = None  # the file on disk, once `keep_journal` has been called
        self._file = None

    def keep_journal(self, folder: Path) -> Path | None:
        """Write every record to a file in `folder` from now on, starting with what is already held."""
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for old in sorted(folder.glob("session-*.jsonl"))[: -(JOURNALS_KEPT - 1) or None]:
                old.unlink(missing_ok=True)
            self.journal = folder / JOURNAL_NAME.format(self.started)
            self._file = open(self.journal, "a", encoding="utf-8")  # noqa: SIM115  (kept open for the whole run)
            for entry in list(self.records):
                self._write(entry)
        except OSError:
            self.journal = self._file = None  # no journal is not a reason to stop
        return self.journal

    def _write(self, entry: dict) -> None:
        if self._file is None:
            return
        try:
            self._file.write(json.dumps(entry, default=str) + "\n")
            self._file.flush()
        except (OSError, ValueError):
            self._file = None

    def note(self, kind: str, context: dict | None = None, **fields) -> None:
        """A record that is not a radio event: the session's start, the plan, the trace. Journal only."""
        entry = {"t": round(time.time(), 2), "type": kind, **fields}
        if context:
            entry["context"] = context
        self._write(entry)

    def record(self, event: dict, context: dict | None = None) -> None:
        kind = event.get("type")
        if kind in SKIPPED:
            return
        entry = {"t": round(time.time(), 2), **event}
        if context and kind in WITH_CONTEXT:
            entry["context"] = context
        self.records.append(entry)
        self._write(entry)

    def text(self) -> str:
        """The conversation as a readable transcript."""
        lines = [f"Pro ATC flight log, started {self.started:%Y-%m-%d %H:%M}", ""]
        for entry in self.records:
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
        folder.mkdir(parents=True, exist_ok=True)
        stem = folder / f"flight-{datetime.now():%Y%m%d-%H%M%S}"
        extra = dict(extra or {})
        stem.with_suffix(".txt").write_text(self.text() + extra.pop("debrief_text", ""), encoding="utf-8")
        payload = {"started": self.started.isoformat(timespec="seconds"), **(extra or {}), "events": list(self.records)}
        stem.with_suffix(".json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        return stem.with_suffix(".txt")
