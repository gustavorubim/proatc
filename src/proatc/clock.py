"""Time as the radio's threads see it.

The session, the ATIS broadcaster and the second radio wait, sleep and look at the time only through a
`Clock`. In the app it is the real one. The timing tests (tests/sim.py) pass a simulated one, so that the
threads can be run together, in simulated seconds, and give the same result on every run.
"""

from __future__ import annotations

import threading
import time


class Clock:
    """Real time."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def wait(self, event: threading.Event, timeout: float | None = None) -> bool:
        """Block until the event is set or the timeout runs out. True if it was set."""
        return event.wait(timeout)


REAL = Clock()
