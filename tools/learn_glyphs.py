"""Cut weapon-glyph templates out of screenshots.

    python tools/learn_glyphs.py                 # rebuild from the known examples
    python tools/learn_glyphs.py --check         # classify every row and report

The glyph is the language- and civilisation-independent way to tell a troop's
type. Templates live in data/glyphs.json; this is how they get there.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rok import config as config_module  # noqa: E402
from rok import ocr  # noqa: E402
from rok.glyphs import GlyphTable, glyph_mask  # noqa: E402
from rok.parse import Reading  # noqa: E402
from rok.units import UnitTable  # noqa: E402

# Rows whose type was read off the glyph by eye. The unit NAME is only used to
# find the row in the screenshot - the template it produces is name-independent.
EXAMPLES = [
    ("12.webp", "long swordsman", "Infantry"),
    ("12.webp", "mamluk", "Cavalry"),
    ("12.webp", "crossbowman", "Archer"),
    ("5.webp", "crossbowman", "Archer"),
    ("6.webp", "long swordsman", "Infantry"),
    ("6.webp", "crossbowman", "Archer"),
    ("9.webp", "heavy cavalry", "Cavalry"),
    ("11.webp", "light cavalry", "Cavalry"),
    ("8.webp", "long swordsman", "Infantry"),
    ("7.webp", "royal crossbowman", "Archer"),
    ("4.webp", "battering ram", "Siege"),
]


def rows_of(image: Image.Image, table: UnitTable):
    """Yield (matched name, glyph box) for every row we can locate."""
    names = sorted(table.units, key=lambda n: (-len(n.split()), -len(n)))
    for variant in ocr._variants(image):
        lines = ocr._group_lines(ocr._words(variant, "--psm 6"))
        reading = Reading()
        _button, wounded, ram = ocr._read_totals(lines, reading)
        if not (wounded and ram):
            continue
        scale = variant.width / image.width
        unit = (ram.top - wounded.top) / scale
        found = []
        for line in lines:
            if "/" in line.text:
                continue
            name = ocr._match_unit(line.flat, names)
            portrait = ocr._portrait_box(image, line, scale, unit)
            name_left = ocr._name_start(line)
            if portrait is None or name_left is None:
                continue
            centre_y = ((line.top + line.bottom) / 2) / scale
            box = (
                portrait[2] + 2,
                int(centre_y - unit * 0.42),
                int(name_left / scale) - 2,
                int(centre_y + unit * 0.42),
            )
            found.append((name, box))
        if found:
            return found
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Classify instead of learn.")
    args = parser.parse_args()

    cfg = config_module.load()
    ocr.configure(cfg.tesseract_cmd)
    table = UnitTable(cfg.units_file)
    glyph_path = ROOT / "data" / "glyphs.json"

    if args.check:
        glyphs = GlyphTable(glyph_path)
        print(f"templates: {', '.join(glyphs.types) or '(none)'}\n")
        agree = disagree = unknown = 0
        for name in sorted(
            {e[0] for e in EXAMPLES} | {"1.webp", "13.webp", "27_different_game_language.webp"}
        ):
            path = ROOT / "tests" / "images" / name
            if not path.exists():
                continue
            image = Image.open(path).convert("RGB")
            for matched, box in rows_of(image, table):
                got = glyphs.classify(glyph_mask(image, box))
                entry = table.lookup(matched) if matched else None
                want = entry.get("type") if entry else None
                if got is None:
                    unknown += 1
                    mark = "unknown"
                elif want is None:
                    mark = "(no table type to compare)"
                elif got == want:
                    agree += 1
                    mark = "ok"
                else:
                    disagree += 1
                    mark = f"DIFFERS from table ({want})"
                print(f"  {name:34} {str(matched):20} glyph={str(got):9} {mark}")
        print(f"\nagree {agree}   differ {disagree}   unknown {unknown}")
        return 0

    glyphs = GlyphTable(glyph_path)
    glyphs.templates.clear()
    learned = 0
    for name, unit_name, troop_type in EXAMPLES:
        path = ROOT / "tests" / "images" / name
        if not path.exists():
            continue
        image = Image.open(path).convert("RGB")
        for matched, box in rows_of(image, table):
            if matched != unit_name:
                continue
            mask = glyph_mask(image, box)
            if mask is None:
                print(f"  {name} {unit_name}: no glyph found")
                continue
            glyphs.add(troop_type, mask)
            learned += 1
            print(f"  {name:12} {unit_name:20} -> {troop_type}")
    glyphs.save()
    print(f"\nLearned {learned} templates across {len(glyphs.types)} types -> {glyph_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
