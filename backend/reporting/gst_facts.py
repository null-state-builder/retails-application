"""Copy what the GST report needs into the reporting store (store operations ticket 47).

The same rules as the sales copy (``reporting.sales_facts``): it runs on the
worker's clock (``scheduled_refresh``) and from ``manage.py refresh_gst_report``,
never inside a bill and never while a report page loads; it only *reads* the
billing tables, with plain reads that lock nothing; each run writes in one
transaction and stamps the as-of time the report states; and ``full=True``
rebuilds the copy from the documents at any time.

What it copies:

* **Lines** (``GstLineFact``) - every piece sold on an accepted bill, every piece
  given back on one (an exchange leg), and every piece of an old standalone
  return, with its HSN, rate, taxable value and tax split into IGST, CGST and
  SGST. A piece given back takes its B2B or B2C and its tax split from the bill
  it was bought on.
* **Documents** (``GstDocumentFact``) - each B2B invoice with its IRN status, and
  each credit note: ticket 13's exchange credit notes, pieces netted on a bill
  that issued none (before ticket 13's switch), and old standalone returns.
* **Numbers** (``GstNumberFact``) - every number of every tax document series,
  used or cancelled, for GSTR-1 Table 13: the new-format register
  (``IssuedDocumentNumber``, where month end records unused offline numbers as
  cancelled), and today's numbers read off the bills, credit notes and returns.
  A number whose document was cancelled counts as cancelled.
* **Open blocks** (``GstOpenBlockFact``) - offline number blocks month end has
  not yet closed, so the report can say Table 13 is not final.

**Incremental.** A run re-reads what changed since the last run began, less the
overlap: bills (and their IRN queue rows), old returns, store-credit notes and
register numbers. A changed bill's rows are replaced whole, and so are the
register rows naming it, so a cancelled bill's number turns cancelled.

The number register is walled per tenant (row-level security), so it and the
stores' registrations are read inside each tenant's context.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.db import connection, transaction
from django.utils import timezone

from core.commands import database_now
from core.documents import DocStatus
from core.tenancy import tenant_context
from masters.document_series import render
from masters.document_series_models import DocumentSeries, IssuedDocumentNumber
from masters.goods_models import Tenant
from masters.models import Store
from reporting.models import (
    GstDocumentFact,
    GstLineFact,
    GstNumberFact,
    GstOpenBlockFact,
    GstSplit,
    GstSupply,
    ReportRefresh,
    SalesLineFact,
)
from reporting.sales_facts import CHUNK, RefreshResult, business_day, overlap
from sell.models import (
    CreditNote,
    ExchangeCreditNote,
    IrnQueueItem,
    Return,
    ReturnLine,
    Sale,
    SaleLine,
    TillNumberBlock,
)
from sell.services.invoice_numbers import UNUSED_BLOCK_REASON

logger = logging.getLogger(__name__)

#: The ``ReportRefresh`` row this copy keeps its freshness in.
KEY = "gst"
#: Its own advisory lock, apart from the sales copy's.
LOCK_ID = 7_310_047

SOURCE_SALE = SalesLineFact.Source.SALE.value
SOURCE_OLD_RETURN = SalesLineFact.Source.OLD_RETURN.value
#: Document sources (``GstDocumentFact.source``).
DOC_SALE = "sale"
DOC_EXCHANGE_CN = "exchange_cn"
DOC_NETTED = "netted"
DOC_OLD_RETURN = "old_return"
SALE_DOC_SOURCES = (DOC_SALE, DOC_EXCHANGE_CN, DOC_NETTED)

#: Number origins (``GstNumberFact.origin``).
ORIGIN_REGISTER = "number"
ORIGIN_SALE = "sale"
ORIGIN_CN = "cn"
ORIGIN_STORE_CREDIT = "store_credit"
ORIGIN_RETURN = "return"

#: What each new-format series is, in GSTR-1 Table 13's words (``nature``).
NATURE_OF_SERIES = {
    DocumentSeries.TAX_INVOICE.value: "invoice",
    DocumentSeries.CREDIT_NOTE.value: "credit_note",
    DocumentSeries.RECEIPT_VOUCHER.value: "receipt_voucher",
    DocumentSeries.GIFT_VOUCHER.value: "gift_voucher",
    DocumentSeries.DELIVERY_CHALLAN.value: "delivery_challan",
    DocumentSeries.DEBIT_NOTE.value: "debit_note",
}
#: Register rows whose document is a bill: cancelled with it.
BILL_DOCUMENTS = ("sale", "exchange_credit_note")

#: Where a series' running number sits in its label (``DEA/26-27/#``).
PLACEHOLDER = "#"
_RUNNING = re.compile(r"^(.*?)(\d+)(\D*)$")


@dataclass(frozen=True)
class _Registration:
    tenant_id: Any
    gstin: str


@dataclass(frozen=True)
class _Bill:
    """What a piece given back needs to know about the bill it was bought on."""

    number: str
    supply: str
    split: str
    buyer_gstin: str


def scheduled_refresh(_tenant_id: uuid.UUID) -> None:
    """The worker's call. A run already in progress elsewhere is simply skipped."""
    refresh()


