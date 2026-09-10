"""Local Tesseract reader.

Reads the whole window in one pass, groups words into visual rows, and takes each
row's troop count from the number printed beside the unit name - not the boxed
number at the right, which is the heal-amount slider and can be dragged down.

A second digit-only pass over each row was tried and measured *worse*: the row band
spans the portrait, the name and the green bar, so a single-line whitelist read
fragments the number rather than sharpening it. The variants below (two polarities
x two page-segmentation modes) are what actually buys accuracy - the pipeline keeps
the first candidate that satisfies the checksum in parse.py.
"""
from __future__ import annotations

import colorsys
import io
import re
from pathlib import Path
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageOps

from .glyphs import GlyphTable, glyph_mask
from .parse import Reading, TroopRow, clean_number_token, parse_fraction, parse_number

_GLYPHS: GlyphTable | None = None


def glyph_table() -> GlyphTable:
    """The weapon-glyph templates, loaded once."""
    global _GLYPHS
    if _GLYPHS is None:
        _GLYPHS = GlyphTable(Path(__file__).resolve().parent.parent / "data" / "glyphs.json")
    return _GLYPHS

try:  # pytesseract imports fine without the binary; the binary is checked lazily.
    import pytesseract
    from pytesseract import Output
except ImportError:  # pragma: no cover
    pytesseract = None
    Output = None


class TesseractUnavailable(RuntimeError):
    """Raised when the Tesseract binary is missing or unusable."""


_LAYOUT_CONFIGS = ("--psm 6", "--psm 11")
_MIN_CONF = 30
_BUTTON_WORDS = ("instant", "heal", "help")
_COST_CHARS = "0123456789.,KkMm"
_NUMERIC = re.compile(r"^[0-9][0-9.,]*\s*[KkMmBb]?$")


def configure(tesseract_cmd: str = "") -> None:
    if pytesseract is None:
        raise TesseractUnavailable("pytesseract is not installed (pip install pytesseract)")
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd


def available() -> bool:
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


@dataclass
class _Word:
    text: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def cx(self) -> int:
        return (self.left + self.right) // 2


