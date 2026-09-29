"""Copy each bill's margin share split for the statement (store operations ticket 27).

Kept apart from billing exactly as the sales copy is (``reporting.sales_facts``):
it runs on the worker's clock (``scheduled_refresh``) and from ``manage.py
refresh_margin_share``, never inside a bill and never while a page waits. It
reads the billing tables with plain reads that take no lock and writes only
``report_margin_share_fact``. The statement reads that table alone.

Incremental in the same way: each run re-reads the bills changed since the last
run began, less the same overlap, and replaces their rows whole, so a cancelled
bill drops out. Its freshness is its own ``ReportRefresh`` row (key
``margin_share``), under its own lock.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import connection, transaction
from django.utils import timezone

from core.commands import database_now
from core.documents import DocStatus
from reporting.models import MarginShareFact, ReportRefresh
from reporting.sales_facts import business_day, changed_sales, chunks, overlap
from sell.models import Sale, SaleLine, SaleLineMarginShare

logger = logging.getLogger(__name__)

#: The ``ReportRefresh`` row this copy keeps its freshness in.
KEY = "margin_share"
#: Its own advisory lock, so it never waits on another copy or runs twice.
LOCK_ID = 7_310_027


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
            logger.exception("margin share refresh: advisory unlock failed")


def _refresh(full: bool) -> RefreshResult:
    clock = time.monotonic()
    started = database_now()
    state, _ = ReportRefresh.objects.get_or_create(key=KEY)
    since = None if full or state.as_of is None else state.as_of - overlap()
    sale_ids = changed_sales(since)
    with transaction.atomic():
        if since is None:
            MarginShareFact.objects.all().delete()
        for chunk in chunks(sale_ids):
            if since is not None:
                MarginShareFact.objects.filter(bill_id__in=chunk).delete()
            MarginShareFact.objects.bulk_create(_facts(chunk), batch_size=5000)
        took = int((time.monotonic() - clock) * 1000)
        state.as_of = started
        state.took_ms = took
        state.documents = len(sale_ids)
        state.failed_at = None
        state.failure = ""
        state.save()
    return RefreshResult(
        ran=True, as_of=started, documents=state.documents, took_ms=took, full=since is None
    )


def _signed(value: int | None, sign: int) -> int | None:
    return None if value is None else sign * int(value)


def _facts(ids: list[int]) -> list[MarginShareFact]:
    """The rows for these bills: accepted, numbered and not cancelled only."""
    sales = {
        row["id"]: row
        for row in Sale.objects.filter(id__in=ids, doc_number__isnull=False)
        .exclude(docstatus=DocStatus.CANCELLED)
        .values("id", "store_id", "billed_at", "doc_number")
    }
    if not sales:
        return []
    rows = SaleLineMarginShare.objects.filter(line__sale_id__in=list(sales)).values(
        "line_id",
        "line__sale_id",
        "line__direction",
        "line__brand",
        "line__season",
        "line__barcode",
        "line__qty",
        "brand_ref_id",
        "model",
        "margin_percent",
        "value_paise",
        "kdps_paise",
        "brand_paise",
        "unknown_reason",
    )
    facts: list[MarginShareFact] = []
    for row in rows:
        sale = sales[row["line__sale_id"]]
        sign = 1 if row["line__direction"] == SaleLine.Direction.SALE else -1
        facts.append(
            MarginShareFact(
                store_id=sale["store_id"],
                day=business_day(sale["billed_at"]),
                bill_id=sale["id"],
                doc_number=sale["doc_number"] or "",
                line_id=row["line_id"],
                sign=sign,
                brand=row["line__brand"] or "",
                brand_ref_id=row["brand_ref_id"],
                season=row["line__season"] or "",
                barcode=row["line__barcode"] or "",
                qty=sign * int(row["line__qty"]),
                model=row["model"] or "",
                margin_percent=row["margin_percent"],
                value_paise=sign * int(row["value_paise"]),
                kdps_paise=_signed(row["kdps_paise"], sign),
                brand_paise=_signed(row["brand_paise"], sign),
                unknown_reason=row["unknown_reason"] or "",
            )
        )
    return facts