def refresh(*, full: bool = False) -> RefreshResult:
    """Bring the GST copy up to date; ``full`` rebuilds it from nothing."""
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
            logger.exception("gst report refresh: advisory unlock failed")


def _refresh(full: bool) -> RefreshResult:
    clock = time.monotonic()
    started = database_now()
    state, _ = ReportRefresh.objects.get_or_create(key=KEY)
    since = None if full or state.as_of is None else state.as_of - overlap()
    stores = _registrations()
    sale_ids = _changed(Sale, since, extra=_irn_changed(since))
    return_ids = _changed(Return, since)
    store_credit_ids = _changed(CreditNote, since)
    with transaction.atomic():
        if since is None:
            for model in (GstLineFact, GstDocumentFact, GstNumberFact):
                model.objects.all().delete()
        for chunk in _chunks(sale_ids):
            if since is not None:
                _forget_sales(chunk)
            _write(*_sale_facts(chunk, stores))
        for chunk in _chunks(return_ids):
            if since is not None:
                _forget_returns(chunk)
            _write(*_old_return_facts(chunk, stores))
        for chunk in _chunks(store_credit_ids):
            if since is not None:
                GstNumberFact.objects.filter(
                    origin=ORIGIN_STORE_CREDIT, origin_ref__in=[str(i) for i in chunk]
                ).delete()
            GstNumberFact.objects.bulk_create(_store_credit_numbers(chunk, stores), batch_size=5000)
        registered = _register_numbers(since, sale_ids, stores)
        _open_blocks(stores)
        _restamp(stores)
        took = int((time.monotonic() - clock) * 1000)
        state.as_of = started
        state.took_ms = took
        state.documents = len(sale_ids) + len(return_ids) + len(store_credit_ids) + registered
        state.failed_at = None
        state.failure = ""
        state.save()
    return RefreshResult(
        ran=True, as_of=started, documents=state.documents, took_ms=took, full=since is None
    )


def _chunks[T](ids: list[T]) -> Iterator[list[T]]:
    for start in range(0, len(ids), CHUNK):
        yield ids[start : start + CHUNK]


def _write(
    lines: list[GstLineFact], docs: list[GstDocumentFact], numbers: list[GstNumberFact]
) -> None:
    GstLineFact.objects.bulk_create(lines, batch_size=5000)
    GstDocumentFact.objects.bulk_create(docs, batch_size=5000)
    GstNumberFact.objects.bulk_create(numbers, batch_size=5000)


