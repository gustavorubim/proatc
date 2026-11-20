"""Global push-to-talk key. Works while X-Plane has focus."""

from __future__ import annotations

import threading

from pynput import keyboard


def _parse_key(name: str):
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)
    try:
        return getattr(keyboard.Key, name)
    except AttributeError:
        raise ValueError(f"Unknown push-to-talk key '{name}'. Use a pynput key name such as ctrl_r or f13.") from None


class PushToTalk:
    def __init__(self, key_name: str, on_down=None, on_up=None):
        self.key = _parse_key(key_name)
        self.on_down = on_down  # called once each time transmission starts
        self.on_up = on_up  # called once each time it ends: the key let go, a window gone, or too long
        self.pressed = threading.Event()
        self.released = threading.Event()
        self.released.set()
        # A press, a release, and the microphone opened late for a press, one at a time: a key let go cannot be
        # followed by the microphone opening for it.
        self.lock = threading.RLock()
        self.presses = 0  # which press this is: a job for an earlier one does not open the microphone
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)

    def set_key(self, key_name: str) -> None:
        self.key = _parse_key(key_name)

    def _on_press(self, key) -> None:
        if key == self.key:
            self.press()

    def _on_release(self, key) -> None:
        if key == self.key:
            self.release()

    def press(self) -> None:
        """Start transmitting. Also called by the desktop app's hold-to-talk button."""
        with self.lock:
            if not self.pressed.is_set():
                self.presses += 1
                self.released.clear()
                self.pressed.set()
                if self.on_down:
                    self.on_down()

    def release(self) -> None:
        with self.lock:
            was_down = self.pressed.is_set()
            self.pressed.clear()
            self.released.set()
            if was_down and self.on_up:
                self.on_up()

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()

    def listening(self) -> bool:
        """Whether the keyboard listener is running. It stops by itself where it cannot work."""
        return self._listener.is_alive()
