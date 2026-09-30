"""C07 transfer approval adapter: exact pieces, complete cells and versioned routes.

Historical unpinned transfers remain readable but cannot acquire a new approval
or dispatch permission. Decisions and the existing reservation writer share one
command; intermediate steps record evidence and move nothing.
"""
from __future__ import annotations

import uuid
from typing import Any

from approvals.goods_models import ApprovalRequest
from approvals.goods_policy import Amounts, _check_in_force, band_failure, pin
from approvals.goods_services import DecisionContext, create_request, decide, register_subject_cells, register_subject_handler
from core.canonical import content_hash
from core.commands import CommandRun, LockRank
from core.goods_documents import pending_lines
from core.kernel_models import DocumentHead
from core.refusals import Refusal
from masters.goods_config import CONFIGURATION, ConfigTarget, resolve
from masters.goods_identity_models import ProductSku
from masters.goods_models import ConfigVersion, EffectiveVersionPeriod
from outbound.goods_models import GoodsTransfer
from stockledger.goods_models import Origin

ACTION = "pt.approve.transfer"


def snapshot(run: CommandRun, transfer: GoodsTransfer, head: DocumentHead, plan: DocumentHead) -> dict[str, Any]:
    rows = pending_lines(run, plan)
    brands = dict(ProductSku.objects.filter(pk__in=[line.payload.get("sku_id") for line in rows]).values_list("pk", "style__brand_id"))
    origins = {str(row.pk): row for row in Origin.objects.filter(pk__in={p["origin_id"] for line in rows for p in line.payload.get("portions", []) if p.get("origin_id")})}
    cells: set[tuple[int, int]] = set()
    value, qty = 0, 0
    unknown = False
    for line in rows:
        sku = line.payload.get("sku_id")
        brand = brands.get(uuid.UUID(sku)) if sku else None
        if brand is None:
            raise Refusal("APPROVAL_SOURCE_INACTIVE", "Every transfer line needs trusted stable brand ownership.", status=409)
        cells.update((site, brand) for site in (transfer.source_site_id, transfer.destination_site_id))
        qty += int(line.payload["qty"])
        for piece in line.payload.get("portions", []):
            origin = origins.get(str(piece.get("origin_id")))
            if origin is None:
                unknown = True
            elif str(origin.sku_id) != str(sku):
                raise Refusal("APPROVAL_SOURCE_INACTIVE", "The frozen origin conflicts with the transfer's stable identity.", status=409)
            else:
                value += (int(piece["upper"]) - int(piece["lower"])) * int(origin.unit_cost)
    if not cells or qty < 1:
        raise Refusal("APPROVAL_SOURCE_INACTIVE", "An empty transfer has no approval authority.", status=409)
    return {"transfer_id": str(transfer.pk), "document_id": str(transfer.document_id),
        "source_site_id": transfer.source_site_id, "destination_site_id": transfer.destination_site_id,
        "custody": transfer.custody, "maker_id": str(transfer.document.maker_id),
        "head_revision_id": str(head.draft_revision_id), "head_hash": head.draft_revision.content_hash if head.draft_revision else None,
        "plan_document_id": str(plan.document_id), "plan_revision_id": str(plan.draft_revision_id),
        "plan_hash": plan.draft_revision.content_hash if plan.draft_revision else None,
        "lines_hash": content_hash([{"key": str(line.line_key), "payload": line.payload} for line in rows]),
        "cells": [list(cell) for cell in sorted(cells)], "qty": qty, "value_paise": None if unknown else str(value)}