def _forget_sales(ids: list[int]) -> None:
    GstLineFact.objects.filter(source=SOURCE_SALE, doc_id__in=ids).delete()
    GstDocumentFact.objects.filter(source__in=SALE_DOC_SOURCES, doc_id__in=ids).delete()
    GstNumberFact.objects.filter(
        origin__in=(ORIGIN_SALE, ORIGIN_CN), origin_ref__in=[str(i) for i in ids]
    ).delete()


def _forget_returns(ids: list[int]) -> None:
    GstLineFact.objects.filter(source=SOURCE_OLD_RETURN, doc_id__in=ids).delete()
    GstDocumentFact.objects.filter(source=DOC_OLD_RETURN, doc_id__in=ids).delete()
    GstNumberFact.objects.filter(
        origin=ORIGIN_RETURN, origin_ref__in=[str(i) for i in ids]
    ).delete()


# -- what changed ----------------------------------------------------------------------


def _changed(model: Any, since: datetime | None, extra: Iterable[int] = ()) -> list[int]:
    numbered = model.objects.filter(doc_number__isnull=False)
    if since is None:
        return list(numbered.order_by("id").values_list("id", flat=True))
    ids = set(numbered.filter(updated_at__gte=since).values_list("id", flat=True))
    return sorted(ids | set(extra))


def _irn_changed(since: datetime | None) -> list[int]:
    """Bills whose IRN queue row moved (Accounts typed in an IRN): the bill did not."""
    if since is None:
        return []
    return list(
        IrnQueueItem.objects.filter(updated_at__gte=since).values_list("sale_id", flat=True)
    )


def _tenants() -> list[Any]:
    return list(Tenant.objects.order_by("id").values_list("id", flat=True))


def _registrations() -> dict[int, _Registration]:
    """Every store's GSTIN today, read inside its tenant's wall (baseline B73)."""
    out: dict[int, _Registration] = {}
    for tenant_id in _tenants():
        with tenant_context(tenant_id):
            for row in Store.objects.filter(tenant_id=tenant_id).values("id", "gstin__gstin"):
                out[row["id"]] = _Registration(
                    tenant_id, (row["gstin__gstin"] or "").strip().upper()
                )
    return out


def _restamp(stores: dict[int, _Registration]) -> None:
    """Every copied row under its store's GSTIN today, whatever it was copied under.

    A store given (or moved to) a registration after its rows were copied would
    otherwise keep the old one until a full rebuild. Head office's own numbers
    (no store) keep the GSTIN their prefix names.
    """
    for store_id, registration in stores.items():
        for model in (GstLineFact, GstDocumentFact, GstNumberFact, GstOpenBlockFact):
            model.objects.filter(store_id=store_id).exclude(gstin=registration.gstin).update(
                gstin=registration.gstin
            )


def _gstin(stores: dict[int, _Registration], store_id: int | None) -> str:
    found = stores.get(store_id or 0)
    return found.gstin if found else ""


# -- arithmetic ------------------------------------------------------------------------


def split_tax(tax: int, split: str) -> tuple[int, int, int]:
    """``(IGST, CGST, SGST)`` of ``tax`` paise: all IGST across states, else CGST the
    half rounded down and SGST the rest - the customer's copy's own rule
    (``till/gstin.ts`` ``splitTax``), so the report files what the paper shows."""
    if split == GstSplit.INTER:
        return tax, 0, 0
    cgst = tax // 2
    return 0, cgst, tax - cgst


def _settle(lines: list[GstLineFact]) -> None:
    """Make one document's lines add up to the split of its whole tax.

    The customer's copy splits the document's total tax once, so a document of
    two lines with odd tax has one paisa more CGST than its lines split one by
    one. The first odd lines take that paisa from SGST, so every line still adds
    up and the document matches the paper exactly.
    """
    intra = [line for line in lines if line.split == GstSplit.INTRA]
    short = sum(int(line.tax_paise) for line in intra) // 2 - sum(
        int(line.cgst_paise) for line in intra
    )
    for line in intra:
        if short <= 0:
            break
        if int(line.tax_paise) % 2:
            line.cgst_paise = int(line.cgst_paise) + 1
            line.sgst_paise = int(line.sgst_paise) - 1
            short -= 1


