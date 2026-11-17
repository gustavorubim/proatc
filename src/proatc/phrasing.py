"""What the pilot wants, in their own words, turned into the call to make.

The calls that fit the flight right now come from the rules engine (`Engine.suggestions`): each is already
worded properly and carries its intent. The language model only picks the one that says what the pilot wants.
When none does, it words the request itself; that call is then heard by the controller like any typed one.

The controller's last transmission is given too: when it asked something, what the pilot types is most likely
the answer, and the answers the engine offers are listed first and marked. The facts in an answer (the procedure,
the waypoint, the level) come from those calls, never from the model.
"""

from __future__ import annotations

import json
import re

PHRASER = """You help a pilot in a flight simulator say what they want to air traffic control in standard radio phraseology.
You get what the pilot wants in their own words (in any language), the station they are tuned to, what that
controller last said to the pilot, and a numbered list of standard calls that fit the flight right now. Answer with
one JSON object and nothing else.

0. If the controller's last transmission asks the pilot something (to confirm, verify, report, advise, check, or
   "say intentions") and the pilot's words answer it or respond to it, even loosely ("yes", "no", "I am on it",
   "I lost the route", "what should I say"), the call is that answer: choose the listed call marked
   "(answers the controller)" that says what the pilot means. Never answer a question with facts that are not in
   the pilot's words or in the listed calls.

1. If one of the listed calls does what the pilot wants, even loosely, choose it: {"choice": <its number>}.
   A pilot who is lost on the ground wants progressive taxi; one who did not catch the last message wants
   "say again". If that call contains {destination}, also give "destination": the place the pilot named, or ""
   if they named none.
2. Otherwise word the request yourself: {"call": "<the request>"}. Anything a pilot can ask or tell a controller
   can be worded: a different runway or level, a delay ("request two minutes, will call ready"), the weather
   ("request current wind and Q N H"), returning to the stand, a problem. Word it as a pilot says it on the
   radio: brief, standard ICAO phraseology (FAA wording when the station is in the United States), in English,
   every number digit by digit in words ("flight level one two zero", "heading two seven zero", "runway zero
   niner left", "niner" for nine, "decimal" in a frequency). Do not include the station's name or the aircraft's
   callsign: they are added for you. Do not invent numbers the pilot did not give. An urgent problem starts with
   "PAN PAN, PAN PAN, PAN PAN"; a life-threatening one with "MAYDAY, MAYDAY, MAYDAY".
3. Only if the pilot's words have nothing to do with flying or air traffic control, or you cannot tell at all what
   they want: {"unclear": "<one short sentence, to the pilot, on what to add>"}."""

NO_MODEL = "Add an OpenRouter key (or a local model) and I can word any request. Until then, use the buttons above."
SMALL_WORDS = {"the", "and", "for", "you", "can", "want", "would", "like", "please", "request", "get", "have", "with", "that"}


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z]+", text.lower()) if len(word) > 2 and word not in SMALL_WORDS}


YES = {"yes", "affirm", "affirmative", "correct", "yeah", "able", "following"}
NO = {"no", "negative", "not", "unable", "lost"}


def closest(wanted: str, calls: list[dict]) -> dict | None:
    """Without a language model: the listed call whose label shares the most words with what was typed.
    A plain yes or no is the matching answer to the controller's question, when one is offered."""
    typed = set(re.findall(r"[a-z]+", wanted.lower()))
    answers = [call for call in calls if call.get("answers")]
    if answers and typed & (YES | NO):
        yes = bool(typed & YES) and not typed & NO
        found = next((call for call in answers if (call["intent"]["intent"] == "affirm") == yes), None)
        if found:
            return found
    asked = _words(wanted)
    best, score = None, 0
    for call in calls:
        shared = len(asked & _words(call["label"]))
        if shared > score:
            best, score = call, shared
    return best


def _answer(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def _tidy(call: str) -> str:
    call = re.sub(r"\s+", " ", str(call)).strip().strip("\"'“”.")
    return call[:1].lower() + call[1:] if call[:1].isupper() and not call[:2].isupper() else call


def _without_opening(call: str, opening: str) -> str:
    """The call without the station's name and the callsign, should the model have put them in anyway."""
    call = call.strip().strip("\"'“”")
    names = [part.strip().lower() for part in opening.split(",") if part.strip()]
    trimmed = True
    while trimmed:
        trimmed = False
        for name in names:
            if call.lower().startswith(name) and call[len(name) : len(name) + 1] in (",", " ", ""):
                call, trimmed = call[len(name) :].lstrip(", "), True
    return call


def phrase(wanted: str, station: str, opening: str, calls: list[dict], complete=None, last_said: str = "") -> dict:
    """{"text": the call, "intent": its intent or None} or {"note": why there is none}.

    `calls` are the engine's suggestions, the answers to the controller's question first; `opening` is how a
    call to this station begins ("Luton Ground, Golf Alfa Bravo, "); `complete(system, user)` asks the language
    model, None when there is none; `last_said` is what this controller last said to the pilot."""
    wanted = wanted.strip()[:500]
    if not wanted:
        return {"note": "Type what you want first."}
    if complete is None:
        found = closest(wanted, calls)
        if found is None or found.get("needs"):
            return {"note": NO_MODEL}
        return {"text": found["text"], "intent": found["intent"]}
    listed = "\n".join(
        f"{number}. {call['label']}{' (answers the controller)' if call.get('answers') else ''}: {call['text']}"
        for number, call in enumerate(calls, start=1)
    )
    heard = last_said.strip()[:400] or "(nothing yet)"
    asked = (
        f"Station: {station}\nThe controller last said: {heard}\nStandard calls that fit now:\n{listed or '(none)'}\n\n"
        f"The pilot wants: {wanted}"
    )
    answer = _answer(complete(PHRASER, asked))
    choice = answer.get("choice")
    if isinstance(choice, int) and not isinstance(choice, bool) and 1 <= choice <= len(calls):
        call = calls[choice - 1]
        text, intent = call["text"], dict(call["intent"])
        if call.get("needs") == "destination":
            place = str(answer.get("destination") or "").strip()[:60]
            if not place:
                return {"note": "Say where to: for example “flight following to Manchester”."}
            text, intent["destination"] = text.replace("{destination}", place), place
        return {"text": text, "intent": intent}
    worded = _tidy(_without_opening(str(answer.get("call") or ""), opening))
    if worded and len(worded) <= 300:
        return {"text": f"{opening}{worded}", "intent": None}
    unclear = str(answer.get("unclear") or "").strip()
    return {"note": unclear[:300] or "I could not tell what you want to ask for. Say it another way."}
