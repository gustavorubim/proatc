"""What can be checked about this computer without the person: "This computer" in the settings.

Mac and Linux testers meet problems a Windows user never sees: macOS will not let a program hear keys
pressed in another program until it is allowed under Input Monitoring, and under Wayland on Linux it
cannot at all. Each item is a plain sentence and, when it failed, what to do about it. The list is
shown in the settings, told at start-up on macOS and Linux when something failed, and put in a
problem report.

Written from Apple's and the desktops' documentation; the macOS and Linux branches are tested only
with stand-ins for those systems (see tests/test_report.py).
"""

from __future__ import annotations

import os
import sys

# IOHIDCheckAccess (IOKit, macOS 10.15 and newer): may this program listen to keyboard events?
IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
LISTEN_EVENT = 1  # kIOHIDRequestTypeListenEvent
ACCESS = {0: "granted", 1: "denied", 2: "unknown"}  # kIOHIDAccessTypeGranted, Denied, Unknown

INPUT_MONITORING_FIX = (
    "Open System Settings, Privacy & Security, Input Monitoring, and switch Pro ATC on (also under Accessibility "
    "if it is listed there). Then quit and reopen Pro ATC. Until then, use the Hold to talk button or a joystick button."
)
WAYLAND_FIX = (
    "Use the Hold to talk button in this window, or set a joystick button under Push-to-talk. Or log in to an X11 "
    "session (on Ubuntu: the gear at the login screen, “Ubuntu on Xorg”)."
)


def item(key: str, label: str, ok: bool | None, text: str, fix: str = "") -> dict:
    """ok: True passed, False failed, None could not be told."""
    return {"id": key, "label": label, "ok": ok, "text": text, "fix": "" if ok else fix}


def input_monitoring() -> str:
    """macOS: "granted", "denied" or "unknown" (not asked yet, or a macOS too old to say)."""
    import ctypes

    try:
        iokit = ctypes.cdll.LoadLibrary(IOKIT)
        check = iokit.IOHIDCheckAccess
    except (OSError, AttributeError):
        return "unknown"
    check.restype = ctypes.c_uint
    check.argtypes = [ctypes.c_uint]
    return ACCESS.get(int(check(LISTEN_EVENT)), "unknown")


def on_wayland(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(env.get("WAYLAND_DISPLAY"))


def keyboard(listening: bool | None, system: str | None = None, env=None, access=input_monitoring) -> dict:
    """Whether the push-to-talk key can be heard while X-Plane has the focus.

    `listening` is whether the app's key listener is running (None when there is none, as from the terminal).
    A key press cannot be made up to test it: it would go to whatever window has the focus, X-Plane included."""
    system = system or sys.platform
    env = os.environ if env is None else env
    label = "Push-to-talk key"
    if listening is False:
        return item("keyboard", label, False, "The keyboard listener did not start, so the push-to-talk key will not work.",
                    "Use the Hold to talk button or a joystick button, and send a problem report.")  # fmt: skip
    if system == "darwin":
        state = access()
        if state == "granted":
            return item("keyboard", label, True, "macOS lets Pro ATC hear the push-to-talk key in other programs.")
        if state == "denied":
            return item("keyboard", label, False, "macOS does not let Pro ATC hear keys pressed in other programs, "
                        "so the push-to-talk key does nothing while X-Plane has the focus.", INPUT_MONITORING_FIX)  # fmt: skip
        return item("keyboard", label, None, "macOS has not said yet whether Pro ATC may hear keys pressed in other "
                    "programs. If the push-to-talk key does nothing, it may not.", INPUT_MONITORING_FIX)  # fmt: skip
    if system.startswith("linux"):
        if on_wayland(env):
            return item("keyboard", label, False, "This is a Wayland session: a program cannot see keys pressed in "
                        "another window, so the push-to-talk key cannot work while X-Plane has the focus.", WAYLAND_FIX)  # fmt: skip
        if not env.get("DISPLAY"):
            return item("keyboard", label, False, "No X11 display was found, so the push-to-talk key cannot be heard.",
                        WAYLAND_FIX)  # fmt: skip
        return item("keyboard", label, True, "An X11 session: the push-to-talk key works while X-Plane has the focus.")
    return item("keyboard", label, True, "The push-to-talk key works while X-Plane has the focus.")


def sound_library_fix(system: str) -> str:
    if system.startswith("linux"):
        return "Install PortAudio (sudo apt install libportaudio2, or sudo dnf install portaudio), then restart Pro ATC."
    return "Reinstall Pro ATC: the sound library comes with it."


def sound(system: str | None = None, load=None) -> list[dict]:
    """The sound library, and whether there is something to play on and something to record from."""
    system = system or sys.platform
    try:
        sd = load() if load else __import__("sounddevice")
        devices = list(sd.query_devices())
    except Exception as error:  # sounddevice raises OSError when PortAudio itself is missing
        why = f"{type(error).__name__}: {error}"
        return [
            item("sound_library", "Sound library", False, f"The sound library (PortAudio) did not load ({why}).",
                 sound_library_fix(system)),
            item("speakers", "Speakers or headset", None, "Not known: the sound library did not load."),
            item("microphone", "Microphone", None, "Not known: the sound library did not load."),
        ]  # fmt: skip
    outputs = [device["name"] for device in devices if device.get("max_output_channels", 0) > 0]
    inputs = [device["name"] for device in devices if device.get("max_input_channels", 0) > 0]

    def default(kind: str, names: list[str]) -> str:
        # Each device is listed once per Windows sound system: name the one used when none is chosen.
        try:
            return str(sd.query_devices(kind=kind)["name"])
        except Exception:
            return names[0]

    mic_fix = "Connect a microphone or headset, and check it is not switched off in the system's sound settings."
    if system == "darwin":
        mic_fix += " Allow Pro ATC under System Settings, Privacy & Security, Microphone."
    return [
        item("sound_library", "Sound library", True, "The sound library (PortAudio) loaded."),
        item("speakers", "Speakers or headset", bool(outputs),
             f"Found. The default is {default('output', outputs)}." if outputs else "No speakers or headset found.",
             "Connect speakers or a headset, then restart Pro ATC."),
        item("microphone", "Microphone", bool(inputs),
             f"Found. The default is {default('input', inputs)}." if inputs else "No microphone found.",
             mic_fix),
    ]  # fmt: skip


def this_computer(listening: bool | None = None, system: str | None = None, env=None, access=input_monitoring,
                  load=None) -> list[dict]:  # fmt: skip
    """Every check, in the order the settings show them."""
    return [*sound(system, load), keyboard(listening, system, env, access)]


def startup_notes(checks: list[dict], system: str | None = None) -> list[str]:
    """What to tell a Mac or Linux user at start-up: each failed check, with its fix. Nothing on Windows,
    where these have been met in flight."""
    system = system or sys.platform
    if not (system == "darwin" or system.startswith("linux")):
        return []
    return [f"This computer: {check['text']} {check['fix']}".strip() for check in checks if check["ok"] is False]