def _supply(buyer_gstin: str) -> str:
    return GstSupply.B2B if (buyer_gstin or "").strip() else GstSupply.B2C


def _split(b2b_tax_kind: str) -> str:
    """A B2C bill at the counter is always within the store's state."""
    return GstSplit.INTER if b2b_tax_kind == Sale.B2bTaxKind.IGST else GstSplit.INTRA


def series_of(number: str) -> tuple[str, int] | None:
    """``("DEA/26-27/#", 74)`` for ``DEA/26-27/74``: the series and its running number."""
    matched = _RUNNING.match(number or "")
    if not matched:
        return None
    return f"{matched.group(1)}{PLACEHOLDER}{matched.group(3)}", int(matched.group(2))


def _month(day: date) -> date:
    return day.replace(day=1)


def _status(docstatus: int) -> str:
    return (
        GstNumberFact.Status.CANCELLED
        if docstatus == DocStatus.CANCELLED
        else GstNumberFact.Status.ISSUED
    )


def _line(
    *,
    base: dict[str, Any],
    kind: str,
    bill: _Bill,
    hsn: str,
    rate: Decimal,
    pieces: int,
    value: int,
    tax: int,
    late: bool = False,
) -> GstLineFact:
    igst, cgst, sgst = split_tax(tax, bill.split)
    return GstLineFact(
        **base,
        kind=kind,
        supply=bill.supply,
        split=bill.split,
        hsn=(hsn or "").strip(),
        rate=rate,
        pieces=pieces,
        taxable_paise=value - tax,
        igst_paise=igst,
        cgst_paise=cgst,
        sgst_paise=sgst,
        tax_paise=tax,
        value_paise=value,
        late=late,
    )


def _document(
    *,
    base: dict[str, Any],
    kind: str,
    bill: _Bill,
    lines: list[GstLineFact],
    number: str,
    original_number: str = "",
    late: bool = False,
    irn: Mapping[str, Any] | None = None,
) -> GstDocumentFact:
    """One document, its money the sum of its lines (so the two views agree)."""
    irn_status = ""
    if kind == GstDocumentFact.Kind.INVOICE and bill.supply == GstSupply.B2B:
        irn_status = irn["status"] if irn else ""
    elif kind == GstDocumentFact.Kind.CREDIT_NOTE and bill.supply == GstSupply.B2B:
        irn_status = "untracked"
    return GstDocumentFact(
        **base,
        kind=kind,
        number=number or "",
        original_number=original_number,
        supply=bill.supply,
        split=bill.split,
        buyer_gstin=bill.buyer_gstin,
        pieces=sum(line.pieces for line in lines),
        taxable_paise=sum(int(line.taxable_paise) for line in lines),
        igst_paise=sum(int(line.igst_paise) for line in lines),
        cgst_paise=sum(int(line.cgst_paise) for line in lines),
        sgst_paise=sum(int(line.sgst_paise) for line in lines),
        tax_paise=sum(int(line.tax_paise) for line in lines),
        value_paise=sum(int(line.value_paise) for line in lines),
        late=late,
        irn_status=irn_status,
        irn=(irn or {}).get("irn") or "",
        irn_due_on=(irn or {}).get("due_on"),
    )


# -- bills -----------------------------------------------------------------------------

SALE_FIELDS = (
    "id",
    "store_id",
    "billed_at",
    "buyer_gstin",
    "b2b_tax_kind",
    "doc_number",
    "tax_invoice_number",
    "docstatus",
)
LINE_FIELDS = (
    "id",
    "sale_id",
    "direction",
    "hsn",
    "qty",
    "net_paise",
    "gst_rate",
    "gst_paise",
    "original_line_id",
)


