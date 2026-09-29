"""Copy approved shrinkage into the reporting store (store operations ticket 44, ST-INV-4).

**What counts.** At a goods-v1 site, only documents the Owner approved that
still stand (``DocumentHead.state`` official, so anything no longer official is
left out):

* a **shrinkage adjustment** (purpose ``shrinkage``) - recorded pieces lost,
  usually found short at a count - line by line, its pieces and the recorded
  layer cost it removed from stock;
* a **write-off** (purpose ``writeoff``) whose reason code is one of
  ``KDPS_SHRINKAGE_WRITEOFF_REASONS`` - its pieces and the recorded cost it
  recognised as lost.

An adjust-down is a counting or recording error, not a loss, and a write-off for
another reason (damage, mildew) is not shrinkage; neither is copied. A found
adjustment adds stock and is never shrinkage. A goods-v1 count's own difference
is not posted anywhere yet (goods ticket 17A), so it cannot be copied; the
report says so.

At a site still on the legacy stock contract, the approved and posted legacy
documents (``docstatus`` submitted; a cancelled one is left out):

* a **stock adjustment** line that took pieces off (``adj_qty`` below nought)
  whose reason - the line's own where a recount gave one, else the document's -
  is ``shrinkage``; a miscount or damage line is not;
* a **write-off** whose reason, the whole text, is one of the same shrinkage
  reasons.

Legacy lines carry their own frozen brand, item (category) and unit cost, as
legacy bills do; a unit cost of nought means none was recorded (unknown). They
are dated by the document's last change, which is its posting: a posted legacy
document is frozen.

**Cost** is each portion's own recorded layer cost (``Origin.unit_cost``), the
same figure the document posted. A line with a portion that had no recorded
cost (unvalued found custody) has unknown cost, never nought.

**Brand and category** of goods-v1 lines are named as the till names them on
a bill (``candidates_for``: the style's brand and the item's grade), so a
store's shrinkage and its sales line up in one row. They are read from today's
masters on every run, so a renamed brand shows under its new name.

**Kept apart.** Runs on the worker's clock and from ``manage.py
refresh_shrinkage_report``, never while a report page loads. It only reads the
goods and legacy stock tables, with plain reads, and writes only its own
table, whole, in one transaction: there are few such documents, so every run
rebuilds the copy and a reversal or a changed reason list is picked up at once.
The worker calls it once per tenant each tick; a copy rebuilt less than half a
tick ago is left alone.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from core.commands import database_now
from core.documents import DocStatus
from core.kernel_models import DocumentHead, OfficialLine, OfficialVersion
from core.outbox import ANCHOR_INTERVAL
from core.tenancy import tenant_context
from masters.goods_identity_services import candidates_for
from masters.goods_models import Tenant
from outbound.models import AdjustmentReason, StockAdjustmentLine, WriteOffLine
from reporting.models import ReportRefresh, ShrinkageLineFact
from stockledger.goods_models import Origin

logger = logging.getLogger(__name__)

#: The ``ReportRefresh`` row this copy keeps its freshness in.
KEY = "shrinkage"
#: Its own advisory lock, apart from the other copies'.
LOCK_ID = 7_310_044

SHRINKAGE = ShrinkageLineFact.Source.SHRINKAGE.value
WRITEOFF = ShrinkageLineFact.Source.WRITEOFF.value


@dataclass(frozen=True)
class RefreshResult:
    #: False when another run held the lock, so this one did nothing.
    ran: bool
    as_of: datetime | None = None
    documents: int = 0
    took_ms: int = 0


def shrinkage_reasons() -> frozenset[str]:
    """The write-off reason codes that count as shrinkage, in capitals."""
    return frozenset(str(code).strip().upper() for code in settings.KDPS_SHRINKAGE_WRITEOFF_REASONS)


def is_shrinkage(kind: str, reason_code: str) -> bool:
    """Is an approved document of ``kind`` with ``reason_code`` shrinkage?"""
    if kind == SHRINKAGE:
        return True
    return kind == WRITEOFF and reason_code.strip().upper() in shrinkage_reasons()


def scheduled_refresh(_tenant_id: uuid.UUID) -> None:
    """The worker's call, once per tenant each tick. The copy covers every tenant,
    so a copy rebuilt within the last half tick is fresh enough; a run already in
    progress elsewhere is simply skipped."""
    state = ReportRefresh.objects.filter(key=KEY).first()
    if state and state.as_of and timezone.now() - state.as_of < ANCHOR_INTERVAL / 2:
        return
    refresh()


def refresh() -> RefreshResult:
    """Rebuild the shrinkage copy from the approved documents."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        row = cursor.fetchone()
    if not row or not row[0]:
        return RefreshResult(ran=False)
    try:
        return _refresh()
    except Exception as exc:
        ReportRefresh.objects.update_or_create(
            key=KEY,
            defaults={"failed_at": timezone.now(), "failure": f"{type(exc).__name__}: {exc}"[:500]},
        )
        raise
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
        except Exception:  # noqa: BLE001 - logged, the original error stands
            logger.exception("shrinkage report refresh: advisory unlock failed")


