"""Multi-screenshot merging.

A player with several troop types cannot fit the whole wounded list on one screen,
so a submission is a session keyed by governor ID: each screenshot adds the rows it
can see, and the session is complete once the accumulated rows match the totals the
game printed. Rows are keyed by unit, so re-sending the same screenshot is harmless.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .pipeline import ExtractionResult
from .units import ResolvedRow, UnitTable, summarise


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Submission:
    governor_id: str
    discord_user_id: int
    discord_user_name: str
    rows: dict[str, ResolvedRow] = field(default_factory=dict)
    wounded_current: int | None = None
    wounded_capacity: int | None = None
    ram_current: int | None = None
    ram_capacity: int | None = None
    food: int | None = None
    wood: int | None = None
    stone: int | None = None
    gold: int | None = None
    rss_approx: bool = False
    cost_incomplete: bool = False
    image_urls: list[str] = field(default_factory=list)
    # Permalinks to the Discord messages. Unlike attachment URLs these do
    # not expire, so they stay checkable long after the event.
    message_urls: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    @property
    def all_rows(self) -> list[ResolvedRow]:
        return sorted(
            self.rows.values(), key=lambda r: (r.in_ram_zone, r.tier or "T9", r.name)
        )

    @property
    def summary(self) -> dict:
        return summarise(self.all_rows)

    @property
    def total_in_hospital(self) -> int | None:
        """Every wounded troop, siege included. The headline number.

        The bottom-left panel prints this whatever the list is scrolled to, so it
        survives the cropping that hides individual rows.
        """
        if self.wounded_current is None:
            return None
        return self.wounded_current + (self.ram_current or 0)

    @property
    def identified_troops(self) -> int:
        return sum(r.count for r in self.all_rows)

    @property
    def unaccounted(self) -> int | None:
        """Troops counted in the totals but not attributed to a named unit."""
        if self.total_in_hospital is None:
            return None
        return max(0, self.total_in_hospital - self.identified_troops)

    def high_tier_troops(self, table: UnitTable) -> int:
        """Troops we can see that belong to a tier the fill rule counts."""
        return sum(r.count for r in self.all_rows if r.tier in table.high_tiers)

    @property
    def unknown_tier_troops(self) -> int:
        """Troops on rows whose unit name we could read but do not recognise.

        Their tier is unknown, so they can neither be counted towards the fill
        rule nor held against it.
        """
        return sum(r.count for r in self.all_rows if r.tier is None)

    @property
    def unknown_unit_names(self) -> list[str]:
        return sorted({r.name for r in self.all_rows if r.tier is None})

    def fill_check(self, table: UnitTable) -> tuple[str, str]:
        """Did they fill with high-tier troops? Returns (verdict, explanation).

        The rule is a minimum, so the rows in one screenshot can settle it both
        ways without the full breakdown: enough high-tier troops already visible
        is a pass whatever is scrolled off, and a hospital too small to reach the
        minimum even if every hidden troop qualified is a fail.
        """
        need = table.min_high_tier_troops
        if not need:
            return "Unknown", "No fill rule configured."
        if not self.recordable:
            return "Unknown", "Hospital total not readable."

        seen = self.high_tier_troops(table)
        tiers = " or ".join(table.high_tiers)
        # Troops that might still qualify: not yet seen, or seen on a row whose
        # unit name we do not recognise. Treating an unknown name as low-tier
        # would fail an honest player for using a civilisation we have not met.
        unaccounted = self.unaccounted or 0
        unknown = self.unknown_tier_troops
        uncertain = unaccounted + unknown

        if seen >= need:
            return "Pass", f"{seen:,} {tiers} troops seen, at or above the {need:,} required."
        if seen + uncertain < need:
            short = need - (seen + uncertain)
            return "FAIL", (
                f"Only {seen:,} {tiers} troops, and even if all {uncertain:,} "
                f"unidentified troops were {tiers} the hospital is {short:,} short "
                f"of {need:,}."
            )

        reason = []
        if unknown:
            reason.append(
                f"{unknown:,} troops are on rows I don't recognise "
                f"({', '.join(self.unknown_unit_names)}) - an admin can add them with "
                "/hospital learn and the check will settle"
            )
        if unaccounted:
            reason.append(
                f"{unaccounted:,} troops aren't broken down yet - send a screenshot "
                "scrolled to the rest of the list"
            )
        return "Unconfirmed", (
            f"{seen:,} {tiers} troops confirmed so far, {need - seen:,} short of "
            f"{need:,}. " + ". ".join(reason) + "."
        )

    @property
    def recordable(self) -> bool:
        """Enough to write a row: the totals were read."""
        return self.wounded_current is not None

    def conflicts(self) -> list[str]:
        """Contradictions a second screenshot will not fix."""
        problems: list[str] = []
        rows = self.all_rows

        if self.wounded_current is not None:
            listed = sum(r.count for r in rows if not r.in_ram_zone)
            if listed > self.wounded_current:
                problems.append(
                    f"Wounded rows add up to {listed:,} but the total says "
                    f"{self.wounded_current:,}."
                )
        if self.ram_current is not None:
            siege = sum(r.count for r in rows if r.in_ram_zone)
            if siege > self.ram_current:
                problems.append(
                    f"Battering Ram Zone rows add up to {siege:,} but the total says "
                    f"{self.ram_current:,}."
                )
        unknown = sorted({r.name for r in rows if not r.known})
        if unknown:
            problems.append(
                "Unknown unit(s): " + ", ".join(unknown)
                + " - an admin can add them with /hospital learn."
            )
        return problems

    @property
    def breakdown_complete(self) -> bool:
        """Every troop in the hospital is attributed to a known unit."""
        return self.recordable and not self.conflicts() and not self.unaccounted

    @property
    def status(self) -> str:
        if not self.recordable:
            return "Unreadable"
        if self.conflicts():
            return "Needs review"
        return "Complete" if self.breakdown_complete else "Totals only"

    def missing(self) -> list[str]:
        """What is still unknown. Empty does not gate recording any more."""
        notes = self.conflicts()
        if not self.recordable:
            notes.insert(0, "Haven't read the 'Severely Wounded Units' total yet.")
        elif self.unaccounted:
            notes.append(
                f"{self.unaccounted:,} of {self.total_in_hospital:,} troops aren't broken "
                "down by unit yet - send a screenshot scrolled to the rest of the list if "
                "you want the tier detail."
            )
        return notes

    # Kept for the reply wording: a partial breakdown is fixable by the player,
    # a conflict is not.
    @property
    def awaiting_more(self) -> bool:
        return bool(self.unaccounted) and not self.conflicts()

    @property
    def needs_human(self) -> bool:
        return bool(self.conflicts())

    @property
    def complete(self) -> bool:
        return self.breakdown_complete

    def merge(
        self,
        result: ExtractionResult,
        image_url: str = "",
        message_url: str = "",
    ) -> list[str]:
        """Fold one screenshot into this submission. Returns notes about the merge."""
        notes: list[str] = []
        reading = result.reading

        # A changed total means the player healed or took more losses between shots.
        # Old rows describe a hospital that no longer exists, so they are dropped.
        if (
            reading.wounded_current is not None
            and self.wounded_current is not None
            and reading.wounded_current != self.wounded_current
        ):
            notes.append(
                f"Hospital total changed ({self.wounded_current:,} -> "
                f"{reading.wounded_current:,}); earlier screenshots discarded."
            )
            self.rows.clear()

        for row in result.rows:
            if row.count <= 0 or row.name.startswith("("):
                continue
            self.rows[row.key] = row

        for field_name in (
            "wounded_current",
            "wounded_capacity",
            "ram_current",
            "ram_capacity",
            "food",
            "wood",
            "stone",
            "gold",
        ):
            value = getattr(reading, field_name)
            if value is not None:
                setattr(self, field_name, value)

        self.rss_approx = self.rss_approx or reading.rss_approx
        # A partly-read cost would understate cost-per-troop and could fail an
        # honest player, so the fill check refuses to run on one.
        self.cost_incomplete = any(
            getattr(self, r) is None for r in reading.expected_resources
        ) if reading.expected_resources else self.cost_incomplete
        if image_url:
            self.image_urls.append(image_url)
        if message_url:
            self.message_urls.append(message_url)
        self.sources.append(reading.source)
        self.notes.extend(reading.warnings)
        self.updated_at = _now()
        return notes


class SessionStore:
    """In-memory, per-governor submission sessions with a rolling timeout."""

    def __init__(self, timeout_minutes: int = 30):
        self.timeout = timedelta(minutes=timeout_minutes)
        self._lock = threading.Lock()
        self._sessions: dict[str, Submission] = {}

    def _evict(self) -> None:
        cutoff = _now() - self.timeout
        for gid in [g for g, s in self._sessions.items() if s.updated_at < cutoff]:
            del self._sessions[gid]

    def get_or_create(
        self, governor_id: str, discord_user_id: int, discord_user_name: str
    ) -> Submission:
        with self._lock:
            self._evict()
            existing = self._sessions.get(governor_id)
            if existing is None:
                existing = Submission(
                    governor_id=governor_id,
                    discord_user_id=discord_user_id,
                    discord_user_name=discord_user_name,
                )
                self._sessions[governor_id] = existing
            else:
                # Keep attribution on whoever most recently submitted for this ID.
                existing.discord_user_id = discord_user_id
                existing.discord_user_name = discord_user_name
            return existing

    def get(self, governor_id: str) -> Submission | None:
        with self._lock:
            self._evict()
            return self._sessions.get(governor_id)

    def clear(self, governor_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(governor_id, None) is not None

    def open_ids(self) -> list[str]:
        with self._lock:
            self._evict()
            return sorted(self._sessions)
