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
from .ocr import (
    _LAYOUT_CONFIGS,
    _Word,
    _group_lines,
    _is_frame,
    _merge_split_numbers,
    _variants,
    _words,
    tier_from_portrait,
)
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

# How far right of the frame the type glyph (sword/wheels/etc.) extends, as a
# fraction of the frame's width. Shared between the glyph classification box
# below and _read_number_near's crop, so the number-OCR crop can never
# include the glyph icon itself - see _read_number_near's docstring for why
# that overlap matters.
_GLYPH_RIGHT_EDGE = 0.60


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


# Includes "." alongside "," because not every client uses comma-grouped
# thousands - a real report from a Vietnamese-language client had counts
# printed as "200.000". parse_number() already strips bare periods as a
# thousands separator (see rok/parse.py); this regex just has to not
# truncate the token before parse_number ever sees it, which the original
# comma-only version did - "200.000" only matched its trailing "000".
_NUMBER_TAIL = re.compile(r"\d[\d,.]*$")

# Same idea, but allows a literal space within the digit run too (not \s -
# that would also swallow a trailing newline from image_to_string). Only
# used against the targeted zoom-in fallback's raw string output below,
# where the crop is already narrowed to one icon's count - unlike
# _NUMBER_TAIL's per-word use above, there is no risk of pulling in an
# unrelated number here, so a space is safe to treat as part of the count
# rather than a token boundary.
_NUMBER_TAIL_LOOSE = re.compile(r"\d[\d,. ]*$")


def _strip_fused_leading_digits(text: str) -> str:
    """Drop leading digits that cannot belong to a real comma-grouped number.

    A genuine thousands-grouped numeral's first (leftmost) group is always
    1-3 digits - a comma never appears more than 3 digits in from the left
    of the whole number. Production report: a siege icon's real "200,000"
    got read, in one OCR pass, as "42200,000" - a stray "42" fused directly
    onto the real digits with no space or gap _merge_split_numbers could
    ever see (that guard only catches noise arriving as a *separate* token;
    this was one Tesseract-recognised word already). "42200" as a first
    group is structurally impossible for a real number, so trimming down to
    the last 3 digits of it recovers the genuine value. Only acts when a
    comma is present - a bare run of digits with no comma at all could
    still legitimately be an un-grouped OCR read of a real number, and is
    left untouched rather than risk cutting a real one down.
    """
    if "," not in text:
        return text
    first, rest = text.split(",", 1)
    if len(first) > 3:
        first = first[-3:]
    return f"{first},{rest}"


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
                # Extract the trailing digit run from each word first (as
                # before - tolerates leading OCR noise like "ES 200.000"),
                # *then* merge adjacent extracted numbers across a small gap:
                # a space-grouped count ("162 320") comes back as two
                # separate word tokens the same way it did on the wounded-
                # list screen (see _merge_split_numbers in rok/ocr.py for the
                # full story - reused here rather than duplicated, since this
                # module builds its own word list instead of going through a
                # _Line's numeric_words()). Merging before extracting would
                # instead drop noisy-prefixed words outright.
                extracted = []
                for word in line.words:
                    m = _NUMBER_TAIL.search(word.text)
                    if not m:
                        continue
                    extracted.append(
                        _Word(text=_strip_fused_leading_digits(m.group(0)), left=word.left,
                              top=word.top, right=word.right, bottom=word.bottom)
                    )
                for word in _merge_split_numbers(extracted):
                    if len(word.text.replace(",", "").replace(".", "")) < 1:
                        continue
                    value, _ = parse_number(word.text)
                    if value is None:
                        continue
                    left, top = int(word.left / scale), int(word.top / scale)
                    right, bottom = int(word.right / scale), int(word.bottom / scale)
                    # Round position to merge near-duplicate reads of the
                    # same token from different passes into one entry.
                    key = (round(left / 15), round(top / 15))
                    if key not in found or (right - left) > (found[key][2] - found[key][0]):
                        found[key] = (left, top, right, bottom, word.text)
    return list(found.values())


def _header_stats_bottom(blobs: list[tuple[int, int, int, int]]) -> int:
    """Y-coordinate above which numbers belong to the header banner
    ("Total Number of Units: 1,096,517", "Troop Power: ...", or their
    translation into whatever language the client is set to) and must never
    be mistaken for a grid entry.

    Originally found by OCR-matching the English tab labels ("In the City" /
    "On the Map") - broken on every other client language, which is exactly
    the kind of language dependency this project otherwise goes out of its
    way to avoid (see rok/ocr.py's module docstring). The icon grid's own
    position is a language-independent landmark instead: nothing in the
    header banner has a gold portrait frame, so the topmost frame blob marks
    where the real grid starts.
    """
    if not blobs:
        return 0
    return min(top for _, top, _, _ in blobs) - 20


