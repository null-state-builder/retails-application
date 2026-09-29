"""Rebuild the materialised projections from the append-only stock ledger.

`StockOnHand` (at-location) and `InTransitStock` (the in-transit bucket per
transfer) are caches maintained inside each posting transaction; this command
recomputes both from scratch (the ledger is the source of truth) - useful after
a backfill, a bulk import, or any doubt about drift. Idempotent.

Transit legs (``transit_in``/``transit_out``) ride on the *source* store but
are NOT at-location stock - they aggregate into `InTransitStock` keyed by the
transfer's doc number, never into `StockOnHand`. Quarantine legs
(``quarantine_in``/``quarantine_out``) ride on their store but likewise are NOT
free-to-sell - they aggregate into `QuarantineStock` keyed by (store × barcode).
The matching ``damage_out`` leg IS an at-location leg and reduces `StockOnHand`
normally.

The selling kinds need no rule of their own, which is the point of naming them
the way they are named: ``sale_out`` and ``sale_return_in`` are ordinary
at-location legs and fall through to `StockOnHand` here. A customer's *damaged*
return posts ``quarantine_in`` rather than ``sale_return_in``, so it is placed by
the quarantine rule above and this command never has to know what a sale is.
"""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from stockledger.contracts import goods_v1_site_ids
from stockledger.models import (
    QUARANTINE_KINDS,
    TRANSIT_KINDS,
    InTransitStock,
    QuarantineStock,
    StockLedgerEntry,
    StockOnHand,
    identity_dims,
)


