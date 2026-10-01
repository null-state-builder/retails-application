"""Public item names from the exact reviewed opening source, never identities.

Callers supply already authorised origin IDs. The immutable manifest and source
review bind the row's stable SKU and original source hash; a label cannot select
or grant a SKU. Only ItemName leaves this reader, including for older manifests
whose frozen description was a filename and row number.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
import uuid

from core.canonical import content_hash
from ptmapper.goods_models import OpeningManifestRow
from ptmapper.soh_models import SohImportBatch, SohImportReview, SohImportRow
from stockledger.goods_models import Origin


#: Verified review rows by (review id, content hash). A review is append-only
#: evidence; holding the copy that once matched its own hash means a later read
#: never trusts different bytes, and a bill no longer re-hashes a whole source.
_VERIFIED_REVIEWS: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
_VERIFIED_LIMIT = 32


def _verified_review_rows(review_id: Any, review_hash: str) -> dict[str, dict[str, Any]] | None:
    key = (str(review_id), review_hash)
    cached = _VERIFIED_REVIEWS.get(key)
    if cached is not None:
        return cached
    payload = SohImportReview.objects.filter(pk=review_id).values_list("payload", flat=True).first()
    if payload is None or content_hash(payload) != review_hash:
        return None
    rows = {entry["key"]: entry for entry in payload.get("rows", [])}
    if len(_VERIFIED_REVIEWS) >= _VERIFIED_LIMIT:
        _VERIFIED_REVIEWS.pop(next(iter(_VERIFIED_REVIEWS)))
    _VERIFIED_REVIEWS[key] = rows
    return rows


def origin_item_names(tenant_id: Any, origin_ids: Iterable[Any]) -> dict[str, str]:
    origins: list[dict[str, Any]] = [dict(row) for row in Origin.objects.filter(tenant_id=tenant_id, pk__in=list(origin_ids))
                                    .values("id", "sku_id", "frozen_evidence")]
    by_row: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for origin in origins:
        value = (origin["frozen_evidence"] or {}).get("manifest_row_id")
        try:
            row_id = uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            continue
        by_row.setdefault(row_id, []).append(origin)
    if not by_row:
        return {}
    rows = list(OpeningManifestRow.objects.filter(tenant_id=tenant_id, pk__in=by_row)
                .select_related("manifest_version"))
    batches = {batch.manifest_id: batch for batch in SohImportBatch.objects.filter(
        tenant_id=tenant_id, manifest_id__in={row.manifest_version.manifest_id for row in rows},
        source_import__tenant_id=tenant_id, source_import__approved_by__isnull=False,
    ).select_related("source_import")}
    sources = {batch.source_import_id: batch.source_import for batch in batches.values()}
    reviews: dict[Any, dict[str, Any]] = {}
    for review_id, source_id, review_hash in SohImportReview.objects.filter(
            tenant_id=tenant_id, source_import_id__in={batch.source_import_id for batch in batches.values()},
    ).values_list("pk", "source_import_id", "content_hash"):
        if review_hash != sources[source_id].reviewed_hash:
            continue
        verified = _verified_review_rows(review_id, review_hash)
        if verified is not None:
            reviews[source_id] = verified
    source_rows = {(row.source_import_id, row.source_row_key): row for row in SohImportRow.objects.filter(
        tenant_id=tenant_id, source_import_id__in=reviews,
        source_row_key__in={row.source_row_key for row in rows},
    )}
    names: dict[str, str] = {}
    for row in rows:
        batch = batches.get(row.manifest_version.manifest_id)
        if batch is None:
            continue
        source = batch.source_import
        raw = source_rows.get((source.pk, row.source_row_key))
        reviewed = reviews.get(source.pk, {}).get(row.source_row_key, {})
        sku_id = str((row.payload.get("identity") or {}).get("sku_id") or "")
        if (raw is None or not sku_id or str((reviewed.get("mapping") or {}).get("sku_id")) != sku_id
                or row.manifest_version.source_evidence_id != source.source_evidence_id
                or row.payload.get("dataset_key") != f"soh:{source.source_hash}"):
            continue
        original = {"ordinal": raw.ordinal, "barcode": raw.barcode, "quantity": raw.quantity,
                    "mrp_paise": raw.mrp_paise, "source_rate_paise": raw.source_rate_paise, "source": raw.source}
        if content_hash(original) != reviewed.get("source_input_hash"):
            continue
        name = str(raw.source.get("itemname") or "").strip()[:240]
        if name:
            for origin in by_row[row.pk]:
                if str(origin["sku_id"]) == sku_id:
                    names[str(origin["id"])] = name
    return names
