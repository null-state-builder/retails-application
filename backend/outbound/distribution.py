"""The Distribution grid's pre-fill suggestion (#73).

v1, as the issue rules it: "last split for the brand plus each store's size
mix" — for every size, weight a destination store by the most recent
warehouse-allocation quantity it received of that size, for that brand, from
this warehouse. A store with no history for a size falls back to its overall
share across every size it has received for the brand; no history at all
falls back to an equal split.

Deliberately not the whole ownership question a real forecast would ask
(seasonality, sell-through, request backlog) — the issue is explicit that the
suggestion may improve later without changing the contract, so this stays a
starting point the grid always lets a person overwrite, cell by cell, never a
number the grid enforces.
"""

from __future__ import annotations

from collections import defaultdict

from core.documents import DocStatus
from outbound.models import StoreTransferLine, TransferReason

#: The weight key a size falls back to when it has no history of its own —
#: never a real size code (KDPS sizes are alpha/numeric, not underscored),
#: so it can share the same dict as the per-size keys without colliding.
DEFAULT_KEY = "_default"


def suggest_split(
    *, warehouse_id: int, brand: str, store_ids: list[int]
) -> dict[str, dict[str, float]]:
    """Fractional weight of each store, per size, for splitting a brand's
    arrived stock out of ``warehouse_id``. Every size's weights (and
    ``DEFAULT_KEY``'s) sum to 1 across ``store_ids`` — the caller multiplies
    by a line's available qty and puts what floor() leaves over into the
    warehouse buffer, so rounding never invents or drops a piece.

    A size this warehouse has never dispatched to this brand's stores is not
    a key in the result at all — the caller falls back to ``weights[size]``
    if present, else ``weights[DEFAULT_KEY]``, never a missing-key error.
    """
    if not store_ids:
        return {}
    equal_weights = {str(s): 1.0 / len(store_ids) for s in store_ids}

    # The most recent *dispatched* quantity per (store, size) this warehouse
    # has sent for this brand — "last split". Dims (design/color/size/brand)
    # are stamped onto a line only at dispatch, off the source stock that was
    # actually scanned (`outbound.posting._resolve_scan_identity`); a plan
    # line still sitting in draft carries no dims at all, and a draft that
    # was never dispatched is not a split that happened. Ordered newest first
    # so the first row seen per key is the one that wins.
    rows = (
        StoreTransferLine.objects.filter(
            transfer__reason=TransferReason.WAREHOUSE_ALLOCATION,
            transfer__source_store_id=warehouse_id,
            transfer__destination_store_id__in=store_ids,
            transfer__docstatus=DocStatus.SUBMITTED,
            brand=brand,
        )
        .order_by("-transfer__dispatch_date")
        .values_list("transfer__destination_store_id", "size", "qty_dispatched")
    )

    last_by_store_size: dict[tuple[int, str], int] = {}
    for store_id, size, qty_dispatched in rows:
        key = (store_id, size or "")
        last_by_store_size.setdefault(key, qty_dispatched or 0)

    sizes = sorted({size for _store, size in last_by_store_size})
    store_totals: dict[int, int] = defaultdict(int)
    for (store_id, _size), qty in last_by_store_size.items():
        store_totals[store_id] += qty
    grand_total = sum(store_totals.values())

    overall_weights = (
        {str(s): store_totals.get(s, 0) / grand_total for s in store_ids}
        if grand_total > 0
        else equal_weights
    )

    weights: dict[str, dict[str, float]] = {DEFAULT_KEY: overall_weights}
    for size in sizes:
        size_total = sum(last_by_store_size.get((s, size), 0) for s in store_ids)
        weights[size] = (
            {str(s): last_by_store_size.get((s, size), 0) / size_total for s in store_ids}
            if size_total > 0
            else overall_weights
        )
    return weights
