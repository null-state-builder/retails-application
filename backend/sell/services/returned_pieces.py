"""Pieces a customer brought back, waiting for somebody to put them away.

OPS-07 built the mechanism (`goods_sale.accept_returned_pieces`) and left it with
no route and no list: the pieces were really back in the shop, standing unaccepted
at the store's receiving location, and nothing on any screen said so. PRD §10.4 is
explicit that a returned piece is unavailable *until it is accepted*, which only
means anything if somebody can see that it is waiting.

So this module answers one question - "which return legs still have pieces
standing in receiving at this store" - and both readers use it: the receiving
inbox lists them beside vendor deliveries and incoming shipments, and the accept
route puts them away through OPS-07's own service.

A damaged return is deliberately not here. It went to quarantine with a pending
`DamageReport` against it (OPS-07), and quarantine is decided by the damage review
(OPS-05), never by somebody putting stock away.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sell.models import SaleLine


@dataclass(frozen=True)
class PendingReturn:
    """One return leg with pieces still to be put away."""

    sale_line_id: int
    sale_id: int
    store_id: int
    doc_number: str
    till_number: str
    barcode: str
    description: str
    qty: int
    returned_at: Any

    @property
    def reference(self) -> str:
        """What a person calls this row on the inbox: the bill, and the piece."""
        bill = self.till_number or self.doc_number or f"bill {self.sale_id}"
        return f"Customer return · {bill} · {self.description or self.barcode}"


def pending_returns(store_ids: set[int] | None) -> list[PendingReturn]:
    """Return legs whose good pieces are still unaccepted, newest last.

    Filtered in Python against `goods_sale`'s own "is this portion still standing
    in receiving" test rather than by a query of this module's own devising: what
    counts as waiting is a fact about positions, holds and acceptance events, and
    a second spelling of it here would be a list that disagreed with the service
    that acts on it.
    """
    from sell.services.goods_sale import _is_waiting
    from stockledger import goods_engine as engine

    rows = (
        SaleLine.objects.filter(
            direction=SaleLine.Direction.RETURN,
            condition=SaleLine.Condition.GOOD,
        )
        .exclude(goods_allocations=[])
        .select_related("sale", "sale__store")
        .order_by("sale__billed_at", "id")
    )
    if store_ids is not None:
        rows = rows.filter(sale__store_id__in=store_ids)
    receiving_by_site: dict[int, uuid.UUID] = {}
    waiting: list[PendingReturn] = []
    for line in rows:
        store = line.sale.store
        if store.pk not in receiving_by_site:
            receiving_by_site[store.pk] = engine.system_location(store.pk, "receiving").pk
        receiving_id = receiving_by_site[store.pk]
        qty = sum(
            int(row["upper"]) - int(row["lower"])
            for row in (line.goods_allocations or [])
            if _is_waiting(row, store, receiving_id)
        )
        if not qty:
            continue
        waiting.append(
            PendingReturn(
                sale_line_id=line.pk,
                sale_id=line.sale_id,
                store_id=store.pk,
                doc_number=line.sale.doc_number or "",
                till_number=line.sale.till_number or "",
                barcode=line.barcode,
                description=" · ".join(
                    part for part in (line.brand, line.item, line.design, line.size) if part
                )
                or line.manual_desc,
                qty=qty,
                returned_at=line.sale.billed_at,
            )
        )
    return waiting
