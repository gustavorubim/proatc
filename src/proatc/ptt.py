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
    def __init__(self, key_name: str, on_down=None):
        self.key = _parse_key(key_name)
        self.on_down = on_down  # called once each time transmission starts
        self.pressed = threading.Event()
        self.released = threading.Event()
        self.released.set()
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
        if not self.pressed.is_set():
            self.released.clear()
            self.pressed.set()
            if self.on_down:
                self.on_down()

    def release(self) -> None:
        self.pressed.clear()
        self.released.set()

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()

    def listening(self) -> bool:
        """Whether the keyboard listener is running. It stops by itself where it cannot work."""
        return self._listener.is_alive()
