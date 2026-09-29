"""Copy the gift tags the Gift Stock report needs into the reporting store (ticket 14).

The same rules as the other copies (``reporting.sales_facts``): it runs on the
worker's clock (``scheduled_refresh``) and from ``manage.py refresh_gift_report``,
never inside a bill and never while a report page loads; it only *reads* the
billing tables, with plain reads that lock nothing; and each run writes in one
transaction and stamps the as-of time the report states.

What it copies: every gift tag (``sell.GiftPiece``) on a bill that is not
cancelled, with the bill's number, the line's description and how many of its
pieces were given back since on an exchange that is not cancelled either.

**Always in full.** Gift pieces are few (a handful a day at most, and none at a
store whose switch is off), and a piece given back changes a row without
touching its own bill, so every run rebuilds the copy from the tags rather than
chasing what changed. ``full`` is accepted for the command's sake and changes
nothing.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal

from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from core.commands import database_now
from core.documents import DocStatus
from reporting.models import GiftItcFact, ReportRefresh
from reporting.sales_facts import CHUNK, RefreshResult
from sell.models import GiftPiece, SaleLine

logger = logging.getLogger(__name__)

#: The ``ReportRefresh`` row this copy keeps its freshness in.
KEY = "gift_itc"
#: Its own advisory lock, apart from the other copies'.
LOCK_ID = 7_310_014


def scheduled_refresh(_tenant_id: uuid.UUID) -> None:
    """The worker's call. A run already in progress elsewhere is simply skipped."""
    refresh()


def refresh(*, full: bool = True) -> RefreshResult:
    """Rebuild the gift copy from the tags (``full`` is always so; see above)."""
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
            logger.exception("gift report refresh: advisory unlock failed")


def _refresh() -> RefreshResult:
    clock = time.monotonic()
    started = database_now()
    state, _ = ReportRefresh.objects.get_or_create(key=KEY)
    tags = list(
        GiftPiece.objects.exclude(sale__docstatus=DocStatus.CANCELLED)
        .order_by("id")
        .values(
            "id",
            "store_id",
            "gstin",
            "day",
            "sale_id",
            "sale__doc_number",
            "line_id",
            "line__barcode",
            "line__brand",
            "line__item",
            "line__hsn",
            "qty",
            "source",
            "offer_name",
            "cost_paise",
            "itc_paise",
            "missing",
            "layers",
        )
    )
    returned = _returned(tag["line_id"] for tag in tags)
    facts = [
        GiftItcFact(
            store_id=tag["store_id"],
            gstin=tag["gstin"],
            day=tag["day"],
            tag_id=tag["id"],
            doc_id=tag["sale_id"],
            line_id=tag["line_id"],
            doc_number=tag["sale__doc_number"] or "",
            barcode=tag["line__barcode"],
            brand=tag["line__brand"],
            item=tag["line__item"],
            hsn=tag["line__hsn"],
            pieces=tag["qty"],
            source=tag["source"],
            offer_name=tag["offer_name"],
            input_tax_pct=_rates(tag["layers"]),
            cost_paise=tag["cost_paise"],
            itc_paise=tag["itc_paise"],
            missing=tag["missing"],
            returned=returned.get(tag["line_id"], 0),
        )
        for tag in tags
    ]
    with transaction.atomic():
        GiftItcFact.objects.all().delete()
        GiftItcFact.objects.bulk_create(facts, batch_size=CHUNK)
        took = int((time.monotonic() - clock) * 1000)
        state.as_of = started
        state.took_ms = took
        state.documents = len(facts)
        state.failed_at = None
        state.failure = ""
        state.save()
    return RefreshResult(ran=True, as_of=started, documents=len(facts), took_ms=took, full=True)


def _returned(line_ids: Iterable[int]) -> dict[int, int]:
    """Pieces given back against each line, on exchanges not cancelled."""
    ids = list(line_ids)
    out: dict[int, int] = defaultdict(int)
    for start in range(0, len(ids), CHUNK):
        rows = (
            SaleLine.objects.filter(
                direction=SaleLine.Direction.RETURN, original_line_id__in=ids[start : start + CHUNK]
            )
            .exclude(sale__docstatus=DocStatus.CANCELLED)
            .values("original_line_id")
            .annotate(qty=Sum("qty"))
        )
        for row in rows:
            out[int(row["original_line_id"])] += int(row["qty"] or 0)
    return dict(out)


def _rates(layers: list[dict[str, object]] | None) -> str:
    """The layers' input tax rates, in order, each once ("5", "5, 12")."""
    seen: list[str] = []
    for layer in layers or []:
        rate = layer.get("input_tax_pct")
        if rate is None:
            continue
        written = f"{Decimal(str(rate)):f}"
        if "." in written:
            written = written.rstrip("0").rstrip(".")
        if written not in seen:
            seen.append(written)
    return ", ".join(seen)
