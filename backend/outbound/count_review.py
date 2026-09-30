"""C08 reviewed blind-count closure and evidenced shortages at original layer cost.

The blind capture stays in goods_counts. Review freezes its exact selection,
till frontier and source portions; a different authorised person approves once.
Gains and ambiguous or encumbered custody never become invented stock.
"""
from __future__ import annotations

import uuid
from typing import Any

from accounts.principal import AccessContext
from approvals.goods_models import ApprovalRequest
from approvals.goods_policy import Amounts, pin
from approvals.goods_services import DecisionContext, create_request, register_subject_cells, register_subject_handler, supersede_pending
from core.canonical import content_hash, normalise
from core.commands import CommandRun, LockRank
from core.goods_documents import append_revision, lock_heads, officialise, record_event
from core.goods_fields import bounds, portion
from core.operational import ValuePair
from core.refusals import Refusal
from masters.goods_config import CONFIGURATION, ConfigTarget, resolve
from masters.goods_identity_models import ProductSku
from masters.goods_models import EffectiveVersionPeriod, SiteGuard
from outbound import goods_counts as counts
from outbound.goods_models import CountSnapshot, GoodsCountPass, GoodsStocktake
from sell.services.till_authority import snapshot_pause_evidence
from stockledger import goods_engine as engine
from stockledger.goods_models import ActiveHold, ActiveReservation, Origin, Position
from stockledger.ranges import normalise as merged_ranges

ACTION = "count.review"
FIELDS = frozenset({"cost", "layer_value"})
PURPOSE = "blind_count"


def fresh_pause(run: CommandRun, stocktake: GoodsStocktake) -> None:
    if not stocktake.till_pause_evidence:
        return
    from core.kernel_models import DocumentEvent

    event = DocumentEvent.objects.filter(document_id=stocktake.document_id, event_kind=counts.EVENT_STARTED).first()
    if event is None or event.payload.get("till_pause_evidence") != stocktake.till_pause_evidence:
        raise Refusal("COUNT_SNAPSHOT_STALE", "The original till-pause evidence changed.", status=409)
    if normalise(snapshot_pause_evidence(run, stocktake.site)) != stocktake.till_pause_evidence:
        raise Refusal("COUNT_SNAPSHOT_STALE", "The counter's reconciled frontier changed; cancel and restart.", status=409)


def scope_cells(stocktake: GoodsStocktake) -> list[tuple[int, int | None]]:
    frozen = list(CountSnapshot.objects.filter(stocktake=stocktake))
    skus = {uuid.UUID(row.address["sku_id"]) for row in frozen if row.address.get("sku_id")}
    brands = dict(ProductSku.objects.filter(pk__in=skus).values_list("pk", "style__brand_id"))
    if len(brands) != len(skus) or any(brand is None for brand in brands.values()):
        raise Refusal("COUNT_IDENTITY_PENDING", "The counted scope has unresolved stable identity.", status=422)
    return [(stocktake.site_id, brand) for brand in sorted(set(brands.values()))] or [(stocktake.site_id, None)]


def unchanged_book(stocktake: GoodsStocktake, frozen: list[CountSnapshot]) -> str:
    """Compare semantic portions, including zero-difference reviews and damage."""
    from collections import defaultdict

    actual = Position.objects.filter(site_id=stocktake.site_id, boundary="physical", location_id__in=counts.scope_locations(stocktake))
    if stocktake.scope["kind"] == "brand":
        actual = actual.filter(sku_id__in=counts._brand_skus(stocktake.scope["brand_id"]))
    expected: dict[str, list[tuple[int, int]]] = defaultdict(list)
    current: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in frozen:
        expected[content_hash({"lot_id": str(row.lot_id), "address": row.address})].append(bounds(row.portion))
    for piece in actual:
        current[content_hash({"lot_id": str(piece.lot_id), "address": {**engine.Address.of(piece).as_json(), "description": piece.description}})].append(bounds(piece.portion))
    before = {key: merged_ranges(value) for key, value in expected.items()}
    after = {key: merged_ranges(value) for key, value in current.items()}
    if before != after:
        raise Refusal("COUNT_SNAPSHOT_STALE", "Stock custody or condition changed after the blind snapshot; cancel and recount.", status=409)
    return content_hash(before)


