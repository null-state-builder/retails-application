"""Exact goods-v1 money at its two input edges (overall PRD §15.2.1 rule 6, change PRD §14.4).

* Public JSON carries money as a base-10 integer-paise string: ``"10061"``.
* A spreadsheet cell carries rupees with at most two decimal places: ``100.61``.

Blank or null is unknown and comes back as ``None``; a literal zero is a genuine
zero and comes back as ``0`` - whether zero is allowed is the field's own rule.
Non-finite, out-of-range or over-precise input raises ``MoneyInvalid``: it is
refused, never rounded. Nothing here accepts or produces a binary float.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from core.goods_fields import MAX_UNIT_PAISE

_PAISE_TEXT = re.compile(r"[0-9]+")


class MoneyInvalid(ValueError):  # noqa: N818 - a refused amount, named for the caller
    """An amount that cannot be taken exactly: ``code`` says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def paise_from_json(value: Any, *, maximum: int = MAX_UNIT_PAISE) -> int | None:
    """A JSON money value: ``None`` (unknown) or an integer-paise string from 0 to ``maximum``."""
    if value is None:
        return None
    if not isinstance(value, str) or not _PAISE_TEXT.fullmatch(value):
        raise MoneyInvalid("MONEY_NOT_A_NUMBER", "must be whole paise written as a base-10 string")
    # Compare lengths first: a huge digit string is out of range, and int() refuses
    # very long ones with a plain ValueError.
    digits = value.lstrip("0") or "0"
    if len(digits) > len(str(maximum)) or int(digits) > maximum:
        raise MoneyInvalid("MONEY_OUT_OF_RANGE", f"must be at most {maximum} paise")
    return int(digits)


def paise_from_amount(value: Any, *, maximum: int = MAX_UNIT_PAISE) -> int | None:
    """A spreadsheet rupee amount as exact paise; blank is unknown (``None``).

    Numbers must arrive as ``Decimal`` (the decimal a numeric cell holds) or ``int``;
    text may group thousands with commas. A float is refused, as are dates, booleans
    and error cells.
    """
    amount = _decimal_of(value)
    if amount is None:
        return None
    if not amount.is_finite():
        raise MoneyInvalid("MONEY_NOT_FINITE", "must be a finite number")
    # Read the digits and exponent as written. Decimal arithmetic would round to its
    # 28-digit context (0.1000...0001 x 100 becomes 10) or overflow on a huge exponent.
    negative, digit_tuple, exponent = amount.as_tuple()
    written = "".join(str(d) for d in digit_tuple).lstrip("0")
    if not written:
        return 0
    significant = written.rstrip("0")
    # The power of ten that turns the significant digits into paise.
    places = int(exponent) + len(written) - len(significant) + 2
    if places < 0:
        raise MoneyInvalid("MONEY_PRECISION", "must have at most two decimal places")
    out_of_range = MoneyInvalid(
        "MONEY_OUT_OF_RANGE", f"must be from 0.00 to {amount_text(maximum)} rupees"
    )
    if negative or len(significant) + places > len(str(maximum)):
        raise out_of_range
    paise = int(significant + "0" * places)
    if paise > maximum:
        raise out_of_range
    return paise


def _decimal_of(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if not isinstance(value, str):
        raise MoneyInvalid("MONEY_NOT_A_NUMBER", "must be a number")
    text = value.strip().replace(",", "")
    if not text:
        return None
    if "_" in text:
        raise MoneyInvalid("MONEY_NOT_A_NUMBER", "must be a number")
    try:
        return Decimal(text)
    except InvalidOperation:
        raise MoneyInvalid("MONEY_NOT_A_NUMBER", "must be a number") from None


def amount_text(paise: int) -> str:
    """Exact rupees with two places for ``paise`` >= 0: ``10061`` -> ``"100.61"``."""
    if isinstance(paise, bool) or not isinstance(paise, int) or paise < 0:
        raise ValueError(f"amount_text needs non-negative integer paise, got {paise!r}")
    return f"{paise // 100}.{paise % 100:02d}"
