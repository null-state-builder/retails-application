from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models

import core.money


def backfill_identity(apps, schema_editor):
    """Seed the SKU + cohort masters from the existing stock ledger so the live
    preview isn't empty (no-op on a fresh/test DB)."""
    SLE = apps.get_model("stockledger", "StockLedgerEntry")
    Sku = apps.get_model("masters", "Sku")
    Cohort = apps.get_model("masters", "Cohort")

    latest: dict[str, dict] = {}
    for e in SLE.objects.all().iterator():
        bc = (e.sku_code or "")[:64]
        if not bc:
            continue
        cur = latest.get(bc)
        if cur is None or e.id > cur["id"]:
            latest[bc] = {
                "id": e.id, "design": e.design, "color": e.color, "size": e.size,
                "brand": e.brand, "item": e.item, "hsn": e.hsn, "doc": e.doc_number,
            }
    for bc, d in latest.items():
        Sku.objects.update_or_create(
            barcode=bc,
            defaults={
                "design": d["design"] or "", "color": d["color"] or "",
                "size": d["size"] or "", "brand": d["brand"] or "",
                "item": d["item"] or "", "hsn": d["hsn"] or "",
                "mrp_paise": None, "first_doc_number": d["doc"] or "", "is_active": True,
            },
        )

    coh: dict[tuple[str, str], dict] = {}
    for e in SLE.objects.filter(qty__gt=0).iterator():
        bc = (e.sku_code or "")[:64]
        if not bc:
            continue
        unit = int(e.amount // e.qty) if e.qty else 0
        coh[(bc, (e.season or "")[:120])] = {"unit": unit, "doc": e.doc_number}
    for (bc, season), d in coh.items():
        sku = Sku.objects.filter(barcode=bc).first()
        if not sku:
            continue
        Cohort.objects.update_or_create(
            barcode=bc, season=season,
            defaults={
                "sku": sku, "unit_cost_paise": d["unit"],
                "mrp_paise": None, "last_doc_number": d["doc"] or "",
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("masters", "0001_initial"),
        ("stockledger", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="gstslab",
            name="threshold_paise",
            field=core.money.MoneyField(default=250000),
        ),
        migrations.CreateModel(
            name="Sku",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("barcode", models.CharField(db_index=True, max_length=64, unique=True)),
                ("design", models.CharField(blank=True, default="", max_length=120)),
                ("color", models.CharField(blank=True, default="", max_length=60)),
                ("size", models.CharField(blank=True, default="", max_length=24)),
                ("brand", models.CharField(blank=True, default="", max_length=120)),
                ("item", models.CharField(blank=True, default="", max_length=120)),
                ("hsn", models.CharField(blank=True, default="", max_length=24)),
                ("mrp_paise", core.money.MoneyField(blank=True, null=True)),
                ("first_doc_number", models.CharField(blank=True, default="", max_length=128)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={"ordering": ["barcode"]},
        ),
        migrations.AddIndex(
            model_name="sku",
            index=models.Index(fields=["brand"], name="masters_sku_brand_idx"),
        ),
        migrations.AddIndex(
            model_name="sku",
            index=models.Index(fields=["design"], name="masters_sku_design_idx"),
        ),
        migrations.CreateModel(
            name="Cohort",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("barcode", models.CharField(db_index=True, max_length=64)),
                ("season", models.CharField(max_length=120)),
                ("unit_cost_paise", core.money.MoneyField()),
                ("mrp_paise", core.money.MoneyField(blank=True, null=True)),
                ("last_doc_number", models.CharField(blank=True, default="", max_length=128)),
                ("sku", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cohorts", to="masters.sku")),
            ],
            options={"ordering": ["barcode", "season"]},
        ),
        migrations.AddConstraint(
            model_name="cohort",
            constraint=models.UniqueConstraint(fields=("barcode", "season"), name="uq_cohort_barcode_season"),
        ),
        migrations.AddConstraint(
            model_name="cohort",
            constraint=models.CheckConstraint(
                condition=models.Q(mrp_paise__isnull=True)
                | models.Q(unit_cost_paise__lte=models.F("mrp_paise")),
                name="ck_cohort_unit_cost_le_mrp",
            ),
        ),
        migrations.AddIndex(
            model_name="cohort",
            index=models.Index(fields=["season"], name="masters_cohort_season_idx"),
        ),
        migrations.RunPython(backfill_identity, migrations.RunPython.noop),
    ]
