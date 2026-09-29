"""Copy sold bill lines for offer simulation (store operations ticket 30, ST-OFR-3).

Kept apart from billing exactly as the sales copy is (``reporting.sales_facts``):
it runs on the worker's clock (``scheduled_refresh``) and from ``manage.py
refresh_offer_simulation``, never inside a bill and never while a page waits. It
reads the billing tables with plain reads that take no lock and writes only
``report_offer_sim_line_fact``. A simulation reads that table alone.

Incremental in the same way: each run re-reads the bills changed since the last
run began, less the same overlap, and replaces their rows whole. Its freshness
is its own ``ReportRefresh`` row (key ``offer_sim``), under its own lock.

Ticket 31 (return on each offer) reads the same copy, so each line also keeps
the offer that won it and each offer's part of its discount (``offer_parts``).
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
from masters.models import Sku
from offers.resolution import normalise
from reporting.models import OfferSimLineFact, ReportRefresh
from reporting.sales_facts import business_day, changed_sales, chunks, overlap
from sell.models import Sale, SaleLine
from sell.services.discount_funding import parts_of

logger = logging.getLogger(__name__)

#: The ``ReportRefresh`` row this copy keeps its freshness in.
KEY = "offer_sim"
#: Its own advisory lock, so it never waits on the sales copy or runs twice.
LOCK_ID = 7_310_030

LINE_FIELDS = (
    "id",
    "sale_id",
    "line_no",
    "brand",
    "item",
    "design",
    "size",
    "color",
    "barcode",
    "season",
    "qty",
    "mrp_paise",
    "disc_paise",
    "net_paise",
    "gst_paise",
    "gst_rate",
    "unit_cost_paise",
    "costing_status",
    "offer_id",
    "offer_evidence",
)


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
            logger.exception("offer simulation refresh: advisory unlock failed")


def _refresh(full: bool) -> RefreshResult:
    clock = time.monotonic()
    started = database_now()
    state, _ = ReportRefresh.objects.get_or_create(key=KEY)
    since = None if full or state.as_of is None else state.as_of - overlap()
    sale_ids = changed_sales(since)
    with transaction.atomic():
        if since is None:
            OfferSimLineFact.objects.all().delete()
        for chunk in chunks(sale_ids):
            if since is not None:
                OfferSimLineFact.objects.filter(bill_id__in=chunk).delete()
            OfferSimLineFact.objects.bulk_create(_facts(chunk), batch_size=5000)
        took = int((time.monotonic() - clock) * 1000)
        state.as_of = started
        state.took_ms = took
        state.documents = len(sale_ids)
        state.failed_at = None
        state.failure = ""
        state.save()
    return RefreshResult(
        ran=True, as_of=started, documents=len(sale_ids), took_ms=took, full=since is None
    )


def _facts(ids: list[int]) -> list[OfferSimLineFact]:
    """The sold lines of these bills: accepted, numbered and not cancelled only."""
    sales = {
        row["id"]: row
        for row in Sale.objects.filter(id__in=ids, doc_number__isnull=False)
        .exclude(docstatus=DocStatus.CANCELLED)
        .values("id", "store_id", "billed_at")
    }
    if not sales:
        return []
    lines = list(
        # Ticket 22: no offer ever reaches an alteration charge, so none is simulated on one.
        SaleLine.objects.filter(sale_id__in=list(sales), direction=SaleLine.Direction.SALE)
        .exclude(kind=SaleLine.Kind.ALTERATION)
        .values(*LINE_FIELDS)
    )
    barcodes = {line["barcode"] for line in lines if line["barcode"]}
    never = set(
        Sku.objects.filter(barcode__in=barcodes, no_discount=True).values_list("barcode", flat=True)
    )
    facts = []
    for line in lines:
        sale = sales[line["sale_id"]]
        qty = int(line["qty"])
        parts, unread = offer_parts(
            int(line["disc_paise"]), line["offer_id"], line["offer_evidence"]
        )
        facts.append(
            OfferSimLineFact(
                store_id=sale["store_id"],
                day=business_day(sale["billed_at"]),
                bill_id=sale["id"],
                line_id=line["id"],
                line_no=line["line_no"],
                brand=line["brand"] or "",
                brand_key=normalise(line["brand"])[:120],
                item=line["item"] or "",
                design=line["design"] or "",
                size=line["size"] or "",
                color=line["color"] or "",
                barcode=line["barcode"] or "",
                season=line["season"] or "",
                qty=qty,
                mrp_paise=int(line["mrp_paise"]),
                disc_paise=int(line["disc_paise"]),
                net_paise=int(line["net_paise"]),
                gst_paise=int(line["gst_paise"]),
                gst_rate=line["gst_rate"],
                cost_paise=(
                    int(line["unit_cost_paise"]) * qty
                    if line["costing_status"] == SaleLine.CostingStatus.POSTED
                    else None
                ),
                no_discount=line["barcode"] in never,
                offer_id=line["offer_id"],
                offer_parts=parts,
                offer_parts_unread=unread,
            )
        )
    return facts


def offer_parts(
    disc_paise: int, offer_id: int | None, evidence: Any
) -> tuple[dict[str, int], bool]:
    """Each offer's part of a line's discount, ``{offer id: paise}``, read from the
    bill's own offer record exactly as the funding split reads it (ticket 25).

    A record that cannot be read against the discount gives the winning offer the
    whole discount, and says so (``True``): the bill says that offer won it.
    """
    parts = parts_of(disc_paise, offer_id, evidence)
    if parts is None:
        return ({str(offer_id): disc_paise} if offer_id else {}), True
    found: dict[str, int] = {}
    for part in parts:
        if part.from_offer and part.offer_id is not None:
            key = str(part.offer_id)
            found[key] = found.get(key, 0) + part.amount_paise
    return found, False
