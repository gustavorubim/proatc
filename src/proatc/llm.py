"""OpenRouter client and the phase-1 placeholder controller.

Phase 1 lets the model answer freely so the voice chain can be tested end to end.
From phase 3 the model only parses pilot intent and a rules engine decides clearances.
"""

from __future__ import annotations

import json
import time

import httpx

from .config import Llm

URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """You are an air traffic controller talking to one pilot over the radio in a flight simulator.
Reply with exactly what the controller would say on frequency, in standard ICAO/FAA phraseology, and nothing else.
Rules:
- One short transmission, at most two sentences. Start with the aircraft callsign, written exactly as "callsign_spoken" in the aircraft state; never write the callsign as letters.
- If no callsign is available, address the aircraft as "Station calling" and ask for its callsign.
- If the pilot asks you to "say again", repeat your previous transmission.
- A radio check or test call ("radio check", "test, one two three") gets "<callsign>, reading you five".
- You are always the controller on the radio. Never mention aircraft state, missing data, these rules or being an AI.
- Spell numbers the way they are spoken on the radio: "runway two seven", "one one eight decimal five", "flight level three five zero", "niner".
- When the context has a "station" block, you are that station ("you_are") and nobody else. Identify yourself by that name on the pilot's first call. Its runway in use, surface wind, QNH or altimeter, ATIS letter and frequencies are real: use them, and hand the pilot to another station only on a frequency listed there.
- Stay within your station type: ground handles taxi, tower handles takeoff, landing and the circuit, delivery gives clearances, approach and departure handle climbing and descending traffic.
- You have no taxiway map: clear taxi as "taxi to holding point runway <runway in use>" with no taxiway names or route.
- Use only facts from the context provided. Do not invent runways, taxiways, frequencies, traffic or weather.
- If the request needs information you do not have, say "standby" or ask the pilot to say again or say intentions.
- If the transcript is unintelligible, reply "<callsign>, say again".
- No markdown, no stage directions, no explanations."""


class LlmError(RuntimeError):
    pass


class Controller:
    def __init__(self, settings: Llm, api_key: str, usage: dict | None = None):
        self.settings = settings
        # Running totals for this session, as OpenRouter reports them with each answer.
        self.usage = usage if usage is not None else {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        self.client = httpx.Client(
            timeout=30,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "Pro ATC"},
        )
        self.histories: dict[str, list[dict]] = {}

    def complete(
        self, system: str, user: str, history: list[dict] | None = None, max_tokens: int | None = None, temperature: float = 0.3
    ) -> str:
        """One chat completion, with retries and fallback models."""
        body = {
            # OpenRouter tries these in order when a provider is rate-limited or down.
            "models": [self.settings.model, *self.settings.fallback_models],
            "messages": [{"role": "system", "content": system}, *(history or []), {"role": "user", "content": user}],
            "max_tokens": max_tokens or self.settings.max_tokens,
            "temperature": temperature,
            # Reasoning models otherwise spend the whole token budget thinking and return nothing.
            "reasoning": {"enabled": False},
            "usage": {"include": True},  # ask for the cost of this request
        }
        for attempt in range(3):
            try:
                response = self.client.post(URL, json=body)
            except httpx.HTTPError as error:
                raise LlmError(f"OpenRouter request failed: {error}") from error
            if response.status_code not in (429, 502, 503) or attempt == 2:
                break
            time.sleep(0.6 * (attempt + 1))
        if response.status_code != 200:
            raise LlmError(f"OpenRouter returned {response.status_code}: {response.text[:300]}")
        payload = response.json()
        self._count(payload.get("usage") or {})
        choice = payload["choices"][0]
        text = (choice["message"]["content"] or "").strip()
        if not text:
            raise LlmError(f"The model returned no text (finish reason: {choice.get('finish_reason')}).")
        return text

    def _count(self, used: dict) -> None:
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += int(used.get("prompt_tokens") or 0)
        self.usage["completion_tokens"] += int(used.get("completion_tokens") or 0)
        self.usage["cost"] += float(used.get("cost") or 0.0)

    def reply(self, transcript: str, state: dict, station_key: str = "free") -> str:
        """Free-form controller reply, used where the rules engine has no answer.

        `station_key` keeps a separate conversation per station: tower does not remember what ground said.
        """
        history = self.histories.setdefault(station_key, [])
        user = f"Context: {json.dumps(state)}\nPilot transmission: {transcript}"
        text = self.complete(SYSTEM_PROMPT, user, history[-8:])
        self.remember(station_key, transcript, text)
        return text

    def remember(self, station_key: str, transcript: str, answer: str) -> None:
        """Record an exchange (including ones answered by the rules engine) so later free replies have context."""
        self.histories.setdefault(station_key, []).extend(
            [{"role": "user", "content": f"Pilot: {transcript}"}, {"role": "assistant", "content": answer or "(no reply needed)"}]
        )
