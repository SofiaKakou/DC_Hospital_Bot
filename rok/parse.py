"""Shared data model plus the number/text normalisation both readers depend on.

The whole design leans on one fact about the Unit Healing window: the counts in the
list must add up to the totals printed at the bottom left. That gives us a free,
independent checksum on every reading, which is what decides whether a cheap local
OCR pass is trusted or escalated to the vision model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# "3.0K" -> 3000, "26.6K" -> 26600, "1.6M" -> 1600000, "584" -> 584, "1,234" -> 1234
_SCALED = re.compile(r"^([0-9][0-9.,]*)\s*([KkMmBb])?$")
_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

# Digits the game font and Tesseract routinely swap. Applied only to tokens we have
# already decided are numeric, never to unit names.
_DIGIT_FIXES = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"})


def clean_number_token(raw: str) -> str:
    """Strip UI noise from a token we expect to be a number."""
    return raw.strip().strip("()[]{}<>:;").replace(" ", "").translate(_DIGIT_FIXES)


def parse_number(raw: str) -> tuple[int | None, bool]:
    """Parse a possibly-abbreviated count. Returns (value, was_abbreviated).

    The game abbreviates anything over 999, so "26.6K" really means "somewhere in
    26,550-26,649". The flag lets callers record that the value is approximate
    rather than silently presenting a rounded number as exact.
    """
    token = clean_number_token(raw)
    m = _SCALED.match(token)
    if not m:
        return None, False
    digits, suffix = m.group(1), (m.group(2) or "").lower()
    if suffix:
        try:
            value = float(digits.replace(",", ""))
        except ValueError:
            return None, False
        return int(round(value * _SUFFIX[suffix])), True
    if "." in digits:
        # A bare decimal in this UI is an OCR artefact (a stray dot in "1.234").
        digits = digits.replace(".", "")
    try:
        return int(digits.replace(",", "")), False
    except ValueError:
        return None, False


_FRACTION = re.compile(r"([0-9][0-9.,]*\s*[KkMmBb]?)\s*/\s*([0-9][0-9.,]*\s*[KkMmBb]?)")


def parse_fraction(raw: str) -> tuple[int | None, int | None]:
    """Parse the '232/548,725' current/capacity pairs."""
    m = _FRACTION.search(raw.replace(" ", ""))
    if not m:
        return None, None
    current, _ = parse_number(m.group(1))
    capacity, _ = parse_number(m.group(2))
    return current, capacity


def normalise_unit_name(raw: str) -> str:
    """Fold a unit name to the lookup key used in data/units.json."""
    text = raw.lower().strip()
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
    text = re.sub(r"[^a-z' -]", " ", text)
    text = re.sub(r"[-\s]+", " ", text).strip()
    return text


@dataclass
class TroopRow:
    """One line of the wounded list."""

    raw_name: str
    count: int
    name_visible: bool = True
    in_ram_zone: bool = False
    # Tier read off the portrait's rarity colour and type read off the weapon
    # glyph. Both are independent of the unit's name, which is useless across 20
    # civilisations and every game language.
    tier: str | None = None
    troop_type: str | None = None

    @property
    def key(self) -> str:
        return normalise_unit_name(self.raw_name)


@dataclass
class Reading:
    """Everything one screenshot told us. Fields are None when not visible."""

    rows: list[TroopRow] = field(default_factory=list)
    wounded_current: int | None = None
    wounded_capacity: int | None = None
    ram_current: int | None = None
    ram_capacity: int | None = None
    food: int | None = None
    wood: int | None = None
    stone: int | None = None
    gold: int | None = None
    rss_approx: bool = False
    # Which resources this hospital's units actually use, so a caller can tell a
    # value that is missing from one that was never shown.
    expected_resources: list[str] = field(default_factory=list)
    source: str = "unknown"
    warnings: list[str] = field(default_factory=list)

    @property
    def wounded_rows(self) -> list[TroopRow]:
        return [r for r in self.rows if not r.in_ram_zone]

    @property
    def ram_rows(self) -> list[TroopRow]:
        return [r for r in self.rows if r.in_ram_zone]

    @property
    def rss(self) -> dict[str, int | None]:
        return {"food": self.food, "wood": self.wood, "stone": self.stone, "gold": self.gold}


def check_totals(reading: Reading) -> list[str]:
    """Independent arithmetic check. Empty list means the reading is self-consistent.

    Deliberately does NOT repair anything - a reader that adjusts its own numbers to
    satisfy the checksum destroys the only evidence we have that it read correctly.
    """
    problems: list[str] = []

    if reading.wounded_current is None:
        problems.append("Could not read the 'Severely Wounded Units' total.")
    else:
        listed = sum(r.count for r in reading.wounded_rows)
        if listed != reading.wounded_current:
            problems.append(
                f"Wounded rows add up to {listed:,} but the total says "
                f"{reading.wounded_current:,} (off by {abs(listed - reading.wounded_current):,})."
            )

    # The ram zone line is only shown once the player has siege units, so a missing
    # total with no siege rows is normal, not a problem.
    if reading.ram_current is None:
        if reading.ram_rows:
            problems.append("Read siege units but no 'Battering Ram Zone' total.")
    else:
        listed = sum(r.count for r in reading.ram_rows)
        # Over count only, not an exact match: unlike the wounded list, this
        # screen has no way to itemise Ram Zone troops at all except in the
        # rare case where the whole zone happens to be Battering Rams -
        # classify_siege() (rok/pipeline.py) already credits that case by
        # reconciling against the totals rather than assuming it from unit
        # type. Every other real report has zero ram_rows and a non-zero
        # ram_current, which used to fail this check outright on every
        # single one of them - a screenshot whose wounded list reconciled
        # perfectly still got flagged solely because of an inherently
        # un-itemisable, separate total.
        if listed > reading.ram_current:
            problems.append(
                f"Battering Ram Zone rows add up to {listed:,} but the total says "
                f"{reading.ram_current:,}."
            )

    if not reading.rows and (reading.wounded_current or 0) > 0:
        problems.append("No troop rows were read at all.")

    return problems
