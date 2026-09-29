from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models

import core.money


def backfill_on_hand(apps, schema_editor):
    """Materialise current on-hand from the existing ledger (no-op on a fresh DB)."""
    SLE = apps.get_model("stockledger", "StockLedgerEntry")
    OH = apps.get_model("stockledger", "StockOnHand")

    agg: dict[tuple[int, str], list[int]] = {}
    desc: dict[tuple[int, str], dict] = {}
    for e in SLE.objects.all().iterator():
        key = (e.store_id, e.sku_code)
        a = agg.setdefault(key, [0, 0])
        a[0] += e.qty
        a[1] += e.amount
        if e.qty > 0:
            d = desc.get(key)
            if d is None or e.id > d["id"]:
                desc[key] = {
                    "id": e.id, "gstin_id": e.gstin_id, "design": e.design,
                    "color": e.color, "size": e.size, "brand": e.brand,
                    "season": e.season, "item": e.item, "hsn": e.hsn,
                }
    for (store_id, sku_code), (q, v) in agg.items():
        d = desc.get((store_id, sku_code), {})
        OH.objects.update_or_create(
            store_id=store_id, sku_code=sku_code,
            defaults={
                "gstin_id": d.get("gstin_id"),
                "design": d.get("design", "") or "", "color": d.get("color", "") or "",
                "size": d.get("size", "") or "", "brand": d.get("brand", "") or "",
                "season": d.get("season", "") or "", "item": d.get("item", "") or "",
                "hsn": d.get("hsn", "") or "", "net_qty": q, "net_value_paise": v,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("stockledger", "0002_truncate_guard"),
        ("masters", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="StockOnHand",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sku_code", models.CharField(db_index=True, max_length=64)),
                ("design", models.CharField(blank=True, default="", max_length=120)),
                ("color", models.CharField(blank=True, default="", max_length=60)),
                ("size", models.CharField(blank=True, default="", max_length=24)),
                ("brand", models.CharField(blank=True, default="", max_length=120)),
                ("season", models.CharField(blank=True, default="", max_length=120)),
                ("item", models.CharField(blank=True, default="", max_length=120)),
                ("hsn", models.CharField(blank=True, default="", max_length=24)),
                ("net_qty", models.IntegerField(default=0)),
                ("net_value_paise", core.money.MoneyField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="on_hand", to="masters.store")),
                ("gstin", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="on_hand", to="masters.gstin")),
            ],
            options={"db_table": "stockledger_on_hand", "ordering": ["brand", "-net_qty"]},
        ),
        migrations.AddConstraint(
            model_name="stockonhand",
            constraint=models.UniqueConstraint(fields=("store", "sku_code"), name="uq_on_hand_store_sku"),
        ),
        migrations.AddIndex(
            model_name="stockonhand",
            index=models.Index(fields=["brand"], name="onhand_brand_idx"),
        ),
        migrations.AddIndex(
            model_name="stockonhand",
            index=models.Index(fields=["season"], name="onhand_season_idx"),
        ),
        migrations.AddIndex(
            model_name="stockonhand",
            index=models.Index(fields=["net_qty"], name="onhand_net_qty_idx"),
        ),
        migrations.RunPython(backfill_on_hand, migrations.RunPython.noop),
    ]