def report_snapshot(stocktake: GoodsStocktake, report: counts.Variance) -> dict[str, Any]:
    if not report.complete:
        raise Refusal("COUNT_SELECTION_INVALID", "Count the complete assigned scope exactly once before review.", status=422)
    if any(line.delta is None or line.delta > 0 for line in report.lines):
        raise Refusal("COUNT_GAIN_PENDING", "Found goods require reviewed source/custody identity; no stock gain is posted by this count.", status=422)
    frozen = list(CountSnapshot.objects.filter(stocktake=stocktake).order_by("lot_id", "portion"))
    cells = scope_cells(stocktake)
    book_hash = unchanged_book(stocktake, frozen)
    removals: list[dict[str, Any]] = []
    total_qty, total_value = 0, 0
    for line in report.lines:
        remaining = -(line.delta or 0)
        if not remaining:
            continue
        if not line.sku_id or line.condition != "good":
            raise Refusal("COUNT_CUSTODY_PENDING", "Only identified accepted good stock supports a shortage correction here.", status=422)
        for row in frozen:
            address = row.address
            if (address.get("sku_id") != line.sku_id or str(address.get("location_id")) != line.location_id
                    or address.get("condition", "good") != line.condition):
                continue
            lower, upper = bounds(row.portion)
            take = min(remaining, upper - lower)
            if not take:
                break
            interval = portion(lower, lower + take)
            origin = Origin.objects.filter(pk=address.get("origin_id"), sku_id=uuid.UUID(line.sku_id)).first()
            if origin is None or not address.get("accepted_event_id"):
                raise Refusal("COUNT_CUSTODY_PENDING", "This shortage has no accepted own-cost origin; use its custody correction.", status=422)
            if (ActiveHold.objects.filter(lot_id=row.lot_id, portion__overlap=interval).exists()
                    or ActiveReservation.objects.filter(lot_id=row.lot_id, portion__overlap=interval).exists()):
                raise Refusal("COUNT_CUSTODY_PENDING", "Held or reserved goods need their owned correction before this count closes.", status=422)
            live = engine.positions_of(row.lot_id, (lower, lower + take))
            if sum(min(lower + take, bounds(piece.portion)[1]) - max(lower, bounds(piece.portion)[0]) for piece in live
                   if normalise({**engine.Address.of(piece).as_json(), "description": piece.description}) == address) != take:
                raise Refusal("COUNT_SNAPSHOT_STALE", "The reviewed shortage's exact portions changed; recount or cancel.", status=409)
            removals.append({"line_key": str(line.line_key), "lot_id": str(row.lot_id), "lower": lower,
                "upper": lower + take, "origin_id": str(origin.pk), "cost_paise": str(origin.unit_cost), "address": address})
            total_qty += take
            total_value += take * int(origin.unit_cost)
            remaining -= take
            if not remaining:
                break
        if remaining:
            raise Refusal("COUNT_SNAPSHOT_STALE", "The frozen book cannot cover the exact shortage.", status=409)
    return {"count_id": str(stocktake.pk), "document_id": str(stocktake.document_id), "variance_hash": report.hash,
        "selected_pass_ids": [str(key) for key in report.selection], "cells": [list(cell) for cell in cells],
        "removed_qty": total_qty, "removed_value_paise": str(total_value), "removals": removals,
        "book_hash": book_hash, "pause_hash": stocktake.till_pause_evidence.get("hash")}


