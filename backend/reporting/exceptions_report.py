"""Reports, Exceptions (store operations PRD ST-RPT-6; overall PRD R-CTL-005).

The counter's exceptions per store and per staff member - cancelled bills,
manual discounts, manager PIN uses by manager, cash variances, bill-number
holes, returns without a bill (and bills over their cap) and late syncs - read
from the reporting copy alone (``reporting.exception_facts``).

Three views: **by store** (every store in scope, even with nothing to show),
**by staff member** (one row per person per store; missing bill numbers sit on
the store's own row, because a bill that never arrived names nobody) and **the
list** (each exception, newest first, with its bill or count).

Formulas (version ``FORMULA_VERSION``; change the version if one changes) are
the copy's own definitions, restated in ``BASIS`` for the reader. No cost or
margin is in this report: its money is what bills, discounts, counts and
returns were worth, which store roles already see.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db.models import Count, Max, Q, QuerySet, Sum

from reporting.base import (
    Column,
    Missing,
    ReportScope,
    envelope,
    freshness,
    note_scope,
)
from reporting.exception_facts import KEY as FRESHNESS_KEY
from reporting.models import ExceptionFact

REPORT = "exceptions"
TITLE = "Exceptions report"
FORMULA_VERSION = "exceptions-1"
FEATURE_KEY = "exceptions-report"

GROUPINGS: dict[str, str] = {
    "store": "Store",
    "staff": "Staff member",
    "list": "Each exception",
}

#: The longest list the "each exception" view sends; the rest is said, not cut silently.
LIST_LIMIT = 2000

Kind = ExceptionFact.Kind

BASIS = [
    "Cancelled bills: bills cancelled after head office accepted them, by the day they "
    "were cancelled, under the person who cancelled where that is recorded, else the "
    "cashier who billed it. Value = the bill's value.",
    "Prices typed at the counter: sold lines whose barcode is in no book, priced off the "
    "tag by the cashier (stores on the old stock system); value = the typed price.",
    "Manual discounts: sold lines whose discount is more than the head-office offers give "
    "them, as the server worked it out when the bill arrived; value = the part no offer "
    "explains, under the cashier who billed it.",
    "Manager PIN uses: a manager's own PIN approving an override on a bill (such as taking "
    "a piece back after the return window) or confirming a cash variance, under that "
    "manager.",
    "Cash variances: day-close counts that did not match the expected cash, under the "
    "person who counted; value = the difference, short or over.",
    "Bill-number holes: bill numbers a till used that have still not reached head office, "
    "as the nightly check finds them, on the store's own row, dated by the day the gap "
    "first lasted a night. A hole closes once its bills arrive.",
    "Returns without a bill: bills that took a piece back against a bill head office does "
    "not hold, under the cashier; value = what those pieces gave back. Cancelled bills "
    "are left out.",
    "Checklist items missed: items of a store's task checklist not ticked by the list's "
    "time, on the store's own row, dated by the day the list was due. An item ticked late "
    "still counts as missed.",
    "Bills are dated by the day they were billed, India time.",
]
PRICE_OVERRIDE = (
    "Price overrides: a price typed for a piece the books hold but have no price for, or "
    "a known piece billed at another price, is not recorded as such, so only prices typed "
    "for unknown barcodes are shown. Manual discounts on bills from before this report "
    "are not shown."
)

#: Store and staff columns: (key, label, kind, which fact kind, events or value).
_MEASURES: tuple[tuple[str, str, str, str, str], ...] = (
    ("cancelled_bills", "Cancelled bills", "number", Kind.CANCELLED_BILL, "events"),
    ("cancelled_paise", "Cancelled value (Rs)", "money", Kind.CANCELLED_BILL, "value"),
    ("prices_typed", "Prices typed", "number", Kind.PRICE_TYPED, "events"),
    ("price_typed_paise", "Typed price (Rs)", "money", Kind.PRICE_TYPED, "value"),
    ("manual_discounts", "Manual discounts", "number", Kind.MANUAL_DISCOUNT, "events"),
    ("manual_discount_paise", "Manual discount (Rs)", "money", Kind.MANUAL_DISCOUNT, "value"),
    ("pin_uses", "Manager PIN uses", "number", Kind.PIN_USE, "events"),
    ("cash_variances", "Cash variances", "number", Kind.CASH_VARIANCE, "events"),
    ("cash_variance_paise", "Cash difference (Rs)", "money", Kind.CASH_VARIANCE, "value"),
    ("number_holes", "Missing bill numbers", "number", Kind.NUMBER_HOLE, "events"),
    ("no_bill_returns", "Returns without a bill", "number", Kind.NO_BILL_RETURN, "events"),
    (
        "no_bill_return_paise",
        "Given back without a bill (Rs)",
        "money",
        Kind.NO_BILL_RETURN,
        "value",
    ),
    ("no_bill_caps", "Over the no-bill cap", "number", Kind.NO_BILL_CAP, "events"),
    ("late_syncs", "Late syncs", "number", Kind.LATE_SYNC, "events"),
    ("checklist_missed", "Checklist items missed", "number", Kind.CHECKLIST_MISSED, "events"),
)
_SUMS: dict[str, Any] = {
    key: (
        Sum("events", filter=Q(kind=kind))
        if measure == "events"
        else Sum("value_paise", filter=Q(kind=kind))
    )
    for key, _label, _kind, kind, measure in _MEASURES
}


def _facts(scope: ReportScope) -> QuerySet[ExceptionFact]:
    return ExceptionFact.objects.filter(
        store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
    )


def _measures(sums: dict[str, Any]) -> dict[str, int]:
    return {key: int(sums.get(key) or 0) for key, *_rest in _MEASURES}


def build(scope: ReportScope, group_by: str) -> dict[str, Any]:
    """The exceptions report for ``scope``, grouped by ``group_by``."""
    missing = Missing()
    as_of = freshness(FRESHNESS_KEY, missing)
    note_scope(scope, TITLE, missing)
    missing.add("PRICE_OVERRIDE", PRICE_OVERRIDE)
    stores = {store.pk: store for store in scope.stores}
    facts = _facts(scope)

    if group_by == "list":
        rows = _list(facts, stores, missing)
        total = {"key": "total", "label": "Total", "events": sum(r["events"] for r in rows)}
    else:
        rows = _grouped(facts, stores, group_by)
        total = {"key": "total", "label": "Total", **_measures(facts.aggregate(**_SUMS))}

    return envelope(
        report=REPORT,
        title=TITLE,
        formula_version=FORMULA_VERSION,
        scope=scope,
        as_of=as_of,
        basis=[*BASIS, *_setting_basis()],
        missing=missing,
        shows_cost=False,
        extra={
            "group_by": group_by,
            "groupings": [{"key": k, "label": v} for k, v in GROUPINGS.items()],
            "columns": [column_json(column) for column in columns(group_by)],
            "rows": rows,
            "total": total,
        },
    )


def _setting_basis() -> list[str]:
    return [
        f"Late syncs: bills that reached head office more than "
        f"{settings.KDPS_LATE_SYNC_MINUTES} minutes after the till printed them, under the "
        "cashier.",
        f"Over the no-bill cap: bills flagged for taking a customer's phone number past "
        f"{settings.KDPS_NO_BILL_RETURN_CAP_PER_PHONE} or a staff member past "
        f"{settings.KDPS_NO_BILL_RETURN_CAP_PER_STAFF} returns without a bill in a month. "
        "The bill is never refused for it.",
    ]


def _grouped(
    facts: QuerySet[ExceptionFact], stores: dict[int, Any], group_by: str
) -> list[dict[str, Any]]:
    if group_by == "store":
        by_store = {
            row["store_id"]: row for row in facts.values("store_id").annotate(**_SUMS).order_by()
        }
        return [
            {
                "key": str(store.pk),
                "label": f"{store.name} ({store.code})",
                **_measures(by_store.get(store.pk, {})),
            }
            for store in sorted(stores.values(), key=lambda s: s.code)
        ]
    grouped = list(
        facts.values("store_id", "staff_key").annotate(**_SUMS, name=Max("staff_name")).order_by()
    )

    def order(row: dict[str, Any]) -> tuple[str, bool, str]:
        store = stores.get(row["store_id"])
        # Each store's people by name, then the store's own row.
        return (store.code if store else "", not row["staff_key"], (row["name"] or "").lower())

    return [
        {
            "key": f"{row['store_id']}:{row['staff_key'] or 'store'}",
            "label": _staff_label(row, stores),
            **_measures(row),
        }
        for row in sorted(grouped, key=order)
    ]


def _staff_label(row: dict[str, Any], stores: dict[int, Any]) -> str:
    store = stores.get(row["store_id"])
    code = store.code if store else str(row["store_id"])
    if not row["staff_key"]:
        return f"The store itself, no one person ({code})"
    return f"{row['name'] or 'Unnamed login'} ({code})"


def _list(
    facts: QuerySet[ExceptionFact], stores: dict[int, Any], missing: Missing
) -> list[dict[str, Any]]:
    labels = dict(Kind.choices)
    counted = facts.aggregate(n=Count("id"))["n"] or 0
    if counted > LIST_LIMIT:
        missing.add(
            "LIST_CUT",
            f"The list shows the newest {LIST_LIMIT} of {counted} exceptions. Choose a "
            "shorter period or one store to see the rest; the other views count them all.",
        )
    rows = []
    for fact in facts.order_by("-day", "store_id", "kind", "id")[:LIST_LIMIT]:
        store = stores.get(fact.store_id)
        rows.append(
            {
                "key": str(fact.pk),
                "label": fact.day.isoformat(),
                "store": store.code if store else str(fact.store_id),
                "staff": fact.staff_name or ("" if fact.staff_key else "The store itself"),
                "kind": labels.get(fact.kind, fact.kind),
                "reference": fact.reference,
                "events": fact.events,
                "value_paise": fact.value_paise,
            }
        )
    return rows


def columns(group_by: str) -> list[Column]:
    """The table's and the spreadsheet's columns."""
    if group_by == "list":
        return [
            Column("label", "Day", "text"),
            Column("store", "Store", "text"),
            Column("staff", "Staff member", "text"),
            Column("kind", "Exception", "text"),
            Column("reference", "Bill or record", "text"),
            Column("events", "Count"),
            Column("value_paise", "Value (Rs)", "money"),
        ]
    return [
        Column("label", GROUPINGS[group_by], "text"),
        *(Column(key, label, kind) for key, label, kind, *_rest in _MEASURES),
    ]


def column_json(column: Column) -> dict[str, str]:
    return {"key": column.key, "label": column.label, "kind": column.kind}
