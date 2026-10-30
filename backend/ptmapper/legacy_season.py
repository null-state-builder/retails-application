"""Legacy season text → the store's two seasons a year (store test run, synthetic only).

The owner chose (28 Oct 2026) two seasons a year: Spring-Summer ``SS-YY`` and
Autumn-Winter ``AW-YY``. The legacy SOH spells them many ways
(``AUTUMN WINTER(Oct-26)``, ``SS-25``, ``KAW24``, ``27-06-2025``...). An
autumn-winter label dated January to June belongs to the winter that began the
year before (``AUTUMN WINTER(Jan-26)`` → ``AW-25``); a bare date is Spring-Summer
from January to June and Autumn-Winter from July. Anything else (blank,
``CORE``, ``ALLSEASON``) is ``None``: the deliberate unknown historical season.

Pure, with no Django import, so ``scripts/normalize_pt_files.py`` uses the same rule.
"""

from __future__ import annotations

import re

MONTHS = {name: number for number, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_LABELLED = re.compile(r"^(AUTUMN\s*WINTER|SPRING\s*SUMMER)\s*\(\s*([A-Za-z]{3})[A-Za-z]*\s*-\s*(\d{2})\s*\)$", re.I)
_CODE = re.compile(r"^K?(AW|SS)\s*-?\s*(\d{2})$", re.I)
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?$")
_DAY_FIRST = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$")


def _by_month(month: int, year: int) -> str:
    return f"{'SS' if month <= 6 else 'AW'}-{year % 100:02d}"


def normalize_season(raw: object) -> str | None:
    """``SS-YY``, ``AW-YY``, or ``None`` when the text names no half-year."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        return None
    found = _CODE.match(text)
    if found:
        return f"{found[1].upper()}-{found[2]}"
    found = _LABELLED.match(text)
    if found:
        month = MONTHS.get(found[2].casefold())
        if month is None:
            return None
        year = int(found[3])
        if found[1].upper().startswith("AUTUMN"):
            return f"AW-{(year - 1 if month <= 6 else year) % 100:02d}"
        return f"SS-{year:02d}"
    found = _ISO_DATE.match(text)
    if found and 1 <= int(found[2]) <= 12:
        return _by_month(int(found[2]), int(found[1]))
    found = _DAY_FIRST.match(text)
    if found and 1 <= int(found[2]) <= 12:
        return _by_month(int(found[2]), int(found[3]))
    return None


def season_name(code: str) -> str:
    """``AW-26`` → ``Autumn-Winter 2026``."""
    half, year = code.split("-")
    return f"{'Autumn-Winter' if half == 'AW' else 'Spring-Summer'} 20{year}"


def season_order(code: str) -> int:
    """Newest sorts highest: a year's SS before its AW."""
    half, year = code.split("-")
    return int(year) * 2 + (1 if half == "AW" else 0)
