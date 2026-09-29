"""Seed on-hand stock for outbound demo / testing.

Lays legacy on-hand stock in at two of the foundation seed's legacy demo stores,
for the legacy Return to Brand, Stock and quarantine screens and the live suites
that spend it (``tests/_seed_stock.py``). Those screens are not retired yet, so
their stock is still the legacy ledger's - but it no longer arrives through the
legacy receiving pipeline (booking → GRN → PT file → PT inward posting), which
OPS-18 deleted. It goes in the way the legacy ledger takes in stock that came on
no receipt (``_opening_stock``): an ``adjustment`` leg per piece at its P RATE,
the ``Sku``/``Cohort`` masters the till and the cost resolver read, and one
balanced value voucher per batch. No booking, GRN, PT file, vendor bill or
payable is made (OPS-13). The result is observable and verifiable through the
API: ``/api/stockledger/on-hand``, ``/api/finledger/health``.

Seeded data
-----------
1. **Owned @ DEO** (Jharkhand):   2 Peter England SKUs × 15 qty each
2. **Owned @ BANKA** (Bihar):     2 Blackberrys SKUs × 15 qty each
3. **SOR/consignment @ DEO**:     2 Louis Philippe SKUs × 15 qty each

plus every outbound document series (STO, DMG, GAP, RTV, ADJ, WRO, VFL, SRQ) at
every store, without which the outbound screens cannot number a document.

Idempotent: skips when the ledger already holds any of these pieces at these
stores - whether this command laid them in, or the receipt-based version of it
did before 23 September 2026. Safe to run multiple times.

Usage::

    python manage.py seed_outbound_demo
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from core.documents import VoucherSeries
from masters.models import Store
from outbound.management.commands._opening_stock import lay_in_opening_stock
from stockledger.models import StockLedgerEntry


def _fy() -> str:
    d = date.today()
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


# ------- SKU catalogue ---------
OWNED_DEO_SKUS = [
    {
        "BARCODE": "PE-FRM-WHT-40",
        "DESIGN": "Formal Shirt",
        "SIZE": "40",
        "COLOR": "White",
        "BRAND": "Peter England",
        "SEASON": "Spring/Summer 2026",
        "ITEM": "Shirt",
        "HSN": "6205",
        "P RATE": "850.00",
        "BASIC": "850.00",
        "MRP": "1799.00",
        "NAG": 15,
    },
    {
        "BARCODE": "PE-CHK-BLU-42",
        "DESIGN": "Check Casual Shirt",
        "SIZE": "42",
        "COLOR": "Blue",
        "BRAND": "Peter England",
        "SEASON": "Spring/Summer 2026",
        "ITEM": "Shirt",
        "HSN": "6205",
        "P RATE": "780.00",
        "BASIC": "780.00",
        "MRP": "1599.00",
        "NAG": 15,
    },
]

OWNED_BANKA_SKUS = [
    {
        "BARCODE": "BB-BLAZ-NVY-40",
        "DESIGN": "Slim Blazer",
        "SIZE": "40",
        "COLOR": "Navy",
        "BRAND": "Blackberrys",
        "SEASON": "Spring/Summer 2026",
        "ITEM": "Blazer",
        "HSN": "6203",
        "P RATE": "2400.00",
        "BASIC": "2400.00",
        "MRP": "5999.00",
        "NAG": 15,
    },
    {
        "BARCODE": "BB-TROU-GRY-34",
        "DESIGN": "Formal Trouser",
        "SIZE": "34",
        "COLOR": "Grey",
        "BRAND": "Blackberrys",
        "SEASON": "Spring/Summer 2026",
        "ITEM": "Trouser",
        "HSN": "6203",
        "P RATE": "1100.00",
        "BASIC": "1100.00",
        "MRP": "2999.00",
        "NAG": 15,
    },
]

SOR_DEO_SKUS = [
    {
        "BARCODE": "LP-POLO-BLK-L",
        "DESIGN": "Classic Polo",
        "SIZE": "L",
        "COLOR": "Black",
        "BRAND": "Louis Philippe",
        "SEASON": "Spring/Summer 2026",
        "ITEM": "T-Shirt",
        "HSN": "6109",
        "P RATE": "1400.00",
        "BASIC": "1400.00",
        "MRP": "3499.00",
        "NAG": 15,
    },
    {
        "BARCODE": "LP-SLIM-WHT-38",
        "DESIGN": "Slim Fit Shirt",
        "SIZE": "38",
        "COLOR": "White",
        "BRAND": "Louis Philippe",
        "SEASON": "Spring/Summer 2026",
        "ITEM": "Shirt",
        "HSN": "6205",
        "P RATE": "1800.00",
        "BASIC": "1800.00",
        "MRP": "4299.00",
        "NAG": 15,
    },
]


#: One batch per brand per store: (store code, brand, batch tag, pieces).
BATCHES: tuple[tuple[str, str, str, list[dict[str, Any]]], ...] = (
    ("DEO", "Peter England", "pe-deo", OWNED_DEO_SKUS),
    ("BANKA", "Blackberrys", "bb-banka", OWNED_BANKA_SKUS),
    ("DEO", "Louis Philippe", "lp-deo", SOR_DEO_SKUS),
)

#: Every outbound document type, so each store can number its first document.
OUTBOUND_DOC_TYPES = ("STO", "DMG", "GAP", "RTV", "ADJ", "WRO", "VFL", "SRQ")


def _already_seeded() -> bool:
    """Whether any of these pieces is already in the ledger at its store.

    Read off the stock itself rather than a marker row, so a database seeded by
    the receipt-based version of this command - whose marker was a PT file -
    is recognised too, and never gets the same pieces a second time.
    """
    return any(
        StockLedgerEntry.objects.filter(
            store__code=store_code, sku_code__in=[row["BARCODE"] for row in skus]
        ).exists()
        for store_code, _brand, _tag, skus in BATCHES
    )


class Command(BaseCommand):
    help = "Seed legacy on-hand stock, without a receipt, for outbound demo testing."

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        if _already_seeded():
            self.stdout.write(
                self.style.WARNING(
                    "Outbound demo stock already seeded (its pieces are in the ledger). Skipping."
                )
            )
            return

        user = User.objects.filter(role__code="owner", scope_type="all").first()
        if not user:
            raise CommandError(
                "seed_foundation must provide a network-wide Owner to post the demo stock's value."
            )
        self.stdout.write(f"Seeding as user: {user.username}")

        # Ensure VoucherSeries exist for all outbound doc types at all stores.
        fy = _fy()
        for store in Store.objects.all():
            for dt in OUTBOUND_DOC_TYPES:
                VoucherSeries.objects.get_or_create(fy=fy, store_code=store.code, doc_type=dt)

        for store_code, brand, tag, skus in BATCHES:
            laid = lay_in_opening_stock(
                store=Store.objects.get(code=store_code),
                brand=brand,
                tag=tag,
                skus=skus,
                user=user,
            )
            model = "owned" if laid.owned else "SOR/consignment"
            self.stdout.write(
                self.style.SUCCESS(
                    f"  {brand} @ {store_code} ({model}): {laid.doc_number} - "
                    f"{laid.pieces} pieces, value=₹{laid.value_paise / 100:.2f}"
                )
            )

        self.stdout.write(self.style.SUCCESS("\nOutbound demo stock seeded successfully."))
        self.stdout.write("  Verify: GET /api/stockledger/on-hand")
        self.stdout.write("  Verify: GET /api/finledger/health")
