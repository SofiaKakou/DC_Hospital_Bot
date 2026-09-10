"""Logic tests built from the real numbers in the example screenshots.

The screenshots supplied for this build were, in order:
  1) 14 Long Swordsman + 11 Teutonic Knight + 48 Crossbowman  = 73 wounded, 0 ram zone
  2) 43 (name scrolled off) + 22 + 135                        = 200 wounded, 17 ram zone
  3) 47 Long Swordsman + 27 Teutonic Knight + 158 Crossbowman = 232 wounded, 84 ram zone
Screenshot 2 is the interesting one: its top row is cut off, and 200-22-135 proves the
hidden count is 43 - which is exactly the kind of check the checksum performs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rok.parse import (  # noqa: E402
    Reading,
    TroopRow,
    check_totals,
    normalise_unit_name,
    parse_fraction,
    parse_number,
)
from rok.pipeline import ExtractionResult, evaluate  # noqa: E402
from rok.session import SessionStore  # noqa: E402
from rok.sheets import (  # noqa: E402
    SUBMISSION_HEADERS,
    _cell,
    _link,
    _parse_roster,
    submission_record,
)
from rok.units import UnitTable, summarise  # noqa: E402

TABLE = UnitTable(ROOT / "data" / "units.json")


# --------------------------------------------------------------------------- #
# Number parsing
# --------------------------------------------------------------------------- #

def test_abbreviated_values_expand():
    assert parse_number("3.0K") == (3000, True)
    assert parse_number("26.6K") == (26600, True)
    assert parse_number("1.6M") == (1600000, True)


def test_exact_values_are_not_flagged_approximate():
    assert parse_number("584") == (584, False)
    assert parse_number("1,234") == (1234, False)


def test_unparseable_returns_none():
    assert parse_number("") == (None, False)
    assert parse_number("HEAL") == (None, False)


def test_ocr_digit_confusions_are_repaired():
    # Tesseract routinely returns O for 0 and l for 1 in this font.
    assert parse_number("l58")[0] == 158
    assert parse_number("2O0")[0] == 200


def test_fractions():
    assert parse_fraction("232/548,725") == (232, 548725)
    assert parse_fraction("0/50,000") == (0, 50000)
    assert parse_fraction("no fraction here") == (None, None)


def test_name_normalisation_matches_table_keys():
    assert normalise_unit_name("Long Swordsman") == "long swordsman"
    assert normalise_unit_name("Man-at-Arms") == "man at arms"
    assert TABLE.lookup("Man-at-Arms") is not None
    assert TABLE.lookup("  crossbowman  ") is not None


# --------------------------------------------------------------------------- #
# Checksum
# --------------------------------------------------------------------------- #

def _screenshot_three() -> Reading:
    return Reading(
        rows=[
            TroopRow("Long Swordsman", 47),
            TroopRow("Teutonic Knight", 27),
            TroopRow("Crossbowman", 158),
            TroopRow("Battering Ram", 84, in_ram_zone=True),
        ],
        wounded_current=232,
        wounded_capacity=548725,
        ram_current=84,
        ram_capacity=50000,
        food=10800, wood=26600, stone=16600, gold=1800,
        rss_approx=True,
    )


def test_consistent_reading_passes():
    assert check_totals(_screenshot_three()) == []


def test_siege_is_excluded_from_the_wounded_total():
    # 47+27+158 = 232 without the 84 rams; including them would break the check.
    reading = _screenshot_three()
    assert sum(r.count for r in reading.wounded_rows) == reading.wounded_current


def test_misread_digit_is_caught():
    reading = _screenshot_three()
    reading.rows[2].count = 153  # 158 misread as 153
    problems = check_totals(reading)
    assert problems and "off by 5" in problems[0]


def test_missing_row_is_caught():
    reading = _screenshot_three()
    reading.rows.pop(0)
    assert check_totals(reading)


def test_checksum_is_not_silently_repaired():
    reading = _screenshot_three()
    reading.rows[0].count = 40
    check_totals(reading)
    assert reading.rows[0].count == 40, "check_totals must never edit the reading"


# --------------------------------------------------------------------------- #
# Units and power
# --------------------------------------------------------------------------- #

def test_power_totals():
    rows = TABLE.resolve_all(_screenshot_three().rows)
    summary = summarise(rows)
    # Derived from the table rather than hard-coded: the tiers themselves were
    # corrected once the in-game badges were read, and pinning numbers here would
    # just have to be edited again the next time one is verified.
    expected = sum(r.count * TABLE.tier_power[r.tier] for r in rows)
    assert summary["total_power"] == expected
    assert summary["total_troops"] == 316
    # Teutonic Knight is Cavalry - its weapon glyph is a horse. The name table
    # had it as Infantry until the glyph reader disagreed with it.
    assert summary["types_present"] == ["Infantry", "Archer", "Cavalry", "Siege"]
    assert not summary["power_is_partial"]


def test_unknown_unit_does_not_get_guessed_power():
    rows = TABLE.resolve_all([TroopRow("Ceremonial Guard", 100)])
    summary = summarise(rows)
    assert summary["total_power"] == 0
    assert summary["power_is_partial"]
    assert rows[0].tier is None


def test_siege_classification_comes_from_the_table():
    reading = Reading(rows=[TroopRow("Battering Ram", 84), TroopRow("Crossbowman", 158)])
    result = evaluate(reading, TABLE)
    by_name = {r.name: r for r in result.rows}
    assert by_name["Battering Ram"].in_ram_zone
    assert not by_name["Crossbowman"].in_ram_zone


def test_cut_off_row_is_flagged_not_guessed():
    reading = Reading(
        rows=[TroopRow("", 43, name_visible=False), TroopRow("Teutonic Knight", 22),
              TroopRow("Crossbowman", 135)],
        wounded_current=200,
    )
    result = evaluate(reading, TABLE)
    assert not result.ok
    assert any("scrolled out of view" in p for p in result.problems)


# --------------------------------------------------------------------------- #
# Multi-screenshot sessions
# --------------------------------------------------------------------------- #

def _result(reading: Reading) -> ExtractionResult:
    return evaluate(reading, TABLE)


def test_two_screenshots_merge_into_one_complete_submission():
    store = SessionStore(timeout_minutes=30)
    sub = store.get_or_create("1234567", 1, "player")

    # Top of the list.
    sub.merge(_result(Reading(rows=[TroopRow("Long Swordsman", 47)], wounded_current=232)))
    assert not sub.complete and sub.awaiting_more

    # Scrolled down: the rest, plus the ram zone.
    sub.merge(_result(Reading(
        rows=[TroopRow("Teutonic Knight", 27), TroopRow("Crossbowman", 158),
              TroopRow("Battering Ram", 84)],
        wounded_current=232, ram_current=84,
    )))
    assert sub.complete, sub.missing()
    expected = sum(r.count * TABLE.tier_power[r.tier] for r in sub.all_rows)
    assert sub.summary["total_power"] == expected


def test_resending_the_same_screenshot_does_not_double_count():
    store = SessionStore()
    sub = store.get_or_create("1", 1, "p")
    reading = lambda: _result(_screenshot_three())  # noqa: E731
    sub.merge(reading())
    sub.merge(reading())
    assert sub.summary["total_troops"] == 316


def test_changed_hospital_total_discards_stale_rows():
    store = SessionStore()
    sub = store.get_or_create("1", 1, "p")
    sub.merge(_result(Reading(rows=[TroopRow("Crossbowman", 48)], wounded_current=73)))
    notes = sub.merge(_result(Reading(rows=[TroopRow("Crossbowman", 158)], wounded_current=232)))
    assert any("changed" in n for n in notes)
    assert sub.rows["crossbowman"].count == 158
    assert sum(r.count for r in sub.all_rows) == 158


def test_completed_session_is_cleared_and_timeout_evicts():
    store = SessionStore(timeout_minutes=0)
    store.get_or_create("999", 1, "p")
    assert store.get("999") is None  # zero timeout evicts immediately


# --------------------------------------------------------------------------- #
# Roster parsing
# --------------------------------------------------------------------------- #

def test_roster_finds_columns_by_header():
    rows = [
        ["Kingdom scan 2026-09-01", "", ""],
        ["ID", "Name", "Power"],
        ["1234567", "SomePlayer", "50,000,000"],
        ["7654321", "Another", "42,000,000"],
    ]
    assert _parse_roster(rows) == {"1234567": "SomePlayer", "7654321": "Another"}


def test_roster_ignores_non_numeric_ids():
    rows = [["ID", "Name"], ["not-an-id", "X"], ["555", "Y"]]
    assert _parse_roster(rows) == {"555": "Y"}


# --------------------------------------------------------------------------- #
# Heal-cost resources, read from the icons
# --------------------------------------------------------------------------- #

# Icon boxes measured from 12.webp's cost strip.
ICON_BOXES = {
    "food": (441, 448, 476, 478),
    "wood": (576, 448, 606, 478),
    "stone": (716, 448, 746, 478),
    "gold": (849, 448, 879, 478),
}


def test_each_resource_icon_is_identified():
    from PIL import Image

    from rok.ocr import resource_from_icon

    path = ROOT / "tests" / "images" / "12.webp"
    if not path.exists():
        return
    image = Image.open(path).convert("RGB")
    for expected, (x0, y0, x1, y1) in ICON_BOXES.items():
        assert resource_from_icon(image, x0, x1, y0, y1) == expected, expected


def test_a_patch_of_empty_panel_is_not_read_as_a_resource():
    # The icon never fills its box, so panel pixels are discarded - but a box
    # holding nothing else must come back as unknown rather than guessing.
    from PIL import Image

    from rok.ocr import resource_from_icon

    path = ROOT / "tests" / "images" / "12.webp"
    if not path.exists():
        return
    image = Image.open(path).convert("RGB")
    assert resource_from_icon(image, 530, 560, 448, 478) is None


def test_the_stone_sprite_survives_a_loose_window():
    # The window is anchored to the OCR token, which is taller than the sprite,
    # so a lot of panel comes with it. Stone still has to register.
    from PIL import Image

    from rok.ocr import resource_from_icon

    path = ROOT / "tests" / "images" / "12.webp"
    if not path.exists():
        return
    image = Image.open(path).convert("RGB")
    assert resource_from_icon(image, 711, 741, 441, 485) == "stone"


# --------------------------------------------------------------------------- #
# Unit-name matching
# --------------------------------------------------------------------------- #

def test_longer_unit_name_is_not_matched_as_the_shorter_one():
    from rok.ocr import _match_unit

    names = sorted(TABLE.units, key=lambda n: (-len(n.split()), -len(n)))
    # "Royal Crossbowman" is a T4 archer; scoring it as the T3 "Crossbowman"
    # would put the wrong tier and the wrong power on the sheet.
    assert _match_unit("royal crossbowman 1", names) == "royal crossbowman"
    assert _match_unit("crossbowman 158", names) == "crossbowman"


def test_unknown_prefixed_name_is_refused_rather_than_downgraded():
    from rok.ocr import _match_unit

    names = [n for n in TABLE.units if n != "royal crossbowman"]
    names.sort(key=lambda n: (-len(n.split()), -len(n)))
    assert _match_unit("royal crossbowman 1", names) is None


def test_battering_ram_zone_header_is_not_a_unit_row():
    from rok.ocr import _match_unit

    names = sorted(TABLE.units, key=lambda n: (-len(n.split()), -len(n)))
    assert _match_unit("battering ram zone", names) is None
    assert _match_unit("battering ram 84", names) == "battering ram"


# --------------------------------------------------------------------------- #
# Fill check: at least 100,000 T4/T5 troops, any type
# --------------------------------------------------------------------------- #

def _submission(rows, wounded, ram=0):
    store = SessionStore()
    sub = store.get_or_create("1", 1, "p")
    sub.merge(evaluate(Reading(rows=rows, wounded_current=wounded, ram_current=ram), TABLE))
    return sub


def test_honest_high_tier_fill_passes():
    sub = _submission([TroopRow("Crossbowman", 150_000)], 150_000)
    verdict, _ = sub.fill_check(TABLE)
    assert verdict == "Pass"


def test_padding_with_t1_is_caught():
    # 200k battering rams with a token 500 T4 on top: a "is any T4 present?" check
    # would wave this through.
    sub = _submission(
        [TroopRow("Battering Ram", 200_000), TroopRow("Crossbowman", 500)],
        wounded=500, ram=200_000,
    )
    verdict, note = sub.fill_check(TABLE)
    assert verdict == "FAIL"
    assert "99,500 short" in note


def test_scrolled_screenshot_can_still_prove_a_pass():
    # The rule is a minimum, so 120k visible settles it whatever is scrolled off.
    sub = _submission([TroopRow("Crossbowman", 120_000)], 180_000)
    assert sub.unaccounted == 60_000
    assert sub.fill_check(TABLE)[0] == "Pass"


def test_borderline_asks_for_another_screenshot():
    sub = _submission([TroopRow("Crossbowman", 60_000)], 150_000)
    verdict, note = sub.fill_check(TABLE)
    assert verdict == "Unconfirmed"
    assert "scrolled" in note


def test_hospital_too_small_fails_without_the_breakdown():
    sub = _submission([], 40_000)
    verdict, _ = sub.fill_check(TABLE)
    assert verdict == "FAIL"


def test_siege_does_not_count_towards_the_high_tier_minimum():
    sub = _submission([TroopRow("Battering Ram", 500_000)], wounded=0, ram=500_000)
    assert sub.high_tier_troops(TABLE) == 0
    assert sub.fill_check(TABLE)[0] == "FAIL"


# --------------------------------------------------------------------------- #
# Sheet row shape and links
# --------------------------------------------------------------------------- #

def test_submission_row_matches_the_headers():
    # A row narrower or wider than the header shifts every later value into the
    # wrong column, and the sheet still looks plausible - so pin the width.
    sub = _submission([TroopRow("Crossbowman", 150_000)], 150_000)
    record = submission_record(sub, "SomePlayer", TABLE, "2026-09-02 12:00:00")
    assert len(record) == len(SUBMISSION_HEADERS)


def test_screenshot_and_message_cells_are_clickable():
    sub = _submission([TroopRow("Crossbowman", 150_000)], 150_000)
    sub.image_urls.append("https://cdn.discordapp.com/a.png?ex=abc")
    sub.message_urls.append("https://discord.com/channels/1/2/3")
    record = submission_record(sub, "SomePlayer", TABLE, "2026-09-02 12:00:00")
    cells = dict(zip(SUBMISSION_HEADERS, record))
    assert cells["Screenshot"].startswith('=HYPERLINK("https://cdn.discordapp.com')
    assert cells["Discord Message"].startswith('=HYPERLINK("https://discord.com/channels')


def test_missing_links_leave_the_cell_empty_not_broken():
    sub = _submission([TroopRow("Crossbowman", 150_000)], 150_000)
    cells = dict(zip(SUBMISSION_HEADERS,
                     submission_record(sub, "X", TABLE, "2026-09-02 12:00:00")))
    assert cells["Screenshot"] == ""
    assert cells["Discord Message"] == ""


def test_link_label_and_quote_escaping():
    assert _link("", "Open") == ""
    assert _link('https://x/a"b.png', "Open") == '=HYPERLINK("https://x/a%22b.png", "Open")'


def test_text_starting_with_an_operator_is_not_run_as_a_formula():
    # The row is written USER_ENTERED so the HYPERLINK cells evaluate, which means
    # a governor name like "+Bob" would otherwise be parsed as a formula.
    assert _cell("+Bob") == "'+Bob"
    assert _cell("=SUM(A1)") == "'=SUM(A1)"
    assert _cell("Normal Name") == "Normal Name"
    assert _cell(None) == ""
    assert _cell(1234) == 1234


# --------------------------------------------------------------------------- #
# Tier from the portrait, not the name
# --------------------------------------------------------------------------- #

IMAGES = ROOT / "tests" / "images"

# (file, portrait box, expected tier, label). Boxes measured by hand; tiers
# confirmed from the in-game badge and the rarity colour, which agree.
PORTRAITS = [
    ("11.webp", (672, 166, 726, 232), "T2", "Light Cavalry"),
    ("9.webp", (672, 166, 726, 232), "T3", "Heavy Cavalry"),
    ("8.webp", (672, 166, 726, 232), "T4", "Long Swordsman"),
    ("7.webp", (672, 166, 726, 232), "T5", "Royal Crossbowman"),
    ("5.webp", (352, 107, 396, 162), "T4", "Teutonic Knight"),
    ("5.webp", (352, 283, 396, 338), "T1", "Battering Ram"),
]


# Reading the tier off the portrait works on some screenshots and not others.
# These are the ones it currently gets right; the gap is recorded below rather
# than papered over, because the fill check is only as good as the tier.
WORKING_PORTRAITS = [
    ("11.webp", (672, 166, 726, 232), "T2", "Light Cavalry"),
    ("8.webp", (672, 166, 726, 232), "T4", "Long Swordsman"),
    ("7.webp", (672, 166, 726, 232), "T5", "Royal Crossbowman"),
]


def test_tier_is_read_from_the_portrait_colour():
    from PIL import Image

    from rok.ocr import tier_from_portrait

    for name, box, expected, label in WORKING_PORTRAITS:
        path = IMAGES / name
        if not path.exists():
            continue
        image = Image.open(path).convert("RGB")
        assert tier_from_portrait(image, box) == expected, label


@pytest.mark.xfail(
    reason=(
        "Portrait tier detection is not reliable yet. On some screenshots the "
        "backdrop sample is rejected as panel-coloured and returns None, so the "
        "tier still falls back to the unit-name table - which does not survive "
        "20 civilisations and every game language. Unresolved."
    ),
    strict=False,
)
def test_tier_is_read_for_every_known_portrait():
    from PIL import Image

    from rok.ocr import tier_from_portrait

    for name, box, expected, label in PORTRAITS:
        path = IMAGES / name
        if not path.exists():
            continue
        image = Image.open(path).convert("RGB")
        assert tier_from_portrait(image, box) == expected, label


def test_portrait_box_ignores_a_stray_frame_coloured_blob_far_away():
    """Regression: a screenshot with a clipped bottom row produced OCR noise
    ("Cee) Maryannu" for that row's name) whose leftmost word happened to sit
    near a small, unrelated patch of frame-coloured pixels 44px from the real
    portrait. _portrait_box took the min-to-max span of every hit in the
    search band without clustering, so that stray patch inflated a clean 66px
    frame into a bogus 117px span and rejected it - forcing a fallback that
    read this row's actual purple (T4) badge as gold (T5).
    """
    from PIL import Image

    from rok.ocr import _Line, _Word, _portrait_box, tier_from_portrait

    path = IMAGES / "36_clipped_bottom_row_stray_frame_pixels_misread_tier.png"
    if not path.exists():
        return
    image = Image.open(path).convert("RGB")

    # "Chevalier" row, measured directly from the screenshot (source pixels,
    # scale=1): the name starts around x=561, vertically centred near y=365.
    line = _Line([_Word("Chevalier", 561, 333, 650, 373)])
    box = _portrait_box(image, line, scale=1.0, unit=51.0)
    assert box is not None, "the real, clean frame must still be found"
    left, top, right, bottom = box
    assert right - left <= 51.0 * 2.2, "must not span out to the stray blob"
    assert tier_from_portrait(image, box) != "T5", (
        "purple (T4) must never be misread as gold (T5) - a wrong tier can "
        "pass a padded hospital, where an unread one only asks for another shot"
    )


def test_portrait_box_merges_a_frame_split_by_overlapping_text():
    """The same screenshot's "Bretteur" row has its frame broken into three
    detected runs (gaps of ~7-17px on this ~51px unit) by the count text
    partly overlapping its edge. That must still resolve to one box - the fix
    for the stray-blob case above must not swing the other way and start
    rejecting a real, merely-fragmented frame too.
    """
    from PIL import Image

    from rok.ocr import _Line, _Word, _portrait_box, tier_from_portrait

    path = IMAGES / "36_clipped_bottom_row_stray_frame_pixels_misread_tier.png"
    if not path.exists():
        return
    image = Image.open(path).convert("RGB")

    line = _Line([_Word("Bretteur", 562, 225, 650, 260)])
    box = _portrait_box(image, line, scale=1.0, unit=51.0)
    assert box is not None
    assert tier_from_portrait(image, box) == "T4"


def test_unknown_tier_never_becomes_a_wrong_tier():
    # The safe direction. A row we cannot classify must read as None so the fill
    # check treats it as uncertain, rather than inventing a tier that could let a
    # padded hospital pass.
    from PIL import Image

    from rok.ocr import tier_from_portrait

    path = IMAGES / "5.webp"
    if not path.exists():
        return
    image = Image.open(path).convert("RGB")
    got = tier_from_portrait(image, (352, 283, 396, 338))
    assert got in (None, "T1"), f"Battering Ram must not read as a high tier, got {got}"


def test_a_box_that_missed_the_portrait_returns_no_tier():
    # Panel background rather than a portrait. Guessing here is how every missed
    # crop used to report a confident T3.
    from PIL import Image

    from rok.ocr import tier_from_portrait

    path = IMAGES / "5.webp"
    if not path.exists():
        return
    image = Image.open(path).convert("RGB")
    assert tier_from_portrait(image, (150, 300, 200, 350)) is None


def test_portrait_tier_overrides_the_name_table():
    # A civilisation-specific name we have never seen still gets a tier, and a
    # stale table entry never beats what the game itself displays.
    row = TroopRow("some unknown civ unit", 500, tier="T5")
    resolved = TABLE.resolve(row)
    assert resolved.tier == "T5"
    assert resolved.power_each == TABLE.tier_power["T5"]


# --------------------------------------------------------------------------- #
# Admin remove / edit
# --------------------------------------------------------------------------- #

class FakeWorksheet:
    """Just enough of a gspread worksheet to exercise the admin paths."""

    def __init__(self, headers, rows):
        self.rows = [list(headers)] + [list(r) for r in rows]

    def col_values(self, index):
        return [r[index - 1] if index - 1 < len(r) else "" for r in self.rows]

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def row_values(self, index):
        return list(self.rows[index - 1])

    def cell(self, row, col):
        class C:
            value = self.rows[row - 1][col - 1] if col - 1 < len(self.rows[row - 1]) else ""
        return C()

    def update_cell(self, row, col, value):
        while len(self.rows[row - 1]) < col:
            self.rows[row - 1].append("")
        self.rows[row - 1][col - 1] = value

    def delete_rows(self, index, end=None):
        del self.rows[index - 1:(end if end is not None else index)]


def _fake_client(monkeypatch_rows=None):
    from rok.sheets import SUBMISSION_HEADERS, TROOP_HEADERS, SheetsClient

    def blank_row(gid, name):
        row = [""] * len(SUBMISSION_HEADERS)
        row[SUBMISSION_HEADERS.index("Governor ID")] = gid
        row[SUBMISSION_HEADERS.index("Name")] = name
        row[SUBMISSION_HEADERS.index("T4+T5 Troops")] = "50000"
        row[SUBMISSION_HEADERS.index("Fill Check")] = "FAIL"
        return row

    subs = FakeWorksheet(SUBMISSION_HEADERS, [blank_row("111", "Alice"), blank_row("222", "Bob")])
    troops = FakeWorksheet(TROOP_HEADERS, [
        ["t", "111", "Alice", "Crossbowman", "T4", "Archer", 5, 10, 50, "No"],
        ["t", "222", "Bob", "Mamluk", "T4", "Archer", 5, 10, 50, "No"],
        ["t", "111", "Alice", "Long Swordsman", "T4", "Infantry", 5, 10, 50, "No"],
    ])

    client = SheetsClient.__new__(SheetsClient)
    client.table = TABLE
    client.submissions_tab, client.troops_tab = "Submissions", "Troops"
    client._worksheet = lambda title, headers=None: subs if title == "Submissions" else troops
    return client, subs, troops


def test_remove_deletes_from_both_tabs():
    from rok.sheets import SUBMISSION_HEADERS

    client, subs, troops = _fake_client()
    removed = client.remove("111")
    assert removed["Name"] == "Alice"
    assert [r[SUBMISSION_HEADERS.index("Governor ID")] for r in subs.rows[1:]] == ["222"]
    # Both of Alice's troop rows go, Bob's stays.
    assert [r[1] for r in troops.rows[1:]] == ["222"]


def test_remove_reports_nothing_for_an_unknown_id():
    client, _subs, _troops = _fake_client()
    assert client.remove("999") is None


def test_edit_updates_the_field_and_records_who_changed_it():
    from rok.sheets import SUBMISSION_HEADERS

    client, subs, _troops = _fake_client()
    client.edit("111", "Name", "Alice II", editor="Officer")
    row = subs.rows[1]
    assert row[SUBMISSION_HEADERS.index("Name")] == "Alice II"
    assert "edited by Officer" in row[SUBMISSION_HEADERS.index("Notes")]


def test_editing_the_high_tier_count_recomputes_the_fill_check():
    from rok.sheets import SUBMISSION_HEADERS

    client, subs, _troops = _fake_client()
    client.edit("111", "T4+T5 Troops", "150,000", editor="Officer")
    row = subs.rows[1]
    assert row[SUBMISSION_HEADERS.index("T4+T5 Troops")] == "150000"
    # Leaving the old FAIL beside a passing count would make the row contradict itself.
    assert row[SUBMISSION_HEADERS.index("Fill Check")] == "Pass"


def test_edit_rejects_a_non_numeric_value_for_a_number_field():
    client, _subs, _troops = _fake_client()
    with pytest.raises(ValueError):
        client.edit("111", "Total In Hospital", "loads")


def test_edit_refuses_a_field_that_is_not_editable():
    client, _subs, _troops = _fake_client()
    with pytest.raises(ValueError):
        client.edit("111", "Total Power", "999")


def test_edit_reports_a_missing_governor():
    client, _subs, _troops = _fake_client()
    with pytest.raises(LookupError):
        client.edit("999", "Name", "Nobody")


def test_clear_all_removes_every_row_but_keeps_the_headers():
    from rok.sheets import SUBMISSION_HEADERS

    client, subs, troops = _fake_client()
    removed = client.clear_all()
    assert removed == 2
    assert subs.rows == [SUBMISSION_HEADERS], "the header row must survive"
    assert len(troops.rows) == 1


def test_clear_all_on_an_empty_sheet_is_harmless():
    from rok.sheets import SUBMISSION_HEADERS, TROOP_HEADERS

    client, subs, troops = _fake_client()
    client.clear_all()
    assert client.clear_all() == 0
    assert subs.rows == [SUBMISSION_HEADERS]


def test_submission_count_excludes_the_header():
    client, _subs, _troops = _fake_client()
    assert client.submission_count() == 2


def test_tier_power_matches_the_confirmed_values():
    # Confirmed by the kingdom, not seeded defaults.
    assert TABLE.tier_power == {"T1": 1, "T2": 2, "T3": 3, "T4": 4, "T5": 10}
    assert TABLE.verified


def test_count_comes_from_beside_the_name_not_the_slider():
    """The boxed number on the right is the heal-amount slider.

    A player can drag it below what the hospital holds, so reading it would
    under-report them. Here the row reads "Long Swordsman 82,771 [bar] 40,000" -
    a dragged slider - and the count must be the 82,771 next to the name.
    """
    from rok.ocr import _Line, _Word, _count_word

    line = _Line([
        _Word("Long", 100, 10, 140, 30),
        _Word("Swordsman", 145, 10, 230, 30),
        _Word("82,771", 240, 10, 300, 30),
        _Word("40,000", 700, 10, 760, 30),  # the slider box
    ])
    assert _count_word(line).text == "82,771"


def test_a_numeral_left_of_the_name_is_not_taken_as_the_count():
    # The tier numeral sits on the portrait, left of the name, and OCR sometimes
    # reads it as a digit.
    from rok.ocr import _Line, _Word, _count_word

    line = _Line([
        _Word("4", 60, 10, 70, 30),  # tier badge "IV" misread
        _Word("Mamluk", 100, 10, 170, 30),
        _Word("193,195", 180, 10, 250, 30),
        _Word("193,195", 700, 10, 770, 30),
    ])
    assert _count_word(line).text == "193,195"
    assert _count_word(line).left == 180


def test_a_row_with_a_tier_but_no_type_does_not_crash():
    """Regression: this took out every foreign-language submission.

    Tier now comes from the portrait and works for any civilisation, but type
    still comes from the name table. A French "Bretteur" therefore resolves with
    tier T4 and type None - and summarise() indexed by_type with that None,
    raising KeyError(None). Its message is the string "None", which is why the
    log only ever said "Local OCR failed: None".
    """
    resolved = TABLE.resolve(TroopRow("Bretteur", 7, tier="T4"))
    assert resolved.tier == "T4" and resolved.type is None and resolved.known

    summary = summarise([resolved])
    assert summary["total_power"] == 7 * TABLE.tier_power["T4"]
    assert summary["by_tier"]["T4"] == 7
    assert summary["tiers_present"] == ["T4"]
    assert summary["types_present"] == []


def test_untyped_rows_still_count_towards_the_fill_check():
    # The whole point: a unit we cannot name must still be countable.
    rows = [TroopRow("Maryannu d'elite", 120_000, tier="T5")]
    sub = _submission(rows, 120_000)
    assert sub.high_tier_troops(TABLE) == 120_000
    assert sub.fill_check(TABLE)[0] == "Pass"


# --------------------------------------------------------------------------- #
# Troop type from the weapon glyph
# --------------------------------------------------------------------------- #

def _glyphs():
    from rok.glyphs import GlyphTable

    return GlyphTable(ROOT / "data" / "glyphs.json")


def test_every_troop_type_has_a_glyph_template():
    # Without a Siege template a battering ram is untyped, and ram-zone
    # membership now prefers the glyph over the name.
    assert set(_glyphs().types) == {"Infantry", "Archer", "Cavalry", "Siege"}


def test_glyph_identifies_type_without_the_unit_name():
    from PIL import Image

    from rok.glyphs import glyph_mask

    # Glyph boxes measured from 12.webp: sword, horse, bow.
    cases = [((483, 118, 523, 153), "Infantry"), ((485, 206, 523, 241), "Cavalry"),
             ((483, 312, 522, 347), "Archer")]
    path = ROOT / "tests" / "images" / "12.webp"
    if not path.exists():
        return
    image = Image.open(path).convert("RGB")
    glyphs = _glyphs()
    for box, expected in cases:
        assert glyphs.classify(glyph_mask(image, box)) == expected, expected


def test_a_blank_region_yields_no_glyph():
    # The glyph disappears once a heal has started. That row must come back
    # untyped rather than matched to whatever template is nearest.
    from PIL import Image

    from rok.glyphs import glyph_mask

    path = ROOT / "tests" / "images" / "12.webp"
    if not path.exists():
        return
    image = Image.open(path).convert("RGB")
    assert glyph_mask(image, (300, 250, 340, 290)) is None
    assert _glyphs().classify(None) is None


def test_glyph_type_overrides_the_name_table():
    # The table said Teutonic Knight was Infantry; the game draws it with a horse.
    row = TroopRow("Teutonic Knight", 27, tier="T4", troop_type="Cavalry")
    assert TABLE.resolve(row).type == "Cavalry"


def test_siege_goes_to_the_ram_zone_by_glyph_not_by_name():
    reading = Reading(rows=[
        TroopRow("Belier de siege", 500, tier="T1", troop_type="Siege"),
        TroopRow("Maryannu d elite", 900, tier="T5", troop_type="Archer"),
    ])
    result = evaluate(reading, TABLE)
    by_name = {r.name: r for r in result.rows}
    # Neither name is in the table; only the glyph separates them.
    assert by_name["Belier de siege"].in_ram_zone
    assert not by_name["Maryannu d elite"].in_ram_zone


# --------------------------------------------------------------------------- #
# Portrait column fit
# --------------------------------------------------------------------------- #

def test_column_fit_box_is_derived_from_the_row_centre():
    from rok.ocr import _ColumnFit

    fit = _ColumnFit(left=100, right=160, offset=20.0, height=72.0)
    left, top, right, bottom = fit.box(centre_y=200.0)
    assert (left, right) == (100, 160)
    # Portrait hangs below the text centre by the fitted offset.
    assert top == int(200 + 20 - 36)
    assert bottom - top == 72


def test_column_fit_scales_with_the_screenshot():
    # Everything is measured per screenshot in its own pixels, so doubling the
    # resolution doubles the fit and the box lands in the same relative place.
    from rok.ocr import _ColumnFit

    small = _ColumnFit(left=100, right=160, offset=20.0, height=72.0)
    large = _ColumnFit(left=200, right=320, offset=40.0, height=144.0)
    a = small.box(200.0)
    b = large.box(400.0)
    assert [v * 2 for v in a] == list(b)


def test_fit_needs_a_plausible_frame_to_return_anything():
    from PIL import Image

    from rok.ocr import _fit_portrait_column

    # Flat blue panel: no gold frame anywhere, so no geometry can be fitted and
    # the caller falls back to per-row detection.
    blank = Image.new("RGB", (400, 400), (10, 80, 120))
    assert _fit_portrait_column(blank, [100.0, 200.0], name_left=300.0, unit=40.0) is None


# --------------------------------------------------------------------------- #
# Resource icon classification
# --------------------------------------------------------------------------- #
#
# data/reference_icons/ holds the game's own food/wood/stone/gold icon assets
# (extracted from the client, not screenshotted), so resource_from_icon() can
# be checked against ground truth instead of eyeballed test crops.
#
# A fix was attempted here once: measuring these assets found stone's real hue
# (215-240 degrees) sits inside the range resource_from_icon() treats as "the
# panel behind the icon, ignore it" (170-260) - so only the accidental half of
# stone pixels under saturation 0.22 were ever being counted. Widening that
# hue band to claim stone's real range measured correctly in isolation, but
# regressed tools/score_costs.py on the real sample set (41/43 -> 40/44): on
# one real screenshot it let a stray token get matched to three different
# resources at once. Reverted. Real screenshots apparently supply enough
# low-saturation antialiasing at a stone icon's edge for the existing
# sat<0.22 rule to work in practice even though an isolated icon on a flat
# synthetic panel does not - which is why the gap below is real but the fix
# for it needs to be more selective than a bare hue range before it's worth
# trying again.

REFERENCE_ICONS = ROOT / "data" / "reference_icons"

# The real cost-strip panel colour, measured from tests/images/5.webp rather
# than guessed: hue ~190 degrees, saturation ~0.96.
_PANEL_COLOUR = (3, 56, 79)


def _icon_on_panel(name: str):
    """Paste a reference icon onto the real panel colour, as it sits on-screen."""
    from PIL import Image

    path = REFERENCE_ICONS / f"{name}.png"
    if not path.exists():
        return None
    rgba = Image.open(path).convert("RGBA")
    canvas = Image.new("RGB", rgba.size, _PANEL_COLOUR)
    canvas.paste(rgba, (0, 0), rgba)
    return canvas


def test_resource_icon_classification_matches_the_reference_assets():
    """Food, wood and gold each classify as themselves against the game's own
    icon assets, pasted on the real panel colour. Stone is excluded here -
    see test_stone_icon_alone_is_not_yet_reliably_classified.
    """
    from rok.ocr import resource_from_icon

    for name in ("food", "wood", "gold"):
        canvas = _icon_on_panel(name)
        if canvas is None:
            continue
        result = resource_from_icon(canvas, 0, canvas.width, 0, canvas.height)
        assert result == name, f"{name} icon misclassified as {result!r}"


def test_gold_is_not_confused_with_wood_despite_the_shared_hue_range():
    # Gold's hue (measured median ~42 degrees) sits almost exactly on the
    # orange/yellow bucket boundary (40) that also separates it from wood, so
    # this is closer to the boundary than it looks from the bucket names alone.
    from rok.ocr import resource_from_icon

    gold = _icon_on_panel("gold")
    wood = _icon_on_panel("wood")
    if gold is None or wood is None:
        return
    assert resource_from_icon(gold, 0, gold.width, 0, gold.height) == "gold"
    assert resource_from_icon(wood, 0, wood.width, 0, wood.height) == "wood"


@pytest.mark.xfail(
    reason=(
        "Stone's real hue (215-240) overlaps the range treated as background "
        "panel, so an isolated icon on a flat synthetic panel doesn't clear "
        "the sat<0.22 threshold the way a real screenshot's antialiased edges "
        "apparently do (tools/score_costs.py scores stone at 100% on the real "
        "sample set). A hue-range fix was tried and reverted - see the section "
        "comment above. Unresolved: needs a more selective signal than hue "
        "alone before another attempt is worth making."
    ),
    strict=False,
)
def test_stone_icon_alone_is_not_yet_reliably_classified():
    from rok.ocr import resource_from_icon

    stone = _icon_on_panel("stone")
    if stone is None:
        return
    assert resource_from_icon(stone, 0, stone.width, 0, stone.height) == "stone"


# --------------------------------------------------------------------------- #
# Space-grouped thousands (non-comma locales)
# --------------------------------------------------------------------------- #

def test_split_numeric_words_merge_across_a_small_gap():
    """Regression: a real report read a French client's "257 609" (space-
    grouped thousands) as just 609. Tesseract treats a rendered space as a
    word boundary like any other, reporting "257" and "609" as two separate
    words only ~10px apart on a ~30px-tall line - both _count_word (first
    number after the name) and the unrecognised-name fallback (last number
    on the line) assumed a troop count is always one token, so each kept
    only half the real number instead of failing loudly. Fixed by merging
    adjacent numeric-only words across a small gap before either path picks
    from them - see _merge_split_numbers in rok/ocr.py.
    """
    from rok.ocr import _Line, _Word

    line = _Line([
        _Word("Janissaire", 1541, 688, 1708, 718),
        _Word("257", 1725, 689, 1784, 718),
        _Word("609", 1794, 689, 1853, 718),
    ])
    words = line.numeric_words()
    assert len(words) == 1, "the two close tokens must merge into one"
    assert words[0].text == "257 609"


def test_a_genuinely_separate_number_does_not_merge():
    # A tier numeral or other stray digit far to the left of the real count
    # must not get swept into it just because both are numeric-looking.
    from rok.ocr import _Line, _Word

    line = _Line([
        _Word("4", 100, 690, 115, 718),  # unrelated, far away
        _Word("Janissaire", 1541, 688, 1708, 718),
        _Word("257", 1725, 689, 1784, 718),
        _Word("609", 1794, 689, 1853, 718),
    ])
    words = line.numeric_words()
    assert [w.text for w in words] == ["4", "257 609"]


def test_a_stray_digit_beside_an_already_complete_number_does_not_merge():
    """Regression: the gap check alone was not enough of a guard. On the
    siege grid screen (rok/troop_grid.py, which reuses this same function),
    a stray OCR misread of a weapon-glyph icon ("#8" - Tesseract reading
    icon art as text) sat only 13px from a real count - closer than some
    genuine split-number gaps - and merged into it, turning a correct
    55,965 into 855,965. What actually distinguishes them: every group after
    the first in a real thousands-grouped number is exactly three digits;
    a stray single digit fused onto an already-multi-digit number is not.
    """
    from rok.ocr import _Line, _Word

    line = _Line([
        _Word("8", 352, 803, 392, 863),  # stray icon misread
        _Word("55965", 405, 823, 550, 859),  # already a complete number
    ])
    words = line.numeric_words()
    assert [w.text for w in words] == ["8", "55965"]


def test_french_space_grouped_hospital_screenshot_reads_correctly():
    """The real screenshot behind the regression above: a French client,
    "Blessés graves" (Severely Wounded), counts printed with spaces
    ("171 500", "257 609", ...). Janissaire is not in data/units.json (an
    Ottoman/French-flavoured name), so this specifically exercises the
    unrecognised-name fallback path, not the happy path.
    """
    from rok import ocr as ocr_module

    path = IMAGES / "38_french_space_grouped_thousands.webp"
    if not path.exists():
        return
    image_bytes = path.read_bytes()
    readings = ocr_module.read_all(image_bytes, list(TABLE.units))
    rows_by_name = {r.raw_name: r.count for r in readings[0].rows}
    assert rows_by_name.get("Janissaire") == 257_609


def test_turkish_period_grouped_hospital_counts_read_correctly():
    """A Turkish client (period-grouped thousands, e.g. "149.177") reading
    correctly already - included as a companion to the French/space-grouped
    case above so both locales stay covered, not because this one was ever
    broken (rok/parse.py's parse_number already treated a bare period as a
    thousands separator; only the space case needed a fix).
    """
    from rok import ocr as ocr_module

    path = IMAGES / "39_turkish_period_grouped_thousands.webp"
    if not path.exists():
        return
    image_bytes = path.read_bytes()
    readings = ocr_module.read_all(image_bytes, list(TABLE.units))
    counts = sorted(r.count for r in readings[0].rows if r.count)
    assert 82_877 in counts
    assert 149_177 in counts
    assert 100_362 in counts
    assert 102_914 in counts


def test_a_row_clipped_by_the_game_s_own_ui_does_not_get_a_wrong_tier():
    """Production report: a Crossbowman row (name-table tier T4) came back as
    T5 in production and T3 in a local repro - neither correct, and
    disagreeing between environments pointed at a bug, not just noise.

    The portrait was genuinely truncated, but not by the screenshot's own
    edge - there was plenty of clear space below it. It was clipped by the
    game's OWN modal layout: the resource-cost strip ("96.1M / 70.2M / ...")
    starts right where this row's portrait would continue, inside the dialog
    itself. Per-row detection (_portrait_box) correctly found nothing and
    returned None for this row - confirmed directly during the investigation.
    The wrong tier came from the fallback: _fit_portrait_column() pools
    geometry from the OTHER (clean) rows and applies it to every row
    uniformly, without ever checking whether a real frame exists at that
    position for this specific row. Over a clipped row, that pooled box
    landed on UI chrome, not a portrait, and tier_from_portrait() happily
    scored whatever colour was there. See _fit_box_has_frame in rok/ocr.py
    for the fix: require an actual frame ring at the fitted position before
    trusting a reading from it.
    """
    from rok import ocr as ocr_module

    path = IMAGES / "40_clipped_row_wrong_tier_english.webp"
    if not path.exists():
        return
    image_bytes = path.read_bytes()
    readings = ocr_module.read_all(image_bytes, list(TABLE.units))
    rows_by_name = {r.raw_name: r for r in readings[0].rows}
    row = rows_by_name.get("crossbowman")
    assert row is not None
    assert row.tier in (None, "T4")


def test_alliance_auto_heal_banner_fraction_does_not_corrupt_the_totals():
    """Production report: a Vietnamese screenshot's T5 axe-thrower row
    (197,230 troops, clearly visible) got a "needs review" warning instead
    of reading cleanly.

    The window shows two alliance auto-heal banners above the troop list
    ("<name> auto-helped heal your units. 10/30"), each with its own small
    fraction - out of a fixed 30 help slots, a game constant unrelated to
    hospital capacity. Nothing in this window calls that banner out by name
    the way "Battering Ram Zone" does, so its fraction fell through to
    _read_totals' capacity-based default and got recorded as the ram-zone
    total (0/50,000 corrupted to 10/30). Every row's scale is measured from
    that total, so the whole read broke, not just one row.

    A real wounded/ram-zone capacity is never anywhere near this small - see
    the floor added in _read_totals (rok/ocr.py) for the fix, which rejects
    a fraction that small without ever having to string-match the banner's
    (language-dependent) wording.
    """
    from rok import ocr as ocr_module

    path = IMAGES / "42_vietnamese_t5_axethrower_warning.webp"
    if not path.exists():
        pytest.skip("sample image not present locally")
    image_bytes = path.read_bytes()
    readings = ocr_module.read_all(image_bytes, list(TABLE.units))
    reading = readings[0]
    assert reading.ram_capacity == 50_000
    assert reading.wounded_capacity == 584_400
    counts = {r.count: r.tier for r in reading.rows}
    assert counts.get(197_230) == "T5"
