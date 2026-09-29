"""Indian financial year helper (Apr–Mar), shared by every numbering series."""

from __future__ import annotations

import re
from datetime import date

#: The FY label's only spelling - two digits, a dash, two digits (`26-27`). The
#: same shape `financial_year` writes, so a label that round-trips is the only
#: one this module accepts.
_FY_LABEL = re.compile(r"^(\d{2})-(\d{2})$")

#: FY labels carry two digits, so reading one back needs a century. KDPS's books
#: begin in this one; a `26-27` meaning 1926 or 2126 is not a case that exists.
_CENTURY = 2000

#: April - the month an Indian financial year opens on.
FY_START_MONTH = 4


def financial_year(d: date | None = None) -> str:
    """`YY-YY` for the Apr–Mar FY of `d` (today if omitted). Jun-2026 → '26-27'."""
    return _fy_label(_fy_start_year(d or date.today()))


def next_financial_year(d: date | None = None) -> str:
    """The FY after `financial_year(d)`. Jun-2026 → '27-28'.

    Seeds use this: a numbering series has to exist *before* the year turns, and
    the turn happens at midnight on 1 April whether or not anyone deployed that
    week. Date arithmetic ("a year from today") gets this wrong around the
    boundary - from 31 March it would skip a year entirely - so the next FY is
    derived from the FY, not from the date.
    """
    return _fy_label(_fy_start_year(d or date.today()) + 1)


def previous_financial_year(fy: str) -> str:
    """The FY before label `fy`. `'26-27'` → `'25-26'`. Raises like `financial_year_months`."""
    return _fy_label(financial_year_months(fy)[0].year - 1)


def financial_year_months(fy: str) -> list[date]:
    """The twelve months of FY `fy`, each as its own first day, April first.

    `'26-27'` → 1 Apr 2026 … 1 Mar 2027. The inverse of `financial_year`, and
    deliberately the *months* rather than a start/end pair: every caller so far
    wants either the twelve columns of a grid or the ends of the range, and
    handing back the months gives both without a second helper that could
    disagree with this one about where March sits.

    Raises `ValueError` on anything `financial_year` would not have written -
    including a well-formed label whose two halves are not consecutive years, so
    `'26-28'` is refused rather than quietly read as 2026-27.
    """
    matched = _FY_LABEL.match(fy.strip())
    if not matched:
        raise ValueError(f"Not a financial year label: {fy!r}. Expected 'YY-YY', e.g. '26-27'.")
    first, second = (int(part) for part in matched.groups())
    if second != (first + 1) % 100:
        raise ValueError(f"Not a financial year: {fy!r}. The two halves must be consecutive years.")
    start = _CENTURY + first
    return [
        date(start + (0 if month >= FY_START_MONTH else 1), month, 1)
        for month in [*range(FY_START_MONTH, 13), *range(1, FY_START_MONTH)]
    ]


def _fy_start_year(d: date) -> int:
    """The calendar year the Apr–Mar FY containing `d` began in."""
    return d.year if d.month >= FY_START_MONTH else d.year - 1


def _fy_label(start_year: int) -> str:
    """`2026` → `'26-27'`. The one place the label's spelling is written."""
    return f"{start_year % 100:02d}-{(start_year + 1) % 100:02d}"
