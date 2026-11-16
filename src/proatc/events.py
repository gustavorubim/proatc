"""Events the engine emits. The terminal prints them; the desktop app receives them as JSON.

Every event is a dict with a "type":
  info       {text}                             a note for the user (nobody answers, mic silent, ...)
  status     {text}                             start-up progress
  tx         {on}                               the pilot is transmitting
  pilot      {text, source}                     what the pilot said; source is voice, typed or clicked
  understood {intent}                           how the transmission was interpreted
  atc        {station, text, unprompted}        a controller transmission
  silent     {}                                 correct readback, the controller says nothing
  timings    {listen, think, voice, total}      seconds
  atis       {airport, mhz, text}               a new ATIS recording started
  error      {text}
  coach      {text}                             the instructor: an explanation, or what to say next
  capture    {active}                           waiting for the key or button to use for push-to-talk
  log_saved  {path}                             the flight log was written to this file
  chatter    {station, callsign, said, answered} another aircraft heard on the frequency (sound only)
  debrief    {score, verdict, findings, ...}    the review of the flight's radio work (see debrief.py)
"""

from __future__ import annotations

from collections.abc import Callable

Emit = Callable[[dict], None]


def console(event: dict) -> None:
    kind = event.get("type")
    if kind in ("info", "error"):
        print(f"  ({event['text']})")
    elif kind == "status":
        print(event["text"])
    elif kind == "tx" and event["on"]:
        print("  [transmitting...]", end="\r", flush=True)
    elif kind == "pilot":
        print(f"PILOT: {event['text']}")
    elif kind == "understood":
        print(f"       (understood: {event['intent']})")
    elif kind == "atc":
        print(f"{event['station']}: {event['text']}")
    elif kind == "silent":
        print(f"       ({event.get('text') or 'readback correct, no reply needed'})")
    elif kind == "timings":
        print(
            f"       [listen {event['listen']:.2f}s | think {event['think']:.2f}s | "
            f"voice {event['voice']:.2f}s | total {event['total']:.2f}s]"
        )
    elif kind == "coach":
        print(f"INSTRUCTOR: {event['text']}")
    elif kind == "chatter":
        print(f"       ~ {event['said']} / {event['answered']}")
    elif kind == "debrief":
        from .debrief import as_text

        print(as_text(event))
    elif kind == "log_saved":
        print(f"  (flight log saved to {event['path']})")
    elif kind == "atis":
        print(f"ATIS  ({event['airport']} {event['mhz']}): {event['text']}")
