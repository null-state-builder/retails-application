"""Give the transfers already on the road their PT (#72).

"Every transfer generates its PT file" has to be true of the ones dispatched
before this slice landed, or the screen quietly shows a document for some
transfers and nothing for others. Their scanned lines are still there and still
immutable, so the PT is built from exactly what it would have been built from at
dispatch — with MRP blank, because nothing snapshotted the ticketed price back
then and inventing one now from today's master would be a guess, not a record.

The row shape is spelled out here rather than imported from
``outbound.transfer_pt``: a data migration must keep producing the same rows it
produced the day it ran, and that stops being true the moment it depends on code
that is free to change. The golden-file test guards the live builder; this
guards history.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations

KDPS_COLUMNS = [
    "SEASON",
    "BRAND",
    "COLOR",
    "GENDER",
    "SUB CATEGORY",
    "TYPE",
    "ITEM",
    "FIT",
    "SIZE",
    "BARCODE",
    "DESIGN",
    "HSN",
    "QTY",
    "MRP",
    "BASIC",
    "P RATE",
    "INPUT TAX",
    "OUTPUT TAX",
    "NAG",
    "MARGIN",
    "SUGGESTED SUB CATEGORY",
    "SUGGESTED TYPE",
]

SUBMITTED = 1


def _money(paise):
    if not paise:
        return ""
    return str((Decimal(paise) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _margin(mrp_paise, cost_paise):
    if not mrp_paise or not cost_paise or mrp_paise <= 0:
        return ""
    pct = (Decimal(mrp_paise - cost_paise) / Decimal(mrp_paise)) * 100
    return str(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _row(line):
    return {
        **dict.fromkeys(KDPS_COLUMNS, ""),
        "SEASON": line.season,
        "BRAND": line.brand,
        "COLOR": line.color,
        "ITEM": line.item,
        "SIZE": line.size,
        "BARCODE": line.sku_code,
        "DESIGN": line.design,
        "HSN": line.hsn,
        "QTY": line.qty_dispatched,
        "MRP": _money(line.mrp_paise),
        "P RATE": _money(line.unit_cost_paise),
        "NAG": line.qty_dispatched,
        "MARGIN": _margin(line.mrp_paise, line.unit_cost_paise),
    }


def backfill(apps, schema_editor):
    StoreTransfer = apps.get_model("outbound", "StoreTransfer")
    TransferPT = apps.get_model("outbound", "TransferPT")

    dispatched = StoreTransfer.objects.filter(docstatus=SUBMITTED, pt__isnull=True)
    TransferPT.objects.bulk_create(
        [
            TransferPT(
                transfer=transfer,
                rows=[_row(line) for line in transfer.lines.all() if line.qty_dispatched],
                generated_by=transfer.dispatched_by,
            )
            for transfer in dispatched.prefetch_related("lines")
        ]
    )


def drop(apps, schema_editor):
    """Reversing this slice takes the generated PTs with it — they are derived,
    so nothing is lost that a regeneration cannot rebuild."""
    apps.get_model("outbound", "TransferPT").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("outbound", "0014_storetransferline_mrp_paise_transferpt")]

    operations = [migrations.RunPython(backfill, drop)]
