"""Copy bill lines for the brands' Sale reports (store operations ticket 29, ST-BRD-2).

Kept apart from billing exactly as the sales copy is (``reporting.sales_facts``):
it runs on the worker's clock (``scheduled_refresh``) and from ``manage.py
refresh_brand_reports``, never inside a bill and never while a page waits. It
reads the billing tables with plain reads that take no lock and writes only
``report_brand_sale_line_fact``. A brand report reads that table alone.

One row per goods line, signed: a piece sold is positive; a piece given back
(an exchange leg, or an old standalone return) is negative, at the MRP of the
line it came off and what the customer was paid back, as the sales copy
values it. Discount is always Price less Total, so every row adds up as the
brand's sheet does.

Incremental in the same way: each run re-reads the documents changed since the
last run began, less the same overlap, and replaces their rows whole. Its
freshness is its own ``ReportRefresh`` row (key ``brand_sales``), under its own lock.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import connection, transaction
from django.utils import timezone

from core.commands import database_now
from core.documents import DocStatus
from offers.resolution import normalise
from reporting.models import BrandSaleLineFact, ReportRefresh
from reporting.sales_facts import (
    business_day,
    changed_returns,
    changed_sales,
    chunks,
    overlap,
)
from sell.models import Return, ReturnLine, Sale, SaleLine

logger = logging.getLogger(__name__)

#: The ``ReportRefresh`` row this copy keeps its freshness in.
KEY = "brand_sales"
#: Its own advisory lock, so it never waits on another copy or runs twice.
LOCK_ID = 7_310_029

SOURCE_SALE = BrandSaleLineFact.Source.SALE.value
SOURCE_OLD_RETURN = BrandSaleLineFact.Source.OLD_RETURN.value

DIMS = ("brand", "item", "design", "size", "color", "barcode", "season")


@dataclass(frozen=True)
class RefreshResult:
    #: False when another run held the lock, so this one did nothing.
    ran: bool
    as_of: datetime | None = None
    documents: int = 0
    took_ms: int = 0
    full: bool = False


def scheduled_refresh(_tenant_id: uuid.UUID) -> None:
    """The worker's call. A run already in progress elsewhere is simply skipped."""
    refresh()