def submit(run: CommandRun, transfer: GoodsTransfer, head: DocumentHead, plan: DocumentHead, *, new_subject: bool = True) -> ApprovalRequest:
    evidence = snapshot(run, transfer, head, plan)
    brands = sorted({cell[1] for cell in evidence["cells"]})
    policy = pin(run, action=ACTION, purpose="transfer", site_id=transfer.source_site_id,
        brand_ids=brands, amounts=Amounts(evidence["qty"], int(evidence["value_paise"]) if evidence["value_paise"] is not None else None))
    for site in (transfer.source_site_id, transfer.destination_site_id):
        current = resolve(run.tenant_id, "approval", ConfigTarget.of(run.now, site_id=site, brand_ids=brands, purpose="transfer"), match={"action": ACTION}, code="APPROVAL_POLICY_BLOCKED", path="approval_policy")
        if current.pk != policy.version_id:
            raise Refusal("APPROVAL_POLICY_BLOCKED", "Both sites must share one explicit transfer approval policy.", status=422)
    version = ConfigVersion.objects.get(pk=policy.version_id)
    for step in policy.basis["steps"]:
        if band_failure({**version.payload, **step}, Amounts(evidence["qty"], int(evidence["value_paise"]) if evidence["value_paise"] is not None else None)):
            raise Refusal("APPROVAL_POLICY_BLOCKED", "The exact transfer exceeds a route step limit.", status=422)
    policy.basis.update(snapshot=evidence, cells=evidence["cells"], fields=["cost"],
        site_ids=sorted({transfer.source_site_id, transfer.destination_site_id}),
        maker_ids=[str(transfer.document.maker_id), str(run.principal.human_id)])
    return create_request(run, subject_kind="transfer", subject_key=str(transfer.pk),
        revision=1, reviewed_hash=content_hash(evidence), requested_action=ACTION,
        site_id=transfer.source_site_id, policy=policy, new_subject=new_subject,
        scope={"scope_kind": "sites", "site_ids": policy.basis["site_ids"], "brand_ids": brands},
        title="Review exact transfer pieces and both stores")


def request_for(transfer: GoodsTransfer) -> ApprovalRequest:
    row = ApprovalRequest.objects.filter(subject_kind="transfer", subject_key=str(transfer.pk), requested_action=ACTION).order_by("-created_at", "-id").first()
    if row is None or not row.policy_basis.get("snapshot"):
        raise Refusal("APPROVAL_STALE", "This historical transfer has no exact policy/input pin; validated resubmission is required.", status=409)
    return row


def validate(run: CommandRun, transfer: GoodsTransfer, head: DocumentHead, plan: DocumentHead, request: ApprovalRequest) -> None:
    actual = snapshot(run, transfer, head, plan)
    if actual != request.policy_basis.get("snapshot") or content_hash(actual) != request.reviewed_hash:
        raise Refusal("APPROVAL_STALE", "The transfer inputs, identities, sites or frozen pieces changed.", status=409)
    if request.policy_version_id is None:
        raise Refusal("APPROVAL_STALE", "No versioned approval policy was pinned.", status=409)
    run.lock(LockRank.DRAFT, EffectiveVersionPeriod.objects.filter(target_kind=CONFIGURATION, target_id=request.policy_version_id))
    version = ConfigVersion.objects.get(pk=request.policy_version_id)
    _check_in_force(run, request, version, request.policy_basis)
    amounts = Amounts(actual["qty"], int(actual["value_paise"]) if actual["value_paise"] is not None else None)
    for step in request.policy_basis["steps"]:
        if band_failure({**version.payload, **step}, amounts):
            raise Refusal("APPROVAL_STALE", "The exact transfer exceeds a pinned route limit.", status=409)


def approve(run: CommandRun, transfer: GoodsTransfer, access: Any, reason: str | None, reviewed_hash: str, revision: int) -> None:
    request = request_for(transfer)
    if reviewed_hash != request.reviewed_hash or revision != request.revision:
        raise Refusal("REVISION_SUPERSEDED", "Reload the exact approval inputs and current route step.", status=409)
    decide(run, access=access, request_id=request.pk, decision="approve", reviewed_hash=reviewed_hash, reason_code=reason)


def _decide(run: CommandRun, context: DecisionContext) -> dict[str, Any]:
    from outbound import transfers

    transfer = GoodsTransfer.objects.filter(pk=uuid.UUID(context.request.subject_key)).first()
    if transfer is None:
        raise Refusal("NOT_FOUND", "That transfer was not found.")
    return transfers.decide_approval(run, transfer.pk, context)


def dispatch_check(run: CommandRun, transfer: GoodsTransfer, head: DocumentHead, plan: DocumentHead) -> None:
    request = request_for(transfer)
    if request.state != "approved" or request.decisions.filter(outcome="step_approved").count() + 1 != len(request.policy_basis["steps"]):
        raise Refusal("APPROVAL_STALE", "The complete independent route has not approved this transfer.", status=409)
    validate(run, transfer, head, plan, request)


def install() -> None:
    register_subject_handler("transfer", ACTION, _decide)

    def cells(requests: list[ApprovalRequest]) -> dict[Any, frozenset[tuple[int | None, int | None]]]:
        return {row.pk: frozenset((int(site), int(brand)) for site, brand in row.policy_basis.get("cells") or []) for row in requests}

    register_subject_cells("transfer", ACTION, cells)
