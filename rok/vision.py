"""Claude vision fallback - used only when the local OCR pass fails its checksum.

The prompt is deliberately written to forbid the model from making the numbers add
up. If it silently corrected a misread digit to satisfy the total, we would lose the
one independent signal we have that the reading is right.
"""
from __future__ import annotations

import base64
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from .parse import Reading, TroopRow

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Anthropic per-image limit


class VisionRow(BaseModel):
    name: str = Field(description="Unit name exactly as printed. Empty string if not readable.")
    name_readable: bool = Field(description="False when the row is cut off and the name is not fully visible.")
    count: Optional[int] = Field(description="The number of troops on this row. Null if not readable.")
    section: str = Field(description="Either 'wounded' or 'ram_zone', depending on which heading the row sits under.")


class VisionReading(BaseModel):
    rows: list[VisionRow]
    current_wounded: Optional[int]
    capacity_wounded: Optional[int]
    current_ram_zone: Optional[int]
    capacity_ram_zone: Optional[int]
    food: Optional[int]
    wood: Optional[int]
    stone: Optional[int]
    gold: Optional[int]
    values_were_abbreviated: bool = Field(
        description="True if any resource value was printed with a K or M suffix."
    )
    notes: str = Field(description="Anything cut off, ambiguous or unreadable. Empty string if all clean.")


PROMPT = """You are reading a screenshot of the "Unit Healing" window from the game Rise of Kingdoms.

Layout of that window:
- A scrollable list headed "Severely Wounded Units". Each row shows: a unit portrait
  with a tier badge, the unit's name, the count, a green bar, and the same count again
  in a box on the far right. The boxed number is the easiest to read - report the count
  once, not twice.
- Below that list there may be a second section headed "Battering Ram Zone" holding
  siege units. Those are tracked separately from the wounded units.
- A resource cost strip with four values, always in this left-to-right order:
  food (corn icon), wood (log icon), stone (grey stone icon), gold (coin icon).
  Values over 999 are abbreviated: "3.0K" is 3000, "26.6K" is 26600, "1.6M" is 1600000.
  Report the abbreviated values expanded to whole numbers.
- A panel at the bottom left printing "Severely Wounded Units <current>/<capacity>" and
  "Battering Ram Zone <current>/<capacity>".

Rules, in order of importance:
1. Transcribe what is printed. Do not calculate, round, correct or infer any number.
2. The list is scrollable, so the first or last visible row is often cut off. If a row's
   count is readable but its NAME is not fully visible, still report the row with
   name_readable=false and name="". Never guess which unit a cut-off row is.
3. If a value is not visible in this screenshot, return null. Never substitute 0 for
   "not visible" - 0 is a real, different reading.
4. The row counts would normally sum to the totals in the bottom-left panel, but DO NOT
   adjust any number to make them agree. Report exactly what you see and describe the
   discrepancy in notes. The caller checks the arithmetic itself and depends on your
   numbers being independent of it.
5. Ignore the resource bar along the very top of the screen if one is visible - that is
   the player's stockpile, not the healing cost."""


def _media_type(image_bytes: bytes) -> str:
    """Sniff the format from magic bytes (stdlib imghdr was removed in Python 3.13)."""
    head = image_bytes[:12]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def read(image_bytes: bytes, *, api_key: str, model: str = "claude-opus-5") -> Reading:
    """Read one screenshot with Claude. Raises on API failure - the caller decides."""
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image is {len(image_bytes) / 1e6:.1f} MB; the API limit is {MAX_IMAGE_BYTES / 1e6:.0f} MB."
        )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.parse(
        model=model,
        max_tokens=8000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _media_type(image_bytes),
                            "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
                        },
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        output_format=VisionReading,
    )

    parsed: VisionReading | None = response.parsed_output
    if parsed is None:
        raise RuntimeError("Vision model returned no structured output.")

    return _to_reading(parsed)


def _to_reading(parsed: VisionReading) -> Reading:
    reading = Reading(
        wounded_current=parsed.current_wounded,
        wounded_capacity=parsed.capacity_wounded,
        ram_current=parsed.current_ram_zone,
        ram_capacity=parsed.capacity_ram_zone,
        food=parsed.food,
        wood=parsed.wood,
        stone=parsed.stone,
        gold=parsed.gold,
        rss_approx=parsed.values_were_abbreviated,
        source="claude",
    )

    for row in parsed.rows:
        if row.count is None:
            reading.warnings.append(
                f"Row '{row.name or 'unnamed'}' had no readable count and was skipped."
            )
            continue
        reading.rows.append(
            TroopRow(
                raw_name=row.name.strip(),
                count=row.count,
                name_visible=row.name_readable and bool(row.name.strip()),
                in_ram_zone=(row.section or "").strip().lower() == "ram_zone",
            )
        )

    if parsed.notes.strip():
        reading.warnings.append(parsed.notes.strip())

    return reading