def _bill_of(row: Mapping[str, Any]) -> _Bill:
    buyer = (row.get("buyer_gstin") or "").strip().upper()
    return _Bill(
        number=row.get("tax_invoice_number") or row.get("doc_number") or "",
        supply=_supply(buyer),
        split=_split(row.get("b2b_tax_kind") or ""),
        buyer_gstin=buyer,
    )


def _bills_of_lines(line_ids: Iterable[int]) -> dict[int, _Bill]:
    """The bill each of these lines was sold on, by line id."""
    wanted = [i for i in set(line_ids) if i]
    if not wanted:
        return {}
    rows = SaleLine.objects.filter(id__in=wanted).values(
        "id",
        "sale__buyer_gstin",
        "sale__b2b_tax_kind",
        "sale__doc_number",
        "sale__tax_invoice_number",
    )
    return {
        row["id"]: _bill_of(
            {
                "buyer_gstin": row["sale__buyer_gstin"],
                "b2b_tax_kind": row["sale__b2b_tax_kind"],
                "doc_number": row["sale__doc_number"],
                "tax_invoice_number": row["sale__tax_invoice_number"],
            }
        )
        for row in rows
    }


def _sale_facts(
    ids: list[int], stores: dict[int, _Registration]
) -> tuple[list[GstLineFact], list[GstDocumentFact], list[GstNumberFact]]:
    sales = {
        row["id"]: row
        for row in Sale.objects.filter(id__in=ids, doc_number__isnull=False).values(*SALE_FIELDS)
    }
    if not sales:
        return [], [], []
    notes = {
        row["sale_id"]: row
        for row in ExchangeCreditNote.objects.filter(sale_id__in=list(sales)).values(
            "sale_id", "number", "series", "issued_on", "late", "original_sale_id"
        )
    }
    numbers: list[GstNumberFact] = []
    for sale in sales.values():
        numbers += _sale_numbers(sale, notes.get(sale["id"]), stores)

    live = [sale_id for sale_id, sale in sales.items() if sale["docstatus"] != DocStatus.CANCELLED]
    lines = list(SaleLine.objects.filter(sale_id__in=live).values(*LINE_FIELDS))
    bought_on = _bills_of_lines(line["original_line_id"] for line in lines)
    irns = {
        row["sale_id"]: row
        for row in IrnQueueItem.objects.filter(sale_id__in=live).values(
            "sale_id", "status", "irn", "due_on"
        )
    }
    by_sale: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        by_sale[line["sale_id"]].append(line)

    facts: list[GstLineFact] = []
    docs: list[GstDocumentFact] = []
    for sale_id in live:
        sale = sales[sale_id]
        bill = _bill_of(sale)
        note = notes.get(sale_id)
        day = business_day(sale["billed_at"])
        base = {
            "store_id": sale["store_id"],
            "gstin": _gstin(stores, sale["store_id"]),
        }
        sold: list[GstLineFact] = []
        given_back: list[GstLineFact] = []
        originals: list[_Bill] = []
        for line in by_sale.get(sale_id, []):
            line_base = {**base, "day": day, "source": SOURCE_SALE, "doc_id": sale_id}
            line_base["line_id"] = line["id"]
            if line["direction"] == SaleLine.Direction.SALE:
                sold.append(
                    _line(
                        base=line_base,
                        kind=GstLineFact.Kind.INVOICE,
                        bill=bill,
                        hsn=line["hsn"],
                        rate=line["gst_rate"],
                        pieces=int(line["qty"]),
                        value=int(line["net_paise"]),
                        tax=int(line["gst_paise"]),
                    )
                )
                continue
            original = bought_on.get(line["original_line_id"] or 0) or bill
            originals.append(original)
            given_back.append(
                _line(
                    base=line_base,
                    kind=GstLineFact.Kind.CREDIT,
                    bill=original,
                    hsn=line["hsn"],
                    rate=line["gst_rate"],
                    pieces=int(line["qty"]),
                    value=int(line["net_paise"]),
                    tax=int(line["gst_paise"]),
                    late=bool(note and note["late"]),
                )
            )
        _settle(sold)
        _settle(given_back)
        facts += sold + given_back
        doc_base = {**base, "doc_id": sale_id}
        if bill.supply == GstSupply.B2B and sold:
            docs.append(
                _document(
                    base={**doc_base, "day": day, "source": DOC_SALE},
                    kind=GstDocumentFact.Kind.INVOICE,
                    bill=bill,
                    lines=sold,
                    number=bill.number,
                    irn=irns.get(sale_id),
                )
            )
        if given_back:
            docs.append(_credit_document(doc_base, day, note, given_back, originals, bill))
    return facts, docs, numbers


