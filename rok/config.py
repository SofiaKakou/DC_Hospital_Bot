"""Environment configuration. Everything the bot needs comes from .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


# Values left as-is from .env.example must not be mistaken for real configuration -
# an unedited "sk-ant-..." would otherwise look like a key and fail at call time.
_PLACEHOLDERS = {
    "your-bot-token",
    "sk-ant-...",
    "123456789012345678",
    "service_account.json-path-here",
    "your-google-sheet-id",
}


def _str(name: str, default: str = "") -> str:
    raw = os.getenv(name, "").strip()
    return default if raw in _PLACEHOLDERS else (raw or default)


def _int(name: str, default: int | None = None) -> int | None:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} in .env must be a whole number, got {raw!r}")


@dataclass(frozen=True)
class Config:
    discord_token: str
    submission_channel_id: int | None
    verification_log_channel_id: int | None
    siege_check_channel_id: int | None
    admin_role_id: int | None

    anthropic_api_key: str
    vision_model: str

    google_credentials_file: Path
    spreadsheet_id: str
    roster_tab: str
    submissions_tab: str
    troops_tab: str
    siege_check_tab: str

    tesseract_cmd: str
    session_timeout_minutes: int
    save_submissions_dir: Path | None
    submission_workers: int

    units_file: Path = ROOT / "data" / "units.json"

    @property
    def vision_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def sheets_enabled(self) -> bool:
        return bool(self.spreadsheet_id) and self.google_credentials_file.exists()


def _resolve_credentials_file() -> Path:
    """Where the service-account key actually lives.

    Locally this is just GOOGLE_CREDENTIALS_FILE, a path to a file that sits
    next to bot.py and is gitignored. A host like Railway has nowhere to put
    that file before the container even starts, so it instead gets the key's
    full JSON content as one environment variable (GOOGLE_CREDENTIALS_JSON,
    pasted into the platform's dashboard) - this writes that out to a real
    file once at startup so the rest of the code never has to care which
    source it came from.
    """
    raw_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if raw_json:
        path = ROOT / "service_account.runtime.json"
        # Only rewritten when missing/changed - avoids a pointless disk write
        # (and a changed mtime) on every restart when it already matches.
        if not path.exists() or path.read_text(encoding="utf-8") != raw_json:
            path.write_text(raw_json, encoding="utf-8")
        return path

    creds = _str("GOOGLE_CREDENTIALS_FILE", "service_account.json")
    creds_path = Path(creds)
    if not creds_path.is_absolute():
        creds_path = ROOT / creds_path
    return creds_path


def load() -> Config:
    creds_path = _resolve_credentials_file()

    return Config(
        discord_token=_str("DISCORD_TOKEN"),
        submission_channel_id=_int("SUBMISSION_CHANNEL_ID"),
        # Where every verification result is mirrored, whatever the outcome -
        # separate from the submission channel so admins can watch results
        # without sitting in the (potentially noisy) player-facing channel.
        # Optional: leave unset and nothing is mirrored anywhere.
        verification_log_channel_id=_int("VERIFICATION_LOG_CHANNEL_ID"),
        # A separate submission channel for the siege-composition check - its
        # own screenshot type (Troop Details, not Unit Healing) and its own
        # rules, sharing the roster/verification-log plumbing but otherwise
        # independent of the hospital-drop flow.
        siege_check_channel_id=_int("SIEGE_CHECK_CHANNEL_ID"),
        admin_role_id=_int("ADMIN_ROLE_ID"),
        anthropic_api_key=_str("ANTHROPIC_API_KEY"),
        vision_model=_str("VISION_MODEL", "claude-opus-5"),
        google_credentials_file=creds_path,
        spreadsheet_id=_str("SPREADSHEET_ID"),
        roster_tab=_str("ROSTER_TAB", "Roster"),
        submissions_tab=_str("SUBMISSIONS_TAB", "Submissions"),
        troops_tab=_str("TROOPS_TAB", "Troops"),
        siege_check_tab=_str("SIEGE_CHECK_TAB", "SiegeCheck"),
        tesseract_cmd=_str("TESSERACT_CMD"),
        save_submissions_dir=(
            (ROOT / _str("SAVE_SUBMISSIONS_DIR", "submissions")).resolve()
            if _str("SAVE_SUBMISSIONS_DIR", "submissions").lower() not in ("", "off", "none")
            else None
        ),
        session_timeout_minutes=_int("SESSION_TIMEOUT_MINUTES", 30) or 30,
        # How many screenshots get OCR'd at once. Reading is the slow, CPU-bound
        # part - reacting and downloading the image happen immediately either
        # way, so a burst of submissions queues for OCR rather than blocking
        # the next player's reaction. Too high a number just makes every
        # submission in the burst equally slow instead of helping; 2 is a
        # reasonable default for a single-core box.
        submission_workers=_int("SUBMISSION_WORKERS", 2) or 2,
    )
