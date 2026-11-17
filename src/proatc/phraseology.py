"""Radio pronunciation: numbers, letters, runways, frequencies, altitudes."""

from __future__ import annotations

DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "niner"]
LETTERS = (
    "Alfa Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliett Kilo Lima Mike November "
    "Oscar Papa Quebec Romeo Sierra Tango Uniform Victor Whiskey X-ray Yankee Zulu"
).split()
PHONETIC = {**dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", LETTERS)), **{str(i): word for i, word in enumerate(DIGITS)}}
RUNWAY_SUFFIX = {"L": "left", "R": "right", "C": "center"}


def digits(value) -> str:
    """270 -> 'two seven zero'. Non-digits are dropped."""
    return " ".join(DIGITS[int(c)] for c in str(value) if c.isdigit())


def letter(char: str) -> str:
    return PHONETIC[char.upper()]


def spoken_callsign(tail_number: str) -> str:
    """C-GTLT -> 'Charlie Golf Tango Lima Tango'."""
    return " ".join(PHONETIC[c] for c in tail_number.upper() if c in PHONETIC)


def runway(ident: str) -> str:
    """'07L' -> 'zero seven left'."""
    number = "".join(c for c in ident if c.isdigit())
    suffix = RUNWAY_SUFFIX.get(ident[-1].upper(), "")
    return f"{digits(number)} {suffix}".strip()


def taxiway(name: str) -> str:
    """'A' -> 'Alfa'; 'N4W' -> 'November four Whiskey'; 'LINK56' / 'Link 41' -> 'Link five six'."""
    compact = name.replace(" ", "").upper()
    if compact.startswith("LINK") and compact[4:].isdigit():
        return f"Link {digits(compact[4:])}"
    if len(compact) <= 5 and compact.isalnum() and compact[0].isalpha():
        return " ".join(PHONETIC[c] for c in compact)
    return name.title()


def stand(name: str) -> str:
    """'12' -> 'one two'; '209R' -> 'two zero niner Romeo'; 'Cargo 3' stays as words."""
    compact = name.replace(" ", "").upper()
    if len(compact) <= 5 and compact.isalnum() and compact.isascii():
        return " ".join(PHONETIC[c] for c in compact)
    return name.title()


def join(items: list[str]) -> str:
    """['A', 'B', 'C'] -> 'A, B and C'."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def frequency(khz: int) -> str:
    """118505 -> 'one one eight decimal five zero five'; 118700 -> 'one one eight decimal seven'."""
    decimals = f"{khz % 1000:03d}".rstrip("0") or "0"
    return f"{digits(khz // 1000)} decimal {digits(decimals)}"


def altitude(feet: float) -> str:
    """2500 -> 'two thousand five hundred'; 12000 -> 'one two thousand'."""
    feet = int(round(feet / 100.0)) * 100
    thousands, hundreds = divmod(feet, 1000)
    parts = []
    if thousands:
        parts.append(f"{digits(thousands)} thousand")
    if hundreds:
        parts.append(f"{DIGITS[hundreds // 100]} hundred")
    return " ".join(parts) or "zero"


def qnh(hectopascals: float) -> str:
    """'Q N H one zero one three'; below 1000 the unit is said, so that 998 is not taken for inches: 'Q N H niner niner eight hectopascals'."""
    value = int(hectopascals)
    return f"Q N H {digits(value)}" + (" hectopascals" if value < 1000 else "")


def signed(value: float) -> str:
    """-2 -> 'minus two'; 15 -> 'one five'."""
    value = int(round(value))
    return ("minus " if value < 0 else "") + digits(abs(value))


def zulu_time(seconds: float) -> str:
    minutes = int(seconds // 60) % 1440
    return digits(f"{minutes // 60:02d}{minutes % 60:02d}")


def level(feet: float, transition_ft: int, faa: bool = False) -> str:
    """24000 -> 'flight level two four zero'; 6000 -> 'altitude six thousand feet' (or 'six thousand' in the US)."""
    feet = int(round(feet / 100.0)) * 100
    if feet > transition_ft:
        return f"flight level {digits(f'{feet // 100:03d}')}"
    return altitude(feet) if faa else f"altitude {altitude(feet)} feet"


def heading(degrees: float) -> str:
    """90 -> 'zero niner zero'; 0 -> 'three six zero'."""
    value = int(round(degrees)) % 360 or 360
    return digits(f"{value:03d}")


# ICAO airline designator -> the name said on the radio.
TELEPHONY = {
    "AAL": "American", "ACA": "Air Canada", "AFR": "Air France", "AMX": "Aeromexico", "ANA": "All Nippon",
    "ASA": "Alaska", "AUA": "Austrian", "AVA": "Avianca", "AZU": "Azul", "BAW": "Speedbird", "BEL": "Beeline",
    "CMP": "Copa", "CPA": "Cathay", "DAL": "Delta", "DLH": "Lufthansa", "EIN": "Shamrock", "ETD": "Etihad",
    "EXS": "Channex", "EZY": "Easy", "FDX": "FedEx", "FIN": "Finnair", "GLO": "Gol", "IBE": "Iberia",
    "JAL": "Japan Air", "JBU": "JetBlue", "KLM": "K L M", "LOT": "Lot", "NAX": "Norwegian", "QFA": "Qantas",
    "QTR": "Qatari", "RYR": "Ryanair", "SAS": "Scandinavian", "SIA": "Singapore", "SWA": "Southwest",
    "SWR": "Swiss", "TAM": "Tam", "TAP": "Air Portugal", "THY": "Turkish", "UAE": "Emirates", "UAL": "United",
    "UPS": "U P S", "VIR": "Virgin", "VLG": "Vueling", "WJA": "WestJet", "WZZ": "Wizz Air",
}  # fmt: skip


def airline_callsign(filed: str) -> str | None:
    """'BAW123' -> 'Speedbird one two three'; None when it is not a known airline flight number."""
    filed = filed.upper().replace(" ", "")
    name = TELEPHONY.get(filed[:3])
    number = filed[3:]
    if not name or not number or not number[0].isdigit() or not number.isalnum() or len(number) > 4:
        return None
    return f"{name} {' '.join(PHONETIC[c] for c in number)}"
