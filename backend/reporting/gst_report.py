"""Reports, GST (store operations PRD ST-RPT-5; overall PRD R-FIN-016).

The outward-supply figures Accounts needs to file GST, read from the reporting
copy (``reporting.gst_facts``) alone. **Prepared for filing, not a filing:**
nothing here is sent to the GST portal, and every answer and every spreadsheet
says so.

Five views, one at a time:

* ``rate`` - outward supplies by GSTIN, B2B or B2C, and rate.
* ``hsn`` - the same by HSN and rate (the shape of GSTR-1 Table 12).
* ``b2b`` - each B2B invoice with the buyer's GSTIN and its IRN status.
* ``credit_notes`` - each credit note, B2B or B2C, with its IRN status.
* ``documents`` - documents issued and cancelled in each series (GSTR-1
  Table 13), unused offline numbers included.

Formulas (version ``FORMULA_VERSION``):

* **Taxable value** = what the customer paid for a piece, GST included, less
  its GST. **Net** = invoices less credit notes.
* **IGST / CGST / SGST**: a B2B bill to another state is IGST; everything else
  (every B2C bill at the counter) is CGST and SGST, half each, the odd paisa on
  CGST. A credit note takes the split of the bill the piece was bought on.
* A piece given back after the credit-note deadline reduced no tax (ticket 13):
  it is listed under credit notes and left out of the rate and HSN summaries.
* **Table 13**: per GSTIN, nature and series, over the whole months the period
  touches: first and last number, total = last - first + 1, cancelled, and net
  issued = total - cancelled. Numbers between first and last with no document
  and no cancellation are counted apart and said.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import Count, Max, Min, Q, QuerySet, Sum
from django.utils import timezone

from masters.store_feature_registry import GST_REPORTS
from reporting.base import Column, Missing, ReportScope, envelope, freshness, note_scope
from reporting.gst_facts import KEY as FRESHNESS_KEY
from reporting.gst_facts import PLACEHOLDER
from reporting.models import (
    GstDocumentFact,
    GstLineFact,
    GstNumberFact,
    GstOpenBlockFact,
    GstSupply,
)
from reporting.sales_report import period_months

REPORT = "gst"
TITLE = "GST report: prepared for filing, not a filing"
SHEET = "GST report"
FORMULA_VERSION = "gst-1"
FEATURE_KEY = GST_REPORTS

NOT_A_FILING = (
    "Prepared for Accounts to file GST. This is not a filing: nothing here is sent to the "
    "GST portal, and the CA checks it before anything is filed."
)

VIEWS: dict[str, str] = {
    "rate": "By rate",
    "hsn": "By HSN (Table 12)",
    "b2b": "B2B invoices and IRN",
    "credit_notes": "Credit notes",
    "documents": "Documents issued (Table 13)",
}

SUPPLY_LABELS = {GstSupply.B2B.value: "B2B", GstSupply.B2C.value: "B2C"}
SPLIT_LABELS = {"intra": "CGST + SGST", "inter": "IGST"}
IRN_LABELS = {
    "pending": "Pending",
    "generated": "Generated",
    "failed": "Failed",
    "untracked": "Not tracked yet",
    "": "Not queued",
}
NATURES: dict[str, str] = {
    "invoice": "Invoices for outward supply",
    "credit_note": "Credit notes",
    "debit_note": "Debit notes",
    "receipt_voucher": "Receipt vouchers",
    "gift_voucher": "Gift vouchers",
    "delivery_challan": "Delivery challans",
}

BASIS = [
    NOT_A_FILING,
    "Documents the server has accepted and not cancelled, dated by the document's own day, "
    "India time. Each is under the GSTIN its store is registered with today (bills do not "
    "record one).",
    "Taxable value = what the customer paid, GST included, less its GST. Net = invoices "
    "less credit notes. A bank offer is a payment and does not lower taxable value.",
    "B2B = the bill carries the buyer's GSTIN. A B2B bill to another state is IGST; every "
    "other bill is CGST and SGST, half each, with an odd paisa on CGST. A credit note takes "
    "B2B or B2C and its split from the bill the piece was bought on.",
    "Credit notes: the credit note an exchange issues beside its new invoice, pieces netted "
    "on a bill that issued no credit note, and old standalone returns. A piece given back "
    "after the credit-note deadline reduced no tax, so it is listed under credit notes and "
    "left out of the rate and HSN summaries.",
    "Table 13 counts whole months: per GSTIN, nature and series, the first and last number, "
    "total = last - first + 1, cancelled, and net issued = total - cancelled. A number whose "
    "document was cancelled counts as cancelled, and so does an unused number of an offline "
    "till's block, recorded at month end. Store-credit notes use the credit-note series.",
    "IRN days left count from today to the 30th day after the invoice.",
]

_MONEY_KEYS = (
    "taxable_paise",
    "igst_paise",
    "cgst_paise",
    "sgst_paise",
    "tax_paise",
    "value_paise",
)


def _line_facts(scope: ReportScope) -> QuerySet[GstLineFact]:
    return GstLineFact.objects.filter(
        store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
    )


def _doc_facts(scope: ReportScope) -> QuerySet[GstDocumentFact]:
    return GstDocumentFact.objects.filter(
        store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
    )


def _rate(value: Any) -> float:
    return float(Decimal(value).normalize()) if value is not None else 0.0


def _store_codes(scope: ReportScope) -> dict[int, str]:
    return {store.pk: store.code for store in scope.stores}


# -- rate and HSN ------------------------------------------------------------------------


def _summary(
    scope: ReportScope, by_hsn: bool, missing: Missing
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    facts = _line_facts(scope)
    late = facts.filter(late=True).aggregate(pieces=Sum("pieces"), value=Sum("value_paise"))
    if late["pieces"]:
        missing.add(
            "LATE_CREDIT",
            f"{int(late['pieces'])} piece(s) worth Rs {int(late['value'] or 0) / 100:,.2f} were "
            "given back after the credit-note deadline. They reduced no tax, so they are left "
            "out of this summary; see Credit notes.",
        )
    facts = facts.filter(late=False)
    keys = ["gstin", "supply", "rate", *(["hsn"] if by_hsn else [])]
    invoice = Q(kind=GstLineFact.Kind.INVOICE)
    credit = Q(kind=GstLineFact.Kind.CREDIT)
    sums: dict[str, Any] = {
        "inv_pieces": Sum("pieces", filter=invoice),
        "cr_pieces": Sum("pieces", filter=credit),
    }
    for field in _MONEY_KEYS:
        sums[f"inv_{field}"] = Sum(field, filter=invoice)
        sums[f"cr_{field}"] = Sum(field, filter=credit)
    rows = []
    for found in facts.values(*keys).annotate(**sums):
        row = _measures(found)
        rows.append(
            {
                "key": "|".join(str(found[k]) for k in keys),
                "gstin": found["gstin"] or "(no GSTIN)",
                "supply": SUPPLY_LABELS.get(found["supply"], found["supply"]),
                **({"hsn": found["hsn"] or "(no HSN)"} if by_hsn else {}),
                "rate": _rate(found["rate"]),
                **row,
            }
        )
    rows.sort(
        key=lambda r: (r["gstin"], r["supply"], r["rate"], r.get("hsn", "")),
    )
    total = _measures(facts.aggregate(**sums))
    total.update({"key": "total", "gstin": "Total"})
    no_hsn = facts.filter(hsn="").aggregate(pieces=Sum("pieces"))["pieces"]
    if no_hsn:
        missing.add(
            "NO_HSN",
            f"{int(no_hsn)} piece(s) have no HSN on their bill line; they are shown as (no HSN).",
        )
    return rows, total


def _measures(sums: dict[str, Any]) -> dict[str, Any]:
    def got(key: str) -> int:
        return int(sums.get(key) or 0)

    out: dict[str, Any] = {
        "pieces": got("inv_pieces") - got("cr_pieces"),
        "invoice_taxable_paise": got("inv_taxable_paise"),
        "invoice_tax_paise": got("inv_tax_paise"),
        "credit_taxable_paise": got("cr_taxable_paise"),
        "credit_tax_paise": got("cr_tax_paise"),
    }
    for field in _MONEY_KEYS:
        out[field] = got(f"inv_{field}") - got(f"cr_{field}")
    return out


def _summary_columns(by_hsn: bool) -> list[Column]:
    return [
        Column("gstin", "GSTIN", "text"),
        Column("supply", "B2B / B2C", "text"),
        *([Column("hsn", "HSN", "text")] if by_hsn else []),
        Column("rate", "Rate %", "rate"),
        Column("pieces", "Pieces (net)"),
        Column("invoice_taxable_paise", "Invoices taxable (Rs)", "money"),
        Column("invoice_tax_paise", "Invoices tax (Rs)", "money"),
        Column("credit_taxable_paise", "Credit notes taxable (Rs)", "money"),
        Column("credit_tax_paise", "Credit notes tax (Rs)", "money"),
        Column("taxable_paise", "Net taxable value (Rs)", "money"),
        Column("igst_paise", "IGST (Rs)", "money"),
        Column("cgst_paise", "CGST (Rs)", "money"),
        Column("sgst_paise", "SGST (Rs)", "money"),
        Column("tax_paise", "Net tax (Rs)", "money"),
        Column("value_paise", "Net value (Rs)", "money"),
    ]


# -- B2B invoices and credit notes --------------------------------------------------------


def _doc_money(row: Mapping[str, Any]) -> dict[str, int]:
    return {field: int(row[field]) for field in _MONEY_KEYS}


def _doc_total(rows: list[dict[str, Any]], label_key: str) -> dict[str, Any]:
    total: dict[str, Any] = {"key": "total", label_key: "Total"}
    for field in (*_MONEY_KEYS, "pieces"):
        total[field] = sum(int(row.get(field) or 0) for row in rows)
    return total


def _b2b(
    scope: ReportScope, today: date, missing: Missing
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    codes = _store_codes(scope)
    found = (
        _doc_facts(scope)
        .filter(kind=GstDocumentFact.Kind.INVOICE, supply=GstSupply.B2B)
        .order_by("day", "number", "id")
        .values()
    )
    rows = []
    pending = overdue = failed = 0
    for doc in found:
        status = doc["irn_status"]
        due = doc["irn_due_on"]
        days_left = (due - today).days if due and status != "generated" else None
        if status in ("pending", "failed", ""):
            pending += 1
            if days_left is not None and days_left < 0:
                overdue += 1
        failed += status == "failed"
        rows.append(
            {
                "key": f"inv:{doc['doc_id']}",
                "number": doc["number"],
                "day": doc["day"],
                "store": codes.get(doc["store_id"], ""),
                "gstin": doc["gstin"],
                "buyer_gstin": doc["buyer_gstin"],
                "split": SPLIT_LABELS.get(doc["split"], doc["split"]),
                "pieces": int(doc["pieces"]),
                **_doc_money(doc),
                "irn_status": IRN_LABELS.get(status, status),
                "irn": doc["irn"],
                "irn_due_on": due,
                "irn_days_left": days_left,
            }
        )
    if pending:
        missing.add(
            "IRN_PENDING",
            f"{pending} B2B invoice(s) have no IRN yet"
            + (f", {overdue} of them past their 30 days" if overdue else "")
            + (f"; {failed} failed at the portal" if failed else "")
            + ". Money, IRN queue is where Accounts types them in.",
        )
    return rows, _doc_total(rows, "number")


def _b2b_columns() -> list[Column]:
    return [
        Column("number", "Invoice number", "text"),
        Column("day", "Date", "date"),
        Column("store", "Store", "text"),
        Column("gstin", "GSTIN", "text"),
        Column("buyer_gstin", "Buyer GSTIN", "text"),
        Column("split", "Tax", "text"),
        Column("pieces", "Pieces"),
        Column("taxable_paise", "Taxable value (Rs)", "money"),
        Column("igst_paise", "IGST (Rs)", "money"),
        Column("cgst_paise", "CGST (Rs)", "money"),
        Column("sgst_paise", "SGST (Rs)", "money"),
        Column("tax_paise", "Tax (Rs)", "money"),
        Column("value_paise", "Invoice value (Rs)", "money"),
        Column("irn_status", "IRN status", "text"),
        Column("irn", "IRN", "text"),
        Column("irn_due_on", "IRN due", "date"),
        Column("irn_days_left", "IRN days left"),
    ]


def _credit_notes(
    scope: ReportScope, missing: Missing
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    codes = _store_codes(scope)
    found = (
        _doc_facts(scope)
        .filter(kind=GstDocumentFact.Kind.CREDIT_NOTE)
        .order_by("day", "number", "id")
        .values()
    )
    rows = []
    netted = unnumbered = untracked = late = 0
    for doc in found:
        number = doc["number"]
        if doc["source"] == "netted":
            netted += 1
            number = "(none: netted on the bill)"
        elif not number:
            unnumbered += 1
            number = "(no number)"
        untracked += doc["irn_status"] == "untracked"
        late += bool(doc["late"])
        rows.append(
            {
                "key": f"{doc['source']}:{doc['doc_id']}",
                "number": number,
                "day": doc["day"],
                "store": codes.get(doc["store_id"], ""),
                "gstin": doc["gstin"],
                "original_number": doc["original_number"],
                "supply": SUPPLY_LABELS.get(doc["supply"], doc["supply"]),
                "buyer_gstin": doc["buyer_gstin"],
                "pieces": int(doc["pieces"]),
                **_doc_money(doc),
                "late": "Yes: no tax reduced" if doc["late"] else "No",
                "irn_status": IRN_LABELS.get(doc["irn_status"], "")
                if doc["supply"] == GstSupply.B2B
                else "",
            }
        )
    if netted:
        missing.add(
            "NETTED_RETURNS",
            f"{netted} bill(s) took pieces back with no credit note (netted on the bill, "
            "before the exchange and return tax switch). They are listed without a number.",
        )
    if unnumbered:
        missing.add(
            "CREDIT_NOTE_UNNUMBERED",
            f"{unnumbered} credit note(s) could not be given a number; the bill is flagged "
            "for head office.",
        )
    if untracked:
        missing.add(
            "CREDIT_NOTE_IRN",
            f"{untracked} credit note(s) are against B2B bills. Their IRN is not tracked "
            "here yet (the IRN queue holds invoices only).",
        )
    if late:
        missing.add(
            "LATE_CREDIT_NOTES",
            f"{late} credit note(s) were issued after the credit-note deadline and reduce no tax.",
        )
    return rows, _doc_total(rows, "number")


def _credit_columns() -> list[Column]:
    return [
        Column("number", "Credit note number", "text"),
        Column("day", "Date", "date"),
        Column("store", "Store", "text"),
        Column("gstin", "GSTIN", "text"),
        Column("original_number", "Original invoice", "text"),
        Column("supply", "B2B / B2C", "text"),
        Column("buyer_gstin", "Buyer GSTIN", "text"),
        Column("pieces", "Pieces"),
        Column("taxable_paise", "Taxable value (Rs)", "money"),
        Column("igst_paise", "IGST (Rs)", "money"),
        Column("cgst_paise", "CGST (Rs)", "money"),
        Column("sgst_paise", "SGST (Rs)", "money"),
        Column("tax_paise", "Tax (Rs)", "money"),
        Column("value_paise", "Value (Rs)", "money"),
        Column("late", "After the deadline", "text"),
        Column("irn_status", "IRN status", "text"),
    ]


# -- Table 13 ------------------------------------------------------------------------------


def _number(series: str, n: int) -> str:
    return series.replace(PLACEHOLDER, str(n))


def _documents(
    scope: ReportScope, today: date, missing: Missing
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    period = period_months(scope.date_from, scope.date_to, today)
    first_month, last_month = period.months[0], period.months[-1]
    if not period.whole:
        missing.add(
            "WHOLE_MONTHS",
            "GSTR-1 Table 13 is counted by whole months, so it covers "
            f"{first_month:%B %Y} to {last_month:%B %Y} in full, not only the days chosen.",
        )
    rows_in = GstNumberFact.objects.filter(period__gte=first_month, period__lte=last_month)
    wanted = Q(store_id__in=scope.store_ids)
    head_office = Q(store__isnull=True, gstin__in=_scope_gstins(scope))
    if not scope.picked:
        # Head office's own numbers (debit notes) belong to a GSTIN, not a store.
        wanted |= head_office
    elif rows_in.filter(head_office).exists():
        missing.add(
            "HEAD_OFFICE_LEFT_OUT",
            "Head office's own numbers for this GSTIN (debit notes) belong to no store, "
            "so they are left out when one store is picked. Choose all stores to see them.",
        )
    grouped = (
        rows_in.filter(wanted)
        .values("gstin", "nature", "series")
        .annotate(
            first=Min("n"),
            last=Max("n"),
            issued=Count("id", filter=Q(status=GstNumberFact.Status.ISSUED)),
            cancelled=Count("id", filter=Q(status=GstNumberFact.Status.CANCELLED)),
            unused=Count("id", filter=Q(unused_offline=True)),
        )
    )
    order = list(NATURES)
    rows = []
    for found in grouped:
        count = int(found["last"]) - int(found["first"]) + 1
        cancelled = int(found["cancelled"])
        unaccounted = count - int(found["issued"]) - cancelled
        series = found["series"]
        row = {
            "key": f"{found['gstin']}|{series}",
            "gstin": found["gstin"] or "(no GSTIN)",
            "nature": NATURES.get(found["nature"], found["nature"]),
            "series": series.replace(PLACEHOLDER, "n"),
            "first_number": _number(series, int(found["first"])),
            "last_number": _number(series, int(found["last"])),
            "total": count,
            "cancelled": cancelled,
            "net_issued": count - cancelled,
            "unused_offline": int(found["unused"]),
            "unaccounted": unaccounted,
            "_order": order.index(found["nature"]) if found["nature"] in order else len(order),
        }
        rows.append(row)
        if unaccounted > 0:
            missing.add(
                f"UNACCOUNTED_{series}",
                f"{row['series']}: {unaccounted} number(s) between {row['first_number']} and "
                f"{row['last_number']} have no document and no cancellation (a till still "
                "holding bills, or a gap). Net issued counts them; check before filing.",
            )
    rows.sort(key=lambda r: (r["gstin"], r.pop("_order"), r["series"]))
    total: dict[str, Any] = {"key": "total", "gstin": "Total"}
    for key in ("total", "cancelled", "net_issued", "unused_offline", "unaccounted"):
        total[key] = sum(int(row[key]) for row in rows)
    _open_blocks(scope, first_month, last_month, today, missing)
    return rows, total


def _scope_gstins(scope: ReportScope) -> list[str]:
    return sorted(
        {
            (store.gstin.gstin or "").strip().upper()
            for store in scope.stores
            if store.gstin_id and store.gstin is not None
        }
    )


def _open_blocks(
    scope: ReportScope, first: date, last: date, today: date, missing: Missing
) -> None:
    blocks = GstOpenBlockFact.objects.filter(
        store_id__in=scope.store_ids, month__gte=first, month__lte=last
    )
    by_month: dict[date, int] = defaultdict(int)
    for block in blocks.values("month"):
        by_month[block["month"]] += 1
    this_month = today.replace(day=1)
    for month, count in sorted(by_month.items()):
        if month < this_month:
            text = (
                f"{count} offline number block(s) for {month:%B %Y} have not been closed at "
                "month end yet, so their unused numbers are not yet counted as cancelled."
            )
        else:
            text = (
                f"{count} offline number block(s) for {month:%B %Y} are still in use; their "
                "unused numbers are counted as cancelled when the month ends."
            )
        missing.add(f"OPEN_BLOCKS_{month:%Y%m}", text)


def _document_columns() -> list[Column]:
    return [
        Column("gstin", "GSTIN", "text"),
        Column("nature", "Nature of document", "text"),
        Column("series", "Series", "text"),
        Column("first_number", "Sr. no. from", "text"),
        Column("last_number", "Sr. no. to", "text"),
        Column("total", "Total number"),
        Column("cancelled", "Cancelled"),
        Column("net_issued", "Net issued"),
        Column("unused_offline", "Of which unused offline numbers"),
        Column("unaccounted", "No document and not cancelled"),
    ]


def columns(view: str) -> list[Column]:
    if view in ("rate", "hsn"):
        return _summary_columns(view == "hsn")
    if view == "b2b":
        return _b2b_columns()
    if view == "credit_notes":
        return _credit_columns()
    return _document_columns()


def build(scope: ReportScope, view: str) -> dict[str, Any]:
    """The GST report for ``scope``, one view at a time."""
    missing = Missing()
    as_of = freshness(FRESHNESS_KEY, missing)
    note_scope(scope, "GST report", missing)
    today = timezone.localdate()
    if view in ("rate", "hsn"):
        rows, total = _summary(scope, view == "hsn", missing)
    elif view == "b2b":
        rows, total = _b2b(scope, today, missing)
    elif view == "credit_notes":
        rows, total = _credit_notes(scope, missing)
    else:
        rows, total = _documents(scope, today, missing)
    return envelope(
        report=REPORT,
        title=TITLE,
        formula_version=FORMULA_VERSION,
        scope=scope,
        as_of=as_of,
        basis=BASIS,
        missing=missing,
        shows_cost=False,
        extra={
            "not_a_filing": NOT_A_FILING,
            "view": view,
            "views": [{"key": key, "label": label} for key, label in VIEWS.items()],
            "columns": [{"key": c.key, "label": c.label, "kind": c.kind} for c in columns(view)],
            "rows": rows,
            "total": total,
        },
    )
