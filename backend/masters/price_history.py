"""Every movement of a ticket, written where the ticket moves (D11 §4).

One function, called from the two places an MRP can change: a PT posting, which
registers what the brand's tag says, and a deliberate re-ticket from head office.
Both write the same row, so "what was this priced at on the day that bill was
made" has one answer and not two.

Silent about no-ops on purpose: a PT that re-states the same MRP it stated last
month is not a price change, and a trail full of those is a trail nobody reads.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.utils import timezone

from masters.models import PriceChange, Sku


def record_price_change(
    *,
    barcode: str,
    to_paise: int | None,
    from_paise: int | None = None,
    source: str,
    sku: Sku | None = None,
    doc_number: str = "",
    reason: str = "",
    user: Any = None,
    effective_from: date | None = None,
) -> PriceChange | None:
    """Append one movement of a ticket. Returns ``None`` when nothing moved."""
    if to_paise is None or int(to_paise) <= 0:
        return None
    if from_paise is not None and int(from_paise) == int(to_paise):
        return None
    return PriceChange.objects.create(
        barcode=barcode[:64],
        sku=sku,
        from_paise=from_paise,
        to_paise=int(to_paise),
        source=source,
        doc_number=doc_number[:32],
        reason=reason.strip()[:200],
        changed_by=user if getattr(user, "pk", None) else None,
        effective_from=effective_from or timezone.localdate(),
    )
