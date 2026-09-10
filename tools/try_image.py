"""Run the extraction pipeline against local screenshots - no Discord, no sheet.

    python tools/try_image.py tests/images/shot1.png
    python tools/try_image.py tests/images/*.png --no-vision

Use this to check accuracy on your own screenshots before pointing the bot at a
live channel, and whenever you edit data/units.json.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rok import config as config_module  # noqa: E402
from rok import ocr, pipeline  # noqa: E402
from rok.units import UnitTable, summarise  # noqa: E402


def show(path: Path, cfg, table: UnitTable, use_vision: bool) -> bool:
    result = pipeline.extract(
        path.read_bytes(),
        table,
        anthropic_api_key=cfg.anthropic_api_key if use_vision else "",
        vision_model=cfg.vision_model,
        tesseract_cmd=cfg.tesseract_cmd,
    )
    reading = result.reading
    summary = result.summary or summarise(result.rows)

    print(f"\n=== {path.name} ===")
    print(f"read by: {result.source}")

    for row in result.rows:
        tier = row.tier or "??"
        kind = row.type or "unknown"
        power = f"{row.power_total:>8,}" if row.known else "       ?"
        zone = "  [ram zone]" if row.in_ram_zone else ""
        print(f"  {row.count:>6,}  {row.name:<20} {tier} {kind:<9} {power} power{zone}")

    def show_total(label: str, current, capacity) -> None:
        if current is None:
            print(f"  {label:<22} not visible")
        else:
            cap = f" / {capacity:,}" if capacity else ""
            print(f"  {label:<22} {current:,}{cap}")

    print()
    show_total("Severely wounded", reading.wounded_current, reading.wounded_capacity)
    show_total("Battering ram zone", reading.ram_current, reading.ram_capacity)
    approx = "  (game-rounded)" if reading.rss_approx else ""
    print(
        f"  {'Cost':<22} food={reading.food} wood={reading.wood} "
        f"stone={reading.stone} gold={reading.gold}{approx}"
    )
    print(f"  {'Total troops':<22} {summary['total_troops']:,}")
    print(f"  {'Power dropped':<22} {summary['total_power']:,}")

    if result.ok:
        print("  STATUS: OK - totals reconcile, all units known")
    else:
        print("  STATUS: needs attention")
        for problem in result.problems:
            print(f"    - {problem}")
    for warning in reading.warnings:
        print(f"    note: {warning}")
    return result.ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument(
        "--no-vision", action="store_true", help="Local OCR only; never call the API."
    )
    args = parser.parse_args()

    cfg = config_module.load()
    table = UnitTable(cfg.units_file)
    try:
        ocr.configure(cfg.tesseract_cmd)
    except Exception:
        pass

    print(f"Tesseract: {'available' if ocr.available() else 'NOT FOUND'}")
    print(f"Vision:    {'enabled' if cfg.vision_enabled and not args.no_vision else 'disabled'}")
    if not table.verified:
        print("WARNING: data/units.json is not marked verified_by_human.")

    paths = [p for p in args.images if p.is_file()]
    if not paths:
        print("No readable image paths given.")
        return 2

    passed = sum(show(p, cfg, table, not args.no_vision) for p in paths)
    print(f"\n{passed}/{len(paths)} screenshots reconciled cleanly.")
    return 0 if passed == len(paths) else 1


if __name__ == "__main__":
    raise SystemExit(main())
