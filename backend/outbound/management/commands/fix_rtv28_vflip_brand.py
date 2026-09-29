"""One-shot fix: cancel polluted RTV id=28 + backfill 'V KDPS' brand labels.

SLE is append-only (DB trigger prevents UPDATE/DELETE), so brand corrections
are done via compensating entries: −qty 'V KDPS' + +qty 'V {original_brand}'.
StockOnHand is mutable and updated directly.

Safe to re-run (idempotent). Prints a summary of all actions taken.
"""

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Cancel polluted RTV id=28 and backfill V-flip brand labels"

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        from core.documents import DocStatus
        from outbound.models import ReturnToVendor, VFlip
        from stockledger.contracts import require_legacy_stock_writer
        from stockledger.models import StockLedgerEntry, StockOnHand

        actions = []

        # --- 1. Cancel RTV id=28 and reverse stock ---
        try:
            rtv = ReturnToVendor.objects.select_related("store").get(pk=28)
        except ReturnToVendor.DoesNotExist:
            self.stdout.write("RTV id=28 not found — skipping.")
            rtv = None

        if rtv and rtv.docstatus == DocStatus.SUBMITTED:
            require_legacy_stock_writer(rtv.store_id, operation="Legacy RTV repair")
            sle_entries = StockLedgerEntry.objects.filter(doc_number=rtv.doc_number)
            for entry in sle_entries:
                # Append-only reversal entry
                StockLedgerEntry.objects.create(
                    store=entry.store,
                    gstin=entry.gstin,
                    sku_code=entry.sku_code,
                    design=entry.design,
                    color=entry.color,
                    size=entry.size,
                    brand=entry.brand,
                    season=entry.season,
                    item=entry.item,
                    hsn=entry.hsn,
                    qty=-entry.qty,
                    amount=-entry.amount,
                    kind=entry.kind,
                    doc_number=f"{entry.doc_number}/REV",
                    line_no=entry.line_no,
                )
                # Restore StockOnHand
                try:
                    oh = StockOnHand.objects.get(
                        store=entry.store,
                        sku_code=entry.sku_code,
                    )
                    oh.net_qty -= entry.qty
                    oh.net_value_paise -= entry.amount
                    oh.save()
                    actions.append(
                        f"Restored {-entry.qty} units of {entry.sku_code} "
                        f"at {entry.store.code} (now {oh.net_qty})"
                    )
                except StockOnHand.DoesNotExist:
                    actions.append(
                        f"WARNING: No StockOnHand for {entry.sku_code} at {entry.store.code}"
                    )

            ReturnToVendor.objects.filter(pk=28).update(docstatus=DocStatus.CANCELLED)
            actions.append(f"Cancelled RTV id=28 ({rtv.doc_number})")
        elif rtv and rtv.docstatus == DocStatus.CANCELLED:
            actions.append("RTV id=28 already cancelled — no action.")

        # --- 2. Backfill 'V KDPS' → 'V {original_brand}' ---
        vflips = VFlip.objects.filter(
            docstatus=DocStatus.SUBMITTED,
        ).select_related("original_brand", "store")

        for vf in vflips:
            require_legacy_stock_writer(vf.store_id, operation="Legacy V-flip repair")
            correct_v_brand = f"V {vf.original_brand.name}" if vf.original_brand else None
            if not correct_v_brand:
                continue

            # SLE: append-only — post compensating entries for each wrong row
            # Check if brand-fix already applied (idempotent)
            already_fixed = StockLedgerEntry.objects.filter(
                doc_number=f"{vf.doc_number}/BRAND-FIX",
            ).exists()
            if already_fixed:
                continue

            wrong_sle = list(
                StockLedgerEntry.objects.filter(
                    doc_number=vf.doc_number,
                    kind="vflip_in",
                    brand="V KDPS",
                )
            )
            for entry in wrong_sle:
                # Reverse the wrong-brand entry
                StockLedgerEntry.objects.create(
                    store=entry.store,
                    gstin=entry.gstin,
                    sku_code=entry.sku_code,
                    design=entry.design,
                    color=entry.color,
                    size=entry.size,
                    brand="V KDPS",
                    season=entry.season,
                    item=entry.item,
                    hsn=entry.hsn,
                    qty=-entry.qty,
                    amount=-entry.amount,
                    kind="vflip_in",
                    doc_number=f"{entry.doc_number}/BRAND-FIX",
                    line_no=entry.line_no,
                )
                # Re-post with correct brand
                StockLedgerEntry.objects.create(
                    store=entry.store,
                    gstin=entry.gstin,
                    sku_code=entry.sku_code,
                    design=entry.design,
                    color=entry.color,
                    size=entry.size,
                    brand=correct_v_brand,
                    season=entry.season,
                    item=entry.item,
                    hsn=entry.hsn,
                    qty=entry.qty,
                    amount=entry.amount,
                    kind="vflip_in",
                    doc_number=f"{entry.doc_number}/BRAND-FIX",
                    line_no=entry.line_no + 1000,
                )
                actions.append(
                    f"SLE brand-fix for {entry.sku_code} doc {vf.doc_number}: "
                    f"'V KDPS' → '{correct_v_brand}'"
                )

            # StockOnHand: mutable — direct update
            for line in vf.lines.all():
                updated_oh = StockOnHand.objects.filter(
                    store_id=vf.store_id,
                    sku_code=line.sku_code,
                    brand="V KDPS",
                ).update(brand=correct_v_brand)
                if updated_oh:
                    actions.append(
                        f"Fixed StockOnHand {line.sku_code} at "
                        f"{vf.store.code}: 'V KDPS' → '{correct_v_brand}'"
                    )

        # --- Summary ---
        if actions:
            for a in actions:
                self.stdout.write(self.style.SUCCESS(f"  ✓ {a}"))
        else:
            self.stdout.write("No actions needed — data already clean.")
