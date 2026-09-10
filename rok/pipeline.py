"""Extraction pipeline: cheap local OCR first, Claude vision only when it fails.

The escalation trigger is the arithmetic checksum in parse.py, not a confidence
score. That matters: a reading either adds up to the totals the game printed or it
does not, and only readings that add up are ever written to the sheet unattended.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import ocr
from .parse import Reading, check_totals
from .units import ResolvedRow, UnitTable, summarise

log = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    reading: Reading
    rows: list[ResolvedRow] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only when the numbers add up and every unit name is known."""
        return not self.problems

    @property
    def source(self) -> str:
        return self.reading.source


def classify_siege(reading: Reading, table: UnitTable) -> None:
    """Set in_ram_zone from the unit table rather than from screen geometry.

    Siege units are the ones that live in the Battering Ram Zone, so the table
    already knows the answer. Reading it off the layout would mean locating a
    section header in a two-column window that is often scrolled or cropped.
    """
    for row in reading.rows:
        # The glyph is the better source - it works for a civilisation or a
        # language the table has never seen - and the name is only consulted
        # when no glyph could be read.
        if row.troop_type is not None:
            row.in_ram_zone = row.troop_type == "Siege"
            continue
        if not row.name_visible:
            continue
        entry = table.lookup(row.raw_name)
        if entry is not None:
            row.in_ram_zone = entry.get("type") == "Siege"


def evaluate(reading: Reading, table: UnitTable) -> ExtractionResult:
    classify_siege(reading, table)
    rows = table.resolve_all(reading.rows)
    summary = summarise(rows)

    problems = check_totals(reading)
    for row in summary["unknown_rows"]:
        if row.count and not row.name.startswith("("):
            problems.append(
                f"Unknown unit '{row.name}' - not in the unit table, so its power cannot be scored."
            )
    cutoff = [r for r in reading.rows if not r.name_visible]
    if cutoff:
        counts = ", ".join(str(r.count) for r in cutoff)
        problems.append(
            f"{len(cutoff)} row(s) had the name scrolled out of view (count: {counts}). "
            "Send a screenshot scrolled to the top of the list."
        )

    return ExtractionResult(reading=reading, rows=rows, summary=summary, problems=problems)


def extract(
    image_bytes: bytes,
    table: UnitTable,
    *,
    anthropic_api_key: str = "",
    vision_model: str = "claude-opus-5",
    tesseract_cmd: str = "",
) -> ExtractionResult:
    """Read one screenshot. Escalates to the vision model only if OCR does not add up."""
    known_names = list(table.units)
    attempts: list[ExtractionResult] = []

    try:
        ocr.configure(tesseract_cmd)
        for candidate in ocr.read_all(image_bytes, known_names):
            result = evaluate(candidate, table)
            if result.ok:
                return result
            attempts.append(result)
    except ocr.TesseractUnavailable as exc:
        log.info("Skipping local OCR: %s", exc)
    except Exception:  # pragma: no cover - defensive; vision still gets a turn
        # Log the type and traceback, not just str(exc). A KeyError(None) prints
        # as the bare word "None", which said nothing about where it came from.
        log.exception("Local OCR failed")

    if anthropic_api_key:
        try:
            # Imported here so the rest of the pipeline runs without the anthropic SDK.
            from . import vision

            reading = vision.read(image_bytes, api_key=anthropic_api_key, model=vision_model)
            result = evaluate(reading, table)
            if result.ok or not attempts:
                return result
            attempts.append(result)
        except Exception as exc:
            log.exception("Vision read failed")
            if not attempts:
                failed = Reading(source="failed")
                failed.warnings.append(str(exc))
                return ExtractionResult(
                    reading=failed,
                    problems=[f"Could not read the screenshot: {exc}"],
                )

    if not attempts:
        failed = Reading(source="failed")
        return ExtractionResult(
            reading=failed,
            problems=[
                "No reader was available. Install Tesseract or set ANTHROPIC_API_KEY in .env."
            ],
        )

    # Nothing passed - hand back whichever attempt got closest so a human can judge.
    attempts.sort(key=lambda r: (len(r.problems), -len(r.rows)))
    return attempts[0]