def _credit_document(
    base: dict[str, Any],
    day: date,
    note: Mapping[str, Any] | None,
    legs: list[GstLineFact],
    originals: list[_Bill],
    bill: _Bill,
) -> GstDocumentFact:
    """The credit note for the pieces a bill took back.

    Ticket 13's note where the bill issued one; otherwise the pieces were netted
    on the bill with no credit note, and the row says so by having no number.
    A credit note is B2B when the pieces were bought on a B2B bill.
    """
    b2b = [o for o in originals if o.supply == GstSupply.B2B]
    credited = b2b[0] if b2b else (originals[0] if originals else bill)
    numbers = sorted({o.number for o in originals if o.number})
    if note is not None:
        return _document(
            base={**base, "day": note["issued_on"], "source": DOC_EXCHANGE_CN},
            kind=GstDocumentFact.Kind.CREDIT_NOTE,
            bill=credited,
            lines=legs,
            number=note["number"] or "",
            original_number=", ".join(numbers),
            late=bool(note["late"]),
        )
    return _document(
        base={**base, "day": day, "source": DOC_NETTED},
        kind=GstDocumentFact.Kind.CREDIT_NOTE,
        bill=credited,
        lines=legs,
        number="",
        original_number=", ".join(numbers),
    )


def _sale_numbers(
    sale: Mapping[str, Any], note: Mapping[str, Any] | None, stores: dict[int, _Registration]
) -> list[GstNumberFact]:
    """Today's-format numbers a bill used. New-format ones are in the register."""
    out: list[GstNumberFact] = []
    status = _status(sale["docstatus"])
    base = {"store_id": sale["store_id"], "gstin": _gstin(stores, sale["store_id"])}
    if not sale["tax_invoice_number"]:
        parsed = series_of(sale["doc_number"])
        if parsed is not None:
            out.append(
                GstNumberFact(
                    **base,
                    nature="invoice",
                    series=parsed[0],
                    n=parsed[1],
                    number=sale["doc_number"],
                    status=status,
                    period=_month(business_day(sale["billed_at"])),
                    origin=ORIGIN_SALE,
                    origin_ref=str(sale["id"]),
                )
            )
    if note and note["number"] and note["series"] == ExchangeCreditNote.Series.TODAY:
        parsed = series_of(note["number"])
        if parsed is not None:
            out.append(
                GstNumberFact(
                    **base,
                    nature="credit_note",
                    series=parsed[0],
                    n=parsed[1],
                    number=note["number"],
                    status=status,
                    period=_month(note["issued_on"]),
                    origin=ORIGIN_CN,
                    origin_ref=str(sale["id"]),
                )
            )
    return out


# -- old standalone returns and store-credit notes -------------------------------------


