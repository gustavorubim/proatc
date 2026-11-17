# Installing Pro ATC

Download the file for your system from the [latest release](https://github.com/gustavorubim/proatc/releases/latest). The app is not code-signed (a certificate costs money this free project does not have), so each system warns you the first time.

On every system, the first start downloads Python, the speech libraries and the speech models: several minutes and about 3 GB of disk.

## Windows 10 or 11

1. Run `ProATC-<version>-windows-x64-setup.exe`.
2. SmartScreen says "Windows protected your PC": choose "More info", then "Run anyway".

The app updates itself: "Restart to update" appears in the header when a new version is ready.

With an NVIDIA graphics card, speech recognition runs on it and takes about 0.3 seconds a call.

### Check that it works

1. With X-Plane closed, or the aircraft parked on the ground, open Settings and click **Sound check**. It plays a controller, then two radios at once, then each voice, then records two seconds from the microphone, and asks after each step whether you heard it. The verdict is shown in the window and kept.
2. Under **This computer** at the bottom of Settings, every line should have a tick.
3. Something wrong? Click **Report a problem** in the header. It writes one file and opens its folder; attach it to an [issue](https://github.com/gustavorubim/proatc/issues/new).

## macOS (Apple Silicon: M1 or newer, macOS 12 or newer)

1. Open `ProATC-<version>-mac-arm64.dmg` and drag Pro ATC to Applications.
2. The first time, macOS refuses to open it because the developer is not identified. Open System Settings, Privacy & Security, scroll down, and click "Open Anyway" next to Pro ATC. On older versions of macOS, right-click the app and choose Open.
3. Allow the microphone when asked.
4. For the push-to-talk key to work while X-Plane has the focus, allow Pro ATC under System Settings, Privacy & Security, Input Monitoring (and Accessibility if asked). Without it, use the on-screen button or a joystick button.

Updates are not installed automatically on a Mac: when a new version is out, the header shows "Get v…", which opens the download page.

Speech recognition runs on the processor: expect a second or two a call. Intel Macs are not supported yet.

### Check that it works

1. Open Settings and look at **This computer** at the bottom. "Push-to-talk key" says whether macOS lets Pro ATC hear the key while X-Plane has the focus; if not, it says where to allow it (Input Monitoring). After allowing it, quit and reopen Pro ATC.
2. With X-Plane closed, or the aircraft parked on the ground, click **Sound check** in Settings and answer its questions. The microphone step shows the level it recorded: if it stays silent while you speak, macOS is not letting Pro ATC hear the microphone (System Settings, Privacy & Security, Microphone).
3. Click **Report a problem** in the header and attach the file it writes to an [issue](https://github.com/gustavorubim/proatc/issues/new), even when everything worked: nobody has flown the Mac version yet, and the sound check's result says how it went.

## Linux (x64)

1. Install PortAudio, which the app needs for the microphone and speakers: `sudo apt install libportaudio2` on Debian and Ubuntu, `sudo dnf install portaudio` on Fedora.
2. Make the download executable and run it: `chmod +x ProATC-<version>-linux-x86_64.AppImage`, then `./ProATC-<version>-linux-x86_64.AppImage`. An AppImage needs FUSE (`libfuse2`) on some distributions.
3. The keyboard push-to-talk key works under X11. Under Wayland a program cannot see keys pressed in another window: use the on-screen button or a joystick button, or run an X11 session.

The AppImage updates itself. Speech recognition runs on the processor.

### Check that it works

1. Open Settings and look at **This computer** at the bottom. It says whether the sound library (PortAudio) loaded, whether there are speakers and a microphone, and whether the push-to-talk key can work in this session (it cannot under Wayland).
2. With X-Plane closed, or the aircraft parked on the ground, click **Sound check** in Settings and answer its questions.
3. Click **Report a problem** in the header and attach the file it writes to an [issue](https://github.com/gustavorubim/proatc/issues/new), even when everything worked: nobody has flown the Linux version yet.

## All systems

The sound check never starts by itself, and refuses to run while the aircraft is moving or a controller is talking: it would talk over your flight. Its result goes into the app's journal and into the next problem report. From a source checkout the same tools are `uv run proatc sound-check` and `uv run proatc report` (add `--hide` to mask the callsign and positions).

In X-Plane, Settings > Network must allow incoming traffic: the app reads the sim through X-Plane's Web API on port 8086. If X-Plane was not installed with its installer and the app cannot find it, set `xplane_path` under `[sim]` in `config.toml` in the app's data folder.
