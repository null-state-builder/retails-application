"""Reports, Staff Performance (store operations PRD ST-RPT-4; overall PRD R-HR-004).

Each salesperson's results at each store - sales, bills, units per bill, average
bill value, returns against their sales and target achievement - read from the
sales copy (``reporting.sales_facts``), which already credits a split line to
each person by their share (ticket 08) and takes a piece given back off whoever
sold it. Targets are the person's own monthly targets (``masters.StaffTarget``,
set through ``reporting.staff_targets``).

**Who sees whom** (overall PRD R-HR-004: "employees see their authorised target
and achievement; managers see scoped team results"): the team is shown to a
manager - a login holding ``sell: approve``, the rung that approves at the
counter (tickets 06 and 41) - and to head office (a network or company-wide
login).
Everyone else sees only their own rows: their login's person, through that
person's staff record. The server filters; a browser is never sent a
colleague's row it must not draw.

Formulas (version ``FORMULA_VERSION``; change the version if one changes):

* **Sold** - pieces and value of the person's share of sold lines.
* **Returned** - pieces and value given back against their sales, on the day
  given back. **Return %** = returned value / sold value in the period.
* **Net sales** = sold less returned; **pieces** likewise. These equal the
  Sales report's figures by salesperson.
* **Bills** - bills with a sold piece of theirs; a shared bill counts for both.
* **Units per bill** = net pieces / bills. **Average bill value** = net sales /
  bills (the Sales report's own definitions).
* **Target achievement** = net sales / the person's targets at that store for
  the months in the period; whole months only (or a month up to today).
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Max, Q, QuerySet, Sum
from django.utils import timezone

from accounts.goods_models import Staff
from accounts.models import ScopeType
from accounts.permissions import user_can
from accounts.sections import CAP_APPROVE, CAP_MANAGE
from masters.models import StaffTarget
from reporting.base import (
    Column,
    Missing,
    ReportScope,
    envelope,
    freshness,
    note_scope,
    paise_ratio,
    percent,
    ratio,
    sees_targets,
)
from reporting.models import SalesLineFact
from reporting.sales_facts import KEY as FRESHNESS_KEY
from reporting.sales_report import period_months

REPORT = "staff"
TITLE = "Staff performance report"
FORMULA_VERSION = "staff-1"
FEATURE_KEY = "staff-performance-report"

TEAM = "team"
OWN = "own"
#: Logins that see every salesperson in their scope without ``sell: approve``.
HEAD_OFFICE_SCOPES = frozenset({ScopeType.ALL, ScopeType.ENTITY})

BASIS = [
    "Bills the server has accepted and not cancelled, dated by the bill's time at the till, "
    "India time; read from the same copy as the Sales report.",
    "A line shared between two salespeople is credited to each by their percentage (split "
    "sale): pieces, value and returns alike.",
    "Returned: pieces given back against the person's sales, at what they were sold for, "
    "on the day they were given back - so a return this period can be against an earlier "
    "sale. Return % = returned value / sold value in the period.",
    "Net sales = sold less returned, GST included, after discount. Units per bill = net "
    "pieces / bills. Average bill value = net sales / bills. A bill counts for everyone who "
    "sold a piece on it, so people's bills can add to more than the total.",
    "Target achievement = net sales / the person's targets at that store for the months in "
    "the period (set on this page); a month still running is measured against its whole "
    "target. Someone with a target and no sales is listed at nought.",
]
OWN_BASIS = "You see your own results only: sales credited to your staff record."


def sees_team(user: Any) -> bool:
    """Is ``user`` a manager (the team) rather than a salesperson (their own rows)?"""
    if getattr(user, "is_superuser", False):
        return True
    if user_can(user, "sell", CAP_APPROVE):
        return True
    # Head office by name, so a scope added later starts on "own rows only".
    return getattr(user, "scope_type", None) in HEAD_OFFICE_SCOPES


def may_set_targets(user: Any) -> bool:
    """Whoever sets store targets sets staff targets: ``money: manage`` (#171)."""
    return user_can(user, "money", CAP_MANAGE)


def own_staff(user: Any) -> Staff | None:
    """The staff record behind this login, through its person; None if it has none."""
    human_id = getattr(user, "human_id", None)
    if human_id is None:
        return None
    return Staff.objects.filter(human_id=human_id).first()


def staff_key(staff: Staff) -> str:
    """How the sales copy names a staff record (``sales_facts._seller``)."""
    return f"staff:{staff.pk}"


_SOLD = Q(pieces_x100__gt=0)
_BACK = Q(pieces_x100__lt=0)
#: Named ``sum_*`` so no aggregate shadows the column it sums.
_SUMS: dict[str, Any] = {
    "sum_bills": Count("bill_id", distinct=True),
    "sum_sold_x100": Sum("pieces_x100", filter=_SOLD),
    "sum_back_x100": Sum("pieces_x100", filter=_BACK),
    "sum_sold": Sum("value_paise", filter=_SOLD),
    "sum_back": Sum("value_paise", filter=_BACK),
}


def _measures(sums: dict[str, Any]) -> dict[str, Any]:
    bills = int(sums.get("sum_bills") or 0)
    sold_x100 = int(sums.get("sum_sold_x100") or 0)
    back_x100 = -int(sums.get("sum_back_x100") or 0)
    sold = int(sums.get("sum_sold") or 0)
    back = -int(sums.get("sum_back") or 0)
    net_x100 = sold_x100 - back_x100
    net = sold - back
    return {
        "bills": bills,
        "sold_pieces": sold_x100 / 100,
        "sold_paise": sold,
        "returned_pieces": back_x100 / 100,
        "returned_paise": back,
        "return_pct": percent(back, sold),
        "pieces": net_x100 / 100,
        "value_paise": net,
        "upt": ratio(net_x100, bills * 100),
        "abv_paise": paise_ratio(net, bills),
    }


def _facts(scope: ReportScope, only: str | None) -> QuerySet[SalesLineFact]:
    facts = SalesLineFact.objects.filter(
        store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
    )
    return facts if only is None else facts.filter(salesperson_key=only)


def _label(key: str, name: str, code: str) -> str:
    if not key:
        return "No salesperson recorded"
    label = f"{name or 'Unnamed'} ({code})" if code else (name or "Unnamed")
    return f"{label}, before the staff list" if key.startswith("old:") else label


def build(scope: ReportScope) -> dict[str, Any]:
    """The staff report for ``scope``, as the viewer may see it."""
    user = scope.user
    view = TEAM if sees_team(user) else OWN
    show_target = sees_targets(user)
    missing = Missing()
    as_of = freshness(FRESHNESS_KEY, missing)
    note_scope(scope, TITLE, missing)
    stores = {store.pk: store for store in scope.stores}

    only: str | None = None
    me: Staff | None = None
    if view == OWN:
        me = own_staff(user)
        if me is None:
            missing.add(
                "NO_STAFF_RECORD",
                "Your login is not linked to a staff record, so there are no results of "
                "your own to show. Ask whoever manages People and access to link it.",
            )
            # A key no row carries: the answer is nothing, never someone else's.
            only = "staff:none"
        else:
            only = staff_key(me)

    facts = _facts(scope, only)
    found = {
        (row["store_id"], row["salesperson_key"]): row
        for row in facts.values("store_id", "salesperson_key")
        .annotate(name=Max("salesperson_name"), code=Max("salesperson_code"), **_SUMS)
        .order_by()
    }
    targets = _targets(scope, me if view == OWN else None, only, show_target, missing)
    # Someone with a target and no sales is listed, at nought.
    for (store_id, key), (name, code) in targets.people.items():
        found.setdefault(
            (store_id, key),
            {"store_id": store_id, "salesperson_key": key, "name": name, "code": code},
        )

    rows = []
    for (store_id, key), row in found.items():
        store = stores.get(store_id)
        measures = _measures(row)
        out = {
            "key": f"{store_id}:{key or 'none'}",
            "label": _label(key, row.get("name") or "", row.get("code") or ""),
            "store": store.code if store else str(store_id),
            **measures,
        }
        if show_target:
            target = targets.by_person.get((store_id, key))
            out["target_paise"] = target
            out["target_pct"] = percent(measures["value_paise"], target) if target else None
        rows.append(out)
    order = {s.pk: s.code for s in scope.stores}
    rows.sort(
        key=lambda r: (
            order.get(int(r["key"].split(":", 1)[0]), ""),
            -r["value_paise"],
            r["label"],
        )
    )
    if targets.judged:
        _note_no_target(rows, missing)
    if view == TEAM and any(r["key"].endswith(":none") for r in rows):
        missing.add(
            "NO_SALESPERSON",
            "Some pieces have no salesperson recorded; they are shown as their own row.",
        )

    total = {
        "key": "total",
        "label": "Total",
        "store": "",
        **_measures(facts.aggregate(**_SUMS)),
    }
    if show_target:
        with_target = [r for r in rows if r.get("target_paise") is not None]
        target_total = sum(int(r["target_paise"]) for r in with_target)
        achieved = sum(int(r["value_paise"]) for r in with_target)
        total["target_paise"] = target_total if with_target else None
        total["target_pct"] = percent(achieved, target_total) if target_total else None

    return envelope(
        report=REPORT,
        title=TITLE,
        formula_version=FORMULA_VERSION,
        scope=scope,
        as_of=as_of,
        basis=[*BASIS, *([OWN_BASIS] if view == OWN else [])],
        missing=missing,
        shows_cost=False,
        extra={
            "view": view,
            "shows_target": show_target,
            "can_set_targets": may_set_targets(user),
            "columns": [column_json(column) for column in columns(show_target)],
            "rows": rows,
            "total": total,
        },
    )


class _Targets:
    def __init__(self) -> None:
        #: (store id, salesperson key) -> the period's target, or None where a month has none.
        self.by_person: dict[tuple[int, str], int | None] = {}
        #: Everyone with a target in the period: (store id, key) -> (name, code).
        self.people: dict[tuple[int, str], tuple[str, str]] = {}
        #: False when targets could not be judged at all (hidden, or not whole months).
        self.judged = False


def _targets(
    scope: ReportScope,
    me: Staff | None,
    only: str | None,
    show_target: bool,
    missing: Missing,
) -> _Targets:
    """Each person's target for the period at each store; say where there is none.

    Never invents one: a month with no target for a person leaves them with none.
    """
    out = _Targets()
    if not show_target:
        missing.add(
            "TARGETS_HIDDEN",
            "Target achievement is not shown to you: targets are Money data your role does "
            "not read.",
        )
        return out
    if only is not None and me is None:
        return out
    period = period_months(scope.date_from, scope.date_to, timezone.localdate())
    if not period.whole:
        missing.add(
            "TARGETS_PARTIAL_MONTH",
            "Targets are set per person per month, so achievement is shown only for whole "
            "months (or a month up to today). Choose a period that starts on the 1st.",
        )
        return out
    out.judged = True
    rows = StaffTarget.objects.filter(
        store_id__in=scope.store_ids, month__in=period.months
    ).select_related("staff__human")
    if me is not None:
        rows = rows.filter(staff=me)
    set_for: dict[tuple[int, str], dict[Any, int]] = {}
    for row in rows:
        key = (row.store_id, staff_key(row.staff))
        set_for.setdefault(key, {})[row.month] = int(row.target_paise)
        out.people[key] = (row.staff.human.display_name, row.staff.human.staff_code)
    for key, months in set_for.items():
        if all(month in months for month in period.months):
            out.by_person[key] = sum(months.values())
        else:
            out.by_person[key] = None
    return out


def _note_no_target(rows: list[dict[str, Any]], missing: Missing) -> None:
    if any(r.get("target_paise") is None for r in rows):
        missing.add(
            "NO_TARGET",
            "Some people have no target for every month of this period; their achievement "
            "is blank. Targets are set on this page, per person and month.",
        )


def columns(show_target: bool) -> list[Column]:
    """The table's and the spreadsheet's columns, as this viewer may see them."""
    out = [
        Column("label", "Salesperson", "text"),
        Column("store", "Store", "text"),
        Column("bills", "Bills"),
        Column("sold_pieces", "Pieces sold"),
        Column("sold_paise", "Sold (Rs)", "money"),
        Column("returned_pieces", "Pieces returned"),
        Column("returned_paise", "Returned (Rs)", "money"),
        Column("return_pct", "Return %"),
        Column("value_paise", "Net sales (Rs)", "money"),
        Column("upt", "Units per bill"),
        Column("abv_paise", "Average bill value (Rs)", "money"),
    ]
    if show_target:
        out += [
            Column("target_paise", "Target (Rs)", "money"),
            Column("target_pct", "Target achievement %"),
        ]
    return out


def column_json(column: Column) -> dict[str, str]:
    return {"key": column.key, "label": column.label, "kind": column.kind}