# The game's own "this list is empty" text on the Troop Details grid, one
# entry per client language a real report has confirmed. Only ever consulted
# when the frame scan already found zero icons - this is a positive
# confirmation that the emptiness is genuine, not a substitute for the icon
# scan itself. Unlike a unit name, this isn't something an admin can teach
# via /hospital learn, so a language missing here needs a code change - add
# the phrase as it's reported, the same way a new unit name gets added to
# data/units.json.
_EMPTY_GRID_PHRASES = ("no units",)


def _confirmed_no_units(image: Image.Image) -> bool:
    """Whether an icon-less grid is a genuinely empty army, not a bad read.

    Production report: a governor who had disbanded every troop submitted
    this screen exactly as the game shows it - no gold-frame icons
    anywhere, "No Units" printed where the grid would be - and it was
    rejected outright as unreadable. All-zero trivially passes every siege
    rule, so this should have been recorded as a clean Pass.
    """
    for variant in _variants(image):
        for config in _LAYOUT_CONFIGS:
            for line in _group_lines(_words(variant, config)):
                if any(phrase in line.flat for phrase in _EMPTY_GRID_PHRASES):
                    return True
    return False


def read_siege(image_bytes: bytes) -> SiegeReading:
    """Read one Troop Details screenshot, keeping only siege entries."""
    import io

    image = Image.open(io.BytesIO(image_bytes))
    image = image.convert("RGB")

    reading = SiegeReading()
    blobs = _find_frame_blobs(image)
    if not blobs:
        if _confirmed_no_units(image):
            # Genuinely empty, not unreadable - a governor who has
            # disbanded/lost every troop gets exactly this: no icons at
            # all, "No Units" printed in the middle of the grid. All-zero
            # trivially satisfies every siege rule, so this is a real Pass,
            # not a screenshot that failed to read.
            return reading
        reading.warnings.append("No troop icons found in the screenshot.")
        return reading

    header_bottom = _header_stats_bottom(blobs)
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
            right + int(fw * _GLYPH_RIGHT_EDGE),
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

    The crop starts past the type glyph (_GLYPH_RIGHT_EDGE), not at the
    frame's own right edge. Production report: a wagon-wheels glyph
    immediately left of a genuine "51.877" got fed into this crop and read
    by Tesseract as noise glued directly onto the real digits with no space
    to split on ("251.877") - unlike a stray token elsewhere in the row,
    fused noise inside one OCR'd word can't be caught by any of the
    merge/trim guards downstream, since those only ever look at *separate*
    tokens or groups. Cropping the glyph out entirely removes the noise
    source instead of trying to filter it after the fact. Confirmed by
    direct comparison: the un-cropped icon read as "#5 51.877" /
    "�5 51.877" on this build's Tesseract even locally (harmless here only
    because the noise happened to land as its own space-separated group);
    past _GLYPH_RIGHT_EDGE the same crop reads cleanly as "51.877" alone.
    """
    import pytesseract
    from PIL import ImageOps

    left, top, right, bottom = frame_box
    fw = right - left
    number_left = right + int(fw * _GLYPH_RIGHT_EDGE)
    crop = image.crop((number_left, top + int(fw * 0.05), min(image.width, right + fw * 3), bottom))
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
                m = _NUMBER_TAIL_LOOSE.search(text.strip())
                if not m:
                    continue
                digits = _trim_spurious_leading_groups(m.group(0))
                # Prefer the longest read; a short one is more likely a
                # truncated/partial match than a genuinely small count for a
                # siege entry (which are never single digits in practice).
                if best is None or len(digits) > len(best):
                    best = digits
    return best


def _trim_spurious_leading_groups(text: str) -> str:
    """Drop a leading group that isn't really part of this number.

    _NUMBER_TAIL_LOOSE tolerates a space *within* the digit run so a
    space-grouped count ("162 320") isn't truncated - but that alone once
    turned a stray OCR misread of the weapon-glyph icon itself ("#8", read
    as text) into "8 55,965", corrupting a correct 55,965 into 855,965.

    The signal that separates the two: the last (rightmost) group is the
    one actually anchored to the count position. If it already contains a
    comma or period, Tesseract already resolved it as one complete number
    by itself, and anything before it is more likely a stray misread than
    a genuine further digit group - so nothing gets prepended. If the last
    group is bare digits (no punctuation of its own), it may genuinely be
    the tail of a larger space-grouped number, so bare-digit groups before
    it keep merging leftward - stopping at the first punctuated group,
    since a real number's groups don't mix "already grouped" with "not".
    """
    groups = text.split()
    if not groups:
        return text
    kept = [groups[-1]]
    if "," not in kept[0] and "." not in kept[0]:
        for group in reversed(groups[:-1]):
            if "," in group or "." in group:
                break
            kept.insert(0, group)
    return " ".join(kept)


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
