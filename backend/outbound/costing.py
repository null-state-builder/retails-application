"""What the books say a piece costs — and the error raised when they cannot say.

This module exists to break a cycle, and its contents are chosen for exactly
that: ``outbound.posting`` imports ``outbound.maker_checker`` at module level
(every posting path asks whether a second person is needed), while
``maker_checker`` needed two things back from ``posting`` — the cost of a piece,
to size the document against the approval tolerance, and the error type to refuse
with. That was answered with four function-level imports of ``posting`` inside
``maker_checker``, which works but leaves a real cycle standing behind a deferred
import: import order becomes load-order-sensitive, and every future reader has to
re-derive why the imports are hiding inside functions.

Both of those things depend on nothing in the posting engine — a cost is a read
of the cohort and the on-hand row — so they live here, below both modules. The
``outbound`` layers contract in ``pyproject.toml`` now forbids the edge from ever
being added back.

``outbound.posting`` re-exports both names, so the many callers that say
``from outbound.posting import OutboundPostingError`` keep working: for a caller,
the error still belongs to posting.
"""

from __future__ import annotations

from collections.abc import Mapping

from stockledger.models import StockOnHand


class OutboundPostingError(Exception):
    """Raised when an outbound document cannot be posted."""


def book_unit_cost(store_id: int, sku_code: str, season: str = "") -> int:
    """What the books say one piece costs at this location, in paise.

    The cohort's frozen P-RATE where there is one, else the on-hand average.
    Returns 0 when the books cannot price the piece — "unknown", which every
    caller must treat as unknown rather than as free.

    ``season`` is only read for a piece the location holds none of (a stocktake
    surplus): with no on-hand row there is no season to look the cohort up by,
    so the caller supplies the one on its line.
    """
    return book_unit_costs(store_id, {sku_code: season})[sku_code]


def book_unit_costs(store_id: int, seasons_by_barcode: Mapping[str, str]) -> dict[str, int]:
    """``book_unit_cost`` for many barcodes at one location, in two queries.

    The single-piece call above delegates here, so the rule below is the only
    copy of it — a screen that prices a whole brand's returnable pool must not
    get its costs from a second, subtly different reading of the books (#75).
    Two queries whatever the size of the pool: one for the location's on-hand
    rows, one for every candidate cohort.

    Every requested barcode comes back, so a caller may index the result without
    guarding; a piece the books cannot price answers 0 — "unknown", which every
    caller must treat as unknown rather than as free.
    """
    from masters.models import Cohort

    barcodes = list(seasons_by_barcode)
    if not barcodes:
        return {}

    on_hand: dict[str, StockOnHand] = {
        row.sku_code: row
        for row in StockOnHand.objects.filter(store_id=store_id, sku_code__in=barcodes)
    }
    cohorts = list(Cohort.objects.filter(barcode__in=barcodes))
    by_key = {(c.barcode, c.season): c for c in cohorts}
    # How many cohorts each barcode has at all — the "is it ambiguous?" question
    # the single-piece version answered by fetching two rows.
    count_by_barcode: dict[str, int] = {}
    for c in cohorts:
        count_by_barcode[c.barcode] = count_by_barcode.get(c.barcode, 0) + 1

    priced: dict[str, int] = {}
    for barcode, fallback_season in seasons_by_barcode.items():
        held = on_hand.get(barcode)
        season = held.season if held is not None else fallback_season
        cohort = by_key.get((barcode, season))
        if cohort is None and not season:
            # Nothing said which season. A cohort is keyed (barcode, season)
            # because the same SKU is bought across seasons at different locked
            # costs, so one cohort is the only answer the books can give and more
            # than one is a guess — and a guess priced as a book value is the
            # exact thing this seam exists to stop.
            if count_by_barcode.get(barcode) == 1:
                cohort = next(c for c in cohorts if c.barcode == barcode)
        if cohort is not None and cohort.unit_cost_paise:
            priced[barcode] = int(cohort.unit_cost_paise)
        elif held is not None and held.net_qty > 0:
            priced[barcode] = int(held.net_value_paise or 0) // held.net_qty
        else:
            priced[barcode] = 0
    return priced


def on_hand_valuation(row: StockOnHand, mrp_paise: int) -> dict[str, int | None]:
    """Cost, landed value and margin for one on-hand row, in paise (#74).

    Read straight off the materialised ``StockOnHand`` projection rather than
    re-summing the ledger — the same number every other stock screen shows for
    this row. ``mrp_paise`` comes from the item master, not the row: on-hand
    prices what is on the shelf, never what it should sell for. A margin needs
    an MRP to compare against; without one it is unknown, not zero.
    """
    unit_cost = row.net_value_paise // row.net_qty if row.net_qty else 0
    margin = (mrp_paise * row.net_qty - row.net_value_paise) if mrp_paise else None
    return {
        "unit_cost_paise": unit_cost,
        "landed_value_paise": row.net_value_paise,
        "margin_paise": margin,
    }