class Command(BaseCommand):
    help = "Recompute StockOnHand + InTransitStock from the append-only stock ledger."

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        legacy_sites = StockLedgerEntry.objects.values_list("store_id", flat=True).distinct()
        fenced = goods_v1_site_ids(legacy_sites)
        if fenced:
            raise CommandError(
                "Legacy stock projections cannot be rebuilt for goods-v1 sites: "
                + ", ".join(str(site_id) for site_id in sorted(fenced))
            )
        agg: dict[tuple[int, str], list[int]] = {}
        desc: dict[tuple[int, str], dict[str, Any]] = {}
        transit_agg: dict[tuple[str, str], list[int]] = {}
        transit_desc: dict[tuple[str, str], dict[str, Any]] = {}
        quar_agg: dict[tuple[int, str], list[int]] = {}
        quar_desc: dict[tuple[int, str], dict[str, Any]] = {}

        for e in StockLedgerEntry.objects.all().iterator():
            if e.kind in TRANSIT_KINDS:
                tkey = (e.doc_number, e.sku_code)
                t = transit_agg.setdefault(tkey, [0, 0])
                t[0] += e.qty
                t[1] += int(e.amount or 0)
                if e.qty > 0:
                    transit_desc[tkey] = {
                        "source_store_id": e.store_id,
                        "gstin_id": e.gstin_id,
                        **identity_dims(e, e.store.tenant_id),
                    }
                continue

            if e.kind in QUARANTINE_KINDS:
                qkey = (e.store_id, e.sku_code)
                qa = quar_agg.setdefault(qkey, [0, 0])
                qa[0] += e.qty
                qa[1] += int(e.amount or 0)
                if e.qty > 0:
                    prev = quar_desc.get(qkey)
                    # Latest positive quarantine leg wins for who/when.
                    if prev is None or e.id > prev["id"]:
                        quar_desc[qkey] = {
                            "id": e.id,
                            "gstin_id": e.gstin_id,
                            "marked_by_id": e.posted_by_id,
                            "marked_at": e.created_at,
                            **identity_dims(e, e.store.tenant_id),
                        }
                continue

            key = (e.store_id, e.sku_code)
            a = agg.setdefault(key, [0, 0])
            a[0] += e.qty
            a[1] += int(e.amount or 0)
            if e.qty > 0:
                d = desc.get(key)
                if d is None or e.id > d["id"]:
                    desc[key] = {
                        "id": e.id,
                        "gstin_id": e.gstin_id,
                        "design": e.design,
                        "color": e.color,
                        "size": e.size,
                        "brand": e.brand,
                        "season": e.season,
                        "item": e.item,
                        "hsn": e.hsn,
                        **identity_dims(e, e.store.tenant_id),
                    }

        StockOnHand.objects.all().delete()
        rows = []
        for (store_id, sku_code), (q, v) in agg.items():
            d = desc.get((store_id, sku_code), {})
            rows.append(
                StockOnHand(
                    store_id=store_id,
                    sku_code=sku_code,
                    gstin_id=d.get("gstin_id"),
                    design=d.get("design", "") or "",
                    color=d.get("color", "") or "",
                    size=d.get("size", "") or "",
                    brand=d.get("brand", "") or "",
                    season=d.get("season", "") or "",
                    item=d.get("item", "") or "",
                    hsn=d.get("hsn", "") or "",
                    net_qty=q,
                    net_value_paise=v,
                )
            )
        StockOnHand.objects.bulk_create(rows)

        # In-transit bucket: destination comes from the transfer document
        # (lazy lookup - the generic ledger app holds no FK to outbound).
        StoreTransfer = apps.get_model("outbound", "StoreTransfer")
        destinations = dict(
            StoreTransfer.objects.filter(
                doc_number__in={doc for doc, _ in transit_agg}
            ).values_list("doc_number", "destination_store_id")
        )
        InTransitStock.objects.all().delete()
        transit_rows = []
        for (doc_number, sku_code), (q, v) in transit_agg.items():
            if q == 0:
                continue
            d = transit_desc.get((doc_number, sku_code), {})
            source_store_id = d.get("source_store_id")
            destination_store_id = destinations.get(doc_number)
            if source_store_id is None or destination_store_id is None:
                self.stderr.write(
                    f"Skipping orphan transit rows for {doc_number}/{sku_code}: "
                    "no positive transit leg or no matching transfer document."
                )
                continue
            transit_rows.append(
                InTransitStock(
                    transfer_doc_number=doc_number,
                    sku_code=sku_code,
                    source_store_id=source_store_id,
                    destination_store_id=destination_store_id,
                    gstin_id=d.get("gstin_id"),
                    design=d.get("design", "") or "",
                    color=d.get("color", "") or "",
                    size=d.get("size", "") or "",
                    brand=d.get("brand", "") or "",
                    season=d.get("season", "") or "",
                    item=d.get("item", "") or "",
                    hsn=d.get("hsn", "") or "",
                    qty=q,
                    value_paise=v,
                )
            )
        InTransitStock.objects.bulk_create(transit_rows)

        # Quarantine bucket: keyed by (store × barcode), carrying who/when.
        QuarantineStock.objects.all().delete()
        quar_rows = []
        for (store_id, sku_code), (q, v) in quar_agg.items():
            if q == 0:
                continue
            d = quar_desc.get((store_id, sku_code), {})
            quar_rows.append(
                QuarantineStock(
                    store_id=store_id,
                    sku_code=sku_code,
                    gstin_id=d.get("gstin_id"),
                    design=d.get("design", "") or "",
                    color=d.get("color", "") or "",
                    size=d.get("size", "") or "",
                    brand=d.get("brand", "") or "",
                    season=d.get("season", "") or "",
                    item=d.get("item", "") or "",
                    hsn=d.get("hsn", "") or "",
                    qty=q,
                    value_paise=v,
                    marked_by_id=d.get("marked_by_id"),
                    marked_at=d.get("marked_at"),
                )
            )
        QuarantineStock.objects.bulk_create(quar_rows)
        self.stdout.write(
            self.style.SUCCESS(
                f"Rebuilt {len(rows)} StockOnHand + {len(transit_rows)} InTransitStock "
                f"+ {len(quar_rows)} QuarantineStock rows."
            )
        )