def _refresh() -> RefreshResult:
    clock = time.monotonic()
    started = database_now()
    facts: list[ShrinkageLineFact] = []
    documents = 0
    for tenant_id in Tenant.objects.order_by("id").values_list("id", flat=True):
        with tenant_context(tenant_id):
            found, count = _tenant_facts(tenant_id)
        facts += found
        documents += count
    found, count = _legacy_facts()
    facts += found
    documents += count
    with transaction.atomic():
        state, _ = ReportRefresh.objects.select_for_update().get_or_create(key=KEY)
        ShrinkageLineFact.objects.all().delete()
        ShrinkageLineFact.objects.bulk_create(facts, batch_size=5000)
        took = int((time.monotonic() - clock) * 1000)
        state.as_of = started
        state.took_ms = took
        state.documents = documents
        state.failed_at = None
        state.failure = ""
        state.save()
    return RefreshResult(ran=True, as_of=started, documents=documents, took_ms=took)


def _tenant_facts(tenant_id: uuid.UUID) -> tuple[list[ShrinkageLineFact], int]:
    heads = list(
        DocumentHead.objects.filter(
            tenant_id=tenant_id,
            state=DocumentHead.State.OFFICIAL,
            live_version__isnull=False,
            # A goods movement's ``purpose`` is its kind; ``kind`` is its series (SHR, WOF).
            document__purpose__in=(SHRINKAGE, WRITEOFF),
        )
        .select_related("document", "live_version")
        .order_by("document__created_at", "document_id")
    )
    counted = [
        head for head in heads if is_shrinkage(head.document.purpose, _reason(head.live_version))
    ]
    if not counted:
        return [], 0
    lines: dict[uuid.UUID, list[OfficialLine]] = {}
    for line in OfficialLine.objects.filter(
        version_id__in=[head.live_version_id for head in counted]
    ).order_by("version_id", "line_no"):
        lines.setdefault(line.version_id, []).append(line)
    bodies = [line.payload for found in lines.values() for line in found]
    costs = _costs(bodies)
    described = {
        row["sku_id"]: row
        for row in candidates_for(tenant_id, {str(b["sku_id"]) for b in bodies if b.get("sku_id")})
    }
    out: list[ShrinkageLineFact] = []
    for head in counted:
        version = head.live_version
        assert version is not None
        document = head.document
        day = timezone.localtime(version.event_at).date()
        reason = _reason(version)
        for line in lines.get(version.pk, []):
            body = line.payload
            sku_id = str(body.get("sku_id") or "")
            identity = described.get(sku_id, {})
            pieces, cost = _measure(body, costs)
            out.append(
                ShrinkageLineFact(
                    store_id=document.held_site_id,
                    day=day,
                    source=document.purpose,
                    document_id=document.pk,
                    number=document.official_number or "",
                    line_key=line.stable_line_key,
                    reason_code=reason[:60],
                    sku_id=uuid.UUID(sku_id) if sku_id else None,
                    brand=str(identity.get("brand") or "")[:120],
                    brand_ref_id=identity.get("brand_id"),
                    category=str(identity.get("grade") or "")[:120],
                    pieces=pieces,
                    cost_paise=cost,
                )
            )
    return out, len(counted)


