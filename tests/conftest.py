"""Shared pytest setup.

Most of this suite tests pure-Python logic directly (parsing, checksums,
colour geometry) without needing a real OCR pass. test_troop_grid.py is the
first to call through to Tesseract itself, which needs the binary path from
.env the same way bot.py and the tools/ scripts already configure it.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rok import config as config_module, ocr  # noqa: E402

try:
    ocr.configure(config_module.load().tesseract_cmd)
except ocr.TesseractUnavailable:
    pass  # tests that need it will skip/fail on their own with a clear cause
