"""The rules every report follows (store operations PRD §17; overall PRD R-AN-003/004).

A report built on this module:

* reads only the reporting store (``reporting.models``), never the billing or
  receiving tables, so a bill never waits on it (R-AN-003, §27);
* states its **basis**, its **as-of** time and any **missing data** (R-AN-004),
  in the envelope ``envelope`` builds and the spreadsheet ``workbook`` writes;
* is limited to the viewer's stores on the server (``report_scope``:
  ``actionable_stores``, narrowed by the top-bar unit, and only stores where the
  report's feature switch is on);
* sends cost and margin only to a viewer ``sees_cost`` allows, so a store role's
  browser never receives them (overall PRD §10.4);
* exports to ``.xlsx``, and records each export as an audit entry
  (``record_export``) - the only thing a report ever writes.

Later reports (tickets 14, 43-48) reuse ``report_scope``, ``sees_cost``,
``sees_targets``, ``envelope``, ``strip_cost``, ``workbook``, ``xlsx_response``
and ``record_export``, and add their own facts and their own report module.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone

from accounts.role_assignments import effective_assignments
from accounts.principal import AccessContext
from accounts.sections import CAP_MANAGE, CAP_VIEW
from accounts.unified_policy import role_actions, role_fields
from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from core.outbox import ANCHOR_INTERVAL
from core.refusals import Refusal
from masters.models import Store
from masters.scoping import actionable_stores, active_store_ids
from masters.store_features import feature, switch_states
from ptmapper.goods_workbook import Amount, workbook_bytes
from reporting.models import ReportRefresh

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Every field that carries cost or margin. ``strip_cost`` removes all of them.
COST_FIELDS = ("cost_paise", "margin_paise", "margin_pct", "cost_share_pct")

#: The audit action every report export is recorded under.
EXPORT_ACTION = "reports.export"


# -- who sees what ---------------------------------------------------------------


def _covers_report_fields(
    user: Any, stores: Iterable[Store] | None, fields: set[str],
    *, section: str | None = None, minimum: str = CAP_VIEW,
) -> bool:
    """Every store in an aggregate must have its own qualifying assignment.

    A report may group several sites into one row or total. If one site lacks a
    protected field, the entire aggregate loses that field; projecting after
    aggregation would reveal the unauthorised site's contribution.
    """
    if stores is None or not getattr(user, "is_active", False):
        return False
    sites = list(stores)
    if not sites or any(site.tenant_id != getattr(user, "tenant_id", None) for site in sites):
        return False
    human_id = getattr(user, "human_id", None)
    if human_id is None:
        return False
    rows = [
        row for row in effective_assignments(human_id)
        if row.all_brands
        and "section.reports.view" in role_actions(row.role)
        and (section is None or f"section.{section}.{minimum}" in role_actions(row.role))
        and fields <= role_fields(row.role)
    ]
    return all(
        any(row.all_sites or site.pk in row.site_ids for row in rows)
        for site in sites
    )


def sees_cost(user: Any, stores: Iterable[Store] | None = None) -> bool:
    """Cost and margin leave only when every reported store permits both fields."""
    return _covers_report_fields(user, stores, {"cost", "margin"})


def sees_targets(user: Any, stores: Iterable[Store] | None = None) -> bool:
    """Store targets are financial data, scoped to every store in the report."""
    return _covers_report_fields(user, stores, {"financial"}, section="money")


def sees_financial_report(user: Any, stores: Iterable[Store] | None = None) -> bool:
    """A full Money report needs manage and financial access at every store."""
    return _covers_report_fields(
        user, stores, {"financial"}, section="money", minimum=CAP_MANAGE
    )


def strip_cost(row: dict[str, Any]) -> dict[str, Any]:
    """``row`` without any cost or margin key - not blanked, absent."""
    return {key: value for key, value in row.items() if key not in COST_FIELDS}


# -- which stores, which days --------------------------------------------------


@dataclass(frozen=True)
class ReportScope:
    """The stores and days one report request covers, all checked on the server."""

    user: Any
    #: Stores the viewer may read where the report is switched on.
    options: list[Store]
    #: The stores this request reports on: one picked store, or all of ``options``.
    stores: list[Store]
    date_from: date
    date_to: date
    picked: Store | None = None
    #: Stores in the viewer's scope left out because the report is off there.
    switched_off: tuple[Store, ...] = ()

    @property
    def store_ids(self) -> list[int]:
        return [store.pk for store in self.stores]

    def filters(self) -> dict[str, Any]:
        return {
            "store": self.picked.code if self.picked else None,
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
        }


def _date(params: Any, key: str, default: date) -> date:
    raw = (params.get(key) or "").strip()
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise Refusal("INVALID_REQUEST", f"{key} must be a date, YYYY-MM-DD.") from None


def viewer_stores(user: Any) -> list[Store]:
    """The viewer's active stores, narrowed by the top-bar unit (which may only
    narrow). A brand-scoped person has none."""
    stores = list(actionable_stores(user, section="reports", minimum=CAP_VIEW))
    narrowed = active_store_ids(user, section="reports", minimum="view")
    if narrowed is not None:
        allowed = set(narrowed)
        stores = [store for store in stores if store.pk in allowed]
    return stores


def report_scope(
    user: Any, params: Any, feature_key: str, readable_when_off: Iterable[int] = ()
) -> ReportScope:
    """The viewer's stores where ``feature_key`` is on, and the period asked for.

    ``readable_when_off`` names stores whose records stay readable with the
    switch off (a switch refuses only new work, ticket 01): they count as on here.

    Refuses rather than narrowing silently: a store outside the viewer's scope is
    ``SCOPE_DENIED``, a store in scope with the report switched off is
    ``FEATURE_OFF``, and a period longer than ``KDPS_REPORT_MAX_DAYS`` is
    ``PERIOD_TOO_LONG``. A brand-scoped person has no stores and is refused.
    """
    stores = viewer_stores(user)
    if not stores:
        raise Refusal(
            "SCOPE_DENIED",
            "No store is in your scope, so there is nothing to report on.",
            status=403,
        )
    report = feature(feature_key)
    on = {state.site_id for state in switch_states(stores, [report]) if state.enabled}
    on |= set(readable_when_off)
    options = [store for store in stores if store.pk in on]
    code = (params.get("store") or "").strip().upper()
    picked: Store | None = None
    if code:
        picked = next((store for store in stores if store.code.upper() == code), None)
        if picked is None:
            raise Refusal("SCOPE_DENIED", f"Store {code} is not one of your stores.", status=403)
        if picked.pk not in on:
            raise Refusal(
                "FEATURE_OFF",
                f"{report.name} is switched off for {picked.name}. "
                "Admin can switch it on in Setup, Feature Switches.",
                status=403,
            )
    elif not options:
        raise Refusal(
            "FEATURE_OFF",
            f"{report.name} is switched off at every store you can see. "
            "Admin can switch it on in Setup, Feature Switches.",
            status=403,
        )
    today = timezone.localdate()
    date_to = _date(params, "date_to", today)
    date_from = _date(params, "date_from", date_to.replace(day=1))
    if date_to < date_from:
        raise Refusal("INVALID_REQUEST", "The period ends before it starts.")
    longest = int(settings.KDPS_REPORT_MAX_DAYS)
    if (date_to - date_from).days + 1 > longest:
        raise Refusal(
            "PERIOD_TOO_LONG",
            f"A report covers at most {longest} days. Choose a shorter period.",
            status=400,
        )
    return ReportScope(
        user=user,
        options=options,
        stores=[picked] if picked else options,
        date_from=date_from,
        date_to=date_to,
        picked=picked,
        switched_off=() if picked else tuple(s for s in stores if s.pk not in on),
    )


# -- basis, as-of, missing data -------------------------------------------------------


@dataclass
class Missing:
    """What a report could not include, each said once in plain words."""

    items: list[dict[str, str]] = field(default_factory=list)

    def add(self, code: str, text: str) -> None:
        if all(item["code"] != code for item in self.items):
            self.items.append({"code": code, "text": text})


def note_scope(scope: ReportScope, report_name: str, missing: Missing) -> None:
    """Say which of the viewer's stores an all-stores report leaves out, and why."""
    if scope.switched_off:
        names = ", ".join(f"{s.name} ({s.code})" for s in scope.switched_off)
        missing.add(
            "SWITCHED_OFF",
            f"Not included: {names}, where the {report_name.lower()} is switched off.",
        )


