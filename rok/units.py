"""Unit name -> tier/type/power lookup, backed by data/units.json.

Unknown names are never guessed. A T4/T5 name the table has not seen is a real
possibility on every submission (those names are civilisation-specific), so the
bot surfaces it for an admin to teach rather than inventing a tier.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

from .parse import TroopRow, normalise_unit_name

TIERS = ("T1", "T2", "T3", "T4", "T5")
TYPES = ("Infantry", "Archer", "Cavalry", "Siege")
RESOURCES = ("food", "wood", "stone", "gold")


@dataclass(frozen=True)
class ResolvedRow:
    """A troop row with its tier, type and power resolved."""

    name: str
    key: str
    count: int
    tier: str | None
    type: str | None
    power_each: int | None
    in_ram_zone: bool

    @property
    def known(self) -> bool:
        return self.tier is not None and self.power_each is not None

    @property
    def power_total(self) -> int:
        return (self.power_each or 0) * self.count


class UnitTable:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.tier_power: dict[str, int] = {k.upper(): int(v) for k, v in raw["tier_power"].items()}
        self.verified: bool = bool(raw.get("verified_by_human", False))
        # Normalise keys the same way lookups are normalised, so "Man-at-Arms" in the
        # file and "Man at Arms" from OCR land on the same entry.
        self.units: dict[str, dict] = {
            normalise_unit_name(name): entry for name, entry in raw["units"].items()
        }
        self.type_resources: dict[str, list[str]] = raw.get("type_resources", {})
        self.high_tiers: tuple[str, ...] = tuple(
            str(x).upper() for x in raw.get("high_tiers", ("T4", "T5"))
        )
        self.min_high_tier_troops: int = int(raw.get("min_high_tier_troops", 0))
        self.gold_from_tier: str = raw.get("gold_from_tier", "T3")

    def reload(self) -> None:
        with self._lock:
            self._load()

    def lookup(self, name: str) -> dict | None:
        return self.units.get(normalise_unit_name(name))

    def power_each(self, tier: str, entry: dict | None = None) -> int | None:
        if entry and entry.get("power") is not None:
            return int(entry["power"])
        return self.tier_power.get(tier.upper())

    def resolve(self, row: TroopRow) -> ResolvedRow:
        entry = self.lookup(row.raw_name) if row.name_visible else None
        # The portrait colour wins over the name table: it is the game's own
        # label, and it works for a civilisation or language we have never seen.
        tier = row.tier or (entry.get("tier") if entry else None)
        # Both read off the screenshot in preference to the table: the game's own
        # drawing beats a name lookup, and it corrected four table entries the
        # first time it ran.
        ttype = row.troop_type or (entry.get("type") if entry else None)
        power = self.power_each(tier, entry) if tier else None
        return ResolvedRow(
            name=row.raw_name.strip() or "(name cut off)",
            key=row.key,
            count=row.count,
            tier=tier,
            type=ttype,
            power_each=power,
            in_ram_zone=row.in_ram_zone,
        )

    def resolve_all(self, rows: list[TroopRow]) -> list[ResolvedRow]:
        return [self.resolve(r) for r in rows]

    def learn(self, name: str, tier: str, ttype: str, power: int | None = None) -> str:
        """Persist a new unit name. Returns the normalised key that was written."""
        tier = tier.upper().strip()
        ttype = ttype.strip().title()
        if tier not in TIERS:
            raise ValueError(f"Tier must be one of {', '.join(TIERS)}")
        if ttype not in TYPES:
            raise ValueError(f"Type must be one of {', '.join(TYPES)}")

        key = normalise_unit_name(name)
        if not key:
            raise ValueError("Unit name is empty after normalisation.")

        with self._lock:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entry: dict = {"tier": tier, "type": ttype}
            if power is not None:
                entry["power"] = int(power)
            raw["units"][key] = entry
            self.path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
            self._load()
        return key



def summarise(rows: list[ResolvedRow]) -> dict:
    """Aggregate resolved rows into the numbers that go on the sheet."""
    known = [r for r in rows if r.known]
    unknown = [r for r in rows if not r.known]

    by_tier = {t: 0 for t in TIERS}
    by_type = {t: 0 for t in TYPES}
    for r in known:
        # A row can have a tier without a type: the tier is read off the portrait
        # and works for any civilisation, while the type still comes from the
        # name table. Indexing by_type with None raised KeyError(None) here, and
        # since its message is the string "None" it surfaced as the useless log
        # line "Local OCR failed: None" - taking out every submission whose units
        # the table had never seen.
        if r.tier in by_tier:
            by_tier[r.tier] += r.count
        if r.type in by_type:
            by_type[r.type] += r.count

    return {
        "total_troops": sum(r.count for r in rows),
        "total_power": sum(r.power_total for r in known),
        "by_tier": by_tier,
        "by_type": by_type,
        "tiers_present": [t for t in TIERS if by_tier[t]],
        "types_present": [t for t in TYPES if by_type[t]],
        "unknown_rows": unknown,
        "power_is_partial": bool(unknown),
    }
