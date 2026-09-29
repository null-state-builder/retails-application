"""Seed a small, clearly-labelled AW25 stock position so the EOSS planning
screen has real numbers to compute against in a fresh preview environment
(where no PT/GRN has been posted yet).

Idempotent (`get_or_create` / doc-number guard) — safe to re-run. Writes the
opening ("PT inward") ledger rows via a raw INSERT because `StockLedgerEntry`
is append-only and its `created_at` is `auto_now_add`; the ORM cannot backdate
it, and this seed needs styles to look genuinely N weeks old for the ladder
to have anything to trigger on.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from masters.models import Brand, Cohort, Season, Sku, Store
from stockledger.contracts import require_legacy_stock_writer
from stockledger.models import StockLedgerEntry, StockOnHand

SEASON_CODE = "AW25"
DOC_PREFIX = "SEED-EOSS-AW25"

# (brand_code, design, color, item, hsn, mrp_rupees, cost_rupees, weeks_ago, sizes)
# sizes: [(size, inward_qty, sold_qty), ...]
STYLES = [
    (
        "peter-england",
        "Chino Trouser",
        "Khaki",
        "Trouser",
        "6203",
        2000,
        1200,
        10,
        [("30", 25, 3), ("32", 25, 4), ("34", 25, 2), ("36", 25, 1)],
    ),
    (
        "us-polo",
        "Polo Tee",
        "Navy",
        "T-Shirt",
        "6109",
        1200,
        700,
        6,
        [("S", 20, 0), ("M", 20, 0), ("L", 20, 20), ("XL", 20, 20)],
    ),
    (
        "van-heusen",
        "Formal Shirt",
        "White",
        "Shirt",
        "6205",
        1800,
        1100,
        4,
        [("38", 15, 3), ("40", 15, 3), ("42", 15, 3)],
    ),
    (
        "allen-solly",
        "Casual Shirt",
        "Blue",
        "Shirt",
        "6205",
        1500,
        900,
        8,
        [("M", 20, 4), ("L", 20, 4)],
    ),
]


class Command(BaseCommand):
    help = "Seed a demo AW25 stock position for the EOSS planning screen."

    def handle(self, *args: Any, **options: Any) -> None:
        if not Season.objects.filter(code=SEASON_CODE).exists():
            self.stderr.write(f"No season {SEASON_CODE} — run seed_foundation first.")
            return
        store = Store.objects.filter(is_active=True).order_by("code").first()
        if store is None:
            self.stderr.write("No active store — run seed_foundation first.")
            return
        require_legacy_stock_writer(store.pk, operation="Legacy EOSS demo stock seed")
        gstin = store.gstin
        now = timezone.now()
        line_no = 0

        for code, design, color, item, hsn, mrp_rs, cost_rs, weeks_ago, sizes in STYLES:
            brand = Brand.objects.filter(code=code).first()
            if brand is None:
                continue
            when = now - timedelta(weeks=weeks_ago)
            for size, inward_qty, sold_qty in sizes:
                barcode = f"{code}-{design}-{color}-{size}".upper().replace(" ", "-")
                mrp_paise = mrp_rs * 100
                cost_paise = cost_rs * 100

                sku, _ = Sku.objects.get_or_create(
                    barcode=barcode,
                    defaults={
                        "design": design,
                        "color": color,
                        "size": size,
                        "brand": brand.name,
                        "item": item,
                        "hsn": hsn,
                        "mrp_paise": mrp_paise,
                    },
                )
                Cohort.objects.get_or_create(
                    sku=sku,
                    barcode=barcode,
                    season=SEASON_CODE,
                    defaults={"unit_cost_paise": cost_paise, "mrp_paise": mrp_paise},
                )

                doc_in = f"{DOC_PREFIX}-{barcode}-IN"
                line_no += 1
                if not StockLedgerEntry.objects.filter(doc_number=doc_in).exists():
                    with connection.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO stockledger_entry
                                (store_id, gstin_id, sku_code, design, color, size, brand, season,
                                 item, hsn, qty, kind, doc_number, line_no, amount, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            [
                                store.id,
                                gstin.id,
                                barcode,
                                design,
                                color,
                                size,
                                brand.name,
                                SEASON_CODE,
                                item,
                                hsn,
                                inward_qty,
                                StockLedgerEntry.Kind.PT_INWARD,
                                doc_in,
                                line_no,
                                inward_qty * cost_paise,
                                when,
                            ],
                        )

                if (
                    sold_qty
                    and not StockLedgerEntry.objects.filter(
                        doc_number=f"{DOC_PREFIX}-{barcode}-OUT"
                    ).exists()
                ):
                    line_no += 1
                    StockLedgerEntry.objects.create(
                        store=store,
                        gstin=gstin,
                        sku_code=barcode,
                        design=design,
                        color=color,
                        size=size,
                        brand=brand.name,
                        season=SEASON_CODE,
                        item=item,
                        hsn=hsn,
                        qty=-sold_qty,
                        kind=StockLedgerEntry.Kind.SALE_OUT,
                        doc_number=f"{DOC_PREFIX}-{barcode}-OUT",
                        line_no=line_no,
                        amount=-sold_qty * mrp_paise,
                    )

                StockOnHand.objects.update_or_create(
                    store=store,
                    sku_code=barcode,
                    defaults={
                        "gstin": gstin,
                        "design": design,
                        "color": color,
                        "size": size,
                        "brand": brand.name,
                        "season": SEASON_CODE,
                        "item": item,
                        "hsn": hsn,
                        "net_qty": inward_qty - sold_qty,
                        "net_value_paise": (inward_qty - sold_qty) * cost_paise,
                    },
                )

        self.stdout.write(
            self.style.SUCCESS(f"Seeded {len(STYLES)} demo styles for {SEASON_CODE}.")
        )
