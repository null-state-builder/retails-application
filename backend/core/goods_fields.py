"""Column types shared by goods-v1 tables (design §3.4, §5.1).

* ``PaiseField`` is ``NUMERIC(30,0)`` integer paise. A 50,000-line document of
  maximum-value lines can exceed a 64-bit sum, so aggregates and stored money use
  the wide exact type; Python always sees an ``int`` and never a float.
* ``PortionField`` is ``INT8RANGE`` with finite, lower-inclusive/upper-exclusive
  integer bounds - a bookkeeping portion of one custody lot, never a garment serial.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.contrib.postgres.fields import BigIntegerRangeField
from django.db import models
from django.db.backends.postgresql.psycopg_any import NumericRange

MAX_UNIT_PAISE = 99_999_999_999  # ₹999,999,999.99
MAX_LINE_QTY = 999_999


class PaiseField(models.DecimalField):  # type: ignore[type-arg]
    description = "Integer paise stored as NUMERIC(30,0)"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["max_digits"] = 30
        kwargs["decimal_places"] = 0
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> Any:
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop("max_digits", None)
        kwargs.pop("decimal_places", None)
        return name, path, args, kwargs

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> int | None:
        if value is None:
            return None
        return int(value)

    def to_python(self, value: Any) -> Any:
        if value is None or isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, Decimal) and value == value.to_integral_value():
            return int(value)
        if isinstance(value, str) and value.lstrip("-").isdigit():
            return int(value)
        raise TypeError(f"paise must be an integer, got {value!r}")

    def get_prep_value(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"paise must be an integer, got {type(value).__name__} {value!r}")
        return Decimal(value)


class PortionField(BigIntegerRangeField):
    description = "Half-open bookkeeping portion of a custody lot"


def portion(lower: int, upper: int) -> NumericRange:
    if lower < 0 or upper <= lower:
        raise ValueError(f"invalid portion [{lower},{upper})")
    return NumericRange(lower, upper, "[)")


def bounds(value: Any) -> tuple[int, int]:
    """(lower, upper) of a stored portion, normalised to [lower, upper)."""
    lower = int(value.lower)
    upper = int(value.upper)
    if not value.lower_inc:
        lower += 1
    if value.upper_inc:
        upper += 1
    return lower, upper
