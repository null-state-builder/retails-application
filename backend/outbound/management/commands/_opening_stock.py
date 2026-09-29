"""Legacy demo stock, laid in without a receipt (OPS-13).

The legacy Stock, Stock Count, quarantine, Return to Brand and Distribution
screens - and the suites written against them - still read the legacy stock
ledger, and nothing retires them yet. Until 23 September 2026 the two demo seeds
stocked them through the legacy receiving pipeline (booking → GRN → PT file →
PT inward posting), which OPS-18 deleted. They now lay the same pieces in the
way the legacy ledger itself takes in stock that arrived on no receipt:

* one ``adjustment`` leg per piece at its unit cost, folded into ``StockOnHand``
  by ``post_on_hand_movement`` - the shared stock writer, fenced below every
  caller, so a goods-v1 site refuses it like any other legacy write;
* the item masters the till and the cost resolver read: ``Sku`` for the ticket
  and the description, ``Cohort`` for the (barcode, season) unit cost;
* one balanced value voucher per batch, so the books hold what the ledger holds.
  Owned stock posts Dr INVENTORY / Cr SUSPENSE, which is what a legacy stock
  adjustment posts for a surplus; brand-owned stock posts the SOR memo pair
  (Dr SOR_STOCK / Cr SOR_CONTRA) that a V-flip or a lost-in-transit closure
  later reverses.

What it deliberately does not make: no booking, GRN, PT file, vendor bill or
payable, and no price-trail row, whose only sources are a PT and a re-ticket.
Demo stock that nobody bought is not a purchase, and its records do not pretend
one happened. Every leg and voucher carries a ``DEMO-OPENING/...`` number, so
nobody reads a seeded piece as a received one.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from core.gl import GLAccount
from core.money import rupees_to_paise
from core.posting import PostingRef, cr, dr, post_entries
from masters.models import Cohort, Sku, Store
from masters.ownership import brand_is_owned
from stockledger.contracts import require_legacy_stock_writer
from stockledger.models import StockLedgerEntry
from stockledger.projections import post_on_hand_movement

#: The number every seeded leg and voucher is filed under, followed by the store
#: and the batch: ``DEMO-OPENING/DEO/pe-deo``. Not a series number - a seed is
#: not a document, and allocating from a real series would leave a hole in it.
OPENING_DOC_PREFIX = "DEMO-OPENING"

#: The value voucher's document type: the one the legacy stock adjustment posts
#: under, since an ``adjustment`` leg is what each seeded piece is.
OPENING_DOC_TYPE = "ADJ"


@dataclass(frozen=True)
class LaidIn:
    doc_number: str
    pieces: int
    value_paise: int
    owned: bool


def opening_doc_number(store_code: str, tag: str) -> str:
    return f"{OPENING_DOC_PREFIX}/{store_code}/{tag}"


def lay_in_opening_stock(
    *, store: Store, brand: str, tag: str, skus: list[dict[str, Any]], user: Any
) -> LaidIn:
    """Put one brand's pieces on hand at one store, with their masters and value.

    ``skus`` are rows in the KDPS PT column shape the seeds already carry
    (BARCODE, DESIGN, SIZE, COLOR, BRAND, SEASON, ITEM, HSN, P RATE, MRP, NAG).
    The unit cost is the row's P RATE, as a PT would have locked it, and a row
    the books could not value - no quantity, no cost, or a cost above the MRP -
    stops the seed rather than going in at a wrong number.
    """
    require_legacy_stock_writer(store, operation="Demo opening stock")
    owned = brand_is_owned(brand)
    if owned is None:
        raise ValueError(f"The masters cannot say who owns {brand} stock, so it cannot be valued.")
    doc_number = opening_doc_number(store.code, tag)

    pieces = 0
    value = 0
    for line_no, row in enumerate(skus, start=1):
        barcode = str(row["BARCODE"])
        qty = int(row["NAG"])
        unit_paise = rupees_to_paise(str(row["P RATE"]))
        mrp_paise = rupees_to_paise(str(row["MRP"]))
        if qty <= 0 or unit_paise <= 0 or unit_paise > mrp_paise:
            raise ValueError(
                f"{barcode} cannot be laid in: {qty} pieces at {unit_paise} paise "
                f"against an MRP of {mrp_paise} paise."
            )
        dims = {
            "design": str(row["DESIGN"]),
            "color": str(row["COLOR"]),
            "size": str(row["SIZE"]),
            "brand": str(row["BRAND"]),
            "season": str(row["SEASON"]),
            "item": str(row["ITEM"]),
            "hsn": str(row["HSN"]),
        }
        item, _ = Sku.objects.update_or_create(
            barcode=barcode,
            defaults={
                **{k: v for k, v in dims.items() if k != "season"},
                "mrp_paise": mrp_paise,
                "is_active": True,
            },
        )
        if not item.first_doc_number:
            item.first_doc_number = doc_number
            item.save(update_fields=["first_doc_number", "updated_at"])
        Cohort.objects.update_or_create(
            barcode=barcode,
            season=dims["season"],
            defaults={
                "sku": item,
                "unit_cost_paise": unit_paise,
                "mrp_paise": mrp_paise,
                "last_doc_number": doc_number,
            },
        )
        post_on_hand_movement(
            store=store,
            gstin=store.gstin,
            sku_code=barcode,
            source=SimpleNamespace(**dims),
            qty=qty,
            unit_cost_paise=unit_paise,
            kind=StockLedgerEntry.Kind.ADJUSTMENT,
            doc_number=doc_number,
            line_no=line_no,
            posted_by=user,
        )
        pieces += qty
        value += qty * unit_paise

    seasons = {str(row["SEASON"]) for row in skus}
    season = seasons.pop() if len(seasons) == 1 else ""
    memo = "Demo opening stock: no receipt, no payable"
    legs = (
        [
            dr(GLAccount.INVENTORY, value, brand=brand, season=season, memo=memo),
            cr(GLAccount.SUSPENSE, value, memo=memo),
        ]
        if owned
        else [
            dr(GLAccount.SOR_STOCK, value, brand=brand, season=season, memo=memo),
            cr(GLAccount.SOR_CONTRA, value, brand=brand, season=season, memo=memo),
        ]
    )
    post_entries(
        PostingRef(
            doc_type=OPENING_DOC_TYPE,
            doc_number=doc_number,
            store=store,
            gstin=store.gstin,
            posted_by=user,
        ),
        legs,
        posted_by=user,
    )
    return LaidIn(doc_number=doc_number, pieces=pieces, value_paise=value, owned=owned)