def submit(run: CommandRun, access: AccessContext, stocktake_id: uuid.UUID, *, reviewed_hash: str,
           selected: list[uuid.UUID], expected_revision: int | None, reason_code: str) -> GoodsStocktake:
    stocktake, guard = counts._lock_site_and_count(run, stocktake_id)
    counts._require_open(stocktake)
    if expected_revision != stocktake.revision or guard is None or guard.freeze_id != stocktake.pk:
        raise Refusal("REVISION_SUPERSEDED", "Reload the open count before preparing its exact review.", status=409)
    passes = run.lock(LockRank.DOCUMENT, GoodsCountPass.objects.filter(stocktake=stocktake))
    if any(row.state == "open" for row in passes):
        raise Refusal("COUNT_SESSION_INVALID", "Finish every blind pass before preparing review.", status=409)
    fresh_pause(run, stocktake)
    report = counts.variance(stocktake, selected)
    if report.hash != reviewed_hash:
        raise Refusal("REVISION_SUPERSEDED", "The reviewed count selection changed.", status=409)
    if not access.covers_all_actions({ACTION}, scope_cells(stocktake), FIELDS):
        raise Refusal("ACTION_DENIED", "Count preparation needs its action, protected fields and full scope together.", status=403)
    evidence = report_snapshot(stocktake, report)
    reason = resolve(run.tenant_id, "reasons", ConfigTarget.of(run.now, site_id=stocktake.site_id,
        brand_ids=[cell[1] for cell in evidence["cells"]]), match={"action": "count.run"}, code="COUNT_REASON_REQUIRED", path="reason")
    if reason_code not in {item["code"] for item in reason.payload["codes"] if not item["retired"]}:
        raise Refusal("COUNT_REASON_REQUIRED", "Choose a current configured count reason.", status=422)
    evidence.update(reason_code=reason_code, reason_version_id=str(reason.pk),
        maker_ids=sorted({str(stocktake.document.maker_id), *(str(row.counter_id) for row in passes), str(access.human_id)}))
    policy = pin(run, action=ACTION, purpose=PURPOSE, site_id=stocktake.site_id,
        brand_ids=[cell[1] for cell in evidence["cells"]], amounts=Amounts(evidence["removed_qty"], int(evidence["removed_value_paise"])))
    policy.basis.update(snapshot=evidence, cells=evidence["cells"], fields=sorted(FIELDS), maker_ids=evidence["maker_ids"])
    head = lock_heads(run, [stocktake.document_id])[stocktake.document_id]
    append_revision(run, head, header={"kind": PURPOSE, "reviewed_hash": reviewed_hash, "reason": reason_code},
        replace_lines=[(uuid.uuid5(stocktake.pk, str(index)), piece) for index, piece in enumerate(evidence["removals"])])
    stocktake.state = "review"
    counts._bump(run, stocktake)
    stocktake.save(update_fields=["state"])
    create_request(run, subject_kind="count", subject_key=str(stocktake.pk), revision=stocktake.revision,
        reviewed_hash=content_hash(evidence), requested_action=ACTION, site_id=stocktake.site_id, policy=policy,
        title="Review exact blind count and original-cost differences")
    record_event(run, stocktake.document_id, "submitted", payload={"to_state": "review", "details": []})
    return stocktake