def _old_return_facts(
    ids: list[int], stores: dict[int, _Registration]
) -> tuple[list[GstLineFact], list[GstDocumentFact], list[GstNumberFact]]:
    docs_by_id = {
        row["id"]: row
        for row in Return.objects.filter(id__in=ids, doc_number__isnull=False).values(
            "id", "store_id", "returned_at", "doc_number", "docstatus"
        )
    }
    numbers: list[GstNumberFact] = []
    for doc in docs_by_id.values():
        parsed = series_of(doc["doc_number"] or "")
        if parsed is None:
            continue
        numbers.append(
            GstNumberFact(
                store_id=doc["store_id"],
                gstin=_gstin(stores, doc["store_id"]),
                nature="credit_note",
                series=parsed[0],
                n=parsed[1],
                number=doc["doc_number"] or "",
                status=_status(doc["docstatus"]),
                period=_month(business_day(doc["returned_at"])),
                origin=ORIGIN_RETURN,
                origin_ref=str(doc["id"]),
            )
        )
    live = [i for i, doc in docs_by_id.items() if doc["docstatus"] != DocStatus.CANCELLED]
    lines: list[dict[str, Any]] = [
        dict(row)
        for row in ReturnLine.objects.filter(return_doc_id__in=live).values(
            "id",
            "return_doc_id",
            "original_line_id",
            "hsn",
            "qty",
            "refund_paise",
            "gst_rate",
            "gst_paise",
        )
    ]
    bought_on = _bills_of_lines(line["original_line_id"] for line in lines)
    by_doc: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        by_doc[line["return_doc_id"]].append(line)
    facts: list[GstLineFact] = []
    docs: list[GstDocumentFact] = []
    for doc_id in live:
        doc = docs_by_id[doc_id]
        day = business_day(doc["returned_at"])
        base = {"store_id": doc["store_id"], "gstin": _gstin(stores, doc["store_id"])}
        legs: list[GstLineFact] = []
        originals: list[_Bill] = []
        for line in by_doc.get(doc_id, []):
            original = bought_on.get(line["original_line_id"]) or _Bill(
                "", GstSupply.B2C, GstSplit.INTRA, ""
            )
            originals.append(original)
            legs.append(
                _line(
                    base={
                        **base,
                        "day": day,
                        "source": SOURCE_OLD_RETURN,
                        "doc_id": doc_id,
                        "line_id": line["id"],
                    },
                    kind=GstLineFact.Kind.CREDIT,
                    bill=original,
                    hsn=line["hsn"],
                    rate=line["gst_rate"],
                    pieces=int(line["qty"]),
                    value=int(line["refund_paise"]),
                    tax=int(line["gst_paise"]),
                )
            )
        _settle(legs)
        facts += legs
        if legs:
            b2b = [o for o in originals if o.supply == GstSupply.B2B]
            docs.append(
                _document(
                    base={**base, "day": day, "source": DOC_OLD_RETURN, "doc_id": doc_id},
                    kind=GstDocumentFact.Kind.CREDIT_NOTE,
                    bill=b2b[0] if b2b else originals[0],
                    lines=legs,
                    number=doc["doc_number"] or "",
                    original_number=", ".join(sorted({o.number for o in originals if o.number})),
                )
            )
    return facts, docs, numbers


def _store_credit_numbers(ids: list[int], stores: dict[int, _Registration]) -> list[GstNumberFact]:
    """Store-credit notes take numbers in the same credit-note series (``CRN``)."""
    out: list[GstNumberFact] = []
    rows = CreditNote.objects.filter(id__in=ids, doc_number__isnull=False).values(
        "id", "store_id", "created_at", "doc_number", "docstatus"
    )
    for row in rows:
        parsed = series_of(row["doc_number"] or "")
        if parsed is None:
            continue
        out.append(
            GstNumberFact(
                store_id=row["store_id"],
                gstin=_gstin(stores, row["store_id"]),
                nature="credit_note",
                series=parsed[0],
                n=parsed[1],
                number=row["doc_number"] or "",
                status=_status(row["docstatus"]),
                period=_month(business_day(row["created_at"])),
                origin=ORIGIN_STORE_CREDIT,
                origin_ref=str(row["id"]),
            )
        )
    return out


# -- the new-format register -------------------------------------------------------------


