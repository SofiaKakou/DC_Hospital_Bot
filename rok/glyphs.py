"""Troop type from the weapon glyph beside the unit name.

The glyph - sword, horse, bow, ram - is the same drawing for every civilisation
and every language, which the unit's name and portrait art are not. It sits
between the portrait and the name, so both edges of its box are already known.

It disappears once a heal has been started. That is expected: those rows are
recorded without a type rather than guessed at.

Templates are 32x32 bitmaps cut from real screenshots and stored in
data/glyphs.json; matching is plain pixel agreement after normalisation, which
is enough for four shapes this distinct.
"""
from __future__ import annotations

import colorsys
import json
from pathlib import Path

from PIL import Image

SIZE = 32
# Below this, the best match is not convincingly better than the runner-up and
# the type is left unknown. A wrong type is worse than none: nothing depends on
# type, so an absent one costs nothing at all.
MIN_SCORE = 0.66
# The runner-up has to be clearly behind. When the portrait box is fitted rather
# than detected the glyph window can sit slightly off, and a loose margin let a
# sword match the horse template. Type is informational, so an unread one costs
# nothing - except for Siege, which decides ram-zone membership.
MIN_MARGIN = 0.08


def glyph_mask(colour: Image.Image, box: tuple[int, int, int, int]) -> list[str] | None:
    """Normalise the glyph to a SIZE x SIZE bitmap of its bright pixels.

    The glyph is near-white on the dark panel, so brightness separates it. The
    mask is cropped to its own bounding box before scaling, which makes the
    result independent of where in the box the glyph sits and of the
    screenshot's resolution.
    """
    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(colour.width, x1), min(colour.height, y1)
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None

    crop = colour.crop((x0, y0, x1, y1)).convert("RGB")
    pixels = crop.load()
    on: list[tuple[int, int]] = []
    for x in range(crop.width):
        for y in range(crop.height):
            r, g, b = pixels[x, y]
            _h, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            # Near-white: bright and barely saturated. The panel behind is a
            # strong blue, so this separates cleanly.
            if val > 0.62 and sat < 0.35:
                on.append((x, y))

    if len(on) < 20:
        return None
    left = min(p[0] for p in on)
    right = max(p[0] for p in on)
    top = min(p[1] for p in on)
    bottom = max(p[1] for p in on)
    if right - left < 3 or bottom - top < 3:
        return None

    filled = set(on)
    width, height = right - left + 1, bottom - top + 1
    rows = []
    for gy in range(SIZE):
        row = []
        for gx in range(SIZE):
            sx = left + int(gx * width / SIZE)
            sy = top + int(gy * height / SIZE)
            row.append("#" if (sx, sy) in filled else ".")
        rows.append("".join(row))
    return rows


def _agreement(a: list[str], b: list[str]) -> float:
    same = sum(1 for ra, rb in zip(a, b) for ca, cb in zip(ra, rb) if ca == cb)
    return same / float(SIZE * SIZE)


class GlyphTable:
    def __init__(self, path: Path):
        self.path = path
        self.templates: dict[str, list[list[str]]] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for troop_type, patterns in raw.get("templates", {}).items():
                self.templates[troop_type] = [list(p) for p in patterns]

    @property
    def types(self) -> list[str]:
        return sorted(self.templates)

    def classify(self, mask: list[str] | None) -> str | None:
        """Best-matching troop type, or None when no template stands out."""
        if not mask or not self.templates:
            return None
        scores = {
            troop_type: max(_agreement(mask, pattern) for pattern in patterns)
            for troop_type, patterns in self.templates.items()
        }
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        best, best_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        if best_score < MIN_SCORE or best_score - runner_up < MIN_MARGIN:
            return None
        return best

    def add(self, troop_type: str, mask: list[str]) -> None:
        self.templates.setdefault(troop_type, []).append(mask)

    def save(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "_README": [
                        "Weapon-glyph templates, 32x32 bitmaps of the icon beside each",
                        "unit name. The glyph is identical across civilisations and",
                        "languages, unlike the unit's name or portrait art.",
                        "Regenerate or extend with tools/learn_glyphs.py.",
                    ],
                    "templates": {k: v for k, v in sorted(self.templates.items())},
                },
                indent=1,
            ),
            encoding="utf-8",
        )
