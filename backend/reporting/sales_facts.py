"""Copy accepted bills into the reporting store (R-AN-003, store operations PRD §17).

**Kept apart from billing.** This runs on the worker's clock
(``scheduled_refresh``, registered in ``reporting.apps``) and from
``manage.py refresh_sales_report``; never inside a bill and never while a report
page loads. It only *reads* the billing tables, with plain reads that take no
row lock, so a bill being accepted never waits on it; it writes only the
reporting tables. Reports read those tables alone.

**Incremental.** Each run re-reads the bills (and old standalone returns) that
changed since the last run began, less an overlap (``KDPS_REPORT_REFRESH_OVERLAP_
MINUTES``) that covers a bill whose transaction started before that run and
committed after it. A changed bill's rows are replaced whole, so re-reading one
twice is harmless. Cancelling a bill touches its ``updated_at``, so a cancelled
bill drops out on the next run; a line priced later by the costing sweep is
found through its ``DeferredCosting`` row.

**As-of.** ``ReportRefresh.as_of`` is when the last successful run started:
every bill the server had accepted by then is in the copy. Each run writes in
one transaction, so a report reads the whole of one run or the whole of the
previous one, never half of each.

The copy is derived, and ``refresh(full=True)`` rebuilds it from the bills at
any time (overall PRD §8.1): it is never a source a report may write back from.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from core.commands import database_now
from core.documents import DocStatus
from reporting.models import ReportRefresh, SalesLineFact, SalesTenderFact
from sell.models import (
    DeferredCosting,
    Return,
    ReturnLine,
    Sale,
    SaleLine,
    SaleLineShare,
    SaleTender,
)
from sell.services.split_shares import split_value

logger = logging.getLogger(__name__)

#: The ``ReportRefresh`` row this copy keeps its freshness in.
KEY = "sales"
#: A Postgres advisory lock, so the worker and the command never run two at once.
LOCK_ID = 7_310_010
#: Documents re-read per batch.
CHUNK = 2000

SOURCE_SALE = SalesLineFact.Source.SALE.value
SOURCE_OLD_RETURN = SalesLineFact.Source.OLD_RETURN.value

LINE_FIELDS = (
    "id",
    "sale_id",
    "direction",
    "brand",
    "brand_ref_id",
    "item",
    "season",
    "qty",
    "mrp_paise",
    "disc_paise",
    "net_paise",
    "gst_paise",
    "unit_cost_paise",
    "costing_status",
    "salesperson_id",
    "salesperson_match_id",
    "salesperson_code",
    "salesperson_name",
    "original_line_id",
)
ORIGINAL_FIELDS = (
    "id",
    "qty",
    "mrp_paise",
    "disc_paise",
    "salesperson_id",
    "salesperson_match_id",
    "salesperson_code",
    "salesperson_name",
)
SHARE_FIELDS = (
    "line_id",
    "position",
    "salesperson_id",
    "salesperson_code",
    "salesperson_name",
    "percent",
    "value_paise",
)


@dataclass(frozen=True)
class RefreshResult:
    #: False when another run held the lock, so this one did nothing.
    ran: bool
    as_of: datetime | None = None
    documents: int = 0
    took_ms: int = 0
    full: bool = False


@dataclass(frozen=True)
class _Seller:
    key: str
    name: str
    code: str


@dataclass(frozen=True)
class _Share:
    position: int
    seller: _Seller
    percent: int
    #: The share's own recorded value, or None to apportion the line's by percent.
    value_paise: int | None


NOBODY = _Seller("", "", "")


def overlap() -> timedelta:
    return timedelta(minutes=int(settings.KDPS_REPORT_REFRESH_OVERLAP_MINUTES))


def scheduled_refresh(_tenant_id: uuid.UUID) -> None:
    """The worker's call. A run already in progress elsewhere is simply skipped."""
    refresh()


