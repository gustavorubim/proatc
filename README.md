# Pro ATC: free AI voice ATC for X-Plane 12

Pro ATC is a free, open-source air traffic control app for the X-Plane 12 flight simulator. You talk to controllers by voice, from clearance to landing, IFR or VFR, at any airport in the sim. Speech recognition and the controller voices run on your own computer.

[![Latest release](https://img.shields.io/github/v/release/gustavorubim/proatc)](https://github.com/gustavorubim/proatc/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/gustavorubim/proatc/total)](https://github.com/gustavorubim/proatc/releases)
[![CI](https://github.com/gustavorubim/proatc/actions/workflows/ci.yml/badge.svg)](https://github.com/gustavorubim/proatc/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

**[Download for Windows](https://github.com/gustavorubim/proatc/releases/latest)** · [Report a problem](https://github.com/gustavorubim/proatc/issues/new/choose) · [How it works](docs/how-it-works.md)

![Pro ATC window: tuned frequency, flight strip, airport frequencies and the calls you can make](docs/screenshot.png)

## Why use it

- **Free.** No subscription. The one outside service is a language model that understands what you said: use your own [OpenRouter](https://openrouter.ai) key (a flight costs about a cent) or a local model (Ollama, LM Studio) for nothing.
- **Nothing is invented.** Replies are built by a rules engine from X-Plane's own data: real frequencies, taxiways, SIDs, STARs, approaches, sectors, airspace and weather.
- **No plugin.** It reads the sim through X-Plane 12's built-in Web API, and uses Navigraph data if you have it.
- **Speak, click or type.** Every call you can make right now is also a button.

## Install

1. Download the installer from the [latest release](https://github.com/gustavorubim/proatc/releases/latest) and run it. It is not code-signed, so Windows SmartScreen warns: choose "More info", then "Run anyway".
2. Start Pro ATC. The first launch downloads the speech software and models (several minutes, about 3 GB).
3. Click "Add OpenRouter key" and paste a key from [openrouter.ai/keys](https://openrouter.ai/keys).
4. In Settings, choose your microphone and push-to-talk key or joystick button (default: right Ctrl).
5. Start X-Plane 12 and load a flight. In X-Plane, Settings > Network must allow incoming traffic.

Requirements: Windows 10 or 11 (64-bit), X-Plane 12.1.1 or newer. An NVIDIA graphics card makes speech recognition fast; without one it is a few seconds slower per call. The app updates itself.

## First flight

1. Load your SimBrief plan under Flight plan (or enter a destination by hand).
2. Click a frequency in the airport list to tune it. Start with the ATIS, then delivery.
3. Click "Request IFR clearance", or hold push-to-talk and say it. "Read back" reads the clearance back for you.
4. Continue through push-back, taxi, takeoff, climb, cruise, approach and landing. After each hand-off, "Tune" sets the next frequency.

For VFR, tune the tower and click "Request VFR departure". "What do I say?" tells you the next call at any time.

## Features

**IFR:** clearance with the right SID, taxi over real taxiways with runway crossings, step climbs, hand-offs at real sector boundaries, descent with the STAR and its restrictions, radar vectors with speed control, holds, ILS, RNAV, visual and circling approaches, go-around, missed approach and diversion.

**VFR:** towers, information services (AFIS) and airfields with no controller (CTAF, UNICOM); circuit joins, touch and go, zone transits through real control zones, special VFR, flight following and the UK's Basic and Traffic Service, airspace warnings.

**Realism:** readbacks checked and corrected, level busts and wrong headings called, ATIS from real weather, a voice for each controller, radio chatter, traffic calls against X-Plane's AI aircraft, mayday and pan-pan.

**Tools:** copilot that reads back and tunes for you, instructor, post-flight debrief, flight log, cost counter, joystick push-to-talk, phone or tablet view, automatic updates.

## Questions

- **What does it cost?** The app is free. With OpenRouter, a spoken call costs a fraction of a cent and clicked calls cost nothing. With a local model everything is free.
- **Nobody answers.** The tuned frequency must belong to a station in range (tower 30 NM, approach 60 NM). The "Tuned" card shows who is listening.
- **"X-Plane not connected".** Load a flight, and allow incoming traffic in X-Plane's network settings (the Web API uses port 8086).
- **Does it work with Navigraph, SimBrief, ToLiss, Zibo?** Navigraph and SimBrief: yes. Any aircraft works; a few add-ons manage their own radios, so tune those in the cockpit.
- **Mac, Linux, MSFS?** Windows and X-Plane 12 only for now.

## Known limits

Other aircraft are seen but not controlled. There is no window inside the sim, so it is awkward in VR. English only. No datalink (CPDLC) or oceanic procedures. Named VFR reporting points are not known.

## Help test it

If a controller says something wrong, click **Save log** in the app and attach the two files to a [new issue](https://github.com/gustavorubim/proatc/issues/new/choose). They hold the conversation and the aircraft's position, never your key or audio.

## More

- [How it works](docs/how-it-works.md): everything the controllers do, in detail.
- [Development](docs/development.md): building from source, tests, releases, code layout.

Speech recognition is [faster-whisper](https://github.com/SYSTRAN/faster-whisper); the voices are [Kokoro](https://github.com/thewh1teagle/kokoro-onnx). Pro ATC is not affiliated with Laminar Research, Navigraph, SimBrief or OpenRouter, and is for flight simulation only.

## Licence

[MIT](LICENSE).
