"""What the count screens read (goods ticket 17; design E102, E103, E163, E211).

Blindness is decided here, per reader, and never left to the screen:

* A counter reads the count, their own passes and the identity of a scanned
  code. Nothing they can read carries a book, on-hand, held, reserved or valued
  quantity, or another counter's observations.
* A reviewer (``count.review``) reads every pass and the variance against the
  frozen as-of - unless they are themselves counting in this count, in which
  case the variance stays closed to them until their pass is submitted.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from accounts.principal import AccessContext
from core.goods_history import document_history
from masters.goods_models import Location
from outbound import goods_counts as counts
from outbound.goods_models import CountAffirmation, CountDecision, GoodsCountPass, GoodsStocktake


def _names(ids: Iterable[Any]) -> dict[uuid.UUID, str]:
    from accounts.goods_models import HumanIdentity

    wanted = sorted({i for i in ids if i}, key=str)
    if not wanted:
        return {}
    return dict(HumanIdentity.objects.filter(pk__in=wanted).values_list("pk", "display_name"))


def _person(human_id: Any, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {"id": str(human_id) if human_id else None, "name": names.get(human_id, "")}


def _location_names(site_ids: Iterable[int]) -> dict[str, str]:
    return {
        str(pk): name
        for pk, name in Location.objects.filter(site_id__in=list(site_ids)).values_list(
            "pk", "name"
        )
    }


def item_labels(sku_ids: Iterable[Any], tenant_id: uuid.UUID) -> dict[str, str]:
    """A readable name for each SKU: brand, style, size and colour - identity only."""
    from masters.goods_identity_services import candidates_for

    out: dict[str, str] = {}
    for item in candidates_for(tenant_id, [s for s in sku_ids if s]):
        parts = [item.get("brand"), item.get("style"), item.get("size"), item.get("colour")]
        out[str(item["sku_id"])] = " · ".join(str(p) for p in parts if p)
    return out


def _scope(stocktake: GoodsStocktake, locations: dict[str, str]) -> dict[str, Any]:
    from masters.models import Brand

    scope = dict(stocktake.scope or {})
    brand_name = None
    if scope.get("brand_id"):
        brand_name = (
            Brand.objects.filter(pk=scope["brand_id"]).values_list("name", flat=True).first()
        )
    return {
        "kind": scope.get("kind"),
        "count_kind": scope.get("count_kind"),
        "location_id": scope.get("location_id"),
        "location_name": locations.get(str(scope.get("location_id")))
        if scope.get("location_id")
        else None,
        "brand_id": str(scope["brand_id"]) if scope.get("brand_id") else None,
        "brand_name": brand_name,
    }


def may_read(access: AccessContext, stocktake: GoodsStocktake) -> bool:
    return access.can(counts.RUN_ACTION, site_id=stocktake.site_id) or access.can(
        counts.REVIEW_ACTION, site_id=stocktake.site_id
    )


def may_review(access: AccessContext, stocktake: GoodsStocktake) -> bool:
    return access.can(counts.REVIEW_ACTION, site_id=stocktake.site_id)


def counting_now(access: AccessContext, stocktake: GoodsStocktake) -> bool:
    """Is this reader holding an unfinished pass of this count? Then the book stays closed."""
    return GoodsCountPass.objects.filter(
        stocktake=stocktake, counter_id=access.human_id, state=GoodsCountPass.State.OPEN
    ).exists()


def summary(
    stocktake: GoodsStocktake,
    passes: list[GoodsCountPass],
    names: dict[uuid.UUID, str],
    locations: dict[str, str],
    now: datetime,
) -> dict[str, Any]:
    site = stocktake.site
    open_passes = [p for p in passes if p.state == GoodsCountPass.State.OPEN]
    return {
        "id": str(stocktake.pk),
        "record_contract": "goods-v1",
        "number": stocktake.document.official_number,
        "state": stocktake.state,
        "revision": stocktake.revision,
        "site": {"id": str(site.pk), "code": site.code, "name": site.name},
        "scope": _scope(stocktake, locations),
        "frozen_at": stocktake.frozen_at.isoformat() if stocktake.frozen_at else None,
        "freeze_active": stocktake.state in (GoodsStocktake.State.OPEN, GoodsStocktake.State.REVIEW),
        "started_by": _person(stocktake.document.maker_id, names),
        "started_at": stocktake.document.created_at.isoformat(),
        "last_activity_at": stocktake.last_activity_at.isoformat(),
        "open_passes": len(open_passes),
        "submitted_passes": sum(p.state != GoodsCountPass.State.OPEN for p in passes),
        "stale_passes": sum(counts.is_stale(p, now) for p in open_passes),
    }


def _scope_label(count_pass: GoodsCountPass, locations: dict[str, str]) -> str:
    scope = count_pass.scope or {}
    if scope.get("kind") == "location":
        return f"Location {locations.get(str(scope.get('location_id')), '')}".strip()
    if scope.get("kind") == "recount":
        cells = scope.get("cells") or []
        return f"Recount of {len(cells)} item{'s' if len(cells) != 1 else ''} at their locations"
    return "Everything this count covers"


def _pass_row(
    count_pass: GoodsCountPass,
    names: dict[uuid.UUID, str],
    locations: dict[str, str],
    observed: dict[uuid.UUID, int],
    show_quantity: bool,
    now: datetime,
) -> dict[str, Any]:
    scope = count_pass.scope or {}
    return {
        "id": str(count_pass.pk),
        "pass_no": count_pass.pass_no,
        "counter": _person(count_pass.counter_id, names),
        "kind": scope.get("kind") or "count",
        "scope_label": _scope_label(count_pass, locations),
        "location_id": scope.get("location_id"),
        "state": count_pass.state,
        "stale": counts.is_stale(count_pass, now),
        "opened_at": count_pass.created_at.isoformat(),
        "last_activity_at": (
            count_pass.last_activity_at.isoformat() if count_pass.last_activity_at else None
        ),
        "submitted_at": count_pass.submitted_at.isoformat() if count_pass.submitted_at else None,
        "replaces_pass_id": str(count_pass.replaces_id) if count_pass.replaces_id else None,
        "reason_code": count_pass.reason_code,
        # A counter sees their own total; a reviewer sees everyone's. Nobody sees
        # another counter's while counting.
        "observed_qty": observed.get(count_pass.pk, 0) if show_quantity else None,
    }


def _observed_totals(pass_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    totals: dict[uuid.UUID, int] = defaultdict(int)
    for row in counts.pass_observations(pass_ids):
        totals[row.stocktake_pass_id] += row.qty
    return dict(totals)


def list_rows(
    access: AccessContext, rows: list[GoodsStocktake], now: datetime
) -> list[dict[str, Any]]:
    passes: dict[uuid.UUID, list[GoodsCountPass]] = defaultdict(list)
    for count_pass in GoodsCountPass.objects.filter(stocktake__in=rows):
        passes[count_pass.stocktake_id].append(count_pass)
    names = _names(row.document.maker_id for row in rows)
    locations = _location_names({row.site_id for row in rows})
    results = []
    for row in rows:
        personal = access.covers_all({counts.RUN_ACTION, counts.REVIEW_ACTION}, {(row.site_id, None)}, {"personal"})
        shown = names if personal else {key: name for key, name in names.items() if key == access.human_id}
        results.append(summary(row, passes.get(row.pk, []), shown, locations, now))
    return results


def progress(
    stocktake: GoodsStocktake, passes: list[GoodsCountPass], reviewer_view: bool
) -> tuple[str, dict[str, Any] | None]:
    """Where the count stands, in words a screen can show - never "completed" while pending.

    ``counting``: someone is still counting, or nothing has been submitted yet.
    ``awaiting_review``: every pass so far is submitted (what a counter sees).
    For a reviewer, the submitted passes are measured against the frozen book:
    ``incomplete`` (not every location is counted once), ``differences_pending``
    (counted differs from the book - waiting for the difference review and the
    Owner's approval, ticket 17A) or ``matches_book`` (ready to close).
    """
    if stocktake.state == GoodsStocktake.State.CLOSED:
        return ("closed_adjusted" if CountDecision.objects.filter(stocktake=stocktake, movement__isnull=False).exists() else "closed_matching"), None
    if stocktake.state == GoodsStocktake.State.CANCELLED:
        return "cancelled", None
    if any(p.state == GoodsCountPass.State.OPEN for p in passes) or not passes:
        stage = "counting"
    else:
        stage = "awaiting_review"
    if not reviewer_view or not any(p.state != GoodsCountPass.State.OPEN for p in passes):
        return stage, None
    report = counts.variance(stocktake)
    outcome = (
        "incomplete"
        if not report.complete
        else ("matches_book" if report.zero else "differences_pending")
    )
    return (stage if stage == "counting" else outcome), {
        "outcome": outcome,
        "variance_hash": report.hash,
        "differing_lines": sum(1 for line in report.lines if line.delta not in (0, None)),
    }


def count_detail(
    access: AccessContext, stocktake: GoodsStocktake, now: datetime, history_cursor: str | None
) -> dict[str, Any]:
    passes = list(GoodsCountPass.objects.filter(stocktake=stocktake).order_by("created_at", "pk"))
    reviewer = may_review(access, stocktake)
    counting = counting_now(access, stocktake)
    names = _names(
        [stocktake.document.maker_id, *[p.counter_id for p in passes]]
        + list(
            CountDecision.objects.filter(stocktake=stocktake).values_list("approver_id", flat=True)
        )
    )
    locations = _location_names([stocktake.site_id])
    observed = _observed_totals([p.pk for p in passes])
    rows = [
        _pass_row(
            p,
            names,
            locations,
            observed,
            show_quantity=(reviewer and not counting) or p.counter_id == access.human_id,
            now=now,
        )
        for p in passes
    ]
    mine = next(
        (
            p
            for p in passes
            if p.counter_id == access.human_id and p.state == GoodsCountPass.State.OPEN
        ),
        None,
    )
    stage, review = progress(stocktake, passes, reviewer and not counting)
    decision = CountDecision.objects.filter(stocktake=stocktake).order_by("-recorded_at").first()
    history = document_history(stocktake.document_id, history_cursor)
    is_open = stocktake.state == GoodsStocktake.State.OPEN
    allowed: list[str] = []
    if is_open and access.can(counts.RUN_ACTION, site_id=stocktake.site_id):
        allowed.append("continue_pass" if mine else "open_pass")
        allowed.append("lookup")
    if is_open and reviewer and not counting:
        allowed += ["variance", "recount"]
        if review is not None and stage != "counting":
            if stocktake.till_pause_evidence or review["outcome"] != "matches_book":
                allowed.append("submit_review")
            else:
                allowed.append("close")
    if stocktake.state in (GoodsStocktake.State.OPEN, GoodsStocktake.State.REVIEW) and (
        reviewer
        or (
            (stocktake.scope or {}).get("count_kind") == "cycle"
            and access.can(counts.RUN_ACTION, site_id=stocktake.site_id)
        )
    ):
        allowed.append("cancel")
    counters = (
        [
            {"id": str(pk), "name": names.get(pk, "")}
            for pk in sorted({p.counter_id for p in passes}, key=str)
        ]
        if reviewer
        else []
    )
    from approvals.goods_models import ApprovalRequest
    from approvals.goods_policy import eligible_checker
    from outbound.count_review import ACTION, FIELDS

    request = ApprovalRequest.objects.filter(subject_kind="count", subject_key=str(stocktake.pk), requested_action=ACTION).order_by("-created_at", "-id").first()
    approval = None
    if request is not None and reviewer and not counting:
        cells = frozenset(tuple(cell) for cell in request.policy_basis.get("cells") or [])
        if cells and access.covers_all_actions({ACTION}, cells, FIELDS):
            evidence = request.policy_basis.get("snapshot") or {}
            approval = {"id": str(request.pk), "revision": request.revision, "reviewed_hash": request.reviewed_hash,
                "state": request.state, "policy_version_id": str(request.policy_version_id) if request.policy_version_id else None,
                "removed_qty": evidence.get("removed_qty", 0), "removed_value_paise": evidence.get("removed_value_paise", "0"),
                "reason_code": evidence.get("reason_code", "")}
            if stocktake.state == GoodsStocktake.State.REVIEW:
                allowed.append("variance")
                if request.state == "pending" and eligible_checker(access, request, cells):
                    allowed += ["approve", "reject"]
    covered = counts.scope_locations(stocktake)
    if not access.covers_all_actions({counts.REVIEW_ACTION if reviewer else counts.RUN_ACTION}, {(stocktake.site_id, None)}, {"personal"}):
        names = {key: name for key, name in names.items() if key == access.human_id}
        for row in rows:
            if row["counter"]["id"] != str(access.human_id):
                row["counter"]["name"] = ""
        for row in counters:
            if row["id"] != str(access.human_id):
                row["name"] = ""
    return {
        **summary(stocktake, passes, names, locations, now),
        "non_trading_event_id": str(stocktake.non_trading_event_id) if stocktake.non_trading_event_id else None,
        "trading_pause_verified": bool(stocktake.till_pause_evidence),
        "approval": approval,
        # What a counter may be assigned to: the count's own locations, by name
        # (identity only - the directory itself stays its own grant's).
        "locations": [
            {"id": str(row.pk), "name": row.name, "kind": row.kind}
            for row in Location.objects.filter(pk__in=list(covered)).order_by("name")
        ],
        "progress": stage,
        "review": review,
        "passes": rows,
        "my_open_pass_id": str(mine.pk) if mine else None,
        "counters": counters,
        "decision": (
            {
                "id": str(decision.pk),
                "decided_by": _person(decision.approver_id, names),
                "decided_at": decision.recorded_at.isoformat(),
                "variance_hash": decision.observation_hash,
                "lines": len(decision.variances or []),
            }
            if decision is not None
            else None
        ),
        "allowed_actions": sorted(set(allowed)),
        "history": history.items,
        "next_history_cursor": history.next_cursor,
    }


def may_read_pass(access: AccessContext, count_pass: GoodsCountPass) -> bool:
    stocktake = count_pass.stocktake
    if count_pass.counter_id == access.human_id:
        return access.can(counts.RUN_ACTION, site_id=stocktake.site_id)
    return may_review(access, stocktake) and not counting_now(access, stocktake)


def pass_detail(access: AccessContext, count_pass: GoodsCountPass, now: datetime, tenant_id: uuid.UUID) -> dict[str, Any]:
    stocktake = count_pass.stocktake
    rows = counts.pass_observations([count_pass.pk])
    names = _names([count_pass.counter_id, *[r.actor_id for r in rows]])
    if not access.covers_all({counts.RUN_ACTION, counts.REVIEW_ACTION}, {(stocktake.site_id, None)}, {"personal"}):
        names = {key: name for key, name in names.items() if key == access.human_id}
    locations = _location_names([stocktake.site_id])
    coverage = counts.pass_coverage(stocktake, count_pass)
    site_locations = {
        str(row.pk): row for row in Location.objects.filter(pk__in=list(coverage)).order_by("name")
    }
    labels = item_labels(
        [r.sku_id for r in rows]
        + [c.get("sku_id") for c in (count_pass.scope or {}).get("cells") or []],
        tenant_id,
    )
    affirmation = CountAffirmation.objects.filter(count_pass=count_pass).first()
    totals: dict[str, int] = {condition: 0 for condition in counts.CONDITION_VALUES}
    for row in rows:
        totals[row.condition] += row.qty
    scope = count_pass.scope or {}
    return {
        "id": str(count_pass.pk),
        "stocktake_id": str(stocktake.pk),
        "stocktake_number": stocktake.document.official_number,
        "stocktake_state": stocktake.state,
        "site_id": str(stocktake.site_id),
        "pass_no": count_pass.pass_no,
        "counter": _person(count_pass.counter_id, names),
        "state": count_pass.state,
        "stale": counts.is_stale(count_pass, now),
        "revision": count_pass.revision,
        "observation_hash": counts.observation_hash(count_pass, rows),
        "scope": {
            "kind": scope.get("kind") or "count",
            "label": _scope_label(count_pass, locations),
            "location_id": scope.get("location_id"),
            "cells": [
                {
                    "sku_id": cell.get("sku_id"),
                    "item": labels.get(str(cell.get("sku_id")), "") if cell.get("sku_id") else "",
                    "description": cell.get("description") or "",
                    "location_id": cell.get("location_id"),
                    "location_name": locations.get(str(cell.get("location_id")), ""),
                }
                for cell in scope.get("cells") or []
            ],
        },
        "reason_code": count_pass.reason_code,
        "replaces_pass_id": str(count_pass.replaces_id) if count_pass.replaces_id else None,
        "locations": [
            {"id": key, "name": row.name, "kind": row.kind} for key, row in site_locations.items()
        ],
        "opened_at": count_pass.created_at.isoformat(),
        "last_activity_at": (
            count_pass.last_activity_at.isoformat() if count_pass.last_activity_at else None
        ),
        "submitted_at": count_pass.submitted_at.isoformat() if count_pass.submitted_at else None,
        "affirmation": (
            {
                "counter": _person(affirmation.counter_id, names),
                "recorded_at": affirmation.recorded_at.isoformat(),
                "observation_hash": affirmation.observation_hash,
                "observation_revision": affirmation.observation_revision,
                "scope_hash": affirmation.scope_hash,
            }
            if affirmation is not None
            else None
        ),
        "observed_qty": sum(totals.values()),
        "totals": totals,
        "acknowledged_scan_keys": [str(r.scan_key) for r in rows],
        "observations": [
            {
                "scan_key": str(r.scan_key),
                "location_id": str(r.location_id) if r.location_id else None,
                "location_name": locations.get(str(r.location_id), ""),
                "sku_id": str(r.sku_id) if r.sku_id else None,
                "item": labels.get(str(r.sku_id), "") if r.sku_id else "",
                "description": r.description,
                "alias_value": r.alias_value,
                "condition": r.condition,
                "qty": r.qty,
                "correction_of_id": str(r.correction_of_id) if r.correction_of_id else None,
                "actual_at": r.event_at.isoformat(),
                "recorded_at": r.recorded_at.isoformat(),
                "counted_by": _person(r.actor_id, names),
            }
            for r in rows
        ],
    }


def variance_detail(
    stocktake: GoodsStocktake, report: counts.Variance, tenant_id: uuid.UUID
) -> dict[str, Any]:
    locations = _location_names([stocktake.site_id])
    labels = item_labels([line.sku_id for line in report.lines], tenant_id)
    return {
        "stocktake_id": str(stocktake.pk),
        "revision": stocktake.revision,
        "variance_hash": report.hash,
        "selected_pass_ids": [str(p) for p in report.selection],
        "complete": report.complete,
        "matches_book": report.zero,
        "issues": report.issues,
        "uncovered_locations": [
            {"id": str(u), "name": locations.get(str(u), "")} for u in report.uncovered
        ],
        "differing_lines": sum(1 for line in report.lines if line.delta not in (0, None)),
        "lines": [
            {
                "line_key": str(line.line_key),
                "sku_id": line.sku_id,
                "item": labels.get(line.sku_id, "") if line.sku_id else "",
                "description": line.description,
                "location_id": line.location_id,
                "location_name": locations.get(line.location_id, ""),
                "condition": line.condition,
                "book_qty": line.book_qty,
                "observed_qty": line.observed_qty,
                "delta": line.delta,
                "pass_id": line.pass_id,
            }
            for line in report.lines
        ],
    }
