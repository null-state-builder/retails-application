"""Items at a store with no HSN, listed for fixing (ticket 12, §6 ST-CMP-3).

The same pieces the store's counter is sent (``sell.services.dataset``), read the
same way, filtered to those whose HSN is blank or not an HSN at all
(``masters.hsn.is_hsn``). A read: it changes nothing.

How each is fixed is named, never done here:

* a goods-v1 store's piece takes its HSN from the PT that brought it in, and
  that PT is official and never edited - it is reversed and reissued with the
  HSN (the existing, audited PT correction path, overall PRD §8.3). The row
  names that PT.
* a legacy store's piece reads the SKU registry, which has no audited HSN
  correction path yet; the row says so rather than offering an unaudited edit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from masters.hsn import is_hsn
from masters.models import Cohort, Store
from sell.services.goods_stock import is_goods_site, read_shelf
from stockledger.goods_models import Origin
from stockledger.models import StockOnHand

#: How a row can be fixed.
FIX_PT = "reissue_pt"
FIX_NONE = "no_audited_path"


@dataclass(frozen=True)
class MissingHsn:
    barcode: str
    season: str
    design: str
    brand: str
    item: str
    size: str
    color: str
    #: What the records hold now: blank, or something that is not an HSN.
    hsn: str
    #: Pieces the counter could sell here now.
    qty: int
    fix: str
    pt_document_id: str | None = None
    pt_number: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "barcode": self.barcode,
            "season": self.season,
            "design": self.design,
            "brand": self.brand,
            "item": self.item,
            "size": self.size,
            "color": self.color,
            "hsn": self.hsn,
            "qty": self.qty,
            "fix": self.fix,
            "pt_document_id": self.pt_document_id,
            "pt_number": self.pt_number,
        }


def missing_hsn_items(store: Store) -> list[MissingHsn]:
    """Every item this store's counter holds whose HSN is missing, by barcode."""
    if is_goods_site(store):
        return _goods(store)
    return _legacy(store)


def _goods(store: Store) -> list[MissingHsn]:
    shelf = read_shelf(store)
    missing = [piece for piece in shelf.pieces if not is_hsn(piece.hsn)]
    origin_ids = [piece.origin_id for piece in missing if piece.origin_id is not None]
    pts: dict[uuid.UUID, tuple[str | None, str]] = {
        row["pk"]: (
            str(row["official_line__version__document_id"])
            if row["official_line__version__document_id"]
            else None,
            row["official_line__version__document__official_number"] or "",
        )
        for row in Origin.objects.filter(pk__in=origin_ids).values(
            "pk",
            "official_line__version__document_id",
            "official_line__version__document__official_number",
        )
    }
    out = []
    for piece in missing:
        document_id, number = (
            pts.get(piece.origin_id, (None, "")) if piece.origin_id else (None, "")
        )
        out.append(
            MissingHsn(
                barcode=piece.barcode,
                season=piece.season,
                design=piece.dims["design"],
                brand=piece.dims["brand"],
                item=piece.dims["item"],
                size=piece.dims["size"],
                color=piece.dims["color"],
                hsn=piece.hsn,
                qty=shelf.quantities.get((piece.barcode, piece.season), 0),
                fix=FIX_PT if document_id else FIX_NONE,
                pt_document_id=document_id,
                pt_number=number,
            )
        )
    return sorted(out, key=lambda row: (row.barcode, row.season))


def _legacy(store: Store) -> list[MissingHsn]:
    on_hand = dict(StockOnHand.objects.filter(store=store).values_list("sku_code", "net_qty"))
    rows = (
        Cohort.objects.filter(barcode__in=list(on_hand), sku__is_active=True)
        .order_by("barcode", "season")
        .values(
            "barcode",
            "season",
            "sku__design",
            "sku__brand",
            "sku__item",
            "sku__size",
            "sku__color",
            "sku__hsn",
        )
    )
    return [
        MissingHsn(
            barcode=row["barcode"],
            season=row["season"],
            design=row["sku__design"],
            brand=row["sku__brand"],
            item=row["sku__item"],
            size=row["sku__size"],
            color=row["sku__color"],
            hsn=row["sku__hsn"] or "",
            qty=int(on_hand.get(row["barcode"]) or 0),
            fix=FIX_NONE,
        )
        for row in rows
        if not is_hsn(row["sku__hsn"])
    ]
