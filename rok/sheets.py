"""Google Sheets writer.

Three tabs:
  Roster       - you maintain this from your kingdom scan (ID -> Name). Read-only here.
  Submissions  - one row per governor, upserted, the live drop-power view.
  Troops       - one row per (governor, unit), rewritten whenever they resubmit.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from .session import Submission
from .units import TIERS, TYPES

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
ROSTER_TTL = timedelta(minutes=5)

SUBMISSION_HEADERS = [
    "Updated (UTC)", "Governor ID", "Name", "Discord User",
    # The headline figure: always readable, whatever the list is scrolled to.
    "Total In Hospital", "Wounded", "Ram Zone", "Identified", "Unaccounted",
    "Food", "Wood", "Stone", "Gold", "RSS Exact?",
    "T4+T5 Troops", "Fill Check", "Fill Note",
    "Total Power", "Power Is Minimum?",
    *TIERS, *TYPES,
    "Tiers", "Types", "Status", "Read By",
    "Screenshot", "Discord Message", "All Images", "Notes",
]

TROOP_HEADERS = [
    "Updated (UTC)", "Governor ID", "Name", "Unit", "Tier", "Type",
    "Count", "Power Each", "Power Total", "Ram Zone?",
]

SIEGE_CHECK_HEADERS = [
    "Updated (UTC)", "Governor ID", "Name", "Discord User",
    "T1 Siege", "T2 Siege", "T3 Siege", "T4 Siege", "T5 Siege", "Total Siege",
    "Verdict", "Notes",
    "Screenshot", "Discord Message",
]


class SheetsClient:
    def __init__(
        self,
        table,
        credentials_file: Path,
        spreadsheet_id: str,
        roster_tab: str = "Roster",
        submissions_tab: str = "Submissions",
        troops_tab: str = "Troops",
        siege_check_tab: str = "SiegeCheck",
    ):
        self.table = table
        self.credentials_file = credentials_file
        self.spreadsheet_id = spreadsheet_id
        self.roster_tab = roster_tab
        self.submissions_tab = submissions_tab
        self.troops_tab = troops_tab
        self.siege_check_tab = siege_check_tab

        self._lock = threading.Lock()
        self._spreadsheet = None
        self._roster: dict[str, str] = {}
        self._roster_fetched: datetime | None = None

    # ---------- connection ----------

    def _open(self):
        if self._spreadsheet is None:
            creds = Credentials.from_service_account_file(
                str(self.credentials_file), scopes=SCOPES
            )
            self._spreadsheet = gspread.authorize(creds).open_by_key(self.spreadsheet_id)
        return self._spreadsheet

    def _worksheet(self, title: str, headers: list[str] | None = None):
        sheet = self._open()
        try:
            worksheet = sheet.worksheet(title)
        except gspread.WorksheetNotFound:
            worksheet = sheet.add_worksheet(
                title=title, rows=1000, cols=max(len(headers or []), 12)
            )
            if headers:
                worksheet.update([headers], "A1")
                worksheet.freeze(rows=1)
            return worksheet

        if headers and not worksheet.acell("A1").value:
            worksheet.update([headers], "A1")
            worksheet.freeze(rows=1)
        return worksheet

    def check_access(self) -> str:
        """Raise if the sheet is unreachable; otherwise return its title."""
        return self._open().title

    # ---------- roster ----------

    def roster(self, force: bool = False) -> dict[str, str]:
        """ID -> Name from the scan tab, cached briefly so edits show up on their own."""
        with self._lock:
            fresh = (
                self._roster_fetched is not None
                and datetime.now(timezone.utc) - self._roster_fetched < ROSTER_TTL
            )
            if fresh and not force:
                return self._roster

            try:
                rows = self._worksheet(self.roster_tab).get_all_values()
            except gspread.WorksheetNotFound:
                log.warning("Roster tab %r not found.", self.roster_tab)
                return self._roster

            self._roster = _parse_roster(rows)
            self._roster_fetched = datetime.now(timezone.utc)
            return self._roster

    def name_for(self, governor_id: str) -> str:
        return self.roster().get(str(governor_id).strip(), "")

    def campaign_totals(self) -> dict:
        """Roll up the Submissions tab: how many players, how much power dropped."""
        try:
            rows = self._worksheet(self.submissions_tab, SUBMISSION_HEADERS).get_all_values()
        except Exception:
            return {"submissions": 0, "power": 0, "troops": 0, "passed": 0, "failed": 0}
        if len(rows) < 2:
            return {"submissions": 0, "power": 0, "troops": 0, "passed": 0, "failed": 0}

        header = [h.strip().lower() for h in rows[0]]

        def column(name: str) -> int | None:
            try:
                return header.index(name.lower())
            except ValueError:
                return None

        power_at = column("Total Power")
        troops_at = column("Total In Hospital")
        check_at = column("Fill Check")

        def number(row: list[str], index: int | None) -> int:
            if index is None or index >= len(row):
                return 0
            digits = row[index].replace(",", "").strip()
            try:
                return int(float(digits))
            except ValueError:
                return 0

        body = [r for r in rows[1:] if any(c.strip() for c in r)]
        verdicts = [
            r[check_at].strip().lower() if check_at is not None and check_at < len(r) else ""
            for r in body
        ]
        return {
            "submissions": len(body),
            "power": sum(number(r, power_at) for r in body),
            "troops": sum(number(r, troops_at) for r in body),
            "passed": sum(1 for v in verdicts if v == "pass"),
            "failed": sum(1 for v in verdicts if v == "fail"),
        }

    # ---------- admin edits ----------

    # Fields an admin may correct by hand. Anything derived from several others
    # (power, tier counts) is left out: editing one of those would leave the row
    # disagreeing with itself.
    EDITABLE = (
        "Name",
        "Total In Hospital",
        "T4+T5 Troops",
        "Food",
        "Wood",
        "Stone",
        "Gold",
        "Fill Check",
        "Notes",
    )
    NUMERIC = ("Total In Hospital", "T4+T5 Troops", "Food", "Wood", "Stone", "Gold")

    def _find_row(self, worksheet, governor_id: str) -> int | None:
        for index, value in enumerate(worksheet.col_values(2)[1:], start=2):
            if value.strip() == str(governor_id).strip():
                return index
        return None

    def remove(self, governor_id: str) -> dict | None:
        """Delete a governor from both tabs. Returns the row that was removed."""
        governor_id = str(governor_id).strip()
        submissions = self._worksheet(self.submissions_tab, SUBMISSION_HEADERS)
        index = self._find_row(submissions, governor_id)
        if index is None:
            return None

        removed = dict(zip(SUBMISSION_HEADERS, submissions.row_values(index)))
        submissions.delete_rows(index)

        troops = self._worksheet(self.troops_tab, TROOP_HEADERS)
        stale = [
            i for i, v in enumerate(troops.col_values(2)[1:], start=2)
            if v.strip() == governor_id
        ]
        for i in reversed(stale):  # descending so earlier indices stay valid
            troops.delete_rows(i)
        return removed

    def edit(self, governor_id: str, field: str, value: str, editor: str = "") -> str:
        """Set one field on a governor's row. Returns a description of the change."""
        if field not in self.EDITABLE:
            raise ValueError(f"{field} is not editable.")
        if field in self.NUMERIC:
            cleaned = value.replace(",", "").replace(" ", "").strip()
            try:
                value = str(int(float(cleaned)))
            except ValueError:
                raise ValueError(f"{field} needs a number, got {value!r}.")

        worksheet = self._worksheet(self.submissions_tab, SUBMISSION_HEADERS)
        index = self._find_row(worksheet, governor_id)
        if index is None:
            raise LookupError(f"No row for governor {governor_id}.")

        column = SUBMISSION_HEADERS.index(field) + 1
        before = worksheet.cell(index, column).value or ""
        worksheet.update_cell(index, column, _cell(value))

        note = f"{field}: {before or '(blank)'} -> {value}"

        # The fill verdict is derived from the T4+T5 count, so correcting the
        # count has to recompute it or the row would contradict itself.
        if field == "T4+T5 Troops":
            need = self.table.min_high_tier_troops
            tiers = " or ".join(self.table.high_tiers)
            seen = int(value)
            verdict = "Pass" if seen >= need else "FAIL"
            reason = (
                f"{seen:,} {tiers} troops (set by {editor or 'an admin'}), "
                f"{'at or above' if verdict == 'Pass' else 'below'} the {need:,} required."
            )
            worksheet.update_cell(index, SUBMISSION_HEADERS.index("Fill Check") + 1, verdict)
            worksheet.update_cell(index, SUBMISSION_HEADERS.index("Fill Note") + 1, _cell(reason))
            note += f"; fill check recomputed to {verdict}"

        # Leave a trail: a hand-edited row should never be mistaken for a read one.
        if field != "Notes":
            notes_column = SUBMISSION_HEADERS.index("Notes") + 1
            existing = worksheet.cell(index, notes_column).value or ""
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            trail = f"edited by {editor or 'admin'} {stamp}: {note}"
            merged = (existing + " | " + trail).strip(" |")[:1000]
            worksheet.update_cell(index, notes_column, _cell(merged))
        return note

    def clear_all(self) -> int:
        """Delete every recorded hospital submission. Returns how many rows went.

        Headers stay; only the data rows are removed. There is no undo.
        Leaves the SiegeCheck tab alone - see clear_siege_all() for that.
        """
        removed = 0
        for tab, headers in (
            (self.submissions_tab, SUBMISSION_HEADERS),
            (self.troops_tab, TROOP_HEADERS),
        ):
            worksheet = self._worksheet(tab, headers)
            rows = len(worksheet.get_all_values())
            if rows > 1:
                worksheet.delete_rows(2, rows)
                if tab == self.submissions_tab:
                    removed = rows - 1
        return removed

    def submission_count(self) -> int:
        worksheet = self._worksheet(self.submissions_tab, SUBMISSION_HEADERS)
        return max(0, len(worksheet.get_all_values()) - 1)

    def clear_siege_all(self) -> int:
        """Delete every recorded siege-check result. Returns how many rows went.

        Separate from clear_all() so the two checks can be reset
        independently - clearing siege results for a new check-in window
        should not also wipe unrelated hospital-drop data, or vice versa.
        """
        worksheet = self._worksheet(self.siege_check_tab, SIEGE_CHECK_HEADERS)
        rows = len(worksheet.get_all_values())
        if rows <= 1:
            return 0
        worksheet.delete_rows(2, rows)
        return rows - 1

    def siege_submission_count(self) -> int:
        worksheet = self._worksheet(self.siege_check_tab, SIEGE_CHECK_HEADERS)
        return max(0, len(worksheet.get_all_values()) - 1)

    # ---------- writing ----------

    def write(self, submission: Submission, name: str = "") -> None:
        name = name or self.name_for(submission.governor_id)
        stamp = submission.updated_at.strftime("%Y-%m-%d %H:%M:%S")
        record = submission_record(submission, name, self.table, stamp)
        self._upsert(self.submissions_tab, SUBMISSION_HEADERS, submission.governor_id, record)

        rows = submission.all_rows
        troop_records = [
            [
                stamp,
                submission.governor_id,
                name,
                row.name,
                row.tier or "?",
                row.type or "?",
                row.count,
                row.power_each if row.power_each is not None else "",
                row.power_total if row.known else "",
                "Yes" if row.in_ram_zone else "No",
            ]
            for row in rows
        ]
        self._replace_rows(self.troops_tab, TROOP_HEADERS, submission.governor_id, troop_records)

    def write_siege(
        self,
        governor_id: str,
        name: str,
        discord_user: str,
        by_tier: dict,
        verdict: str,
        notes: str,
        image_url: str,
        message_url: str,
    ) -> None:
        """Upsert one governor's siege-composition check. One row per governor,
        same as Submissions - a resubmission overwrites their previous result.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        record = [
            stamp, governor_id, name, discord_user,
            *[by_tier.get(t, 0) for t in ("T1", "T2", "T3", "T4", "T5")],
            sum(by_tier.values()),
            verdict, notes,
            _link(image_url, "Open image"),
            _link(message_url, "Go to message"),
        ]
        self._upsert(self.siege_check_tab, SIEGE_CHECK_HEADERS, governor_id, record)

    def _upsert(self, tab: str, headers: list[str], governor_id: str, record: list) -> None:
        worksheet = self._worksheet(tab, headers)
        ids = worksheet.col_values(2)  # column B holds the governor ID
        record = [_cell(v) for v in record]

        for index, value in enumerate(ids[1:], start=2):
            if value.strip() == governor_id:
                end = _column_letter(len(record))
                worksheet.update(
                    [record], f"A{index}:{end}{index}", value_input_option="USER_ENTERED"
                )
                return
        worksheet.append_row(record, value_input_option="USER_ENTERED")

    def _replace_rows(
        self, tab: str, headers: list[str], governor_id: str, records: list[list]
    ) -> None:
        worksheet = self._worksheet(tab, headers)
        ids = worksheet.col_values(2)
        stale = [i for i, v in enumerate(ids[1:], start=2) if v.strip() == governor_id]
        for index in reversed(stale):  # descending so earlier indices stay valid
            worksheet.delete_rows(index)
        if records:
            worksheet.append_rows(
                [[_cell(v) for v in r] for r in records],
                value_input_option="USER_ENTERED",
            )


def submission_record(
    submission: Submission, name: str, table, stamp: str
) -> list:
    """Build the Submissions row. Kept separate so its width can be tested.

    A row that does not match SUBMISSION_HEADERS shifts every later value into the
    wrong column, and nothing about the sheet would look obviously broken.
    """
    summary = submission.summary
    unaccounted = submission.unaccounted or 0
    high_tier = submission.high_tier_troops(table)
    verdict, note = submission.fill_check(table)

    return [
        stamp,
        submission.governor_id,
        name,
        submission.discord_user_name,
        submission.total_in_hospital,
        submission.wounded_current,
        submission.ram_current,
        submission.identified_troops,
        unaccounted,
        submission.food,
        submission.wood,
        submission.stone,
        submission.gold,
        "No (rounded by game)" if submission.rss_approx else "Yes",
        high_tier,
        verdict,
        note,
        summary["total_power"],
        # Power only covers troops attributed to a unit, so with any unaccounted
        # troops the real drop is higher than this figure.
        "Yes" if (unaccounted or summary["power_is_partial"]) else "No",
        *[summary["by_tier"][t] for t in TIERS],
        *[summary["by_type"][t] for t in TYPES],
        ", ".join(summary["tiers_present"]),
        ", ".join(summary["types_present"]),
        submission.status,
        "/".join(dict.fromkeys(submission.sources)),
        _link(submission.image_urls[-1] if submission.image_urls else "", "Open image"),
        _link(submission.message_urls[-1] if submission.message_urls else "", "Go to message"),
        "\n".join(dict.fromkeys(submission.image_urls)),
        " | ".join(dict.fromkeys(submission.missing() + submission.notes))[:1000],
    ]


def _link(url: str, label: str) -> str:
    """A clickable cell. Empty string when there is no URL to link to."""
    if not url:
        return ""
    safe = url.replace('"', "%22")
    return f'=HYPERLINK("{safe}", "{label}")'


def _cell(value):
    """Stop a stray leading =, +, - or @ in text from being read as a formula.

    The row is written with USER_ENTERED so the HYPERLINK cells work, which means
    a governor name like "+Bob" would otherwise be parsed as one.
    """
    if value is None:
        return ""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _parse_roster(rows: list[list[str]]) -> dict[str, str]:
    """Find the ID and Name columns by header, tolerating extra scan columns."""
    if not rows:
        return {}

    id_col, name_col, start = 0, 1, 0
    for index, row in enumerate(rows[:5]):
        lowered = [c.strip().lower() for c in row]
        id_match = next(
            (i for i, c in enumerate(lowered) if c in ("id", "governor id", "governorid")), None
        )
        name_match = next(
            (i for i, c in enumerate(lowered) if c in ("name", "governor name", "governor")), None
        )
        if id_match is not None and name_match is not None:
            id_col, name_col, start = id_match, name_match, index + 1
            break

    roster: dict[str, str] = {}
    for row in rows[start:]:
        if len(row) <= max(id_col, name_col):
            continue
        gid = row[id_col].strip().replace(",", "")
        if gid.isdigit():
            roster[gid] = row[name_col].strip()
    return roster


def _column_letter(count: int) -> str:
    letters = ""
    while count > 0:
        count, remainder = divmod(count - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters
