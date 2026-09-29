"""The stock ledger - the first concrete business ledger over `core.LedgerEntry`.

Append-only (ADR-0004): a posting is only ever INSERTed; a correction is a *new*
today-dated reversing row, never an edit or delete (enforced by the ORM base, the
`BEFORE UPDATE OR DELETE` DB trigger, and a `REVOKE`). Each row is *self-describing*
- it carries its own SKU (barcode) + merchandising dimensions - so the ledger needs
no item master to be auditable. Inward qty is positive; a reversal is negative.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any

from django.db import models

from core.ledger import LedgerEntry
from core.money import MoneyField
from stockledger.contracts import require_legacy_stock_writer

# The 7 merchandising dimensions every stock row carries (Rule 9: every line
# says exactly what item it is). One bundle - copy it with `merch_dims`, never
# by hand-listing the fields.
MERCH_DIM_FIELDS = ("design", "color", "size", "brand", "season", "item", "hsn")


def merch_dims(obj: object) -> dict[str, str]:
    """The merchandising dims of any dim-carrying row/line, as one bundle."""
    return {f: getattr(obj, f, "") or "" for f in MERCH_DIM_FIELDS}


def identity_dims(obj: Any, tenant_id: Any) -> dict[str, Any]:
    """Carry proven identity alongside the unchanged historical display dimensions."""
    from core.refusals import Refusal
    from masters.brand_identity import RESOURCES, identity_id
    from masters.models import Brand

    dims: dict[str, Any] = merch_dims(obj)
    brand_id = getattr(obj, "brand_ref_id", None)
    label = getattr(getattr(obj, "_meta", None), "label_lower", "")
    if brand_id is None and label in RESOURCES and getattr(obj, "pk", None) is not None:
        brand_id = identity_id(obj, tenant_id)
    if brand_id is not None:
        if not Brand.objects.filter(pk=brand_id, tenant_id=tenant_id).exists():
            raise Refusal("IDENTITY_CONFLICT", "Stock identity belongs to another tenant.")
        dims["brand_ref_id"] = brand_id
    return dims


def retain_projection_identity(row: Any, dims: dict[str, Any]) -> None:
    from core.refusals import Refusal

    brand_id = dims.get("brand_ref_id")
    if brand_id is not None:
        if row.brand_ref_id is not None and row.brand_ref_id != brand_id:
            raise Refusal("IDENTITY_CONFLICT", "A stock projection cannot change established brand ownership.")
        row.brand_ref_id = brand_id


class LegacyStockLedgerManager(models.Manager["StockLedgerEntry"]):
    def bulk_create(
        self,
        objs: Iterable[StockLedgerEntry],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[StockLedgerEntry]:
        rows = list(objs)
        require_legacy_stock_writer(
            [row.store_id for row in rows], operation="Legacy stock posting"
        )
        return super().bulk_create(
            rows,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class StockLedgerEntry(LedgerEntry):
    """One signed stock movement (barcode × store), with value in paise (`amount`)."""

    class Kind(models.TextChoices):
        PT_INWARD = "pt_inward", "PT inward"
        PT_REVERSAL = "pt_reversal", "PT reversal"
        # Outbound kinds (Sprint 1)
        TRANSFER_OUT = "transfer_out", "Transfer out"
        TRANSFER_IN = "transfer_in", "Transfer in"
        # In-transit bucket legs (slice #68): dispatch = transfer_out at the
        # source + transit_in under the transfer; receive = transit_out +
        # transfer_in at the destination. Stock is never in no location.
        TRANSIT_IN = "transit_in", "Transit in"
        TRANSIT_OUT = "transit_out", "Transit out"
        # Quarantine bucket (slice #69): mark-damaged posts damage_out at the
        # store (free-to-sell drops) + quarantine_in into the quarantine bucket
        # at the same store. The piece stays owned; it is just not sellable.
        DAMAGE_OUT = "damage_out", "Damaged out of sellable"
        QUARANTINE_IN = "quarantine_in", "Quarantine in"
        QUARANTINE_OUT = "quarantine_out", "Quarantine out"
        # Selling kinds (D10, #177). `sale_out` is the piece leaving the shelf at
        # the counter; `sale_return_in` is a customer's good piece coming back on
        # to it inside an exchange. A piece that comes back *damaged* is not a
        # `sale_return_in` - it never reaches the shelf - and posts the existing
        # `quarantine_in` instead, so one kind still means one bucket and the
        # rebuild command needs no knowledge of the sale to place a row.
        SALE_OUT = "sale_out", "Sale out"
        SALE_RETURN_IN = "sale_return_in", "Sale return in"
        RTV_OUT = "rtv_out", "RTV out"
        SEASONAL_RET = "seasonal_ret", "Seasonal return"
        ADJUSTMENT = "adjustment", "Adjustment"
        WRITE_OFF = "write_off", "Write-off"
        VFLIP_OUT = "vflip_out", "V-flip out"
        VFLIP_IN = "vflip_in", "V-flip in"

    store = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="stock_entries"
    )
    gstin = models.ForeignKey(
        "masters.Gstin", on_delete=models.PROTECT, related_name="stock_entries"
    )
    sku_code = models.CharField(max_length=64, db_index=True)  # barcode = SKU identity
    design = models.CharField(max_length=120, blank=True, default="")
    color = models.CharField(max_length=60, blank=True, default="")
    size = models.CharField(max_length=24, blank=True, default="")
    brand = models.CharField(max_length=120, blank=True, default="")
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    season = models.CharField(max_length=120, blank=True, default="")
    item = models.CharField(max_length=120, blank=True, default="")
    hsn = models.CharField(max_length=24, blank=True, default="")
    qty = models.IntegerField()  # signed: + inward, − reversal
    kind = models.CharField(max_length=16, choices=Kind.choices)
    doc_number = models.CharField(max_length=128, db_index=True)  # gap-free PT voucher
    line_no = models.IntegerField(default=0)
    booking = models.ForeignKey(
        "vendors.Booking",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_entries",
    )
    posted_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)

    objects = LegacyStockLedgerManager()

    class Meta:
        db_table = "stockledger_entry"
        ordering = ["-created_at", "line_no"]

    def __str__(self) -> str:
        return f"{self.doc_number} · {self.sku_code} × {self.qty}"

    def save(self, *args: object, **kwargs: object) -> None:
        require_legacy_stock_writer(self.store_id, operation="Legacy stock posting")
        super().save(*args, **kwargs)


#: Which bucket a ledger kind belongs to. Stated once, here, because three places
#: need the same answer and had been deriving it separately: the rebuild command
#: (which recomputes every projection from the legs), the reversal helper (which
#: has to fold a mirrored leg back into the bucket the original moved), and
#: anything later that walks the ledger without knowing what wrote it. A kind
#: missing from both sets is an at-location leg and moves `StockOnHand`.
TRANSIT_KINDS = frozenset({StockLedgerEntry.Kind.TRANSIT_IN, StockLedgerEntry.Kind.TRANSIT_OUT})
QUARANTINE_KINDS = frozenset(
    {StockLedgerEntry.Kind.QUARANTINE_IN, StockLedgerEntry.Kind.QUARANTINE_OUT}
)


class StockOnHand(models.Model):
    """Materialised net stock position per (store × barcode) - a fast, indexed
    projection of the append-only ledger, maintained INSIDE each post/reverse
    transaction and fully rebuildable (`manage.py rebuild_stock_on_hand`). It is a
    cache, never the source of truth: the ledger is. Mutable (no append-only
    trigger) so it can be updated/rebuilt."""

    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="on_hand")
    gstin = models.ForeignKey(
        "masters.Gstin",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="on_hand",
    )
    sku_code = models.CharField(max_length=64, db_index=True)
    design = models.CharField(max_length=120, blank=True, default="")
    color = models.CharField(max_length=60, blank=True, default="")
    size = models.CharField(max_length=24, blank=True, default="")
    brand = models.CharField(max_length=120, blank=True, default="")
    # Stable scope identity; historical brand text is display evidence only.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    season = models.CharField(max_length=120, blank=True, default="")
    item = models.CharField(max_length=120, blank=True, default="")
    hsn = models.CharField(max_length=24, blank=True, default="")
    net_qty = models.IntegerField(default=0)
    net_value_paise = MoneyField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stockledger_on_hand"
        ordering = ["brand", "-net_qty"]
        constraints = [
            models.UniqueConstraint(fields=["store", "sku_code"], name="uq_on_hand_store_sku"),
        ]
        indexes = [
            models.Index(fields=["brand"], name="onhand_brand_idx"),
            models.Index(fields=["season"], name="onhand_season_idx"),
            models.Index(fields=["net_qty"], name="onhand_net_qty_idx"),
            # The till's dataset delta (#179) filters this column twice on every
            # sync - once for the quantities that moved and once to catch a piece
            # arriving at a store for the first time - and this is the biggest
            # table in the system (a row per store per barcode).
            models.Index(fields=["store", "updated_at"], name="onhand_synced_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.store_id}/{self.sku_code} = {self.net_qty}"


class InTransitStock(models.Model):
    """Materialised in-transit position per (transfer × barcode) - the third
    honest stock number (at-warehouse / in-transit / at-store). Like
    ``StockOnHand`` it is a fast projection of the append-only ledger
    (``transit_in``/``transit_out`` legs, keyed by the transfer's doc number),
    maintained inside each posting transaction and fully rebuildable
    (`manage.py rebuild_stock_on_hand`). A cache, never the source of truth.

    Keyed by ``transfer_doc_number`` (the ledger is self-describing) rather
    than an FK so the generic ledger app stays ignorant of the outbound module.
    """

    transfer_doc_number = models.CharField(max_length=128, db_index=True)
    source_store = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="in_transit_out"
    )
    destination_store = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="in_transit_in"
    )
    gstin = models.ForeignKey(
        "masters.Gstin",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="in_transit",
    )
    sku_code = models.CharField(max_length=64, db_index=True)
    design = models.CharField(max_length=120, blank=True, default="")
    color = models.CharField(max_length=60, blank=True, default="")
    size = models.CharField(max_length=24, blank=True, default="")
    brand = models.CharField(max_length=120, blank=True, default="")
    # Stable scope identity; historical brand text is display evidence only.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    season = models.CharField(max_length=120, blank=True, default="")
    item = models.CharField(max_length=120, blank=True, default="")
    hsn = models.CharField(max_length=24, blank=True, default="")
    qty = models.IntegerField(default=0)
    value_paise = MoneyField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stockledger_in_transit"
        ordering = ["transfer_doc_number", "sku_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["transfer_doc_number", "sku_code"],
                name="uq_in_transit_doc_sku",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.transfer_doc_number}/{self.sku_code} = {self.qty}"


class QuarantineStock(models.Model):
    """Materialised quarantine position per (store × barcode) - damaged / held
    stock that is NOT free-to-sell (issue #69). Like ``StockOnHand`` and
    ``InTransitStock`` it is a fast projection of the append-only ledger
    (``quarantine_in``/``quarantine_out`` legs), maintained inside each posting
    transaction and fully rebuildable (`manage.py rebuild_stock_on_hand`). A
    cache, never the source of truth: the ledger is.

    Quarantine is a *stock state in the ledger*, not a boolean on a stock row -
    entered from anywhere via the global mark-damaged action, and (a later slice)
    the source of the returnable pool. ``marked_by`` / ``marked_at`` carry the
    most-recent mark-damaged actor + time for the inventory quarantine filter
    (Rule 10 - every action has an actor); the full per-event history lives in
    the ledger and each MarkDamaged document.
    """

    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="quarantine")
    gstin = models.ForeignKey(
        "masters.Gstin",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="quarantine",
    )
    sku_code = models.CharField(max_length=64, db_index=True)
    design = models.CharField(max_length=120, blank=True, default="")
    color = models.CharField(max_length=60, blank=True, default="")
    size = models.CharField(max_length=24, blank=True, default="")
    brand = models.CharField(max_length=120, blank=True, default="")
    # Stable scope identity; historical brand text is display evidence only.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    season = models.CharField(max_length=120, blank=True, default="")
    item = models.CharField(max_length=120, blank=True, default="")
    hsn = models.CharField(max_length=24, blank=True, default="")
    qty = models.IntegerField(default=0)
    value_paise = MoneyField(default=0)
    marked_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    marked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stockledger_quarantine"
        ordering = ["store", "sku_code"]
        constraints = [
            models.UniqueConstraint(fields=["store", "sku_code"], name="uq_quarantine_store_sku"),
        ]

    def __str__(self) -> str:
        return f"quarantine {self.store_id}/{self.sku_code} = {self.qty}"


# Goods-v1 operational inventory (design §5.2, §7), registered with this app.
# Store operations ticket 32 (ST-INV-1): broken-size rules and alerts.
from stockledger.broken_size_models import BrokenSizeAlert, SizeRule  # noqa: E402, F401
from stockledger.goods_models import (  # noqa: E402, F401
    AcceptanceEvent,
    AcceptanceSession,
    ActiveHold,
    ActiveReservation,
    AllocationGuard,
    CoverageEvent,
    CustodyLot,
    CustodyMatch,
    EncumbranceLeg,
    HoldEvent,
    JournalBatch,
    LiveCoverage,
    Origin,
    Position,
    QuantityLeg,
    ReservationEvent,
    ValueLeg,
)
