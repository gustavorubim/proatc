# Installing Pro ATC

Download the file for your system from the [latest release](https://github.com/gustavorubim/proatc/releases/latest). The app is not code-signed (a certificate costs money this free project does not have), so each system warns you the first time.

On every system, the first start downloads Python, the speech libraries and the speech models: several minutes and about 3 GB of disk.

## Windows 10 or 11

1. Run `ProATC-<version>-windows-x64-setup.exe`.
2. SmartScreen says "Windows protected your PC": choose "More info", then "Run anyway".

The app updates itself: "Restart to update" appears in the header when a new version is ready.

With an NVIDIA graphics card, speech recognition runs on it and takes about 0.3 seconds a call.

## macOS (Apple Silicon: M1 or newer, macOS 12 or newer)

1. Open `ProATC-<version>-mac-arm64.dmg` and drag Pro ATC to Applications.
2. The first time, macOS refuses to open it because the developer is not identified. Open System Settings, Privacy & Security, scroll down, and click "Open Anyway" next to Pro ATC. On older versions of macOS, right-click the app and choose Open.
3. Allow the microphone when asked.
4. For the push-to-talk key to work while X-Plane has the focus, allow Pro ATC under System Settings, Privacy & Security, Input Monitoring (and Accessibility if asked). Without it, use the on-screen button or a joystick button.

Updates are not installed automatically on a Mac: when a new version is out, the header shows "Get v…", which opens the download page.

Speech recognition runs on the processor: expect a second or two a call. Intel Macs are not supported yet.

## Linux (x64)

1. Install PortAudio, which the app needs for the microphone and speakers: `sudo apt install libportaudio2` on Debian and Ubuntu, `sudo dnf install portaudio` on Fedora.
2. Make the download executable and run it: `chmod +x ProATC-<version>-linux-x86_64.AppImage`, then `./ProATC-<version>-linux-x86_64.AppImage`. An AppImage needs FUSE (`libfuse2`) on some distributions.
3. The keyboard push-to-talk key works under X11. Under Wayland a program cannot see keys pressed in another window: use the on-screen button or a joystick button, or run an X11 session.

The AppImage updates itself. Speech recognition runs on the processor.

## All systems

In X-Plane, Settings > Network must allow incoming traffic: the app reads the sim through X-Plane's Web API on port 8086. If X-Plane was not installed with its installer and the app cannot find it, set `xplane_path` under `[sim]` in `config.toml` in the app's data folder.