def refresh(*, full: bool = False) -> RefreshResult:
    """Bring the reporting copy up to date; ``full`` rebuilds it from nothing."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        row = cursor.fetchone()
    if not row or not row[0]:
        return RefreshResult(ran=False)
    try:
        return _refresh(full)
    except Exception as exc:
        # Recorded outside the failed run's transaction, so a report can say its
        # copy is older than it should be and why.
        ReportRefresh.objects.update_or_create(
            key=KEY,
            defaults={"failed_at": timezone.now(), "failure": f"{type(exc).__name__}: {exc}"[:500]},
        )
        raise
    finally:
        # Never let the unlock replace the error a failed run is owed; a lock on a
        # broken connection goes with the connection.
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
        except Exception:  # noqa: BLE001 - logged, the original error stands
            logger.exception("sales report refresh: advisory unlock failed")


def _refresh(full: bool) -> RefreshResult:
    clock = time.monotonic()
    started = database_now()
    state, _ = ReportRefresh.objects.get_or_create(key=KEY)
    since = None if full or state.as_of is None else state.as_of - overlap()
    sale_ids = changed_sales(since)
    return_ids = changed_returns(since)
    with transaction.atomic():
        if since is None:
            SalesLineFact.objects.all().delete()
            SalesTenderFact.objects.all().delete()
        for chunk in chunks(sale_ids):
            if since is not None:
                _forget(SOURCE_SALE, chunk)
            lines, tenders = _sale_facts(chunk)
            SalesLineFact.objects.bulk_create(lines, batch_size=5000)
            SalesTenderFact.objects.bulk_create(tenders, batch_size=5000)
        for chunk in chunks(return_ids):
            if since is not None:
                _forget(SOURCE_OLD_RETURN, chunk)
            lines, tenders = _old_return_facts(chunk)
            SalesLineFact.objects.bulk_create(lines, batch_size=5000)
            SalesTenderFact.objects.bulk_create(tenders, batch_size=5000)
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


def chunks(ids: list[int]) -> Iterator[list[int]]:
    for start in range(0, len(ids), CHUNK):
        yield ids[start : start + CHUNK]


def _forget(source: str, doc_ids: list[int]) -> None:
    SalesLineFact.objects.filter(source=source, doc_id__in=doc_ids).delete()
    SalesTenderFact.objects.filter(source=source, doc_id__in=doc_ids).delete()


def changed_sales(since: datetime | None) -> list[int]:
    numbered = Sale.objects.filter(doc_number__isnull=False)
    if since is None:
        return list(numbered.order_by("id").values_list("id", flat=True))
    ids = set(numbered.filter(updated_at__gte=since).values_list("id", flat=True))
    # A line the costing sweep priced after the bill: its cost changed, not the bill.
    ids |= set(
        DeferredCosting.objects.filter(updated_at__gte=since).values_list(
            "sale_line__sale_id", flat=True
        )
    )
    return sorted(ids)


def changed_returns(since: datetime | None) -> list[int]:
    numbered = Return.objects.filter(doc_number__isnull=False)
    if since is not None:
        numbered = numbered.filter(updated_at__gte=since)
    return list(numbered.order_by("id").values_list("id", flat=True))


def business_day(moment: datetime) -> Any:
    """The business day in India time (``TIME_ZONE``), whatever the column's zone."""
    return timezone.localtime(moment).date()


def _seller(row: dict[str, Any]) -> _Seller:
    if row.get("salesperson_id"):
        key = f"staff:{row['salesperson_id']}"
    elif row.get("salesperson_match_id"):
        key = f"old:{row['salesperson_match_id']}"
    else:
        return NOBODY
    return _Seller(key, row.get("salesperson_name") or "", row.get("salesperson_code") or "")


def _shares_of(line_ids: Iterable[int]) -> dict[int, list[_Share]]:
    out: dict[int, list[_Share]] = defaultdict(list)
    rows = SaleLineShare.objects.filter(line_id__in=list(line_ids)).order_by("line_id", "position")
    for row in rows.values(*SHARE_FIELDS):
        out[row["line_id"]].append(
            _Share(
                position=row["position"],
                seller=_Seller(
                    f"staff:{row['salesperson_id']}",
                    row["salesperson_name"] or "",
                    row["salesperson_code"] or "",
                ),
                percent=int(row["percent"]),
                value_paise=int(row["value_paise"]),
            )
        )
    return out