@dataclass
class _Line:
    words: list[_Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def top(self) -> int:
        return min(w.top for w in self.words)

    @property
    def bottom(self) -> int:
        return max(w.bottom for w in self.words)

    @property
    def left(self) -> int:
        return min(w.left for w in self.words)

    @property
    def right(self) -> int:
        return max(w.right for w in self.words)

    @property
    def flat(self) -> str:
        text = re.sub(r"[^a-z0-9,./ ]", " ", self.text.lower())
        return re.sub(r"\s+", " ", text).strip()

    def numeric_words(self) -> list[_Word]:
        return _merge_split_numbers(
            [w for w in self.words if _NUMERIC.match(clean_number_token(w.text))]
        )


def _merge_split_numbers(words: list[_Word]) -> list[_Word]:
    """Merge adjacent numeric-only words separated by a small gap into one.

    Not every client groups thousands with a comma: a real report read
    "257 609" (French, space-grouped) as two separate troop counts, 257 and
    609, because Tesseract treats a rendered space as a word boundary the
    same way it would between two actual words - the two tokens were only
    10px apart on a ~30px-tall line. Both callers of numeric_words()
    (_count_word's "first number after the name", and the unrecognised-name
    fallback's "last number on the line") were built assuming one troop
    count is always one token, so each silently kept only half of a
    space-grouped number instead of failing loudly.

    Only merges numeric-only tokens (the caller already filtered to those),
    and only across a gap smaller than the token's own height - a genuinely
    separate number on the same line (a tier numeral near the portrait, say)
    sits behind the whole name and glyph, far wider than a thousands-
    separator space ever is.

    The gap alone was measured not to be enough of a guard: on the siege
    grid's icons, a stray OCR misread of the weapon-glyph graphic itself
    ("#8", from Tesseract trying to read icon art as text) sat 13px from a
    real count - closer than some genuine split-number gaps - and got fused
    into it, turning a correct 55,965 into 855,965. The fix is what a
    thousands separator actually means: every group after the first is
    exactly three digits, never more or fewer. A stray single digit merging
    into an already-complete number fails that test and is left alone.
    """
    if not words:
        return words
    merged = [words[0]]
    for word in words[1:]:
        prev = merged[-1]
        gap = word.left - prev.right
        height = max(prev.bottom - prev.top, 1)
        incoming_digits = word.text.replace(",", "").replace(".", "").replace(" ", "")
        if 0 <= gap <= height * 0.6 and len(incoming_digits) == 3:
            merged[-1] = _Word(
                text=f"{prev.text} {word.text}",
                left=prev.left,
                top=min(prev.top, word.top),
                right=word.right,
                bottom=max(prev.bottom, word.bottom),
            )
        else:
            merged.append(word)
    return merged


# --------------------------------------------------------------------------- #
# Low-level OCR helpers
# --------------------------------------------------------------------------- #

def _words(image: Image.Image, config: str) -> list[_Word]:
    data = pytesseract.image_to_data(image, config=config, output_type=Output.DICT)
    found: list[_Word] = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < _MIN_CONF:
            continue
        left, top = data["left"][i], data["top"][i]
        found.append(_Word(text, left, top, left + data["width"][i], top + data["height"][i]))
    return found


def _group_lines(words: list[_Word]) -> list[_Line]:
    words = sorted(words, key=lambda w: (w.top, w.left))
    lines: list[_Line] = []
    current: list[_Word] = []
    for word in words:
        if current:
            reference = current[0]
            height = max(reference.bottom - reference.top, 1)
            if abs(word.top - reference.top) > max(height * 0.6, 6):
                lines.append(_Line(sorted(current, key=lambda w: w.left)))
                current = []
        current.append(word)
    if current:
        lines.append(_Line(sorted(current, key=lambda w: w.left)))
    return lines


# --------------------------------------------------------------------------- #
# Layout interpretation
# --------------------------------------------------------------------------- #

# Rarity colour of the portrait backdrop -> tier. Confirmed in game.
_TIER_BY_COLOUR = {"GREY": "T1", "GREEN": "T2", "BLUE": "T3", "PURPLE": "T4", "GOLD": "T5"}

# Sample a strip across the upper portrait: above the character's head, inside the
# frame. Measured against known units - a central band reads the Battering Ram's
# brown wooden art as gold and turns a T1 into a T5.
_PORTRAIT_INSET = 0.20
_PORTRAIT_BAND = (0.14, 0.34)
# A real backdrop dominates its sample. Anything less decisive is character
# art or a drifting crop, and a WRONG tier is far worse than no tier: an
# unknown tier only makes the fill check ask for more, while a T1 read as
# T5 would wave a padded hospital straight through.
_PORTRAIT_MIN_SHARE = 0.55

# The fitted column's box is never measured against this specific row - it is
# the pooled median geometry from whichever OTHER rows had a clean frame.
# That is fine for a row whose own frame just fragmented in the OCR pass, but
# a row that is genuinely clipped by the game's own modal layout (the cost
# strip starting partway down its portrait) has no real frame at that
# position at all, and tier_from_portrait() would happily score whatever
# colour happens to sit there - measured on a real production screenshot to
# read a confident, wrong tier from a portrait that was actually cut off by
# the "96.1M / 70.2M / ..." resource strip below it. Requiring an actual
# frame ring at the fitted position before trusting it closes that gap: a
# genuine portrait's four edges are mostly gold bevel even when interior
# detection failed; a box sitting over UI chrome is not.
_FIT_BOX_MIN_FRAME_SHARE = 0.4


def _fit_box_has_frame(source: Image.Image, box: tuple[int, int, int, int]) -> bool:
    """Whether a fitted-column box actually has a frame ring at that spot."""
    left, top, right, bottom = box
    if right - left < 10 or bottom - top < 10:
        return False
    left = max(0, left)
    top = max(0, top)
    right = min(source.width, right)
    bottom = min(source.height, bottom)
    if right - left < 10 or bottom - top < 10:
        return False
    pixels = source.load()
    hits = 0
    total = 0
    for x in range(left, right):
        for y in (top, bottom - 1):
            total += 1
            if _is_frame(*pixels[x, y][:3]):
                hits += 1
    for y in range(top, bottom):
        for x in (left, right - 1):
            total += 1
            if _is_frame(*pixels[x, y][:3]):
                hits += 1
    return total > 0 and hits / total >= _FIT_BOX_MIN_FRAME_SHARE


def _name_start(line: "_Line") -> int | None:
    """Left edge of the unit name, skipping the weapon glyph and OCR noise."""
    for word in line.words:
        if sum(ch.isalpha() for ch in word.text) >= 3:
            return word.left
    return None


def _is_frame(r: int, g: int, b: int) -> bool:
    """The gold ring drawn around every unit portrait.

    The ring is a 3D bevel, not a flat colour: a bright yellow highlight around
    hue 47-51 and a darker orange shadow around hue 31-37. Accepting only the
    highlight found one or two pixels per scan row, so the frame broke into a top
    arc and a bottom arc and the portrait could not be located.
    """
    hue, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return val > 0.28 and sat > 0.45 and 25 <= hue * 360 <= 68


def _portrait_box(
    colour: Image.Image, line: "_Line", scale: float, unit: float
) -> tuple[int, int, int, int] | None:
    """Locate this row's portrait by finding its gold frame.

    Estimating the box as a fixed offset from the name text was measured wrong:
    how far the name starts from the portrait depends on the weapon glyph and on
    whatever OCR noise precedes it, and being 20px out lands the sample on the
    blue panel and reads every unit as T3. The frame is a bright yellow ring and
    is unmistakable, so it is searched for instead.
    """
    name_left = _name_start(line)
    if name_left is None or unit <= 0:
        return None

    centre_y = ((line.top + line.bottom) / 2) / scale
    search_right = int(name_left / scale)
    search_left = max(0, int(search_right - unit * 3.2))
    # The name text sits above the portrait's centre, so the search band is
    # nudged down to be sure it crosses the frame's vertical sides.
    band_top = max(0, int(centre_y - unit * 0.05))
    band_bottom = min(colour.height, int(centre_y + unit * 0.75))
    if search_right - search_left < 8 or band_bottom - band_top < 2:
        return None

    pixels = colour.load()
    hits = []
    for x in range(search_left, min(search_right, colour.width)):
        count = sum(
            1 for y in range(band_top, band_bottom) if _is_frame(*pixels[x, y][:3])
        )
        if count >= max(1, (band_bottom - band_top) // 10):
            hits.append(x)
    if len(hits) < 2:
        return None

    # Cluster into contiguous runs before measuring width. A stray blob of
    # frame-coloured pixels elsewhere in the search band - OCR noise read to
    # the left of the name, a neighbour's edge - must not merge with the real
    # frame just because both are "hits": taking hits[0] to hits[-1] blindly
    # once inflated a clean 66px frame into a bogus 117px span (a noise blob
    # 44px away from it) and rejected an otherwise-correct read.
    #
    # The gap tolerance is scaled to the portrait size, not a fixed pixel
    # count: the ring itself is often broken into two or three pieces by the
    # count text overlapping it, with gaps of ~15-20px on a ~50px unit, and
    # those pieces are one frame, not several. A stray blob sits much further
    # away in practice (fully outside the portrait), so a mid-sized tolerance
    # merges genuine fragments without also merging unrelated noise.
    gap_tolerance = max(3, unit * 0.5)
    runs: list[list[int]] = [[hits[0]]]
    for x in hits[1:]:
        if x - runs[-1][-1] <= gap_tolerance:
            runs[-1].append(x)
        else:
            runs.append([x])

    # The frame sits immediately left of the name, so among runs whose width
    # is plausible, prefer the one closest to it.
    plausible = [r for r in runs if unit * 0.7 <= (r[-1] - r[0]) <= unit * 2.2]
    if not plausible:
        return None
    run = max(plausible, key=lambda r: r[-1])
    left, right = run[0], run[-1]
    width = right - left

    # The portrait is a shield, taller than it is wide, and the name text sits
    # above its centre - so the vertical extent is found the same way as the
    # horizontal one rather than assumed from the row's text position.
    scan_top = max(0, int(centre_y - unit * 1.3))
    scan_bottom = min(colour.height, int(centre_y + unit * 1.3))
    rows_with_frame = []
    for y in range(scan_top, scan_bottom):
        count = sum(1 for x in range(left, right) if _is_frame(*pixels[x, y][:3]))
        if count >= 2:
            rows_with_frame.append(y)
    if len(rows_with_frame) < 2:
        return None

    # Rows sit close together, so the scan sees the neighbouring portraits' frames
    # too. Taking first-to-last merges two portraits into one over-tall box whose
    # sample lands between them; group into runs and keep the one belonging to
    # this row instead.
    runs: list[list[int]] = []
    current: list[int] = []
    gap = max(2, int(unit * 0.15))
    for y in rows_with_frame:
        if current and y - current[-1] > gap:
            runs.append(current)
            current = []
        current.append(y)
    if current:
        runs.append(current)

    # The portrait hangs below the name text rather than centring on it.
    target = centre_y + unit * 0.45
    runs = [r for r in runs if len(r) >= 2]
    if not runs:
        return None
    run = min(runs, key=lambda r: abs((r[0] + r[-1]) / 2 - target))

    top, bottom = run[0], run[-1]
    height = bottom - top
    # A portrait is slightly taller than it is wide. Anything else is a merged or
    # clipped run, and reading a tier from it would be guesswork.
    if not (width * 0.85 <= height <= width * 1.7):
        return None
    return int(left), int(top), int(right), int(bottom)


def _colour_bucket(r: int, g: int, b: int) -> str | None:
    hue, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    degrees = hue * 360
    if val < 0.18:
        return None
    if val > 0.72 and sat > 0.55 and 38 <= degrees <= 62:
        return None
    if sat < 0.25:
        return "GREY"
    if degrees < 70 or degrees >= 330:
        return "GOLD"
    if degrees < 170:
        return "GREEN"
    if degrees < 258:
        return "BLUE"
    return "PURPLE"


def _dominant_colour(source: Image.Image, x0: int, x1: int, y0: int, y1: int) -> str | None:
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    pixels = source.load()
    counts: dict[str, int] = {}
    for x in range(max(0, x0), min(source.width, x1)):
        for y in range(max(0, y0), min(source.height, y1)):
            key = _colour_bucket(*pixels[x, y][:3])
            if key:
                counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def tier_from_portrait(
    source: Image.Image,
    box: tuple[int, int, int, int],
    *,
    inset: float = _PORTRAIT_INSET,
    band: tuple[float, float] = _PORTRAIT_BAND,
    check_outside: bool = True,
) -> str | None:
    """Tier from the portrait's rarity colour, or None if it is not clear.

    `inset`/`band` default to the wounded-list's own geometry but can be
    overridden - a different screen's icon can put the character art at a
    different height within the frame, and sampling the wrong band there
    just means more art and less backdrop in the sample. Measured on the
    Troop Details grid screen: the default band's dominant-colour share came
    out under the confidence threshold (47%) purely from being the wrong
    band for that screen's proportions, not from an unclear reading - a
    higher, tighter band (closer to the frame's top edge) sampled the same
    icons at 70-80% for every tier tried. `check_outside` disables the
    "does this match the panel just outside the frame" guard, which assumes
    there is panel there - not true in a packed grid, where the neighbouring
    cell can sit immediately alongside.
    """
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    if width < 10 or height < 10:
        return None

    x0 = max(0, int(left + width * inset))
    x1 = min(source.width, int(right - width * inset))
    y0 = max(0, int(top + height * band[0]))
    y1 = min(source.height, int(top + height * band[1]))
    if x1 - x0 < 3 or y1 - y0 < 2:
        return None

    pixels = source.load()
    counts: dict[str, int] = {}
    for x in range(x0, x1):
        for y in range(y0, y1):
            value = pixels[x, y]
            r, g, b = value[:3] if isinstance(value, tuple) else (value, value, value)
            hue, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            degrees = hue * 360
            if val < 0.18:
                continue
            # The frame is a bright, strongly saturated yellow ring; the T5
            # backdrop is a duller amber, so this drops the frame but not T5.
            if val > 0.72 and sat > 0.55 and 38 <= degrees <= 62:
                continue
            if sat < 0.25:
                key = "GREY"
            elif degrees < 70 or degrees >= 330:
                key = "GOLD"
            elif degrees < 170:
                key = "GREEN"
            elif degrees < 258:
                key = "BLUE"
            else:
                key = "PURPLE"
            counts[key] = counts.get(key, 0) + 1

    if not counts:
        return None
    winner, hits = max(counts.items(), key=lambda kv: kv[1])

    # If the sample matches the panel just outside the frame, the box missed the
    # portrait and we are reading the window background. The panel is blue, so
    # without this every missed crop confidently reports T3.
    if check_outside:
        outside = _dominant_colour(
            source, max(0, left - int(width * 0.55)), max(0, left - int(width * 0.12)), y0, y1
        )
        if outside is not None and outside == winner:
            return None
    # A backdrop shows as one clear colour. Anything muddier is character art or a
    # bad crop, and guessing a tier from it would be worse than admitting ignorance.
    if hits / sum(counts.values()) < _PORTRAIT_MIN_SHARE:
        return None
    return _TIER_BY_COLOUR[winner]


def _match_unit(line_text: str, known_names: list[str]) -> str | None:
    """Longest known unit name appearing as whole words in the line.

    A name preceded by another word is rejected: "Royal Crossbowman" is a T4
    archer, and matching it as the T3 "Crossbowman" would score it at the wrong
    tier and the wrong power. Better to flag it and let an admin teach the real
    name than to record a confident wrong answer.
    """
    padded = " " + line_text + " "
    for name in known_names:  # pre-sorted longest-first
        idx = padded.find(" " + name + " ")
        if idx == -1:
            continue
        # "Battering Ram Zone" is a section header, not the Battering Ram unit.
        if padded[idx + len(name) + 2:].strip().startswith("zone"):
            continue
        before = padded[:idx].split()
        if before and before[-1].isalpha() and len(before[-1]) >= 4:
            continue
        return name
    return None


def _read_totals(
    lines: list[_Line], reading: Reading
) -> tuple[int | None, _Word | None, _Word | None]:
    """Fill in the current/capacity pairs and return the layout anchors.

    Returns (button y, wounded-total word, ram-zone-total word). Those two words
    are the one landmark that reads reliably at every crop and zoom level, so the
    cost strip is measured from them. They are identified by which total they are,
    not by document order - a stray slash elsewhere in the window would otherwise
    be mistaken for one of them and corrupt the scale.
    """
    button_top: int | None = None
    pending: str | None = None
    wounded_word: _Word | None = None
    ram_word: _Word | None = None

    for line in lines:
        flat = line.flat
        if button_top is None and any(word in flat for word in _BUTTON_WORDS):
            button_top = line.top

        if "/" in line.text:
            current, capacity = parse_fraction(line.text)
            # The alliance auto-heal banner ("<name> auto-helped heal your
            # units. 10/30") prints its own small fraction - out of a fixed
            # 30 help slots, a game constant independent of language or
            # hospital level. Production report: with no "pending" label in
            # front of it (nothing in this window calls it out by name the
            # way "Battering Ram Zone" does), that fraction fell into the
            # capacity-based default below and got recorded as the ram-zone
            # total, corrupting the whole read - every row after depended on
            # it for scale. A real wounded/ram-zone capacity is never this
            # small (the smallest seen in practice is in the tens of
            # thousands), so a floor here is a safe, language-independent
            # way to reject the helper meter without ever having to
            # string-match its wording.
            if capacity is not None and capacity < 1000:
                continue
            if current is not None and capacity is not None:
                anchor = next(
                    (w for w in line.words if "/" in w.text),
                    min(line.words, key=lambda w: w.left),
                )
                label = pending or ("ram" if capacity <= 100_000 else "wounded")
                if label == "ram" and reading.ram_current is None:
                    reading.ram_current, reading.ram_capacity = current, capacity
                    ram_word = anchor
                elif label == "wounded" and reading.wounded_current is None:
                    reading.wounded_current, reading.wounded_capacity = current, capacity
                    wounded_word = anchor
                pending = None
                continue

        if "battering ram zone" in flat:
            pending = "ram"
        elif "severely wounded" in flat:
            pending = "wounded"

    return button_top, wounded_word, ram_word


def _count_word(line: "_Line") -> "_Word | None":
    """The troop count on a row: the number printed beside the unit name.

    NOT the boxed number at the right of the row - that is the heal-amount
    slider, which the player can drag down. Reading it would report less than
    the hospital actually holds. The two agree only while the slider is at max.

    Among the numbers to the right of where the name starts (so a tier
    numeral OCR'd out of the portrait cannot be mistaken for the count),
    picks the LARGEST rather than the first. A weapon/type glyph icon
    between the name and the real count can itself get OCR'd as noise -
    seen elsewhere in this file as a stray token next to a real number - and
    when that noise lands to the right of the name instead of the left, the
    first-numeric-word rule used to hand back a 1-2 digit fragment instead
    of the real count. The largest number after the name is always the real
    count in practice: glyph noise never OCRs as a multi-digit number
    anywhere near the size of an actual troop count, and the only other
    candidate on this line (the heal slider) is capped at the real count,
    never above it.
    """
    name_left = _name_start(line)
    if name_left is None:
        return None
    after_name = [w for w in line.numeric_words() if w.left > name_left]
    if not after_name:
        return None
    def _value(word: "_Word") -> int:
        value, _ = parse_number(word.text)
        return value if value is not None else -1
    return max(after_name, key=_value)


@dataclass
class _ColumnFit:
    """Portrait geometry fitted once per screenshot, in source pixels.

    Everything here is measured from the screenshot itself rather than assumed,
    so it holds at any resolution: the two totals in the corner panel give the
    scale unit, and the frame scan gives the rest.
    """

    left: int
    right: int
    offset: float   # portrait centre minus the row's text centre
    height: float

    def box(self, centre_y: float) -> tuple[int, int, int, int]:
        top = centre_y + self.offset - self.height / 2
        return int(self.left), int(top), int(self.right), int(top + self.height)


def _frame_runs(
    pixels, left: int, right: int, top: int, bottom: int, height: int, gap: int
) -> list[tuple[int, int]]:
    """Contiguous vertical runs of frame pixels between two columns."""
    rows = []
    for y in range(max(0, top), max(0, min(height, bottom))):
        hits = sum(1 for x in range(left, right) if _is_frame(*pixels[x, y][:3]))
        if hits >= 2:  # a mid-portrait row crosses only the two thin sides
            rows.append(y)
    runs: list[tuple[int, int]] = []
    current: list[int] = []
    for y in rows:
        if current and y - current[-1] > gap:
            runs.append((current[0], current[-1]))
            current = []
        current.append(y)
    if current:
        runs.append((current[0], current[-1]))
    return runs


def _fit_portrait_column(
    colour: Image.Image, centres: list[float], name_left: float, unit: float
) -> _ColumnFit | None:
    """Fit the portrait column using every row at once.

    Detecting each portrait on its own was the weak point: one row's frame
    fragments or merges with its neighbour's and that row silently loses its
    tier. The portraits share an x range and a fixed offset from their row's
    text, so those are fitted from all rows together and a row with a faint
    frame inherits the geometry.

    The search is anchored to where the names start rather than scanning the
    whole width, because the wooden bed in the window artwork is the same gold
    as the frame and dominates a global scan.
    """
    if not centres or unit <= 0:
        return None

    pixels = colour.load()
    search_left = max(0, int(name_left - unit * 3.2))
    search_right = min(colour.width, int(name_left - unit * 0.05))
    if search_right - search_left < 8:
        return None

    # Column profile over the union of the rows' bands.
    bands = [
        (max(0, int(c - unit * 0.9)), min(colour.height, int(c + unit * 0.9)))
        for c in centres
    ]
    profile = []
    for x in range(search_left, search_right):
        total = 0
        for top, bottom in bands:
            total += sum(1 for y in range(top, bottom) if _is_frame(*pixels[x, y][:3]))
        profile.append((x, total))

    peak = max((n for _, n in profile), default=0)
    if peak < max(4, unit * 0.4):
        return None

    strong = [x for x, n in profile if n >= peak * 0.45]
    if len(strong) < 2:
        return None

    # Cluster the strong columns; the frame's two sides show as two clusters.
    clusters: list[list[int]] = []
    for x in strong:
        if clusters and x - clusters[-1][-1] <= 3:
            clusters[-1].append(x)
        else:
            clusters.append([x])

    weight = dict(profile)
    best_pair = None
    best_score = 0.0
    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            left, right = clusters[i][0], clusters[j][-1]
            width = right - left
            if not (unit * 0.8 <= width <= unit * 2.0):
                continue
            score = sum(weight[x] for x in clusters[i]) + sum(weight[x] for x in clusters[j])
            if score > best_score:
                best_score, best_pair = score, (left, right)

    if best_pair is None:
        # A single wide cluster happens when the frame reads as one blob.
        widest = max(clusters, key=len)
        if unit * 0.8 <= widest[-1] - widest[0] <= unit * 2.0:
            best_pair = (widest[0], widest[-1])
    if best_pair is None:
        return None

    left, right = best_pair
    width = right - left

    # Fit the vertical offset and height from the rows whose frame reads cleanly.
    offsets: list[float] = []
    heights: list[float] = []
    gap = max(2, int(unit * 0.15))
    for centre in centres:
        runs = _frame_runs(
            pixels, left, right,
            int(centre - unit * 1.3), int(centre + unit * 1.3),
            colour.height, gap,
        )
        usable = [r for r in runs if width * 0.8 <= r[1] - r[0] <= width * 1.8]
        if not usable:
            continue
        run = min(usable, key=lambda r: abs((r[0] + r[1]) / 2 - (centre + unit * 0.45)))
        offsets.append((run[0] + run[1]) / 2 - centre)
        heights.append(run[1] - run[0])

    def median(values: list[float], fallback: float) -> float:
        if not values:
            return fallback
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2

    return _ColumnFit(
        left=left,
        right=right,
        offset=median(offsets, unit * 0.45),
        height=median(heights, width * 1.2),
    )


def _read_rows(
    lines: list[_Line],
    known_names: list[str],
    reading: Reading,
    wounded: _Word | None,
    ram: _Word | None,
    source: Image.Image,
    scale: float,
) -> None:
    """Read the unit rows: count, tier from the portrait, type from the glyph.

    Rows whose name is not in the unit table are still recorded, with the text as
    read. T4 and T5 names are civilisation-specific and the game ships in many
    languages, so an unrecognised name is routine - dropping the row would
    quietly remove its troops from the count.

    A targeted digit-only re-read of each row was tried and measured worse: the
    band spans the portrait, the name and the green bar, so a single-line
    whitelist pass fragments the number instead of sharpening it.
    """
    unit = (ram.top - wounded.top) / scale if (wounded and ram) else 0.0

    # --- find the rows ------------------------------------------------------
    candidates: list[tuple[_Line, str | None, int]] = []
    matched_lines: list[_Line] = []
    for line in lines:
        if "/" in line.text:
            continue
        name = _match_unit(line.flat, known_names)
        if not name:
            continue
        word = _count_word(line)
        if word is None:
            continue
        value, _ = parse_number(word.text)
        if value is None:
            continue
        candidates.append((line, name, value))
        matched_lines.append(line)

    # Rows we could not name. Bounded above the cost strip so resource values can
    # never be mistaken for troop rows, which an earlier unbounded version did.
    if wounded is not None and ram is not None and unit > 0:
        list_floor = wounded.top - 2.2 * (ram.top - wounded.top)
        if matched_lines:
            box_column = sum(
                _count_word(l).cx for l in matched_lines if _count_word(l)
            ) / len(matched_lines)
            tolerance = max(unit * 1.2, 25)
        else:
            box_column = None
            tolerance = 0.0

        unnamed: list[tuple[_Line, str, int]] = []
        for line in lines:
            if any(line is l for l in matched_lines):
                continue
            if "/" in line.text or line.top >= list_floor:
                continue
            if _match_unit(line.flat, known_names):
                continue
            numbers = line.numeric_words()
            if not numbers:
                continue
            candidate = numbers[-1]
            if box_column is not None and abs(candidate.cx - box_column) > tolerance:
                continue
            words = [w.text for w in line.words if w.left < candidate.left]
            letters = [w for w in words if sum(c.isalpha() for c in w) >= 3]
            if not letters:
                continue
            value, _ = parse_number(candidate.text)
            if not value:
                continue
            unnamed.append((line, " ".join(letters).strip(), value))

        # With at least one matched row, box_column above already anchors
        # and filters this. With none - every name on screen unrecognised,
        # routine for an untaught civilisation/language - there is no
        # column to check against at all, and this used to accept anything.
        # Production report: a Vietnamese client's fixed play-time warning
        # banner ("18+ ... 180 minutes ...") happened to print a stray "1"
        # that slipped through this gap and got recorded as a phantom
        # 1-troop unit. A real row always has SOME frame-coloured pixels in
        # the portrait area just left of its name, even on a row whose
        # frame is too faint/fragmented for the stricter box-fitting checks
        # to confirm a clean rectangle (measured: requiring an actual fitted
        # box here, tried first, wrongly dropped a genuine row those
        # stricter checks already miss for other reasons - see
        # test_turkish_period_grouped_hospital_counts_read_correctly). Loose
        # density is enough to tell the two apart: the real row measured at
        # 35-40% in that area on real screenshots, the banner text at
        # exactly 0% - nothing frame-coloured anywhere near it at all.
        if box_column is None and unnamed:
            kept = []
            for line, letters, value in unnamed:
                name_left = _name_start(line)
                if name_left is None:
                    continue
                name_left = name_left / scale
                left = max(0, int(name_left - unit * 3.2))
                right = max(0, int(name_left - unit * 0.05))
                centre = (line.top + line.bottom) / 2 / scale
                top = max(0, int(centre - unit * 0.9))
                bottom = int(centre + unit * 0.9)
                if right - left < 3 or bottom - top < 3:
                    continue
                pixels = source.load()
                hits = sum(
                    1
                    for x in range(left, right)
                    for y in range(top, bottom)
                    if _is_frame(*pixels[x, y][:3])
                )
                total = (right - left) * (bottom - top)
                if total and hits / total >= 0.08:
                    kept.append((line, letters, value))
            unnamed = kept

        candidates.extend(unnamed)

    if not candidates:
        return

    # --- fit the portrait column once, from all the rows --------------------
    centres = [((l.top + l.bottom) / 2) / scale for l, _, _ in candidates]
    name_lefts = [
        _name_start(l) / scale for l, _, _ in candidates if _name_start(l) is not None
    ]
    fit = None
    if name_lefts and unit > 0:
        # The minimum was tried and measured wrong: _name_start takes the
        # first word with 3+ letters, so one row with OCR noise ahead of its
        # real name (a clipped row often reads as garbage like "Cee)") drags
        # the shared search window for every row leftward, off the actual
        # portraits and onto unrelated art - which is how a clipped row once
        # got the whole screenshot's fallback tier reading gold from a wooden
        # bed frame. The median survives that as long as fewer than half the
        # rows are corrupted, which is the case that matters here.
        ordered = sorted(name_lefts)
        mid = len(ordered) // 2
        anchor = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        fit = _fit_portrait_column(source, centres, anchor, unit)

    # --- read each row ------------------------------------------------------
    for (line, name, value), centre in zip(candidates, centres):
        # Per-row detection first: measured at zero wrong tiers, which is the
        # property that matters - a wrong tier can pass a padded hospital, an
        # absent one only asks for another screenshot. The fitted column is used
        # only where the per-row scan found nothing, so it can add coverage but
        # never overrule a reading that already worked.
        box = _portrait_box(source, line, scale, unit)
        tier = tier_from_portrait(source, box) if box else None
        if tier is None and fit is not None:
            fitted = fit.box(centre)
            # The fitted box is pooled geometry, never confirmed against this
            # row - a row whose portrait is genuinely clipped (by the game's
            # own UI, not the screenshot edge) has no real frame there at all,
            # and tier_from_portrait() would score whatever happens to sit in
            # that box. Require an actual frame ring first.
            if _fit_box_has_frame(source, fitted):
                tier = tier_from_portrait(source, fitted)
            box = box or fitted

        troop_type = None
        name_left = _name_start(line)
        if box is not None and name_left is not None and unit > 0:
            glyph_box = (
                box[2] + 2,
                int(centre - unit * 0.42),
                int(name_left / scale) - 2,
                int(centre + unit * 0.42),
            )
            troop_type = glyph_table().classify(glyph_mask(source, glyph_box))

        reading.rows.append(
            TroopRow(raw_name=name or "", count=value, tier=tier, troop_type=troop_type)
        )


# Where each cost value sits, measured from the wounded-total word and expressed
# in units of the gap between the two totals. Stable across every sample
# screenshot, from a tight 814px crop to a 1194px full-window capture.
_COST_ANCHORS = (6.80, 9.60, 12.40, 15.35)
_COST_LABELS = ("food", "wood", "stone", "gold")
_COST_TOLERANCE = 1.2


def resource_from_icon(
    colour: Image.Image, x0: int, x1: int, y0: int, y1: int
) -> str | None:
    """Which resource the icon beside a cost value is.

    Read off the icons themselves: corn is yellow with green leaves, the wood log
    is orange, stone is grey-white, the gold coin is yellow with an orange rim.
    Panel-coloured pixels are discarded first, since the icon never fills its box.

    This replaces working the columns out from the units' troop types, which
    needed the unit name - and a name is worthless across 20 civilisations and
    every language the game ships in.
    """
    pixels = colour.load()
    counts = {"grey": 0, "orange": 0, "yellow": 0, "green": 0}
    for x in range(max(0, x0), min(colour.width, x1)):
        for y in range(max(0, y0), min(colour.height, y1)):
            r, g, b = pixels[x, y][:3]
            hue, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if val < 0.30:
                continue
            # The value text is pure white (sat 0.00, val 1.00) while the stone
            # sprite is a dimmer grey-blue (sat ~0.17, val ~0.78). Without this
            # the digits count as stone, every token claims that column, and the
            # vote ties - which blanked stone on every screenshot.
            if sat < 0.10 and val > 0.88:
                continue
            degrees = hue * 360
            if sat < 0.22:
                counts["grey"] += 1
            elif degrees < 40:
                counts["orange"] += 1
            elif degrees < 70:
                counts["yellow"] += 1
            elif degrees < 170:
                counts["green"] += 1
            # 170-260 is the panel behind the icon; ignored.

    # An icon fills roughly half its box; the rest is panel, which is not counted.
    # Requiring a real share of non-panel pixels stops a stray patch of the window
    # artwork - the wooden bed sits just left of the strip - reading as wood.
    area = max(1, (min(colour.width, x1) - max(0, x0)) * (min(colour.height, y1) - max(0, y0)))
    total = sum(counts.values())
    if total < 60 or total < area * 0.18:
        return None
    grey, orange, yellow, green = (counts[k] / total for k in
                                   ("grey", "orange", "yellow", "green"))
    if grey > 0.50:
        return "stone"
    if green > 0.12:
        return "food"
    if yellow > orange:
        return "gold"
    if orange > 0.45:
        return "wood"
    return None


def _read_costs(
    source: Image.Image,
    colour: Image.Image,
    scale: float,
    wounded: _Word | None,
    ram: _Word | None,
    reading: Reading,
) -> None:
    """Read the cost strip by measuring from the two totals in the corner panel.

    The strip is small white text on a dark gradient, which a full-window pass
    reads badly. Cropping just that band and re-reading it several ways gets the
    values; each is matched to a resource by the order they appear, against the
    set of resources this hospital's units actually use - so a hospital of only
    cavalry (food and stone, no wood) is not read as food and wood.
    """
    if wounded is None or ram is None:
        return

    # Both totals are the same size on screen, so unequal box heights mean one box
    # is inflated and its top cannot be trusted. Measuring the scale from it would
    # shift every cost value into the wrong column, so this candidate reads no
    # costs at all and lets a better-segmented one supply them.
    wounded_height = wounded.bottom - wounded.top
    ram_height = ram.bottom - ram.top
    if min(wounded_height, ram_height) <= 0:
        return
    if max(wounded_height, ram_height) / min(wounded_height, ram_height) > 1.35:
        return

    # Anchors are in the (upscaled) layout image; the strip is read from the
    # original instead. Cropping the already-2x variant and enlarging again
    # over-magnifies the glyphs and Tesseract starts fusing them together.
    origin_y = wounded.top / scale
    origin_x = wounded.left / scale
    unit = (ram.top - wounded.top) / scale
    if unit < 6:
        return

    box = (
        max(0, int(origin_x + 5.0 * unit)),
        max(0, int(origin_y - 1.8 * unit)),
        min(source.width, int(origin_x + 20.0 * unit)),
        max(0, int(origin_y - 0.2 * unit)),
    )
    if box[2] - box[0] < 20 or box[3] - box[1] < 6:
        return

    crop = source.crop(box)

    # Each zoom/polarity pass is an independent read of the same pixels. Every
    # value is labelled by the icon beside it, so votes are counted per resource
    # and a badly-read digit costs only its own column.
    votes: dict[str, dict[int, int]] = {}
    approximate = False

    for zoom in (2, 3, 4, 5):
        scaled = crop.resize((crop.width * zoom, crop.height * zoom), Image.LANCZOS)
        for big in (
            scaled,
            ImageOps.invert(scaled),
            ImageOps.autocontrast(scaled),
            scaled.filter(ImageFilter.SHARPEN),
        ):
            for psm in (6, 7, 11):
                config = f"--psm {psm} -c tessedit_char_whitelist=" + _COST_CHARS
                try:
                    data = pytesseract.image_to_data(
                        big, config=config, output_type=Output.DICT
                    )
                except Exception:
                    continue

                # Tesseract reports conf -1 for whitelisted output, so confidence is
                # not a usable filter here - dropping those read this strip as empty.
                tokens = []
                for i, text in enumerate(data["text"]):
                    text = text.strip()
                    if text and parse_number(text)[0] is not None:
                        tokens.append((
                            box[0] + data["left"][i] / zoom,
                            box[1] + data["top"][i] / zoom,
                            max(data["height"][i] / zoom, 6.0),
                            text,
                        ))

                # The game never prints a bare decimal - "17.7" means the K or M
                # was lost by OCR, and reading it as 177 understates the cost by
                # six orders of magnitude. Recover it from the strip's other values.
                suffixes = [x[-1] for _, _, _, x in tokens if x[-1] in "KkMmBb"]
                fallback = max(set(suffixes), key=suffixes.count) if suffixes else ""

                for tx, ty, th, text in tokens:
                    # Tesseract's word box starts at the icon, not at the first
                    # digit - the sprite is inside the word it reports. So the
                    # icon is looked for at the token's own left edge, and a
                    # little before it, rather than in the gap further left.
                    resource = None
                    for a, b in ((-0.10, 0.55), (-0.85, -0.08), (-0.45, 0.25)):
                        resource = resource_from_icon(
                            colour,
                            int(tx + unit * a),
                            int(tx + unit * b),
                            int(ty - th * 0.35),
                            int(ty + th * 1.35),
                        )
                        if resource is not None:
                            break
                    if resource is None:
                        continue
                    if fallback and "." in text and text[-1] not in "KkMmBb":
                        text += fallback
                    value, approx = parse_number(text)
                    if value is None:
                        continue
                    approximate = approximate or approx
                    tally = votes.setdefault(resource, {})
                    tally[value] = tally.get(value, 0) + 1

    resolved: list[int | None] = [None, None, None, None]
    for resource, tally in votes.items():
        best = max(tally.values())
        winners = [v for v, n in tally.items() if n == best]
        # A resource the strip does not show can still pick up a single stray
        # read. Requiring corroboration from a second pass drops those without
        # costing anything a real value would have.
        if len(winners) == 1 and best >= 2:
            resolved[_COST_LABELS.index(resource)] = winners[0]

    # What the strip actually showed, which is what "complete" means for a cost.
    reading.expected_resources = [r for r in _COST_LABELS if r in votes]

    if any(v is not None for v in resolved):
        reading.food, reading.wood, reading.stone, reading.gold = resolved
        reading.rss_approx = approximate
        missing = [
            label for label, value in zip(_COST_LABELS, resolved)
            if label in reading.expected_resources and value is None
        ]
        if missing:
            reading.warnings.append("Could not read the " + ", ".join(missing) + " cost.")


def _read(
    variant: Image.Image,
    source: Image.Image,
    colour: Image.Image,
    lines: list[_Line],
    known_names: list[str],
) -> Reading:
    reading = Reading(source="tesseract")
    _button_top, wounded, ram = _read_totals(lines, reading)
    # Portraits are classified by rarity colour, so they need the colour image;
    # the cost strip is read from the greyscale one.
    scale = variant.width / colour.width
    _read_rows(lines, known_names, reading, wounded, ram, colour, scale)
    _read_costs(source, colour, scale, wounded, ram, reading)
    return reading


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def _variants(image: Image.Image) -> list[Image.Image]:
    """Grayscale and upscaled, plus an inverted copy.

    The window mixes white-on-dark (unit names) with dark-on-light (the boxed
    counts); Tesseract is trained for the latter, so both polarities get a turn.
    """
    base = ImageOps.autocontrast(image.convert("L"))
    if max(base.size) < 1600:
        base = base.resize((base.width * 2, base.height * 2), Image.LANCZOS)
    return [base, ImageOps.invert(base)]


def read_all(image_bytes: bytes, known_names: list[str]) -> list[Reading]:
    """Every candidate reading Tesseract can produce, most-rows-found first."""
    if pytesseract is None:
        raise TesseractUnavailable("pytesseract is not installed")

    image = Image.open(io.BytesIO(image_bytes))
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    names = sorted(known_names, key=lambda n: (-len(n.split()), -len(n)))
    candidates: list[Reading] = []
    last_error: Exception | None = None
    colour = image if image.mode == "RGB" else image.convert("RGB")
    source = image.convert("L")

    for variant in _variants(image):
        for config in _LAYOUT_CONFIGS:
            try:
                lines = _group_lines(_words(variant, config))
                candidates.append(
                    _read(variant, source, colour, lines, names)
                )
            except Exception as exc:  # binary missing, bad crop, ...
                last_error = exc

    if not candidates:
        raise TesseractUnavailable("Tesseract produced no output: " + str(last_error))

    _share_costs(candidates)
    candidates.sort(key=lambda r: (-len(r.rows), -sum(v is not None for v in r.rss.values())))
    return candidates


def _share_costs(candidates: list[Reading]) -> None:
    """Fill each candidate's missing cost values from the others.

    Every candidate read the same fixed region of the same image, so a value one
    segmentation mode found is as good as another's. Where two candidates disagree
    the value is dropped, not arbitrated - a blank cell is recoverable, a wrong
    one is not.
    """
    fields = ("food", "wood", "stone", "gold")
    agreed: dict[str, int | None] = {}
    for field in fields:
        seen = {getattr(c, field) for c in candidates if getattr(c, field) is not None}
        agreed[field] = seen.pop() if len(seen) == 1 else None

    approx = any(c.rss_approx for c in candidates)
    for candidate in candidates:
        filled = False
        for field in fields:
            if getattr(candidate, field) is None and agreed[field] is not None:
                setattr(candidate, field, agreed[field])
                filled = True
        if filled:
            candidate.rss_approx = candidate.rss_approx or approx
            candidate.warnings = [
                w for w in candidate.warnings if not w.startswith("Could not read the ")
            ]
            missing = [f for f in fields if getattr(candidate, f) is None]
            if missing:
                candidate.warnings.append(
                    "Could not read the " + ", ".join(missing) + " cost."
                )
