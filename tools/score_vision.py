"""Score tier and type read from the screenshot alone, with the name table blinded.

    python tools/score_vision.py            # accuracy across the sample set
    python tools/score_vision.py --scales   # same screenshot at several resolutions

The name table is stripped of tiers and types before running, so anything the
reader reports had to come from the portrait colour and the weapon glyph. That
is the whole point: unit names differ per civilisation and per game language,
so they cannot be relied on.

A WRONG reading is the number that matters. An unread tier only makes the fill
check ask for another screenshot; a wrong one could pass a padded hospital.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rok import config as config_module  # noqa: E402
from rok import pipeline  # noqa: E402
from rok.units import UnitTable  # noqa: E402

# Ground truth, read off the badges and glyphs by eye.
TRUTH: dict[str, dict[str, tuple[str, str]]] = {
    "1.webp": {"long swordsman": ("T4", "Infantry"), "teutonic knight": ("T4", "Cavalry"),
               "crossbowman": ("T4", "Archer")},
    "5.webp": {"teutonic knight": ("T4", "Cavalry"), "crossbowman": ("T4", "Archer"),
               "battering ram": ("T1", "Siege")},
    "6.webp": {"long swordsman": ("T4", "Infantry"), "teutonic knight": ("T4", "Cavalry"),
               "crossbowman": ("T4", "Archer")},
    "7.webp": {"royal crossbowman": ("T5", "Archer")},
    "8.webp": {"long swordsman": ("T4", "Infantry")},
    "9.webp": {"heavy cavalry": ("T3", "Cavalry")},
    "11.webp": {"light cavalry": ("T2", "Cavalry")},
    "12.webp": {"long swordsman": ("T4", "Infantry"), "mamluk": ("T4", "Cavalry"),
                "crossbowman": ("T4", "Archer")},
}


def blinded_table(cfg) -> UnitTable:
    raw = json.loads(Path(cfg.units_file).read_text(encoding="utf-8"))
    for entry in raw["units"].values():
        entry.pop("tier", None)
        entry.pop("type", None)
    path = Path(tempfile.gettempdir()) / "rok_units_blinded.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return UnitTable(path)


def read(image_bytes: bytes, cfg, table):
    return pipeline.extract(
        image_bytes, table, anthropic_api_key="", tesseract_cmd=cfg.tesseract_cmd
    )


def score(cfg, table) -> int:
    tallies = {"tier": [0, 0, 0], "type": [0, 0, 0]}  # correct, wrong, unread
    for name, expected in TRUTH.items():
        path = ROOT / "tests" / "images" / name
        if not path.exists():
            continue
        result = read(path.read_bytes(), cfg, table)
        for row in result.rows:
            want = expected.get(row.name.lower())
            for field, index in (("tier", 0), ("type", 1)):
                got = getattr(row, field)
                target = want[index] if want else None
                if got is None:
                    tallies[field][2] += 1
                elif got == target:
                    tallies[field][0] += 1
                else:
                    tallies[field][1] += 1
                    print(f"  WRONG {field:4} {name:10} {row.name:20} {got} != {target}")

    for field, (ok, wrong, unread) in tallies.items():
        print(f"{field.upper():5} correct {ok:>3}   WRONG {wrong:>3}   unread {unread:>3}")
    return 0 if all(t[1] == 0 for t in tallies.values()) else 1


def scales(cfg, table, name: str = "12.webp") -> int:
    """The same screenshot at several sizes must read the same."""
    path = ROOT / "tests" / "images" / name
    if not path.exists():
        print(f"{name} not found")
        return 1
    base = Image.open(path).convert("RGB")
    print(f"{name} native {base.width}x{base.height}\n")

    readings = []
    for factor in (0.65, 0.8, 1.0, 1.35, 1.75):
        image = base if factor == 1.0 else base.resize(
            (int(base.width * factor), int(base.height * factor)), Image.LANCZOS
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        result = read(buffer.getvalue(), cfg, table)
        rows = [(r.count, r.tier) for r in result.rows]
        readings.append((result.reading.wounded_current, rows))
        shown = "  ".join(f"{c:,}/{t or '--'}" for c, t in rows)
        print(f"  {image.width:>5}x{image.height:<5} total={result.reading.wounded_current}  {shown}")

    counts = {(total, tuple(c for c, _ in rows)) for total, rows in readings}
    if len(counts) == 1:
        print("\nCounts and totals identical at every scale.")
        return 0
    print("\nDIFFERED between scales.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scales", action="store_true")
    args = parser.parse_args()

    cfg = config_module.load()
    table = blinded_table(cfg)
    return scales(cfg, table) if args.scales else score(cfg, table)


if __name__ == "__main__":
    raise SystemExit(main())
