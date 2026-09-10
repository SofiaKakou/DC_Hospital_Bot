"""Score the heal-cost reader against hand-checked ground truth.

    python tools/score_costs.py

Ground truth was read off the screenshots by eye. A blank is a recoverable
miss; a WRONG value is the one that actually matters, because it reaches the
sheet looking like a real number.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rok import config as config_module  # noqa: E402
from rok import pipeline  # noqa: E402
from rok.units import UnitTable  # noqa: E402

TRUTH: dict[str, dict[str, int]] = {
    "1.webp": {"food": 10800, "wood": 26600, "stone": 16600, "gold": 1800},
    "2.webp": {"food": 10800, "wood": 26600, "stone": 16600, "gold": 1800},
    "3.webp": {"food": 10800, "wood": 26600, "stone": 16600, "gold": 1800},
    "4.webp": {"food": 10800, "wood": 26600, "stone": 16600, "gold": 1800},
    "5.webp": {"food": 8200, "wood": 21700, "stone": 14100, "gold": 1600},
    "6.webp": {"food": 3000, "wood": 7400, "stone": 5300, "gold": 584},
    "7.webp": {"wood": 272, "stone": 204, "gold": 136},
    "8.webp": {"food": 102, "wood": 102, "gold": 7},
    "9.webp": {"food": 51, "stone": 39, "gold": 4},
    "10.webp": {"food": 51, "stone": 39, "gold": 4},
    "11.webp": {"food": 34, "stone": 26},
    "12.webp": {"food": 33_100_000, "wood": 10_400_000,
                "stone": 17_700_000, "gold": 2_200_000},
}


def main() -> int:
    cfg = config_module.load()
    table = UnitTable(cfg.units_file)
    right = wrong = blank = 0

    for name, truth in TRUTH.items():
        path = ROOT / "tests" / "images" / name
        if not path.exists():
            continue
        reading = pipeline.extract(
            path.read_bytes(), table, anthropic_api_key="", tesseract_cmd=cfg.tesseract_cmd
        ).reading

        marks = []
        for resource, expected in truth.items():
            got = getattr(reading, resource)
            if got is None:
                blank += 1
                marks.append(f"{resource}=blank")
            elif got == expected:
                right += 1
                marks.append(f"{resource}=ok")
            else:
                wrong += 1
                marks.append(f"{resource}=WRONG({got} != {expected})")
        # A value read for a resource this hospital does not use is also wrong.
        for resource in ("food", "wood", "stone", "gold"):
            if resource not in truth and getattr(reading, resource) is not None:
                wrong += 1
                marks.append(f"{resource}=WRONG(spurious {getattr(reading, resource)})")
        print(f"{name:>9}  " + "  ".join(marks))

    total = right + wrong + blank
    print(f"\ncorrect {right}/{total}   WRONG {wrong}   blank {blank}")
    return 0 if wrong == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
