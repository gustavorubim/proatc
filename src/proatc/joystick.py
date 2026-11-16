"""Push-to-talk on a joystick or yoke button.

Buttons are read with pygame (SDL) on a background thread. X-Plane keeps reading the same
device: both programs can listen to a joystick at once.
"""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")  # no window: only the joystick subsystem is used
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")  # the sim has the focus, not us
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")


class JoystickButtons(threading.Thread):
    """Calls `on_change(pressed)` for one bound button, and can report the next button pressed."""

    def __init__(self, on_change, device: str | None = None, button: int | None = None):
        super().__init__(name="joystick", daemon=True)
        self.on_change = on_change
        self.device, self.button = device, button
        self._halt = threading.Event()
        self._capture = None  # callback(device name, button) for the next press
        self._lock = threading.Lock()
        self.names: list[str] = []

    def bind(self, device: str | None, button: int | None) -> None:
        with self._lock:
            self.device, self.button = device, button

    def capture(self, callback) -> None:
        """Report the next button pressed on any joystick, once."""
        with self._lock:
            self._capture = callback

    def cancel_capture(self) -> None:
        with self._lock:
            self._capture = None

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        try:
            import pygame

            pygame.display.init()
            pygame.joystick.init()
        except Exception:
            return  # no joystick support on this machine: keyboard push-to-talk still works
        sticks: dict[str, object] = {}
        was_down = False
        previous: dict[tuple[str, int], bool] = {}
        last_scan = 0.0
        while not self._halt.wait(0.02):
            try:
                pygame.event.pump()
                if time.monotonic() - last_scan > 3:  # devices plugged in later are picked up
                    last_scan = time.monotonic()
                    sticks = {}
                    for index in range(pygame.joystick.get_count()):
                        stick = pygame.joystick.Joystick(index)
                        sticks.setdefault(stick.get_name(), stick)
                    self.names = list(sticks)
                with self._lock:
                    device, button, capture = self.device, self.button, self._capture
                if capture:
                    for name, stick in sticks.items():
                        for number in range(stick.get_numbuttons()):
                            down = bool(stick.get_button(number))
                            if down and not previous.get((name, number), False):
                                self.cancel_capture()
                                capture(name, number)
                            previous[(name, number)] = down
                    continue
                stick = sticks.get(device) if device else None
                down = bool(stick and button is not None and button < stick.get_numbuttons() and stick.get_button(button))
                if down != was_down:
                    was_down = down
                    self.on_change(down)
            except Exception:
                time.sleep(1)  # a device unplugged mid-read: try again on the next scan
                last_scan = 0.0