def split(amount: int, percents: list[int]) -> list[int]:
    """``amount`` by whole percentages, the spare paisa to the first (baseline B14).

    The counter's own rule (``split_shares.split_value``), worked on the
    magnitude so a negative amount splits exactly as its positive would. The
    parts always add back to ``amount``.
    """
    if len(percents) == 1:
        return [amount]
    sign = -1 if amount < 0 else 1
    return [sign * part for part in split_value(abs(amount), percents)]


def _people(
    line_shares: list[_Share], fallback: _Seller, inherited: list[_Share] | None = None
) -> list[_Share]:
    """Who a line's value belongs to: its own shares, else the shares of the line it
    gives back (by percent), else one person at 100%."""
    if line_shares:
        return line_shares
    if inherited:
        return [_Share(s.position, s.seller, s.percent, None) for s in inherited]
    return [_Share(0, fallback, 100, None)]


def _rows(
    *,
    base: dict[str, Any],
    people: list[_Share],
    sign: int,
    qty: int,
    gross: int,
    disc: int,
    value: int,
    gst: int,
    cost: int | None,
) -> list[SalesLineFact]:
    percents = [p.percent for p in people]
    recorded = [p.value_paise for p in people]
    values = (
        [sign * int(v) for v in recorded if v is not None]
        if len(people) > 1 and all(v is not None for v in recorded)
        else split(sign * value, percents)
    )
    grosses = split(sign * gross, percents)
    discs = split(sign * disc, percents)
    gsts = split(sign * gst, percents)
    costs: list[int | None] = (
        [None] * len(people) if cost is None else list(split(sign * cost, percents))
    )
    return [
        SalesLineFact(
            **base,
            share_position=person.position,
            share_percent=person.percent,
            salesperson_key=person.seller.key,
            salesperson_name=person.seller.name,
            salesperson_code=person.seller.code,
            pieces_x100=sign * qty * person.percent,
            gross_paise=grosses[index],
            disc_paise=discs[index],
            value_paise=values[index],
            gst_paise=gsts[index],
            cost_paise=costs[index],
        )
        for index, person in enumerate(people)
    ]


def _given_back_share(
    original: dict[str, Any] | None, qty: int, refund: int
) -> tuple[int, int] | None:
    """MRP value and discount of ``qty`` pieces given back, or None with no original.

    The MRP is the original line's, per piece. The discount is that MRP less the
    refund, so value = MRP less discount holds on every row, and once a line has
    all come back - in however many parts - its MRP, discount and value net to
    nought exactly: the refunds already add up to what was paid.
    """
    if not original or not original["qty"]:
        return None
    gross = int(original["mrp_paise"]) * qty
    return gross, gross - refund