def refresh_minutes() -> int:
    return max(1, int(ANCHOR_INTERVAL.total_seconds() // 60))


def freshness(key: str, missing: Missing) -> datetime | None:
    """The copy's as-of time, noting in ``missing`` when it is absent or late."""
    state = ReportRefresh.objects.filter(key=key).first()
    as_of = state.as_of if state else None
    if as_of is None:
        missing.add(
            "NOT_BUILT",
            "The reporting copy has not been built yet, so nothing is shown. The server "
            f"builds it every {refresh_minutes()} minutes.",
        )
        return None
    late_after = timedelta(minutes=3 * refresh_minutes())
    if timezone.now() - as_of > late_after:
        missing.add(
            "LATE",
            f"The reporting copy was last brought up to date at "
            f"{timezone.localtime(as_of):%d %b %Y %H:%M}, later than it should be. "
            "Newer bills are not in yet.",
        )
    if state is not None and state.failed_at and state.failed_at > as_of:
        missing.add("REFRESH_FAILED", "The last attempt to bring the copy up to date failed.")
    return as_of


def note_freshness(key: str, prefix: str, missing: Missing) -> None:
    """Say when another copy a report reads is absent or late, each note under
    ``prefix`` ("Sales: The reporting copy ...")."""
    found = Missing()
    freshness(key, found)
    for item in found.items:
        missing.add(f"{prefix.upper()}_{item['code']}", f"{prefix}: {item['text']}")


def envelope(
    *,
    report: str,
    title: str,
    formula_version: str,
    scope: ReportScope,
    as_of: datetime | None,
    basis: list[str],
    missing: Missing,
    shows_cost: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The part of every report's answer that is the same whatever the report."""
    return {
        "report": report,
        "title": title,
        "formula_version": formula_version,
        "date_from": scope.date_from,
        "date_to": scope.date_to,
        "store": scope.picked.code if scope.picked else None,
        "stores": [_store(store) for store in scope.stores],
        "store_options": [_store(store) for store in scope.options],
        "as_of": as_of,
        "refreshed_every_minutes": refresh_minutes(),
        "basis": basis,
        "missing": missing.items,
        "shows_cost": shows_cost,
        **(extra or {}),
    }


def _store(store: Store) -> dict[str, Any]:
    return {"id": store.pk, "code": store.code, "name": store.name}


# -- arithmetic ---------------------------------------------------------------------


def ratio(
    numerator: int | float | Decimal, denominator: int | float | Decimal, places: int = 2
) -> float | None:
    """``numerator / denominator``, half up to ``places``; None for nothing to divide by."""
    if not denominator:
        return None
    exact = Decimal(str(numerator)) / Decimal(str(denominator))
    return float(exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def paise_ratio(numerator: int, denominator: int | float | Decimal) -> int | None:
    """A money figure per unit, in whole paise, half up."""
    if not denominator:
        return None
    exact = Decimal(numerator) / Decimal(str(denominator))
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def percent(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return ratio(Decimal(str(numerator)) * 100, denominator, places=1)


# -- the spreadsheet ------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    #: ``money`` cells hold exact rupees; ``number`` and ``text`` hold what they are.
    kind: str = "number"


def workbook(
    *,
    body: dict[str, Any],
    grouping_label: str,
    columns: Sequence[Column],
    rows: Iterable[dict[str, Any]],
    total: dict[str, Any],
    sheet: str | None = None,
) -> bytes:
    """One sheet: what the report is, its basis, as-of and missing data, then the table.

    ``sheet`` names the tab (at most 31 characters); by default, the title's start.
    """
    as_of = body["as_of"]
    stores = ", ".join(f"{s['name']} ({s['code']})" for s in body["stores"])
    head: list[list[Any]] = [
        [body["title"]],
        ["Stores", stores],
        ["Period", body["date_from"].isoformat(), body["date_to"].isoformat()],
        ["Grouped by", grouping_label],
        [
            "As of",
            timezone.localtime(as_of).strftime("%Y-%m-%d %H:%M") if as_of else "Not built yet",
        ],
        ["Formula version", body["formula_version"]],
    ]
    head += [["Basis", line] for line in body["basis"]]
    head += [["Missing data", item["text"]] for item in body["missing"]] or [
        ["Missing data", "None"]
    ]
    table = [[column.label for column in columns]]
    table += [[_cell(row.get(column.key), column.kind) for column in columns] for row in rows]
    table.append([_cell(total.get(column.key), column.kind) for column in columns])
    return workbook_bytes((sheet or body["title"])[:31], [*head, [], *table])


def _cell(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind == "money":
        return Amount(int(value))
    return value


def xlsx_response(content: bytes, stem: str) -> HttpResponse:
    response = HttpResponse(content, content_type=XLSX)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response["Content-Disposition"] = f'attachment; filename="{stem}-{stamp}.xlsx"'
    return response


def principal_for(user: Any, fallback_tenant_id: Any) -> Principal:
    """Who a report's audit entry names: the person, or the reports service for a
    login with no person behind it."""
    tenant_id = getattr(user, "tenant_id", None) or fallback_tenant_id
    human_id = getattr(user, "human_id", None)
    user_id = getattr(user, "pk", None)
    if human_id is not None:
        return Principal(tenant_id=tenant_id, human_id=human_id, user_id=user_id)
    return Principal(tenant_id=tenant_id, service_code="reports", user_id=user_id)


def record_export(
    user: Any, *, report: str, scope: ReportScope, detail: dict[str, Any],
    contains_cost: bool = False, contains_targets: bool = False,
    contains_financial: bool = False, contains_team: bool = False,
    access: AccessContext | None = None,
) -> None:
    """Who took a copy of which report, with which filters. Failing to record it
    fails the export, as the audit log's own export does (ticket 02).

    This is called after rendering but before returning bytes. Recheck the
    current assignments against the *whole* report and its protected columns,
    so a revocation during a long workbook build cannot ship the old answer.
    """
    from accounts.models import User

    still_active = User.objects.filter(
        pk=getattr(user, "pk", None), tenant_id=getattr(user, "tenant_id", None),
        human__active=True, is_active=True,
    ).exists()
    if not still_active:
        raise Refusal("NOT_FOUND", "That report is no longer in your scope.", status=404)
    current = {site.pk for site in viewer_stores(user)} if getattr(user, "is_active", False) else set()
    if not scope.stores or any(site.pk not in current for site in scope.stores):
        raise Refusal("NOT_FOUND", "That report is no longer in your scope.", status=404)
    if contains_cost and not sees_cost(user, scope.stores):
        raise Refusal("ACTION_DENIED", "Cost access changed while the report was made.", status=403)
    if contains_targets and not sees_targets(user, scope.stores):
        raise Refusal("ACTION_DENIED", "Target access changed while the report was made.", status=403)
    if contains_financial and not sees_financial_report(user, scope.stores):
        raise Refusal("ACTION_DENIED", "Financial access changed while the report was made.", status=403)
    if contains_team:
        from reporting.staff_report import sees_team

        if not sees_team(user, scope.stores):
            raise Refusal("ACTION_DENIED", "Team access changed while the report was made.", status=403)
    if access is None or access.user.pk != user.pk or access.tenant_id != scope.stores[0].tenant_id:
        raise Refusal("AUTH_REQUIRED", "A live session is required to deliver a report.", status=401)
    actions = {"section.reports.view"}
    fields = set()
    if contains_cost:
        fields.update({"cost", "margin"})
    if contains_targets or contains_financial:
        fields.add("financial")
        actions.add("section.money.manage" if contains_financial else "section.money.view")
    if contains_team:
        fields.add("personal")
    with access.guard_legacy_write(lambda current: current.covers_all_actions(
        actions, [(site.pk, None) for site in scope.stores], fields,
    )):
        _record_export(access.principal(), report, scope, detail)


def _record_export(principal: Principal, report: str, scope: ReportScope, detail: dict[str, Any]) -> None:
    after = {"report": report, **scope.filters(), **detail, "format": "xlsx"}

    def handler(run: CommandRun) -> CommandResult:
        run.audit_after = after
        return CommandResult(resource_type="report_export")

    execute_command(
        principal,
        CommandSpec(
            EXPORT_ACTION,
            uuid.uuid4(),
            after,
            subject_key=f"report_export:{report}",
            site_id=scope.picked.pk if scope.picked else None,
        ),
        handler,
    )
