"""Tests for the siege-verification feature (rok/troop_grid.py).

Reads the "Troop Details" grid screen - a different screen from the Unit
Healing window rok/ocr.py reads - and checks siege composition against the
kingdom's rules. Ground truth below was read by hand from each of the 8
sample screenshots in tests/images/siege test/, cross-checking every icon's
portrait tier badge and its type glyph (not the character/weapon art, which
varies by civilisation and is not a reliable type signal - see
rok/troop_grid.py's module docstring for the one case this caught).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rok.troop_grid import (  # noqa: E402
    _NUMBER_TAIL,
    SiegeReading,
    check_siege_rules,
    read_siege,
)
from rok.parse import parse_number  # noqa: E402

IMAGES = ROOT / "tests" / "images" / "siege test"

# (filename, {tier: count}) for every siege entry, read by hand from the
# screenshot. Tiers absent from the dict have zero siege of that tier.
GROUND_TRUTH = [
    ("image-1789042804349.png", {"T1": 200_000}),
    ("image-1789042837047.png", {"T4": 55_965, "T1": 460_020, "T2": 4_800}),
    ("image-1789042840967.webp", {"T1": 200_000}),
    ("image-1789042843864.webp", {"T4": 30_000, "T2": 9_000, "T1": 269_200}),
    ("image-1789042846715.png", {"T4": 12_000, "T1": 477_918}),
    ("image-1789042849295.png", {"T4": 12_000, "T1": 356_359}),
    ("image-1789042852022.webp", {"T1": 248_519}),
    ("image-1789042857434.webp", {"T1": 200_000}),
]


@pytest.mark.parametrize("filename,expected", GROUND_TRUTH, ids=[f for f, _ in GROUND_TRUTH])
def test_siege_counts_match_the_sample_screenshots(filename, expected):
    path = IMAGES / filename
    if not path.exists():
        pytest.skip(f"{filename} not present locally")
    reading = read_siege(path.read_bytes())
    found = {t: c for t, c in reading.by_tier.items() if c}
    assert found == expected


def test_a_bow_shaped_portrait_can_still_be_a_siege_unit():
    """Regression: the 4,800-troop entry in image-1789042837047 has a
    crossbow-shaped character art (a Ballista, visually similar to an
    Archer), which was first mistaken for an Archer during development
    because the portrait's weapon art was trusted over the type glyph. The
    type glyph - the only thing read_siege() actually looks at - correctly
    says Siege regardless of what the weapon model looks like. This is the
    same principle rok/glyphs.py already relies on for the wounded-list
    reader: character art varies by civilisation and unit design, the
    dedicated glyph does not.
    """
    path = IMAGES / "image-1789042837047.png"
    if not path.exists():
        pytest.skip("sample image not present locally")
    reading = read_siege(path.read_bytes())
    assert reading.by_tier.get("T2") == 4_800


@pytest.mark.parametrize(
    "text,expected",
    [
        ("200.000", 200_000),  # Vietnamese-client report: period-grouped thousands
        ("1.620.935", 1_620_935),  # multiple grouping periods, as in the header banner
        ("200,000", 200_000),  # comma-grouped, unaffected by the fix
        ("ES 200.000", 200_000),  # leading OCR noise before the real digits
    ],
)
def test_number_tail_handles_period_grouped_thousands(text, expected):
    """Regression: a real production report had siege counts read as zero
    on a Vietnamese-language client, which prints "200.000" rather than
    "200,000". _NUMBER_TAIL originally only matched [\\d,], so it truncated
    the token at the first period instead of passing the full number to
    parse_number() (which already handles period-grouped thousands
    correctly - rok/ocr.py's own _NUMERIC pattern already included periods
    for exactly this reason; this module's narrower copy did not).
    """
    m = _NUMBER_TAIL.search(text)
    assert m is not None
    value, _ = parse_number(m.group(0))
    assert value == expected


# --------------------------------------------------------------------------- #
# Rule check
# --------------------------------------------------------------------------- #

def _reading(**by_tier) -> SiegeReading:
    r = SiegeReading()
    r.by_tier.update(by_tier)
    return r


def test_all_zero_passes():
    verdict, notes = check_siege_rules(_reading())
    assert verdict == "Pass"


def test_within_caps_passes():
    verdict, notes = check_siege_rules(_reading(T1=200_000, T4=75_000, T5=1_000_000))
    assert verdict == "Pass"


def test_any_t2_fails():
    verdict, notes = check_siege_rules(_reading(T2=1))
    assert verdict == "FAIL"
    assert any("T2" in n for n in notes)


def test_any_t3_fails():
    verdict, notes = check_siege_rules(_reading(T3=1))
    assert verdict == "FAIL"
    assert any("T3" in n for n in notes)


def test_t1_over_cap_fails():
    verdict, notes = check_siege_rules(_reading(T1=200_001))
    assert verdict == "FAIL"
    assert any("T1" in n for n in notes)


def test_t4_over_cap_fails():
    verdict, notes = check_siege_rules(_reading(T4=75_001))
    assert verdict == "FAIL"
    assert any("T4" in n for n in notes)


def test_t5_has_no_cap():
    # No rule was given for T5, so any amount is fine.
    verdict, notes = check_siege_rules(_reading(T5=99_999_999))
    assert verdict == "Pass"


def test_multiple_violations_are_all_reported():
    verdict, notes = check_siege_rules(_reading(T1=300_000, T2=5, T4=80_000))
    assert verdict == "FAIL"
    assert len(notes) == 3