def _originals(ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    wanted = [i for i in set(ids) if i]
    if not wanted:
        return {}
    return {
        row["id"]: row for row in SaleLine.objects.filter(id__in=wanted).values(*ORIGINAL_FIELDS)
    }


def _sale_facts(ids: list[int]) -> tuple[list[SalesLineFact], list[SalesTenderFact]]:
    """The rows for these bills: accepted, numbered and not cancelled only."""
    sales = {
        row["id"]: row
        for row in Sale.objects.filter(id__in=ids, doc_number__isnull=False)
        .exclude(docstatus=DocStatus.CANCELLED)
        .values("id", "store_id", "billed_at")
    }
    if not sales:
        return [], []
    lines = list(SaleLine.objects.filter(sale_id__in=list(sales)).values(*LINE_FIELDS))
    originals = _originals(line["original_line_id"] for line in lines)
    shares = _shares_of([line["id"] for line in lines] + list(originals))
    facts: list[SalesLineFact] = []
    for line in lines:
        sale = sales[line["sale_id"]]
        sold = line["direction"] == SaleLine.Direction.SALE
        qty = int(line["qty"])
        original = originals.get(line["original_line_id"] or 0)
        if sold:
            gross, disc = int(line["mrp_paise"]) * qty, int(line["disc_paise"])
            people = _people(shares.get(line["id"], []), _seller(line))
        else:
            # A piece given back is credited against whoever sold it (Rule 10),
            # at what it was sold for: the line it came off says both.
            gross, disc = _given_back_share(original, qty, int(line["net_paise"])) or (
                int(line["mrp_paise"]) * qty,
                int(line["disc_paise"]),
            )
            seller = _seller(original) if original else _seller(line)
            people = _people(
                shares.get(line["id"], []), seller, shares.get(original["id"]) if original else None
            )
        cost = (
            int(line["unit_cost_paise"]) * qty
            if line["costing_status"] == SaleLine.CostingStatus.POSTED
            else None
        )
        facts += _rows(
            base={
                "store_id": sale["store_id"],
                "day": business_day(sale["billed_at"]),
                "source": SOURCE_SALE,
                "doc_id": sale["id"],
                "line_id": line["id"],
                "bill_id": sale["id"] if sold else None,
                "brand": line["brand"] or "",
                "brand_ref_id": line["brand_ref_id"],
                "category": line["item"] or "",
                "season": line["season"] or "",
            },
            people=people,
            sign=1 if sold else -1,
            qty=qty,
            gross=gross,
            disc=disc,
            value=int(line["net_paise"]),
            gst=int(line["gst_paise"]),
            cost=cost,
        )
    tenders = [
        SalesTenderFact(
            store_id=sales[row["sale_id"]]["store_id"],
            day=business_day(sales[row["sale_id"]]["billed_at"]),
            source=SOURCE_SALE,
            doc_id=row["sale_id"],
            bill_id=row["sale_id"],
            mode=row["mode"],
            amount_paise=int(row["amount_paise"]),
        )
        for row in SaleTender.objects.filter(sale_id__in=list(sales)).values(
            "sale_id", "mode", "amount_paise"
        )
    ]
    return facts, tenders


def _old_return_facts(ids: list[int]) -> tuple[list[SalesLineFact], list[SalesTenderFact]]:
    """Old standalone returns (before #273): pieces and money given back."""
    docs = {
        row["id"]: row
        for row in Return.objects.filter(id__in=ids, doc_number__isnull=False)
        .exclude(docstatus=DocStatus.CANCELLED)
        .values("id", "store_id", "returned_at")
    }
    if not docs:
        return [], []
    lines = list(
        ReturnLine.objects.filter(return_doc_id__in=list(docs)).values(
            "id",
            "return_doc_id",
            "original_line_id",
            "brand",
    "brand_ref_id",
            "item",
            "season",
            "qty",
            "refund_paise",
            "gst_paise",
            "unit_cost_paise",
        )
    )
    originals = _originals(line["original_line_id"] for line in lines)
    shares = _shares_of(list(originals))
    facts: list[SalesLineFact] = []
    given_back: dict[int, int] = defaultdict(int)
    for line in lines:
        doc = docs[line["return_doc_id"]]
        qty = int(line["qty"])
        original = originals.get(line["original_line_id"])
        gross, disc = _given_back_share(original, qty, int(line["refund_paise"])) or (0, 0)
        refund = int(line["refund_paise"])
        given_back[doc["id"]] += refund
        unit_cost = int(line["unit_cost_paise"])
        facts += _rows(
            base={
                "store_id": doc["store_id"],
                "day": business_day(doc["returned_at"]),
                "source": SOURCE_OLD_RETURN,
                "doc_id": doc["id"],
                "line_id": line["id"],
                "bill_id": None,
                "brand": line["brand"] or "",
                "brand_ref_id": line["brand_ref_id"],
                "category": line["item"] or "",
                "season": line["season"] or "",
            },
            people=_people(
                [],
                _seller(original) if original else NOBODY,
                shares.get(original["id"]) if original else None,
            ),
            sign=-1,
            qty=qty,
            gross=gross,
            disc=disc,
            value=refund,
            gst=int(line["gst_paise"]),
            # "Zero only when it was never priced" (ReturnLine): unknown, not nought.
            cost=unit_cost * qty if unit_cost else None,
        )
    tenders = [
        SalesTenderFact(
            store_id=docs[doc_id]["store_id"],
            day=business_day(docs[doc_id]["returned_at"]),
            source=SOURCE_OLD_RETURN,
            doc_id=doc_id,
            bill_id=None,
            mode=SalesTenderFact.GIVEN_BACK,
            amount_paise=-amount,
        )
        for doc_id, amount in given_back.items()
        if amount
    ]
    return facts, tenders
