"""Copy each bill's discount funding split for the report (store operations ticket 25).

Kept apart from billing exactly as the sales copy is (``reporting.sales_facts``):
it runs on the worker's clock (``scheduled_refresh``) and from ``manage.py
refresh_discount_funding``, never inside a bill and never while a page waits. It
reads the billing tables with plain reads that take no lock and writes only
``report_discount_funding_fact``. The report reads that table alone.

Incremental in the same way: each run re-reads the bills changed since the last
run began, less the same overlap, and replaces their rows whole, so a cancelled
bill drops out. Its freshness is its own ``ReportRefresh`` row (key
``discount_funding``), under its own lock.
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
from reporting.models import DiscountFundingFact, ReportRefresh
from reporting.sales_facts import business_day, changed_sales, chunks, overlap
from sell.models import Sale, SaleLine, SaleLineFunding

logger = logging.getLogger(__name__)

#: The ``ReportRefresh`` row this copy keeps its freshness in.
KEY = "discount_funding"
#: Its own advisory lock, so it never waits on another copy or runs twice.
LOCK_ID = 7_310_050


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
            logger.exception("discount funding refresh: advisory unlock failed")


def _refresh(full: bool) -> RefreshResult:
    clock = time.monotonic()
    started = database_now()
    state, _ = ReportRefresh.objects.get_or_create(key=KEY)
    since = None if full or state.as_of is None else state.as_of - overlap()
    sale_ids = changed_sales(since)
    with transaction.atomic():
        if since is None:
            DiscountFundingFact.objects.all().delete()
        for chunk in chunks(sale_ids):
            if since is not None:
                DiscountFundingFact.objects.filter(bill_id__in=chunk).delete()
            DiscountFundingFact.objects.bulk_create(_facts(chunk), batch_size=5000)
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


def _facts(ids: list[int]) -> list[DiscountFundingFact]:
    """The rows for these bills: accepted, numbered and not cancelled only."""
    sales = {
        row["id"]: row
        for row in Sale.objects.filter(id__in=ids, doc_number__isnull=False)
        .exclude(docstatus=DocStatus.CANCELLED)
        .values("id", "store_id", "billed_at")
    }
    if not sales:
        return []
    rows = SaleLineFunding.objects.filter(line__sale_id__in=list(sales)).values(
        "line_id",
        "line__sale_id",
        "line__direction",
        "line__brand",
        "brand_ref_id",
        "part",
        "offer_id",
        "offer_name",
        "funder",
        "discount_paise",
        "brand_paise",
        "kdps_paise",
        "unknown_reason",
    )
    facts: list[DiscountFundingFact] = []
    for row in rows:
        sale = sales[row["line__sale_id"]]
        sign = 1 if row["line__direction"] == SaleLine.Direction.SALE else -1
        facts.append(
            DiscountFundingFact(
                store_id=sale["store_id"],
                day=business_day(sale["billed_at"]),
                bill_id=sale["id"],
                line_id=row["line_id"],
                part=row["part"],
                sign=sign,
                brand=row["line__brand"] or "",
                brand_ref_id=row["brand_ref_id"],
                offer_id=row["offer_id"],
                offer_name=row["offer_name"] or "",
                funder=row["funder"] or "",
                discount_paise=sign * int(row["discount_paise"]),
                brand_paise=_signed(row["brand_paise"], sign),
                kdps_paise=_signed(row["kdps_paise"], sign),
                unknown_reason=row["unknown_reason"] or "",
            )
        )
    return facts
