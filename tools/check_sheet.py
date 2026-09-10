"""Verify the Google Sheets setup, one step at a time.

    python tools/check_sheet.py

Checks the key file, prints the address to share the sheet with, opens the
spreadsheet, and reports what it found in the Roster tab. Every failure comes
with the specific thing to fix rather than a raw API error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rok import config as config_module  # noqa: E402

OK, BAD, INFO = "[ok]", "[--]", "  ->"


def main() -> int:
    cfg = config_module.load()
    print("=" * 68)
    print("Google Sheets setup check")
    print("=" * 68)

    # ---- 1. the key file ----
    path = cfg.google_credentials_file
    if not path.exists():
        print(f"{BAD} No key file at {path}")
        print(f"{INFO} Download the service-account JSON key and save it there.")
        print(f"{INFO} See README step 4.")
        return 1
    try:
        key = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"{BAD} {path.name} is not valid JSON ({exc}).")
        print(f"{INFO} Re-download the key; don't edit it by hand.")
        return 1

    if key.get("type") != "service_account":
        print(f"{BAD} {path.name} is a {key.get('type', 'unknown')!r} key, not a service account.")
        print(f"{INFO} In Credentials, choose 'Service account', not 'OAuth client ID'.")
        return 1

    email = key.get("client_email", "")
    print(f"{OK} Key file: {path.name}")
    print(f"{OK} Project:  {key.get('project_id', '?')}")
    print()
    print("     SHARE YOUR SHEET WITH THIS ADDRESS, AS EDITOR:")
    print(f"     {email}")
    print()

    # ---- 2. the spreadsheet id ----
    if not cfg.spreadsheet_id:
        print(f"{BAD} SPREADSHEET_ID is not set in .env")
        print(f"{INFO} It is the long part of the sheet URL between /d/ and /edit.")
        return 1
    if "/" in cfg.spreadsheet_id or "docs.google" in cfg.spreadsheet_id:
        print(f"{BAD} SPREADSHEET_ID looks like a full URL: {cfg.spreadsheet_id}")
        print(f"{INFO} Use only the id, e.g. 1yEQI8jrdqILHqsEZY7TEDKd5dkBO-eu6xAxEOjm2oXI")
        return 1
    print(f"{OK} Spreadsheet id set ({len(cfg.spreadsheet_id)} chars)")

    # ---- 3. open it ----
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print(f"{BAD} gspread is not installed. Run: pip install -r requirements.txt")
        return 1

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    try:
        creds = Credentials.from_service_account_file(str(path), scopes=scopes)
        sheet = gspread.authorize(creds).open_by_key(cfg.spreadsheet_id)
    except Exception as exc:
        text = str(exc)
        print(f"{BAD} Could not open the spreadsheet.")
        if "PERMISSION_DENIED" in text or "403" in text:
            if "Sheets API has not been used" in text or "disabled" in text:
                print(f"{INFO} The Google Sheets API is not enabled for this project.")
                print(f"{INFO} Enable it: APIs & Services > Library > Google Sheets API.")
            else:
                print(f"{INFO} The sheet is not shared with the service account.")
                print(f"{INFO} Share it with {email} as Editor.")
        elif "404" in text or "not found" in text.lower():
            print(f"{INFO} No sheet with that id. Check SPREADSHEET_ID in .env.")
        else:
            print(f"{INFO} {text[:300]}")
        return 1

    print(f"{OK} Opened: {sheet.title!r}")
    tabs = [w.title for w in sheet.worksheets()]
    print(f"{OK} Tabs:   {', '.join(tabs)}")

    # ---- 4. can we write? ----
    try:
        sheet.worksheets()[0].acell("A1").value
    except Exception as exc:
        print(f"{BAD} Cannot read cells: {exc}")
        return 1

    # ---- 5. the roster ----
    from rok.sheets import SheetsClient
    from rok.units import UnitTable

    client = SheetsClient(
        UnitTable(cfg.units_file), path, cfg.spreadsheet_id,
        cfg.roster_tab, cfg.submissions_tab, cfg.troops_tab,
    )
    if cfg.roster_tab not in tabs:
        print(f"{BAD} No {cfg.roster_tab!r} tab yet.")
        print(f"{INFO} Add a tab named {cfg.roster_tab!r} with headers 'ID' and 'Name',")
        print(f"{INFO} then paste your kingdom scan into it.")
    else:
        roster = client.roster(force=True)
        if roster:
            sample = list(roster.items())[:3]
            print(f"{OK} Roster: {len(roster)} governors, e.g. " +
                  ", ".join(f"{k}={v}" for k, v in sample))
        else:
            print(f"{BAD} {cfg.roster_tab!r} tab has no usable rows.")
            print(f"{INFO} It needs a header row containing 'ID' and 'Name'.")

    print()
    print("Sheets setup is good. The bot will create the Submissions and Troops")
    print("tabs by itself on the first recorded submission.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
