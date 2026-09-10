"""Reader for the "Troop Details" grid screen - a different screen from the
Unit Healing window rok/ocr.py reads, showing every troop type/tier the
governor currently owns (not just wounded ones), laid out as a grid of icons
rather than a scrolling list.

Built for the siege-composition check specifically: nothing here classifies
Infantry/Cavalry/Archer/Scout, because the rules only ever look at siege
counts by tier. An icon that isn't a confident Siege match is simply
ignored, which is the same fail-safe direction as the rest of this project -
an unclassified icon costs nothing, a wrongly-classified one could hide a
real siege violation.

The grid layout is different enough from the wounded-list that this reuses
only the pieces that are screen-agnostic (`_is_frame`, `tier_from_portrait`,
`glyph_mask`/`GlyphTable`) rather than the hospital list's row-anchoring
logic, which assumes a name label next to each portrait - there is no name
here, only an icon, a small type glyph, and a count.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .glyphs import GlyphTable, glyph_mask
from .ocr import _LAYOUT_CONFIGS, _group_lines, _is_frame, _variants, _words, tier_from_portrait
from .parse import parse_number

_SIEGE_GLYPHS: GlyphTable | None = None


def siege_glyph_table() -> GlyphTable:
    global _SIEGE_GLYPHS
    if _SIEGE_GLYPHS is None:
        _SIEGE_GLYPHS = GlyphTable(
            Path(__file__).resolve().parent.parent / "data" / "glyphs_troop_grid.json"
        )
    return _SIEGE_GLYPHS


TIERS = ("T1", "T2", "T3", "T4", "T5")


@dataclass
class SiegeReading:
    """What one Troop Details screenshot showed, siege units only."""

    by_tier: dict[str, int] = field(default_factory=lambda: {t: 0 for t in TIERS})
    total_units: int | None = None
    troop_power: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def total_siege(self) -> int:
        return sum(self.by_tier.values())


def _find_frame_blobs(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Every gold portrait frame in the image, as a connected blob.

    Global detection rather than searching relative to a text anchor: this
    screen has no name label to anchor off (unlike the wounded list), and
    flood-filling the whole image for frame-coloured pixels turned out more
    robust than guessing a per-icon search window - a window sized for one
    icon reliably picked up a neighbour's frame too when icons sit close
    together, which is exactly the failure this replaces.
    """
    w, h = image.size
    pixels = image.load()
    frame_mask = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            if _is_frame(*pixels[x, y][:3]):
                frame_mask[y][x] = True

    visited = [[False] * w for _ in range(h)]
    blobs: list[tuple[int, int, int, int]] = []
    for y0 in range(h):
        for x0 in range(w):
            if not frame_mask[y0][x0] or visited[y0][x0]:
                continue
            q = deque([(x0, y0)])
            visited[y0][x0] = True
            xs, ys = [], []
            while q:
                x, y = q.popleft()
                xs.append(x)
                ys.append(y)
                # A small gap tolerance (2px) survives anti-aliasing breaks
                # in the frame ring without merging genuinely separate icons.
                for dx in (-2, -1, 0, 1, 2):
                    for dy in (-2, -1, 0, 1, 2):
                        nx, ny = x + dx, y + dy
                        if (
                            0 <= nx < w
                            and 0 <= ny < h
                            and not visited[ny][nx]
                            and frame_mask[ny][nx]
                        ):
                            visited[ny][nx] = True
                            q.append((nx, ny))
            left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
            width, height = right - left, bottom - top
            # Icons are roughly square; anything far off that ratio is noise
            # (UI chrome, text, a stray frame-coloured pixel run) not a badge.
            if width < 20 or height < 20 or not (0.6 <= width / height <= 1.7):
                continue
            blobs.append((left, top, right, bottom))
    return blobs


_NUMBER_TAIL = re.compile(r"\d[\d,]*$")


def _find_numbers(image: Image.Image) -> list[tuple[int, int, int, int, str]]:
    """Every count-like number token, tried across several OCR passes.

    A single pass reliably misses some tokens (measured: one config found
    only 3 of 7 real entries on one sample screenshot). Each variant/config
    combo is an independent read of the same pixels, so pooling them and
    deduplicating by position catches what any single pass would miss -
    the same principle the wounded-list reader already relies on.
    """
    found: dict[tuple[int, int], tuple[int, int, int, int, str]] = {}
    for variant in _variants(image):
        scale = variant.width / image.width
        for config in (*_LAYOUT_CONFIGS, "--psm 4"):
            for line in _group_lines(_words(variant, config)):
                for word in line.words:
                    m = _NUMBER_TAIL.search(word.text)
                    if not m or len(m.group(0).replace(",", "")) < 1:
                        continue
                    value, _ = parse_number(m.group(0))
                    if value is None:
                        continue
                    left, top = int(word.left / scale), int(word.top / scale)
                    right, bottom = int(word.right / scale), int(word.bottom / scale)
                    # Round position to merge near-duplicate reads of the
                    # same token from different passes into one entry.
                    key = (round(left / 15), round(top / 15))
                    if key not in found or (right - left) > (found[key][2] - found[key][0]):
                        found[key] = (left, top, right, bottom, m.group(0))
    return list(found.values())


def _header_stats_bottom(image: Image.Image) -> int:
    """Y-coordinate below the tab row ("In the City" / "On the Map").

    The header banner above it ("Total Number of Units: 1,096,517", "Troop
    Power: ...") contains numbers too, and those must never be mistaken for
    a grid entry.
    """
    bottom = 0
    for word in _words(image, "--psm 4"):
        if word.text in ("City", "Map", "Units"):
            bottom = max(bottom, word.bottom)
    return bottom