def refresh(*, full: bool = False) -> RefreshResult:
    """Bring the copy up to date; ``full`` rebuilds it from nothing."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        row = cursor.fetchone()
    if not row or not row[0]:
        return RefreshResult(ran=False)
    try:
        return _refresh(full)
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
            logger.exception("brand sales refresh: advisory unlock failed")


def _refresh(full: bool) -> RefreshResult:
    clock = time.monotonic()
    started = database_now()
    state, _ = ReportRefresh.objects.get_or_create(key=KEY)
    since = None if full or state.as_of is None else state.as_of - overlap()
    sale_ids = changed_sales(since)
    return_ids = changed_returns(since)
    with transaction.atomic():
        if since is None:
            BrandSaleLineFact.objects.all().delete()
        for chunk in chunks(sale_ids):
            if since is not None:
                BrandSaleLineFact.objects.filter(source=SOURCE_SALE, doc_id__in=chunk).delete()
            BrandSaleLineFact.objects.bulk_create(_sale_facts(chunk), batch_size=5000)
        for chunk in chunks(return_ids):
            if since is not None:
                BrandSaleLineFact.objects.filter(
                    source=SOURCE_OLD_RETURN, doc_id__in=chunk
                ).delete()
            BrandSaleLineFact.objects.bulk_create(_old_return_facts(chunk), batch_size=5000)
        took = int((time.monotonic() - clock) * 1000)
        state.as_of = started
        state.took_ms = took
        state.documents = len(sale_ids) + len(return_ids)
        state.failed_at = None
        state.failure = ""
        state.save()
    return RefreshResult(
        ran=True, as_of=started, documents=state.documents, took_ms=took, full=since is None
    )


def _originals(ids: set[int]) -> dict[int, dict[str, Any]]:
    wanted = [i for i in ids if i]
    if not wanted:
        return {}
    rows = SaleLine.objects.filter(id__in=wanted).values("id", "qty", "mrp_paise")
    return {row["id"]: dict(row) for row in rows}


def _dims(line: dict[str, Any]) -> dict[str, Any]:
    out = {dim: str(line.get(dim) or "")[:120] for dim in DIMS}
    out["size"] = out["size"][:24]
    out["color"] = out["color"][:60]
    out["barcode"] = out["barcode"][:64]
    out["brand_key"] = normalise(out["brand"])[:120]
    return out


def _sale_facts(ids: list[int]) -> list[BrandSaleLineFact]:
    """The goods lines of these bills: accepted, numbered and not cancelled only."""
    sales = {
        row["id"]: row
        for row in Sale.objects.filter(id__in=ids, doc_number__isnull=False)
        .exclude(docstatus=DocStatus.CANCELLED)
        .values("id", "store_id", "billed_at", "doc_number")
    }
    if not sales:
        return []
    # Ticket 22: an alteration charge is a service, not a piece of any brand's stock.
    lines = list(
        SaleLine.objects.filter(sale_id__in=list(sales))
        .exclude(kind=SaleLine.Kind.ALTERATION)
        .values(
            "id",
            "sale_id",
            "line_no",
            "direction",
            *DIMS,
            "qty",
            "mrp_paise",
            "disc_paise",
            "net_paise",
            "gst_paise",
            "original_line_id",
        )
    )
    originals = _originals({line["original_line_id"] for line in lines})
    facts = []
    for line in lines:
        sale = sales[line["sale_id"]]
        qty = int(line["qty"])
        net = int(line["net_paise"])
        if line["direction"] == SaleLine.Direction.SALE:
            sign, unit = 1, int(line["mrp_paise"])
        else:
            # Given back at the MRP of the line it came off (the leg's own, if none).
            sign = -1
            original = originals.get(line["original_line_id"] or 0)
            unit = int(original["mrp_paise"]) if original else int(line["mrp_paise"])
        gross = unit * qty
        # Discount is the difference, so Price less Discount is always the Total.
        disc = gross - net
        facts.append(
            BrandSaleLineFact(
                store_id=sale["store_id"],
                day=business_day(sale["billed_at"]),
                source=SOURCE_SALE,
                doc_id=sale["id"],
                doc_number=sale["doc_number"] or "",
                line_id=line["id"],
                line_no=line["line_no"],
                **_dims(line),
                qty=sign * qty,
                mrp_paise=unit,
                gross_paise=sign * gross,
                disc_paise=sign * disc,
                value_paise=sign * net,
                gst_paise=sign * int(line["gst_paise"]),
            )
        )
    return facts


def _old_return_facts(ids: list[int]) -> list[BrandSaleLineFact]:
    """Old standalone returns (before #273): pieces given back, negative."""
    docs = {
        row["id"]: row
        for row in Return.objects.filter(id__in=ids, doc_number__isnull=False)
        .exclude(docstatus=DocStatus.CANCELLED)
        .values("id", "store_id", "returned_at", "doc_number")
    }
    if not docs:
        return []
    lines = list(
        ReturnLine.objects.filter(return_doc_id__in=list(docs)).values(
            "id",
            "return_doc_id",
            "line_no",
            "original_line_id",
            *DIMS,
            "qty",
            "refund_paise",
            "gst_paise",
        )
    )
    originals = _originals({line["original_line_id"] for line in lines})
    facts = []
    for line in lines:
        doc = docs[line["return_doc_id"]]
        qty = int(line["qty"])
        refund = int(line["refund_paise"])
        original = originals.get(line["original_line_id"])
        # With no original line its MRP is unknown: the price is the refund, no
        # discount is claimed, and the MRP stays empty (the report says so).
        unit = int(original["mrp_paise"]) if original else None
        gross = unit * qty if unit is not None else refund
        facts.append(
            BrandSaleLineFact(
                store_id=doc["store_id"],
                day=business_day(doc["returned_at"]),
                source=SOURCE_OLD_RETURN,
                doc_id=doc["id"],
                doc_number=doc["doc_number"] or "",
                line_id=line["id"],
                line_no=line["line_no"],
                **_dims(line),
                qty=-qty,
                mrp_paise=unit,
                gross_paise=-gross,
                disc_paise=-(gross - refund),
                value_paise=-refund,
                gst_paise=-int(line["gst_paise"]),
            )
        )
    return facts
