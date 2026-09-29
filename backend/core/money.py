"""Money = integer paise (ADR-0004).

Every stored monetary amount is an integer number of paise in a `bigint`. This
makes *balance = sum of postings* an exact integer sum with zero float / rounding
drift — the double-entry sum-to-zero checksum is exact. There is no `float` on
any code path here: conversions use `Decimal`/`int` only.

Lakh/Crore *display* formatting and the ₹ input widget are K9 — these helpers do
only lossless conversion.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, TypeVar

from django.db.models import BigIntegerField
from django.db.models.expressions import Combinable

PAISE_PER_RUPEE = 100

_ST = TypeVar("_ST")
_GT = TypeVar("_GT")

# `BigIntegerField` is generic to django-stubs but not subscriptable at runtime,
# so the base is split. Two properties matter and both are load-bearing:
#
#   * the two type parameters stay *free* rather than pinned, which is what makes
#     `MoneyField` itself generic — and a generic field class is the only place
#     django-stubs' plugin can record a per-column answer (it fills the
#     parameters via `copy_modified`). Pin them and every money column in the
#     codebase gets one shared type, which is the bug described on the class
#     below;
#   * the runtime shim only has to make the base subscriptable so the class
#     statement below parses. It adds one inert class to the MRO and changes no
#     field behaviour: `deconstruct()` still reports `core.money.MoneyField`, so
#     migrations are untouched. This is the same trick as
#     `django_stubs_ext.monkeypatch()`, done locally because `django-stubs-ext`
#     ships in the dev dependency group and settings must not import it.
if TYPE_CHECKING:
    _MoneyBase = BigIntegerField[_ST, _GT]
else:

    class _MoneyBase(BigIntegerField):
        def __class_getitem__(cls, _item: Any) -> type[_MoneyBase]:
            return cls


class MoneyField(_MoneyBase[_ST, _GT]):
    """A signed amount in integer paise. The stored Python value is always `int`.

    A semantic subclass of `BigIntegerField` that, unlike its parent, refuses to
    *coerce*: plain `BigIntegerField` runs `int(value)`, so a stray `1.9` or
    `Decimal("1.9")` silently truncates to `1` on write. A money column must never
    swallow that — a `float`/`Decimal` reaching here means precision was already
    lost upstream. Convert at the edge with `rupees_to_paise()`; the field accepts
    only `int` (and `None`), rejecting `bool`, `float`, `Decimal` and everything
    else. This guards every write path, not just the helper.

    **How this types.** The two `_pyi_private_*` markers are how django-stubs'
    plugin decides what `instance.amount_paise` is, and they are the reason a
    money column now types honestly. This class previously pinned the base to
    `BigIntegerField[int | None, int | None]`, which typed *every* money column
    `int | None` whether or not it was nullable — 44 of the 49 in this codebase
    are `NOT NULL`. The cost was not cosmetic: it made every arithmetic
    expression on money a type error, so `strict` could say nothing useful about
    the one part of the system that most needs it, and that noise is a large part
    of why the gate was still parked on `core config`. Worse, the obvious way to
    silence it — an `or 0` on each read — writes a workaround for a *false*
    nullable into the money paths, and a real `None` would then post as a
    genuine zero.

    With the parameters left free above, the plugin reads each column's own
    `null=` and hands back `int` for the 44 and `int | None` for the 5 that
    really are nullable.

    The set type keeps `Combinable` so `F("amount_paise") + 1` still assigns, but
    drops `float | str`: the runtime has always rejected those (`_ensure_int_paise`),
    and mypy can now refuse them at the keystroke rather than in production.
    """

    _pyi_private_set_type: int | Combinable
    _pyi_private_get_type: int
    _pyi_lookup_exact_type: int

    description = "Money amount in integer paise"

    def _ensure_int_paise(self, value: Any) -> int | None:
        if value is None:
            return None
        # bool is an int subclass, but True/False as an amount is always a bug.
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(
                f"{type(self).__name__} stores integer paise; refusing "
                f"{type(value).__name__} {value!r}. Convert at the edge with "
                "rupees_to_paise() — a float/Decimal must never reach a money column."
            )
        return value

    def get_prep_value(self, value: Any) -> int | None:
        # The DB-write boundary: this is where BigIntegerField would coerce.
        return self._ensure_int_paise(value)

    def to_python(self, value: Any) -> int | None:
        # The deserialization / full_clean() boundary.
        return self._ensure_int_paise(value)


def rupees_to_paise(rupees: Decimal | str | int) -> int:
    """Convert a rupee amount to integer paise, half-up at the paise.

    Accepts `Decimal`, `str` or `int` — never `float` (a float would already have
    lost precision before it got here). Rounds half-up to whole paise, the
    India-standard rule (ADR-0004).
    """
    if isinstance(rupees, bool) or isinstance(rupees, float):
        raise TypeError(f"pass Decimal/str/int, never {type(rupees).__name__}")
    amount = rupees if isinstance(rupees, Decimal) else Decimal(rupees)
    paise = (amount * PAISE_PER_RUPEE).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(paise)


def paise_to_rupees_str(paise: int) -> str:
    """Render integer paise as a plain `"<rupees>.<paise>"` string (no grouping).

    Lakh/Crore grouping and the ₹ symbol are K9; this is the lossless inverse of
    `rupees_to_paise` for round-tripping and tests.
    """
    if isinstance(paise, bool) or not isinstance(paise, int):
        raise TypeError(f"paise must be int, got {type(paise).__name__}")
    sign = "-" if paise < 0 else ""
    rupees, sub = divmod(abs(paise), PAISE_PER_RUPEE)
    return f"{sign}{rupees}.{sub:02d}"
