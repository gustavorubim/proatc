"""A record of the flight's radio work, saved on request for bug reports and debriefing.

Every event the session emits is kept with the time and, for transmissions, where the
aircraft was and who it was talking to. Nothing secret is recorded: no key, no audio.
"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime
from pathlib import Path

KEPT = 5000
WITH_CONTEXT = ("pilot", "atc", "atis", "understood", "silent", "info", "error", "coach")
SKIPPED = ("tx", "timings", "status", "settings", "log_saved", "chatter", "debrief", "capture", "phrase")


class FlightLog:
    def __init__(self):
        self.records: deque = deque(maxlen=KEPT)
        self.started = datetime.now()

    def record(self, event: dict, context: dict | None = None) -> None:
        kind = event.get("type")
        if kind in SKIPPED:
            return
        entry = {"t": round(time.time(), 2), **event}
        if context and kind in WITH_CONTEXT:
            entry["context"] = context
        self.records.append(entry)

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