def read_siege(image_bytes: bytes) -> SiegeReading:
    """Read one Troop Details screenshot, keeping only siege entries."""
    import io

    image = Image.open(io.BytesIO(image_bytes))
    image = image.convert("RGB")

    reading = SiegeReading()
    blobs = _find_frame_blobs(image)
    if not blobs:
        reading.warnings.append("No troop icons found in the screenshot.")
        return reading

    header_bottom = _header_stats_bottom(image)
    numbers = [n for n in _find_numbers(image) if n[1] >= header_bottom + 10]

    table = siege_glyph_table()
    used: set[tuple[int, int]] = set()
    for left, top, right, bottom in blobs:
        # Classify the glyph before looking for a number at all: it's cheap
        # (no OCR), and it means the targeted zoom-in fallback below only
        # ever runs for icons that are actually siege - not every icon in
        # the grid.
        fw = right - left
        glyph_box = (
            right + max(1, int(fw * 0.03)),
            top + int(fw * 0.30),
            right + int(fw * 0.60),
            top + int(fw * 0.70),
        )
        mask = glyph_mask(image, glyph_box)
        if table.classify(mask) != "Siege":
            continue

        cy = (top + bottom) / 2
        candidates = [
            n for n in numbers
            if n[0] > right and abs((n[1] + n[3]) / 2 - cy) < (bottom - top) * 0.6
        ]
        text = None
        if candidates:
            n_left, n_top, n_right, n_bottom, cand_text = min(candidates, key=lambda n: n[0])
            pos_key = (n_left, n_top)
            if pos_key not in used:
                used.add(pos_key)
                text = cand_text

        if text is None:
            # The whole-image OCR pass measurably misses some numbers (one
            # sample: found only 3 of 7 real entries) - confirmed by reading
            # the same crop directly with more zoom, which recovered it
            # cleanly. Only worth paying for once we already know this icon
            # is siege.
            text = _read_number_near(image, (left, top, right, bottom))
            if text is None:
                reading.warnings.append(
                    "Found a siege icon but could not read its count - "
                    "send a clearer screenshot if this matters for the check."
                )
                continue

        # This screen's icons put the character/weapon art lower in the frame
        # than the wounded-list's portraits, so the default sampling band
        # picks up too much art and not enough backdrop (measured: dominant
        # colour share fell under the confidence threshold using the default
        # band, but read cleanly at 70-80% with this higher, tighter one -
        # see tier_from_portrait's docstring). check_outside is off because
        # a packed grid has a neighbouring icon immediately to the left, not
        # panel, so that guard would misfire here.
        tier = tier_from_portrait(
            image, (left, top, right, bottom),
            inset=0.20, band=(0.05, 0.20), check_outside=False,
        )
        value, _ = parse_number(text)
        if value is None:
            continue
        if tier is None:
            reading.warnings.append(
                f"Found a siege icon ({value:,} troops) but could not read its tier - "
                "send a clearer screenshot if this matters for the check."
            )
            continue
        reading.by_tier[tier] = reading.by_tier.get(tier, 0) + value

    return reading


def _read_number_near(image: Image.Image, frame_box: tuple[int, int, int, int]) -> str | None:
    """Targeted close-up OCR for one icon's count, tried at several zoom
    levels. Only called for icons already confirmed as siege, since it is
    far more expensive than the single whole-image pass in _find_numbers.
    """
    import pytesseract
    from PIL import ImageOps

    left, top, right, bottom = frame_box
    fw = right - left
    crop = image.crop((right, top + int(fw * 0.05), min(image.width, right + fw * 3), bottom))
    if crop.width < 5 or crop.height < 5:
        return None
    gray = ImageOps.autocontrast(crop.convert("L"))

    best: str | None = None
    for zoom in (2, 3, 4):
        big = gray.resize((gray.width * zoom, gray.height * zoom), Image.LANCZOS)
        for variant in (big, ImageOps.invert(big)):
            for psm in (6, 7, 11):
                try:
                    text = pytesseract.image_to_string(variant, config=f"--psm {psm}")
                except Exception:
                    continue
                m = _NUMBER_TAIL.search(text.strip())
                if not m:
                    continue
                digits = m.group(0)
                # Prefer the longest read; a short one is more likely a
                # truncated/partial match than a genuinely small count for a
                # siege entry (which are never single digits in practice).
                if best is None or len(digits) > len(best):
                    best = digits
    return best


# --------------------------------------------------------------------------- #
# Rule check
# --------------------------------------------------------------------------- #

# T2 and T3 siege are never allowed; T1 and T4 have caps. T5 has no rule.
_ZERO_TIERS = ("T2", "T3")
_CAPS = {"T1": 200_000, "T4": 75_000}


def check_siege_rules(reading: SiegeReading) -> tuple[str, list[str]]:
    """Pass/FAIL against the siege composition rules, with the reasons.

    Returns (verdict, notes) - verdict is "Pass" or "FAIL". Every violation
    is listed, not just the first, so one resubmission can fix everything at
    once instead of finding problems one at a time.
    """
    notes: list[str] = []
    for tier in _ZERO_TIERS:
        count = reading.by_tier.get(tier, 0)
        if count:
            notes.append(f"{tier} siege must be 0, found {count:,}.")
    for tier, cap in _CAPS.items():
        count = reading.by_tier.get(tier, 0)
        if count > cap:
            notes.append(f"{tier} siege must be at most {cap:,}, found {count:,}.")

    verdict = "FAIL" if notes else "Pass"
    if not notes:
        notes.append("All siege tiers within limits.")
    return verdict, notes
