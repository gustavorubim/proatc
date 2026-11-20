"""The caches of parsed navigation data (airspace, airways, centres, fixes and holds), kept in the data folder
so that X-Plane's large files are parsed once.

A pickle can name any function to call while it is read back, so a cache file someone else wrote could run
code. A cache is therefore read with an unpickler that rebuilds only the classes that cache holds, as the Python
documentation recommends ("Restricting Globals"); anything else makes it unreadable, and it is parsed again.
"""

from __future__ import annotations

import io
import pickle
from pathlib import Path

from .config import private


class _OnlyThese(pickle.Unpickler):
    def __init__(self, data: bytes, allowed: tuple[type, ...]):
        super().__init__(io.BytesIO(data))
        self.allowed = {(kind.__module__, kind.__qualname__): kind for kind in allowed}

    def find_class(self, module: str, name: str):
        kind = self.allowed.get((module, name))
        if kind is None:
            raise pickle.UnpicklingError(f"{module}.{name} is not part of this cache")
        return kind


def load(path: Path, stamp: str, *allowed: type):
    """What was saved under `stamp`, or None: no cache, another stamp (the source file changed), or a file that
    holds anything but `allowed` classes and plain data."""
    try:
        saved_stamp, value = _OnlyThese(path.read_bytes(), allowed).load()
    except Exception:
        return None  # missing, unreadable, from another version, or not ours: parsed again
    return value if saved_stamp == stamp else None


def save(path: Path, stamp: str, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps((stamp, value)))
    private(path)
