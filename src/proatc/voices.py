"""A voice for each controller.

Every station keeps the same voice for the whole session and from one session to the next:
it is picked from the station's name, not at random. Where the installed voices allow it the
accent follows the country: British voices in the United Kingdom and Ireland, American ones
in the United States and Canada, any of them elsewhere.
"""

from __future__ import annotations

import zlib

BRITISH = ("EG", "EI", "LX", "FA", "VH", "WS", "WM")  # prefixes where a British voice fits best
SPEEDS = (-0.05, 0.0, 0.0, 0.05)  # small differences in pace, relative to the speed in the settings


def accent(ident: str | None) -> str | None:
    """'b' (British), 'a' (American) or None (no preference) for an airport or centre identifier."""
    ident = (ident or "").upper()
    if ident[:2] in BRITISH or ident[:1] in ("Y", "N"):  # Australia and New Zealand: nearer British than American
        return "b"
    if ident[:1] in ("K", "C", "P") or ident[:2] in ("TJ", "MM"):
        return "a"
    return None


def language(voice: str) -> str:
    """The pronunciation Kokoro should use for a voice: British voices speak British English."""
    return "en-gb" if voice[:1] == "b" else "en-us"


def voice_for(key: str, ident: str | None, voices: list[str], exclude: tuple[str, ...] = ()) -> tuple[str, float] | None:
    """(voice, speed offset) for the station with this key, or None when there is nothing to choose from."""
    pool = sorted(voice for voice in voices if voice not in exclude)
    wanted = accent(ident)
    regional = [voice for voice in pool if voice[:1] == wanted]
    pool = regional or pool
    if not pool:
        return None
    number = zlib.crc32(key.encode("utf-8"))
    return pool[number % len(pool)], SPEEDS[(number >> 8) % len(SPEEDS)]