def _register_numbers(
    since: datetime | None, sale_ids: list[int], stores: dict[int, _Registration]
) -> int:
    """Copy the register rows that changed, or that name a bill that changed."""
    changed_bills = [str(i) for i in sale_ids]
    copied = 0
    for tenant_id in _tenants():
        with tenant_context(tenant_id):
            rows = IssuedDocumentNumber.objects.filter(tenant_id=tenant_id)
            if since is not None:
                wanted = set(rows.filter(updated_at__gte=since).values_list("id", flat=True))
                for chunk in _chunks(changed_bills):
                    wanted |= set(
                        rows.filter(
                            document_type__in=BILL_DOCUMENTS, document_ref__in=chunk
                        ).values_list("id", flat=True)
                    )
                ids = sorted(str(i) for i in wanted)
            else:
                ids = [str(i) for i in rows.order_by("id").values_list("id", flat=True)]
            for chunk in _chunks(ids):
                if since is not None:
                    GstNumberFact.objects.filter(
                        origin=ORIGIN_REGISTER, origin_ref__in=chunk
                    ).delete()
                facts = _register_facts(rows.filter(id__in=chunk), stores)
                GstNumberFact.objects.bulk_create(facts, batch_size=5000)
                copied += len(facts)
    return copied


def _register_facts(rows: Any, stores: dict[int, _Registration]) -> list[GstNumberFact]:
    found = list(
        rows.values(
            "id",
            "number",
            "series",
            "n",
            "status",
            "document_type",
            "document_ref",
            "period",
            "reason",
            "prefix__site_id",
            "prefix__gstin__gstin",
        )
    )
    bills = [
        int(row["document_ref"])
        for row in found
        if row["document_type"] in BILL_DOCUMENTS and str(row["document_ref"]).isdigit()
    ]
    cancelled = set(
        Sale.objects.filter(id__in=bills, docstatus=DocStatus.CANCELLED).values_list(
            "id", flat=True
        )
    )
    out = []
    for row in found:
        status = row["status"]
        ref = str(row["document_ref"])
        if (
            status == IssuedDocumentNumber.Status.ISSUED
            and row["document_type"] in BILL_DOCUMENTS
            and ref.isdigit()
            and int(ref) in cancelled
        ):
            status = GstNumberFact.Status.CANCELLED
        number = row["number"]
        n = int(row["n"])
        site_id = row["prefix__site_id"]
        out.append(
            GstNumberFact(
                store_id=site_id,
                gstin=_gstin(stores, site_id)
                if site_id
                else (row["prefix__gstin__gstin"] or "").strip().upper(),
                nature=NATURE_OF_SERIES.get(row["series"], row["series"]),
                series=f"{number[: len(number) - len(str(n))]}{PLACEHOLDER}",
                n=n,
                number=number,
                status=status,
                period=row["period"],
                unused_offline=(
                    row["status"] == IssuedDocumentNumber.Status.CANCELLED
                    and row["reason"] == UNUSED_BLOCK_REASON
                ),
                origin=ORIGIN_REGISTER,
                origin_ref=str(row["id"]),
            )
        )
    return out


def _open_blocks(stores: dict[int, _Registration]) -> None:
    """Replace the list of offline number blocks month end has not closed."""
    GstOpenBlockFact.objects.all().delete()
    rows = TillNumberBlock.objects.filter(closed_at__isnull=True).values(
        "till__store_id", "month", "prefix_code", "fy", "first_n", "last_n"
    )
    invoice = DocumentSeries.TAX_INVOICE.value
    GstOpenBlockFact.objects.bulk_create(
        [
            GstOpenBlockFact(
                store_id=row["till__store_id"],
                gstin=_gstin(stores, row["till__store_id"]),
                month=row["month"],
                first_number=render(invoice, row["prefix_code"], row["fy"], row["first_n"]),
                last_number=render(invoice, row["prefix_code"], row["fy"], row["last_n"]),
            )
            for row in rows
        ]
    )
