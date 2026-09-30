"""C08 full-store snapshot counts, under the retained canonical inventory writer.

R-INV-009/ST-INV-3: a fresh full export is evidence, not a stock replacement.
Trading must be verifiably paused before the frozen book is captured. The
supported first contract closes exact matches or removes freely held, accepted
good portions at their original cost. Gains and ambiguous/encumbered custody
remain explicit pending work; they never acquire invented identity or value.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from django.db.models import Q
from django.utils.dateparse import parse_datetime

from accounts.principal import AccessContext
from approvals.goods_models import ApprovalRequest
from approvals.goods_policy import Amounts, pin
from approvals.goods_services import DecisionContext, create_request, register_subject_cells, register_subject_handler, supersede_pending
from core.canonical import content_hash, normalise
from core.commands import CommandRun, LockRank
from core.goods_documents import append_revision, lock_heads, new_document, officialise, record_event
from core.goods_fields import bounds
from core.kernel_models import DocumentIdentity
from core.numbering import allocate
from core.operational import ValuePair
from core.refusals import Refusal, issue
from masters.goods_config import CONFIGURATION, ConfigTarget, resolve
from masters.goods_identity_models import GovernanceState, SkuAlias
from masters.goods_models import EffectiveVersionPeriod, SiteGuard
from outbound.goods_soh_models import SohReconciliation, SohReconciliationEvidence
from ptmapper import soh_services
from ptmapper.soh_models import SohImport
from sell.services.till_authority import snapshot_pause_evidence
from sell.services.working_set import current_version
from stockledger import goods_engine as engine
from stockledger.goods_models import ActiveHold, ActiveReservation, JournalBatch, Position

RUN = "count.run"
REVIEW = "count.review"
FIELDS = ("cost", "layer_value", "financial")
SUBJECT = "soh_reconciliation"
PURPOSE = "soh_reconciliation"


def _require(access: AccessContext, action: str, site_id: int, *, fields: tuple[str, ...] = ()) -> None:
    # Full-store omissions are meaningful. One all-brand assignment must cover
    # both the action and every protected field; unrelated grants cannot mix.
    if not access.covers_all({action}, {(site_id, None)}, fields):
        raise Refusal("ACTION_DENIED", "This full-store count needs one qualifying assignment at this store.", status=403)


def _book(site_id: int) -> dict[str, Any]:
    positions = list(Position.objects.filter(site_id=site_id).select_related("origin", "sku__style", "location").order_by("lot_id", "portion"))
    lots = {row.lot_id for row in positions}
    def encumbrances(model: Any) -> list[dict[str, Any]]:
        return [{"id": str(row.pk), "lot_id": str(row.lot_id), "bounds": list(bounds(row.portion)), "event_id": str(row.event_id)}
                for row in model.objects.filter(lot_id__in=lots).order_by("pk")]
    journal = (JournalBatch.objects.filter(Q(quantity_legs__position__site_id=site_id) | Q(value_legs__leg_site_id=site_id))
               .order_by("-event_at", "-id").values("id", "event_at").first())
    return {"working_set_version": current_version(site_id), "journal_watermark": {
                "id": str(journal["id"]), "event_at": journal["event_at"].isoformat()} if journal else None,
            "positions": [{"lot_id": str(row.lot_id), "lower": bounds(row.portion)[0], "upper": bounds(row.portion)[1],
                           "address": engine.Address.of(row).as_json(), "brand_id": row.sku.style.brand_id if row.sku is not None else None,
                           "identity_state": [row.sku.governance_state, row.sku.style.governance_state] if row.sku is not None else None,
                           "description": row.description,
                           "cost_paise": str(row.origin.unit_cost) if row.origin is not None else None,
                           "mrp_paise": str(row.origin.mrp) if row.origin is not None else None,
                           "location_kind": row.location.kind if row.location is not None else None} for row in positions],
            "holds": encumbrances(ActiveHold), "reservations": encumbrances(ActiveReservation)}


def _reason(run: CommandRun, source: SohImport, code: Any) -> dict[str, Any]:
    if not isinstance(code, str) or not code.strip() or len(code) > 60:
        raise Refusal("INVALID_REQUEST", "Record a configured count-correction reason code.")
    version = resolve(run.tenant_id, "reasons", ConfigTarget.of(run.now, site_id=source.site_id, brand_ids=[None], purpose=PURPOSE),
                      match={"action": RUN}, code="SOH_RECONCILIATION_NOT_READY", path="reason_code")
    codes = {row.get("code") for row in version.payload.get("codes", []) if not row.get("retired")}
    if code not in codes:
        raise Refusal("SOH_RECONCILIATION_NOT_READY", "This reason is outside the approved count reasons.", status=422)
    return {"code": code, "version_id": str(version.pk)}


def _differences(source: SohImport, book: dict[str, Any], cutoff: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    portions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for piece in book["positions"]:
        address = piece["address"]
        if (address.get("boundary") != "physical" or not address.get("sku_id") or not address.get("origin_id")
                or not address.get("accepted_event_id") or address.get("condition") != "good"
                or piece["location_kind"] not in engine.SELLING_KINDS or piece["brand_id"] is None
                or piece["cost_paise"] is None or piece["mrp_paise"] is None
                or piece["identity_state"] != [GovernanceState.EFFECTIVE, GovernanceState.EFFECTIVE]):
            problems.append({"code": "NONORDINARY_CUSTODY", "lot_id": piece["lot_id"], "owner": "C08/SO-08"})
        if address.get("sku_id"):
            portions[address["sku_id"]].append(piece)
    encumbered = {row["lot_id"] for row in [*book["holds"], *book["reservations"]]}
    desired: dict[str, int] = defaultdict(int)
    source_keys: dict[str, list[str]] = defaultdict(list)
    labels: dict[str, str] = {}
    source_rows = list(source.rows.order_by("ordinal"))
    aliases: dict[str, list[SkuAlias]] = defaultdict(list)
    for alias in SkuAlias.objects.filter(tenant_id=source.tenant_id, value__in=[row.barcode for row in source_rows],
                                        governance_state=GovernanceState.EFFECTIVE, sku__governance_state=GovernanceState.EFFECTIVE,
                                        sku__style__governance_state=GovernanceState.EFFECTIVE).filter(Q(site_id=source.site_id) | Q(site__isnull=True)).select_related("sku__style"):
        if alias.effective_from <= cutoff and (alias.effective_to is None or cutoff < alias.effective_to):
            aliases[alias.value].append(alias)
    for row in source_rows:
        if row.quantity < 0 or (row.quantity > 0 and row.exclusion_reason):
            problems.append({"code": "SOURCE_EXCLUSION", "barcode": row.barcode, "owner": "C08/SO-08", "reason": row.exclusion_reason or "Negative source quantity"})
            continue
        candidates = aliases[row.barcode]
        trusted_brand = source.configuration.get("brand_mappings", {}).get(row.source_brand)
        if len(candidates) != 1:
            if row.quantity > 0:
                problems.append({"code": "IDENTITY_OR_GAIN_UNRESOLVED", "barcode": row.barcode, "owner": "C08/SO-08"})
            continue  # unresolved zero catalogue is retained evidence, never stock
        sku = candidates[0].sku
        if trusted_brand is None or str(sku.style.brand_id) != str(trusted_brand) or (row.mapping and str(row.mapping.get("sku_id")) != str(sku.pk)):
            problems.append({"code": "IDENTITY_CONFLICT", "barcode": row.barcode, "owner": "C08/SO-08"})
            continue
        verified = row.verification
        observed_at = parse_datetime(str(verified.get("observed_at") or ""))
        if row.quantity > 0 and (verified.get("observed_qty") != row.quantity or verified.get("observed_condition") != "good"
                                  or observed_at is None or observed_at < cutoff):
            problems.append({"code": "PHYSICAL_SOURCE_MISMATCH", "barcode": row.barcode, "owner": "C08/SO-08"})
        if row.quantity == 0 and verified and (verified.get("observed_qty") != 0 or observed_at is None or observed_at < cutoff):
            problems.append({"code": "PHYSICAL_SOURCE_MISMATCH", "barcode": row.barcode, "owner": "C08/SO-08"})
        desired[str(sku.pk)] += row.quantity
        source_keys[str(sku.pk)].append(row.source_row_key)
        labels[str(sku.pk)] = str(row.source.get("itemname") or "")[:240]
    differences = []
    for sku_id in sorted(set(portions) | set(desired)):
        pieces = portions[sku_id]
        book_qty = sum(piece["upper"] - piece["lower"] for piece in pieces)
        target = desired[sku_id]  # explicit full-store affirmation makes omissions zero
        delta = target - book_qty
        removals: list[dict[str, Any]] = []
        remove_qty = max(-delta, 0)
        if delta > 0:
            problems.append({"code": "GAIN_REQUIRES_FOUND_CUSTODY", "sku_id": sku_id, "owner": "C08/SO-08"})
        elif remove_qty:
            if any(piece["lot_id"] in encumbered for piece in pieces):
                problems.append({"code": "ENCUMBERED_REDUCTION", "sku_id": sku_id, "owner": "C08/SO-08"})
            if len({piece["address"].get("origin_id") for piece in pieces}) != 1:
                problems.append({"code": "AMBIGUOUS_ORIGIN", "sku_id": sku_id, "owner": "C08/SO-08"})
            for piece in pieces:
                take = min(remove_qty, piece["upper"] - piece["lower"])
                if take:
                    removals.append({**piece, "upper": piece["lower"] + take})
                    remove_qty -= take
        value = sum((piece["upper"] - piece["lower"]) * int(piece["cost_paise"]) for piece in removals if piece["cost_paise"] is not None)
        differences.append({"sku_id": sku_id, "item_name": labels.get(sku_id) or (pieces[0]["description"] if pieces else sku_id),
                            "source_row_keys": source_keys[sku_id], "book_qty": book_qty, "observed_qty": target,
                            "delta": delta, "value_removed_paise": str(value), "portions": removals})
    return differences, problems


def begin(run: CommandRun, access: AccessContext, source: SohImport, body: dict[str, Any], expected: int | None) -> SohReconciliation:
    _require(access, RUN, source.site_id)
    guards = run.lock(LockRank.SITE, SiteGuard.objects.filter(tenant_id=run.tenant_id, site_id=source.site_id))
    if (not guards or not guards[0].goods_ready or guards[0].stock_contract != SiteGuard.StockContract.GOODS_V1
            or guards[0].lifecycle in {"planned", "closing", "closed"}):
        raise Refusal("SITE_NOT_READY", "This store is not approved for canonical inventory.")
    found = SohReconciliation.objects.filter(source_import=source).first()
    if found:
        return found  # same source hash cannot apply a second delta
    guard = guards[0]
    if guard.freeze_id:
        raise Refusal("UNDER_COUNT", "An unfinished count already freezes this store.")
    pause = normalise(snapshot_pause_evidence(run, source.site))
    source = soh_services.locked_source(run, source, expected)
    if source.state != "approved" or source.approved_by_id is None or not soh_services.requires_inventory_reconciliation(source):
        raise Refusal("SOH_RECONCILIATION_NOT_READY", "Use an independently approved fresh source for an existing store.", status=422)
    if set(body) != {"full_store_export", "whole_store_physically_counted", "omissions_are_zero", "reason_code"} or any(body.get(key) is not True for key in ("full_store_export", "whole_store_physically_counted", "omissions_are_zero")):
        raise Refusal("INVALID_REQUEST", "Affirm the fresh full-store export, whole physical count including empty locations, and omitted goods as zero.")
    cutoff = source.cutoff_at
    paused_at = parse_datetime(str(pause["paused_at"]))
    if cutoff is None or paused_at is None or cutoff < paused_at or source.created_at < cutoff:
        raise Refusal("SOH_CUTOFF_STALE", "Pause all tills first, then export a fresh full snapshot with its exact cutoff.", status=409)
    if soh_services.review_hash(source) != source.reviewed_hash:
        raise Refusal("REVISION_SUPERSEDED", "The reviewed source changed.", status=409)
    soh_services._configuration(source.configuration, source, run.now)
    book = _book(source.site_id)
    watermark = book["journal_watermark"]
    journal_time = parse_datetime(watermark["event_at"]) if watermark else None
    if journal_time and journal_time > cutoff:
        raise Refusal("SOH_CUTOFF_STALE", "Stock moved after the export cutoff; obtain a fresh source.", status=409)
    reason = _reason(run, source, body["reason_code"])
    differences, problems = _differences(source, book, cutoff)
    identity, head = new_document(run, kind="CNT", purpose=DocumentIdentity.Purpose.COUNT,
                                  entity_id=source.site.gstin.legal_entity_id, site_id=source.site_id)
    payload = {"source_id": str(source.pk), "source_revision": source.revision, "source_hash": source.source_hash,
               "source_reviewed_hash": source.reviewed_hash, "cutoff_at": cutoff.isoformat(), "pause": pause,
               "book": book, "differences": differences, "problems": problems, "declarations": body, "reason": reason}
    assert run.principal.human_id is not None
    reconciliation = SohReconciliation.objects.create(tenant_id=run.tenant_id, source_import=source, site_id=source.site_id,
        document=identity, maker_id=run.principal.human_id, frozen_at=run.now, content_hash=content_hash(payload), payload=payload)
    append_revision(run, head, header={"kind": PURPOSE, "source_id": str(source.pk), "reconciliation_hash": reconciliation.content_hash},
                    replace_lines=[(uuid.uuid5(reconciliation.pk, row["sku_id"]), row) for row in differences])
    guard.freeze_id = reconciliation.pk
    guard.save(update_fields=["freeze_id"])
    _evidence(run, reconciliation, "frozen")
    record_event(run, identity.pk, "count_started", payload={"to_state": "frozen", "source_id": str(source.pk), "details": []})
    return reconciliation


def _evidence(run: CommandRun, row: SohReconciliation, outcome: str) -> None:
    run.record(SohReconciliationEvidence(reconciliation=row, revision=row.revision, outcome=outcome,
                                        content_hash=row.content_hash, payload=row.payload))


def _lock(run: CommandRun, row: SohReconciliation, expected: int | None, *, site_locked: bool = False) -> tuple[SohReconciliation, SiteGuard]:
    query = SiteGuard.objects.filter(tenant_id=run.tenant_id, site_id=row.site_id)
    # The shared deciding command already took this exact site before locking
    # its approval request. A subject handler cannot acquire SITE again there.
    guards = list(query) if site_locked else run.lock(LockRank.SITE, query)
    if not guards:
        raise Refusal("NOT_FOUND", "That count was not found.")
    snapshot_pause_evidence(run, row.site)  # device locks precede subject/policy locks
    rows = run.lock(LockRank.DOCUMENT, SohReconciliation.objects.filter(tenant_id=run.tenant_id, pk=row.pk))
    if not rows:
        raise Refusal("NOT_FOUND", "That count was not found.")
    row = rows[0]
    if expected != row.revision:
        raise Refusal("REVISION_SUPERSEDED", "Reload the exact count revision.", status=409)
    if row.state not in {"frozen", "submitted"} or guards[0].freeze_id != row.pk:
        raise Refusal("STATE_CONFLICT", "This count is no longer holding the store freeze.", status=409)
    if not guards[0].goods_ready or guards[0].stock_contract != SiteGuard.StockContract.GOODS_V1 or guards[0].lifecycle in {"closing", "closed"}:
        raise Refusal("SITE_NOT_READY", "This store is no longer approved for this inventory correction.", status=409)
    return row, guards[0]


def _fresh(run: CommandRun, row: SohReconciliation) -> None:
    source = row.source_import
    if (content_hash(row.payload) != row.content_hash or source.state != "approved"
            or source.revision != row.payload["source_revision"] or soh_services.review_hash(source) != row.payload["source_reviewed_hash"]):
        raise Refusal("REVISION_SUPERSEDED", "The frozen source or review evidence changed; cancel and restart.", status=409)
    immutable = row.evidence.filter(outcome="frozen", revision=1).first()
    if immutable is None or immutable.payload != row.payload or immutable.content_hash != row.content_hash:
        raise Refusal("REVISION_SUPERSEDED", "The frozen count does not match its immutable evidence.", status=409)
    if _book(row.site_id) != row.payload["book"] or normalise(snapshot_pause_evidence(run, row.site)) != row.payload["pause"]:
        raise Refusal("SOH_SNAPSHOT_STALE", "The frozen stock or till frontier changed; cancel and restart.", status=409)
    cutoff = parse_datetime(row.payload["cutoff_at"])
    assert cutoff is not None
    differences, problems = _differences(source, row.payload["book"], cutoff)
    if differences != row.payload["differences"] or problems != row.payload["problems"]:
        raise Refusal("SOH_IDENTITY_STALE", "A source identity or physical eligibility changed after the count froze.", status=409)
    soh_services._configuration(source.configuration, source, run.now)
    if _reason(run, source, row.payload["reason"]["code"]) != row.payload["reason"]:
        raise Refusal("APPROVAL_STALE", "The count-correction reason configuration changed.", status=409)


def submit(run: CommandRun, access: AccessContext, row: SohReconciliation, expected: int | None, reviewed_hash: str) -> SohReconciliation:
    _require(access, RUN, row.site_id)
    row, _guard = _lock(run, row, expected)
    if row.maker_id != access.human_id or row.state != "frozen" or reviewed_hash != row.content_hash:
        raise Refusal("REVISION_SUPERSEDED", "The named count maker must submit the exact frozen review.", status=409)
    _fresh(run, row)
    if row.payload["problems"]:
        raise Refusal("SOH_VARIANCE_PENDING", "This source has owned unresolved differences; cancel or resolve through the C08 custody workflow.", status=422,
                      issues=[issue(problem["code"], "Owned unresolved full-store difference", field=problem.get("barcode") or problem.get("sku_id")) for problem in row.payload["problems"][:50]])
    removed = sum(max(-line["delta"], 0) for line in row.payload["differences"])
    value = sum(int(line["value_removed_paise"]) for line in row.payload["differences"])
    brands = sorted({piece["brand_id"] for piece in row.payload["book"]["positions"] if piece["brand_id"] is not None})
    policy = pin(run, action=REVIEW, purpose=PURPOSE, site_id=row.site_id, brand_ids=brands or [None], amounts=Amounts(removed, value))
    row.approval_request = create_request(run, subject_kind=SUBJECT, subject_key=str(row.pk), revision=row.revision,
        reviewed_hash=row.content_hash, requested_action=REVIEW, site_id=row.site_id, policy=policy, require_distinct=True,
        reconciliation={"source_hash": row.payload["source_hash"], "cutoff_at": row.payload["cutoff_at"]}, title="Review full-store SOH inventory difference")
    row.state = "submitted"
    row.revision += 1
    # Approval revision names the exact submitted count, not the pre-submit DTO.
    row.approval_request.revision = row.revision
    row.approval_request.save(update_fields=["revision"])
    row.save()
    _evidence(run, row, "submitted")
    record_event(run, row.document_id, "submitted", payload={"to_state": "submitted", "details": []})
    return row


def cancel(run: CommandRun, access: AccessContext, row: SohReconciliation, expected: int | None, reason: str) -> SohReconciliation:
    if not reason.strip() or len(reason) > 500:
        raise Refusal("INVALID_REQUEST", "Record why this count is cancelled.")
    if not (access.human_id == row.maker_id and access.covers_all({RUN}, {(row.site_id, None)})):
        _require(access, REVIEW, row.site_id)
    # Cancellation releases only this freeze, even if pause/source evidence is
    # now stale. Retaining a stale count must never permanently lock a shop.
    guards = run.lock(LockRank.SITE, SiteGuard.objects.filter(tenant_id=run.tenant_id, site_id=row.site_id))
    rows = run.lock(LockRank.DOCUMENT, SohReconciliation.objects.filter(tenant_id=run.tenant_id, pk=row.pk))
    if not guards or not rows:
        raise Refusal("NOT_FOUND", "That count was not found.")
    row = rows[0]
    if expected != row.revision or row.state not in {"frozen", "submitted"} or guards[0].freeze_id != row.pk:
        raise Refusal("STATE_CONFLICT", "This count is no longer open at that revision.", status=409)
    supersede_pending(run, SUBJECT, str(row.pk), REVIEW)
    row.state = "cancelled"
    row.revision += 1
    row.save()
    _evidence(run, row, "cancelled")
    guards[0].freeze_id = None
    guards[0].save(update_fields=["freeze_id"])
    record_event(run, row.document_id, "count_cancelled", reason_code="SOH_COUNT_CANCELLED", payload={"to_state": "cancelled", "note": reason, "details": []})
    return row


def _decide(run: CommandRun, context: DecisionContext) -> dict[str, Any]:
    row = SohReconciliation.objects.filter(tenant_id=run.tenant_id, pk=context.request.subject_key).first()
    if row is None:
        raise Refusal("NOT_FOUND", "That count was not found.")
    if context.request.site_id != row.site_id:
        raise Refusal("REVISION_SUPERSEDED", "The approval no longer identifies this count's exact store.", status=409)
    _require(context.access, REVIEW, row.site_id, fields=FIELDS)
    row, guard = _lock(run, row, context.request.revision, site_locked=True)
    source = row.source_import
    participants = {str(row.maker_id), str(source.uploaded_by_id), str(source.prepared_by_id),
                    *(str(observer) for observer in source.rows.values_list("verification__observer_id", flat=True) if observer)}
    if str(context.checker_id) in participants:
        raise Refusal("SELF_APPROVAL", "A source uploader, preparer or physical counter cannot review this correction.", status=403)
    if row.state != "submitted" or row.content_hash != context.request.reviewed_hash:
        raise Refusal("REVISION_SUPERSEDED", "The submitted count changed.", status=409)
    heads = lock_heads(run, [row.document_id])
    _fresh(run, row)
    if row.payload["problems"]:
        raise Refusal("SOH_VARIANCE_PENDING", "This source still has unresolved owned differences.", status=422)
    reason_version = uuid.UUID(row.payload["reason"]["version_id"])
    config_versions = [reason_version, uuid.UUID(source.configuration["identity_profile_id"]), uuid.UUID(source.configuration["profile_version_id"])]
    run.lock(LockRank.DRAFT, EffectiveVersionPeriod.objects.filter(target_kind=CONFIGURATION, target_id__in=config_versions))
    soh_services._configuration(source.configuration, source, run.now)
    if _reason(run, source, row.payload["reason"]["code"]) != row.payload["reason"]:
        raise Refusal("APPROVAL_STALE", "The count-correction reason configuration changed.", status=409)
    context.enforce_policy(run)
    if context.decision == "reject":
        row.state = "frozen"
        row.revision += 1
        row.save()
        _evidence(run, row, "rejected")
        record_event(run, row.document_id, "rejected", reason_code=context.reason_code, payload={"to_state": "frozen", "details": []})
        return {"state": row.state}
    lines = row.payload["differences"]
    engine.lock_lots(run, [uuid.UUID(piece["lot_id"]) for line in lines for piece in line["portions"]])
    number = allocate(run, row.site.gstin.legal_entity, "CNT", on=run.now.date())
    version, _official = officialise(run, heads[row.document_id], approved_by_id=context.checker_id,
        canonical_header={"kind": PURPOSE, "source_id": str(source.pk), "reviewed_hash": row.content_hash,
                          "cutoff_at": row.payload["cutoff_at"], "reason": row.payload["reason"]},
        lines=[(uuid.uuid5(row.pk, line["sku_id"]), line) for line in lines], authority=run.authority,
        reconciliation={"source_hash": source.source_hash, "snapshot_hash": content_hash(row.payload["book"]), "pause_hash": row.payload["pause"]["hash"]}, number=number)
    plan = engine.Plan("P13", version.pk, engine.event_key(PURPOSE, row.pk))
    for line in lines:
        for piece in line["portions"]:
            lot_id, origin_id = uuid.UUID(piece["lot_id"]), uuid.UUID(piece["address"]["origin_id"])
            lower, upper = piece["lower"], piece["upper"]
            engine.end_positions(run, plan, lot_id, (lower, upper), "consumed", PURPOSE)
            plan.value.append(ValuePair(origin_id=origin_id, amount=(upper - lower) * int(piece["cost_paise"]),
                source_bucket="stock", destination_bucket="external", source_site_id=row.site_id, destination_site_id=None,
                lot_id=lot_id, lower=lower, upper=upper))
    batch_id = engine.post(run, version, plan)
    row.official_version, row.journal_batch_id = version, batch_id
    row.state = "closed"
    row.revision += 1
    row.save()
    _evidence(run, row, "closed")
    guard.freeze_id = None
    guard.save(update_fields=["freeze_id"])
    record_event(run, row.document_id, "count_closed", version_id=version.pk,
                 payload={"to_state": "closed", "reconciliation_id": str(row.pk), "journal_batch_id": str(batch_id) if batch_id else None, "details": []})
    return {"state": row.state, "journal_batch_id": str(batch_id) if batch_id else None}


def install() -> None:
    register_subject_handler(SUBJECT, REVIEW, _decide)
    def cells(requests: list[ApprovalRequest]) -> dict[Any, frozenset[tuple[int | None, int | None]]]:
        sites = {str(row.pk): row.site_id for row in SohReconciliation.objects.filter(pk__in=[request.subject_key for request in requests])}
        return {request.pk: frozenset({(sites[request.subject_key], None)}) for request in requests if request.subject_key in sites}
    register_subject_cells(SUBJECT, REVIEW, cells)
