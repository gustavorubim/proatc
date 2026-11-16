"""A review of the flight's radio work, from the flight log.

Everything here is counted from what was actually said: the corrections a controller made,
the instructions that had to be repeated, the levels that were bust. The score is a rough
guide, not an examination.
"""

from __future__ import annotations

from datetime import datetime

# (what to look for in a controller transmission, what it means, points lost, kind)
FINDINGS = (
    (", negative, ", "Readback corrected by the controller", 6, "readback"),
    ("I say again, ", "Instruction not read back, and repeated", 4, "missed"),
    ("check your level", "Level bust: you went through your cleared level", 15, "level"),
    ("check your altitude", "Altitude bust: you went through your cleared altitude", 15, "level"),
    ("low altitude alert", "Low altitude alert", 20, "terrain"),
    ("you were to hold short", "Runway entered without a crossing clearance", 25, "runway"),
    ("you were to be level", "Arrival restriction missed", 6, "restriction"),
    ("you were to cross", "Departure crossing restriction missed", 6, "restriction"),
    ("go around, I say again", "Sent around by the tower", 0, "go_around"),
)
HEADING_PROMPT = "fly heading"  # unprompted, with nothing else: the assigned heading was not being flown
DEAD_CALL = "nobody answers"


def _clock(stamp: float) -> str:
    return datetime.fromtimestamp(stamp).strftime("%H:%M:%S")


def review(records) -> dict:
    """Count what happened on the radio. `records` are FlightLog entries."""
    records = list(records)
    pilot = [r for r in records if r.get("type") == "pilot"]
    spoken = [r for r in records if r.get("type") == "atc"]
    findings: list[dict] = []
    lost = 0
    for entry in spoken:
        text = entry.get("text", "")
        for needle, meaning, points, kind in FINDINGS:
            if needle in text:
                findings.append({"time": _clock(entry["t"]), "kind": kind, "text": meaning, "said": text})
                lost += points
                break
        else:
            body = text.split(", ", 1)[1] if ", " in text else text
            if entry.get("unprompted") and body.startswith(HEADING_PROMPT) and "," not in body:
                findings.append(
                    {"time": _clock(entry["t"]), "kind": "heading", "text": "Assigned heading not flown", "said": text}
                )
                lost += 5
    dead = [r for r in records if r.get("type") == "info" and DEAD_CALL in r.get("text", "")]
    for entry in dead:
        findings.append(
            {"time": _clock(entry["t"]), "kind": "frequency", "text": "Call on a frequency with no station", "said": ""}
        )
        lost += 2
    readbacks = sum(1 for r in records if r.get("type") == "understood" and r.get("intent") == "readback")
    wrong = sum(1 for f in findings if f["kind"] == "readback")
    missed = sum(1 for f in findings if f["kind"] == "missed")
    say_again = sum(1 for r in records if r.get("type") == "understood" and r.get("intent") == "say_again")
    sources: dict[str, int] = {}
    for entry in pilot:
        source = entry.get("source", "voice")
        sources[source] = sources.get(source, 0) + 1
    stations = list(dict.fromkeys(entry.get("station", "") for entry in spoken if entry.get("station")))
    stamps = [r["t"] for r in records if "t" in r and r.get("type") in ("pilot", "atc")]
    minutes = round((max(stamps) - min(stamps)) / 60) if len(stamps) > 1 else 0
    score = max(0, 100 - lost) if pilot else None
    return {
        "minutes": minutes,
        "transmissions": len(pilot),
        "by_source": sources,
        "controller_calls": len(spoken),
        "stations": stations,
        "readbacks": {"given": readbacks, "corrected": wrong, "missed": missed},
        "say_again": say_again,
        "findings": sorted(findings, key=lambda f: f["time"]),
        "score": score,
        "verdict": verdict(score, findings),
    }


def verdict(score: int | None, findings: list[dict]) -> str:
    if score is None:
        return "Nothing was said on the radio yet."
    if not findings:
        return "Clean radio work: nothing had to be corrected or repeated."
    worst = {f["kind"] for f in findings}
    if worst & {"runway", "terrain", "level"}:
        return "There was at least one serious event. Go through the list below before the next flight."
    if score >= 85:
        return "Good radio work, with a few things to tidy up."
    return "Several instructions had to be corrected or repeated. Slow down and read back in full."


def as_text(result: dict) -> str:
    """The debrief as plain text, for the saved flight log."""
    lines = ["Debrief", f"  {result['verdict']}"]
    if result["score"] is not None:
        lines.append(f"  Score: {result['score']} of 100")
    lines.append(
        f"  {result['transmissions']} transmissions in {result['minutes']} minutes, to {len(result['stations'])} stations. "
        f"Readbacks: {result['readbacks']['given']} given, {result['readbacks']['corrected']} corrected, "
        f"{result['readbacks']['missed']} missed."
    )
    for finding in result["findings"]:
        lines.append(f"  {finding['time']}  {finding['text']}")
    return "\n".join(lines) + "\n"