def _decide(run: CommandRun, context: DecisionContext) -> dict[str, Any]:
    stocktake = counts._lock_stocktake(run, uuid.UUID(context.request.subject_key))
    guard = SiteGuard.objects.get(site_id=stocktake.site_id)
    passes = run.lock(LockRank.DOCUMENT, GoodsCountPass.objects.filter(stocktake=stocktake))
    if stocktake.state != "review" or guard.freeze_id != stocktake.pk or stocktake.revision != context.request.revision:
        raise Refusal("REVISION_SUPERSEDED", "The submitted count is no longer this exact frozen review.", status=409)
    evidence = context.request.policy_basis.get("snapshot") or {}
    if str(context.checker_id) in evidence.get("maker_ids", []):
        raise Refusal("SELF_APPROVAL", "The count maker, any counter and review preparer cannot approve their own evidence.", status=403)
    if not context.access.covers_all_actions({ACTION}, [tuple(cell) for cell in evidence["cells"]], FIELDS):
        raise Refusal("ACTION_DENIED", "Review needs protected fields and complete scope.", status=403)
    fresh_pause(run, stocktake)
    report = counts.variance(stocktake, [uuid.UUID(key) for key in evidence["selected_pass_ids"]])
    current = report_snapshot(stocktake, report)
    if any(current.get(key) != evidence.get(key) for key in current) or content_hash(evidence) != context.request.reviewed_hash:
        raise Refusal("COUNT_SNAPSHOT_STALE", "The reviewed variance, identities or exact portions changed.", status=409)
    if not context.access.covers_all_actions({ACTION}, [tuple(cell) for cell in evidence["cells"]], FIELDS):
        raise Refusal("ACTION_DENIED", "Review needs protected fields and complete scope.", status=403)
    head = lock_heads(run, [stocktake.document_id])[stocktake.document_id]
    reason_id = uuid.UUID(evidence["reason_version_id"])
    run.lock(LockRank.DRAFT, EffectiveVersionPeriod.objects.filter(target_kind=CONFIGURATION, target_id=reason_id))
    reason = resolve(run.tenant_id, "reasons", ConfigTarget.of(run.now, site_id=stocktake.site_id,
        brand_ids=[cell[1] for cell in evidence["cells"]]), match={"action": "count.run"}, code="APPROVAL_STALE", path="reason", status=409)
    if reason.pk != reason_id:
        raise Refusal("APPROVAL_STALE", "The configured count reason changed.", status=409)
    context.enforce_policy(run)
    if context.decision == "reject":
        stocktake.state = "open"
        counts._bump(run, stocktake)
        stocktake.save(update_fields=["state"])
        record_event(run, stocktake.document_id, "rejected", payload={"to_state": "open", "details": []})
        return {"state": "open"}
    engine.lock_lots(run, [uuid.UUID(piece["lot_id"]) for piece in evidence["removals"]])
    # Re-read exact eligibility after the physical lot locks, before any posting.
    report_snapshot(stocktake, report)
    version, _lines = officialise(run, head, approved_by_id=context.checker_id,
        canonical_header={"kind": PURPOSE, "variance_hash": report.hash, "reason": evidence["reason_code"]},
        lines=[(uuid.uuid5(stocktake.pk, str(index)), piece) for index, piece in enumerate(evidence["removals"])],
        authority=run.authority, reconciliation={"snapshot_hash": context.request.reviewed_hash}, number=stocktake.document.official_number)
    batch_id = None
    if evidence["removals"]:
        plan = engine.Plan("P13", version.pk, engine.event_key(PURPOSE, stocktake.pk))
        for piece in evidence["removals"]:
            lot_id, origin_id = uuid.UUID(piece["lot_id"]), uuid.UUID(piece["origin_id"])
            lower, upper = piece["lower"], piece["upper"]
            engine.end_positions(run, plan, lot_id, (lower, upper), "consumed", PURPOSE)
            plan.value.append(ValuePair(origin_id=origin_id, amount=(upper - lower) * int(piece["cost_paise"]),
                source_bucket="stock", destination_bucket="external", source_site_id=stocktake.site_id, destination_site_id=None,
                lot_id=lot_id, lower=lower, upper=upper))
        batch_id = engine.post(run, version, plan)
    counts.finish_count(run, stocktake, guard, report, passes, movement=stocktake.document if evidence["removals"] else None)
    return {"state": "closed", "journal_batch_id": str(batch_id) if batch_id else None}


def install() -> None:
    register_subject_handler("count", ACTION, _decide)

    def cells(requests: list[ApprovalRequest]) -> dict[Any, frozenset[tuple[int | None, int | None]]]:
        return {row.pk: frozenset((int(site), int(brand) if brand is not None else None) for site, brand in row.policy_basis.get("cells") or []) for row in requests}

    register_subject_cells("count", ACTION, cells)


def cancel_pending(run: CommandRun, stocktake: GoodsStocktake) -> None:
    supersede_pending(run, "count", str(stocktake.pk), ACTION)