def _legacy_facts() -> tuple[list[ShrinkageLineFact], int]:
    """Approved, posted legacy adjustments and write-offs that are shrinkage."""
    out: list[ShrinkageLineFact] = []
    documents: set[str] = set()
    lines = StockAdjustmentLine.objects.filter(
        adjustment__docstatus=DocStatus.SUBMITTED, adj_qty__lt=0
    ).select_related("adjustment")
    for line in lines.order_by("adjustment_id", "id"):
        adjustment = line.adjustment
        if (line.reason or adjustment.reason) != AdjustmentReason.SHRINKAGE:
            continue
        documents.add(f"adj:{adjustment.pk}")
        out.append(_legacy_fact(adjustment, line, SHRINKAGE, str(adjustment.reason), -line.adj_qty))
    reasons = shrinkage_reasons()
    for wline in (
        WriteOffLine.objects.filter(writeoff__docstatus=DocStatus.SUBMITTED)
        .select_related("writeoff")
        .order_by("writeoff_id", "id")
    ):
        writeoff = wline.writeoff
        if writeoff.reason.strip().upper() not in reasons:
            continue
        documents.add(f"wro:{writeoff.pk}")
        out.append(_legacy_fact(writeoff, wline, WRITEOFF, writeoff.reason, wline.qty))
    return out, len(documents)


def _legacy_fact(
    document: Any, line: Any, source: str, reason: str, pieces: int
) -> ShrinkageLineFact:
    unit_cost = int(line.unit_cost_paise or 0)
    return ShrinkageLineFact(
        store_id=document.store_id,
        day=timezone.localtime(document.updated_at).date(),
        source=source,
        document_id=document.idempotency_uuid,
        number=document.doc_number or "",
        line_key=uuid.uuid5(uuid.NAMESPACE_URL, f"kdps-legacy-{source}-line:{line.pk}"),
        reason_code=reason.strip()[:60],
        sku_id=None,
        brand=(line.brand or "")[:120],
        brand_ref_id=line.brand_ref_id,
        category=(line.item or "")[:120],
        pieces=pieces,
        cost_paise=pieces * unit_cost if unit_cost else None,
    )


def _reason(version: OfficialVersion | None) -> str:
    header = version.canonical_payload if version is not None else None
    return str((header or {}).get("reason_code") or "")


def _portions(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(body.get("portions") or [])


def _costs(bodies: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    wanted = {
        str(piece["origin_id"])
        for body in bodies
        for piece in _portions(body)
        if piece.get("origin_id")
    }
    return {
        str(pk): int(cost)
        for pk, cost in Origin.objects.filter(pk__in=wanted).values_list("pk", "unit_cost")
    }


def _measure(body: Mapping[str, Any], costs: Mapping[str, int]) -> tuple[int, int | None]:
    """A line's pieces and recorded cost, portion by portion, as the document posted it.

    The cost is unknown (None) when any portion had no recorded cost.
    """
    portions = _portions(body)
    if not portions:
        return int(body.get("qty") or 0), None
    pieces = 0
    cost: int | None = 0
    for piece in portions:
        length = int(piece["upper"]) - int(piece["lower"])
        pieces += length
        origin = str(piece.get("origin_id") or "")
        if cost is None or origin not in costs:
            cost = None
            continue
        cost += length * costs[origin]
    return pieces, cost
