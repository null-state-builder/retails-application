"""Pure paise allocation for a partial or final return."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def refund_share(
    *,
    paid_paise: int,
    line_qty: int,
    returning: int,
    returned_qty: int,
    returned_paise: int,
) -> int:
    """Split a sold line's paid value, settling the remainder on its final piece.

    Half-up rounding matches the offline till's golden cases. The caller owns
    validation of quantity, return history and nonnegative paid value.
    """
    if returned_qty + returning >= line_qty:
        return paid_paise - returned_paise
    share = Decimal(paid_paise) * returning / line_qty
    return int(share.quantize(Decimal(1), rounding=ROUND_HALF_UP))
