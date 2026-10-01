"""Master sheet import packages: upload, review, submit, approve (SO-04 C01).

One uploaded KDPS master sheet becomes one package. Its uploader reviews what it
would change (``masters.master_sheet.plan_changes``), picks what to retire, leave
out or choose, and submits the whole package for one ``config.approve`` decision
by a different person. Approval re-plans against the live lists - a package whose
lists changed since review is refused, never half-applied - and writes, in one
command, through the product's own writers:

* one ``vocabulary`` configuration version per changed list
  (``goods_services.freeze_config_version``), keeping every published key;
* the Season and Brand masters the sheet names and the tenant lacks
  (``goods_services.create_brand_like``); a new brand carries no commercial terms
  version, so its terms read as unknown until someone sets them;
* the ITEM -> SUB CATEGORY / TYPE suggestion rules (``write_crosswalk``), and a
  correction of each existing rule the sheet now points elsewhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from accounts.principal import AccessContext
from approvals.goods_services import (
    DecisionContext,
    create_request,
    register_subject_handler,
    supersede_pending,
)
from core.canonical import content_hash
from core.commands import CommandRun, LockRank
from core.refusals import Refusal
from files.goods_models import EvidenceObject
from masters.goods_identity_models import GovernanceState, SourceCrosswalk
from masters.goods_identity_services import (
    ITEM_ISSUER,
    record_master_version,
    save_master,
    sync_crosswalk_exception,
    vocabulary,
    write_crosswalk,
)
from masters.goods_models import ConfigDraft, ConfigVersion
from masters.master_sheet import DIMENSIONS, match_key, plan_changes
from masters.master_sheet_models import MasterSheetImport, MasterSheetImportReview
from masters.models import Brand, Season

SUBJECT = "master_sheet_import"
DRAFT_ACTION = "config.draft"
APPROVE_ACTION = "config.approve"
BRAND_ACTION = "vendor.manage"
RULE_ACTION = "crosswalk.manage"
REASON = "MASTER_SHEET_IMPORT"
OPEN_STATES = (MasterSheetImport.State.REVIEW, MasterSheetImport.State.SUBMITTED)
SELECTION_KEYS = frozenset(
    {
        "retire",
        "skip_values",
        "skip_brands",
        "skip_seasons",
        "rule_choices",
        "acknowledged",
    }
)
MAX_SELECTIONS = 5000
#: KDPS buys at P RATE = BASIC x 1.1: ``rates.transport_pct`` 10 (owner's decision,
#: 2026-10-01). Product lists warns when the rates in force say otherwise.
EXPECTED_TRANSPORT_PCT = Decimal("10")


# ----------------------------------------------------------------------------- the live lists


def snapshot(tenant_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    """The tenant's lists as a plan reads them (``master_sheet.plan_changes``'s ``current``)."""
    from masters.goods_config import in_force

    lists = vocabulary(tenant_id, now)
    values: dict[str, list[dict[str, Any]]] = {}
    version_ids: dict[str, str | None] = {}
    labels: dict[str, str] = {}
    for dimension in DIMENSIONS:
        rows = lists.get(dimension, [])
        values[dimension] = [
            {
                "value_id": str(v.id),
                "value_key": v.value_key,
                "label": v.label,
                "sort_order": v.sort_order,
                "retired": v.retired,
            }
            for v in rows
        ]
        version_ids[dimension] = str(rows[0].config_version_id) if rows else None
        labels.update({str(v.id): v.label for v in rows})
    rules: dict[str, dict[str, Any]] = {}
    for row in SourceCrosswalk.objects.filter(
        tenant_id=tenant_id,
        kind__in=["sub_category", "type"],
        issuer_key=ITEM_ISSUER,
        governance_state=GovernanceState.EFFECTIVE,
        retired_at__isnull=True,
    ).order_by("source_key", "id"):
        rules.setdefault(
            f"{row.kind}\x1f{match_key(row.source_key)}",
            {
                "rule_id": str(row.pk),
                "target_id": row.target_key,
                "target_label": labels.get(row.target_key, ""),
            },
        )
    pending = sorted(
        {
            str((draft.payload or {}).get("dimension") or "")
            for draft in ConfigDraft.objects.filter(
                tenant_id=tenant_id,
                kind="vocabulary",
                state__in=[ConfigDraft.State.DRAFT, ConfigDraft.State.SUBMITTED],
            )
        }
        - {""}
    )
    vocab_dims = {
        str(v.pk): str((v.payload or {}).get("dimension") or "")
        for v in ConfigVersion.objects.filter(tenant_id=tenant_id, kind="vocabulary")
    }
    pins: list[dict[str, Any]] = []
    allow: dict[str, bool] = {}
    for version in ConfigVersion.objects.filter(
        tenant_id=tenant_id, kind__in=["profile", "identity_profile"]
    ).order_by("pk"):
        if not in_force(version, now):
            continue
        payload = version.payload if isinstance(version.payload, dict) else {}
        if version.kind == "profile":
            for column in payload.get("columns") or []:
                pinned = isinstance(column, dict) and column.get(
                    "vocabulary_version_id"
                )
                if pinned and vocab_dims.get(str(pinned)):
                    pins.append(
                        {
                            "profile_version_id": str(version.pk),
                            "dimension": vocab_dims[str(pinned)],
                            "pinned_version_id": str(pinned),
                        }
                    )
        else:
            for dimension, key in (
                (payload.get("size_dimension"), "allowed_size_values"),
                (payload.get("colour_dimension"), "allowed_colour_values"),
            ):
                if dimension and payload.get(key):
                    allow[str(dimension)] = True
    return {
        "values": values,
        "version_ids": version_ids,
        "brands": [
            {"id": pk, "code": code, "name": name}
            for pk, code, name in Brand.objects.filter(is_active=True)
            .order_by("pk")
            .values_list("pk", "code", "name")
        ],
        "seasons": [
            {"id": pk, "code": code, "name": name}
            for pk, code, name in Season.objects.filter(historical_unknown=False)
            .order_by("pk")
            .values_list("pk", "code", "name")
        ],
        "item_rules": rules,
        "pending_drafts": pending,
        "pinned_profiles": pins,
        "allow_lists": allow,
    }


def summary(tenant_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    """What the lists hold now, and the P RATE factor the rates in force apply."""
    from masters.goods_identity_services import effective_configs
    from masters.master_sheet import COLUMN_LABELS

    lists = vocabulary(tenant_id, now)
    rates = []
    for version in effective_configs(tenant_id, "rates", now):
        payload = version.payload if isinstance(version.payload, dict) else {}
        try:
            pct = Decimal(str(payload.get("transport_pct")))
        except (InvalidOperation, ValueError):
            continue
        rates.append(
            {
                "rates_version_id": str(version.pk),
                "transport_pct": str(pct),
                "factor": str((1 + pct / 100).normalize()),
                "expected": pct == EXPECTED_TRANSPORT_PCT,
            }
        )
    open_import = (
        MasterSheetImport.objects.filter(tenant_id=tenant_id, state__in=OPEN_STATES)
        .order_by("-created_at", "-pk")
        .values_list("pk", flat=True)
        .first()
    )
    return {
        "lists": [
            {
                "dimension": dimension,
                "column": COLUMN_LABELS[dimension],
                "active": sum(1 for v in lists.get(dimension, []) if not v.retired),
                "retired": sum(1 for v in lists.get(dimension, []) if v.retired),
            }
            for dimension in DIMENSIONS
        ],
        "brands": Brand.objects.filter(is_active=True).count(),
        "seasons": Season.objects.filter(historical_unknown=False).count(),
        "item_rules": SourceCrosswalk.objects.filter(
            tenant_id=tenant_id,
            kind__in=["sub_category", "type"],
            issuer_key=ITEM_ISSUER,
            governance_state=GovernanceState.EFFECTIVE,
            retired_at__isnull=True,
        ).count(),
        "p_rate": {
            "expected_factor": str((1 + EXPECTED_TRANSPORT_PCT / 100).normalize()),
            "in_force": rates,
        },
        "open_import_id": str(open_import) if open_import else None,
    }


def is_stale(source: MasterSheetImport, now: datetime) -> bool:
    """The tenant's lists changed since this package was planned."""
    return content_hash(snapshot(source.tenant_id, now)) != source.base_hash


def review_hash(source: MasterSheetImport) -> str:
    return content_hash(
        {
            "source_hash": source.source_hash,
            "base_hash": source.base_hash,
            "selections": source.selections,
            "plan": source.plan,
        }
    )


def _replan(
    run: CommandRun, source: MasterSheetImport, current: dict[str, Any]
) -> None:
    source.base = {"version_ids": current["version_ids"]}
    source.base_hash = content_hash(current)
    source.plan = plan_changes(source.parsed, current, source.selections)
    source.reviewed_hash = review_hash(source)


def _record_review(run: CommandRun, source: MasterSheetImport) -> None:
    run.record(
        MasterSheetImportReview(
            source_import=source,
            revision=source.revision,
            content_hash=source.reviewed_hash,
            payload={
                "selections": source.selections,
                "base_hash": source.base_hash,
                "plan": source.plan,
            },
        )
    )


# ----------------------------------------------------------------------------- commands


def create_import(
    run: CommandRun, *, evidence: EvidenceObject, parsed: dict[str, Any]
) -> MasterSheetImport:
    """One package per open upload of the same file: a second upload returns the first."""
    found = MasterSheetImport.objects.filter(
        tenant_id=run.tenant_id, source_hash=evidence.sha256, state__in=OPEN_STATES
    ).first()
    if found is not None:
        run.audit_after = {"master_sheet_import_id": str(found.pk), "unchanged": True}
        return found
    assert run.principal.human_id is not None
    source = MasterSheetImport(
        tenant_id=run.tenant_id,
        source_evidence=evidence,
        source_hash=evidence.sha256,
        source_name=evidence.filename[:240],
        parsed=parsed,
        selections=carried_selections(run.tenant_id),
        uploaded_by_id=run.principal.human_id,
    )
    _replan(run, source, snapshot(run.tenant_id, run.now))
    source.save()
    _record_review(run, source)
    run.audit_after = {
        "master_sheet_import_id": str(source.pk),
        "source_hash": source.source_hash,
        "change_count": source.plan["change_count"],
    }
    return source


def carried_selections(tenant_id: uuid.UUID) -> dict[str, Any]:
    """What the last approved package left out stays left out until someone includes it.

    A sheet keeps its typos and unwanted brands season after season; the reviewer
    should not have to leave them out again on every upload.
    """
    last = (
        MasterSheetImport.objects.filter(
            tenant_id=tenant_id, state=MasterSheetImport.State.APPROVED
        )
        .order_by("-created_at", "-pk")
        .first()
    )
    if last is None:
        return {}
    return {
        key: last.selections[key]
        for key in ("skip_values", "skip_brands", "skip_seasons")
        if last.selections.get(key)
    }


def locked_import(
    run: CommandRun,
    pk: Any,
    expected: int | None,
    *,
    states: tuple[str, ...] = OPEN_STATES,
) -> MasterSheetImport:
    found = run.lock(
        LockRank.DOCUMENT,
        MasterSheetImport.objects.filter(tenant_id=run.tenant_id, pk=pk),
    )
    if not found:
        raise Refusal("NOT_FOUND", "That master sheet import was not found.")
    source = found[0]
    if expected is not None and expected != source.revision:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "This import changed; reload its current revision.",
            status=409,
        )
    if source.state not in states:
        raise Refusal(
            "STATE_CONFLICT",
            f"This import is {source.state} and can no longer change.",
            status=409,
        )
    return source


def _require_uploader(access: AccessContext, source: MasterSheetImport) -> None:
    if source.uploaded_by_id != access.human_id:
        raise Refusal(
            "ACTION_DENIED",
            "Only the person who uploaded this master sheet can change or submit it.",
            status=403,
        )


def clean_selections(raw: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """The reviewer's choices, checked against the plan they were made on."""
    if not isinstance(raw, dict) or set(raw) - SELECTION_KEYS:
        raise Refusal(
            "INVALID_REQUEST",
            "selections may hold only " + ", ".join(sorted(SELECTION_KEYS)) + ".",
        )

    def texts(value: Any, name: str) -> list[str]:
        if not isinstance(value, list) or len(value) > MAX_SELECTIONS:
            raise Refusal("INVALID_REQUEST", f"{name} must be a list of texts.")
        if not all(isinstance(v, str) and 0 < len(v) <= 240 for v in value):
            raise Refusal("INVALID_REQUEST", f"{name} must be a list of texts.")
        return sorted(set(value))

    out: dict[str, Any] = {}
    retirable = {
        n["value_id"]
        for d in plan.get("dimensions", [])
        for n in d.get("not_in_sheet", [])
    }
    if "retire" in raw:
        retire = texts(raw["retire"], "retire")
        unknown = [v for v in retire if v not in retirable]
        if unknown:
            raise Refusal(
                "INVALID_REQUEST",
                "Only a value that is not in the sheet can be retired.",
            )
        out["retire"] = retire
    if "skip_values" in raw:
        if not isinstance(raw["skip_values"], dict) or set(raw["skip_values"]) - set(
            DIMENSIONS
        ):
            raise Refusal("INVALID_REQUEST", "skip_values is keyed by list name.")
        out["skip_values"] = {
            dim: texts(v, f"skip_values.{dim}")
            for dim, v in sorted(raw["skip_values"].items())
        }
    for name in ("skip_brands", "skip_seasons", "acknowledged"):
        if name in raw:
            out[name] = texts(raw[name], name)
    if "rule_choices" in raw:
        choices = raw["rule_choices"]
        if not isinstance(choices, dict) or len(choices) > MAX_SELECTIONS:
            raise Refusal("INVALID_REQUEST", "rule_choices is keyed by ITEM.")
        cleaned: dict[str, dict[str, str]] = {}
        for item, picked in choices.items():
            if not isinstance(picked, dict) or set(picked) - {"sub_category", "type"}:
                raise Refusal(
                    "INVALID_REQUEST", "Each rule choice names sub_category or type."
                )
            if not all(isinstance(v, str) and len(v) <= 100 for v in picked.values()):
                raise Refusal("INVALID_REQUEST", "Each rule choice is a list value.")
            cleaned[match_key(str(item))] = dict(sorted(picked.items()))
        out["rule_choices"] = dict(sorted(cleaned.items()))
    return out


def update_selections(
    run: CommandRun,
    access: AccessContext,
    source: MasterSheetImport,
    raw: Any,
    expected: int | None,
) -> MasterSheetImport:
    """Change the reviewer's choices; a submitted package goes back to review."""
    source = locked_import(run, source.pk, expected)
    _require_uploader(access, source)
    source.selections = clean_selections(raw, source.plan)
    return _back_to_review(run, source)


def refresh(
    run: CommandRun,
    access: AccessContext,
    source: MasterSheetImport,
    expected: int | None,
) -> MasterSheetImport:
    """Re-plan against the lists as they are now."""
    source = locked_import(run, source.pk, expected)
    _require_uploader(access, source)
    return _back_to_review(run, source)


def _back_to_review(run: CommandRun, source: MasterSheetImport) -> MasterSheetImport:
    supersede_pending(run, SUBJECT, str(source.pk), APPROVE_ACTION)
    source.approval_request = None
    source.state = MasterSheetImport.State.REVIEW
    source.revision += 1
    _replan(run, source, snapshot(run.tenant_id, run.now))
    source.save()
    _record_review(run, source)
    run.audit_after = {
        "master_sheet_import_id": str(source.pk),
        "revision": source.revision,
        "reviewed_hash": source.reviewed_hash,
    }
    return source


def submit(
    run: CommandRun,
    access: AccessContext,
    source: MasterSheetImport,
    expected: int | None,
    reviewed_hash: str,
) -> MasterSheetImport:
    """Send the whole reviewed package for one approval by a different person."""
    source = locked_import(
        run, source.pk, expected, states=(MasterSheetImport.State.REVIEW,)
    )
    _require_uploader(access, source)
    if reviewed_hash != source.reviewed_hash:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "What you reviewed is no longer this import's plan.",
            status=409,
        )
    if is_stale(source, run.now):
        raise Refusal(
            "REVISION_SUPERSEDED",
            "The lists changed since you reviewed this import. Refresh it and review again.",
            status=409,
        )
    plan = source.plan
    if not plan["change_count"]:
        raise Refusal(
            "NOTHING_TO_CHANGE",
            "The lists already match this master sheet.",
            status=422,
        )
    if not all(w["acknowledged"] for w in plan["warnings"]):
        raise Refusal(
            "MASTER_SHEET_NOT_READY",
            "Acknowledge every warning before submitting.",
            status=422,
        )
    if any(b["include"] for b in plan["brands"]["to_create"]) or any(
        s["include"] for s in plan["seasons"]["to_create"]
    ):
        access.require(BRAND_ACTION)
    if plan["item_rules"]["new"] or plan["item_rules"]["changed"]:
        access.require(RULE_ACTION)
    source.approval_request = create_request(
        run,
        subject_kind=SUBJECT,
        subject_key=str(source.pk),
        revision=source.revision,
        reviewed_hash=source.reviewed_hash,
        requested_action=APPROVE_ACTION,
        site_id=None,
        require_distinct=True,
        title=f"Product lists update from {source.source_name}",
    )
    source.state = MasterSheetImport.State.SUBMITTED
    source.save(update_fields=["approval_request", "state"])
    run.audit_after = {
        "master_sheet_import_id": str(source.pk),
        "reviewed_hash": source.reviewed_hash,
        "approval_request_id": str(source.approval_request.pk),
    }
    return source


def withdraw(
    run: CommandRun,
    access: AccessContext,
    source: MasterSheetImport,
    expected: int | None,
) -> MasterSheetImport:
    source = locked_import(run, source.pk, expected)
    _require_uploader(access, source)
    supersede_pending(run, SUBJECT, str(source.pk), APPROVE_ACTION)
    source.state = MasterSheetImport.State.WITHDRAWN
    source.revision += 1
    source.save(update_fields=["state", "revision"])
    run.audit_after = {"master_sheet_import_id": str(source.pk), "state": source.state}
    return source


# ----------------------------------------------------------------------------- approval


def _decide(run: CommandRun, context: DecisionContext) -> dict[str, Any] | None:
    request = context.request
    source = locked_import(run, request.subject_key, None)
    if source.uploaded_by_id == context.checker_id:
        raise Refusal(
            "SELF_APPROVAL",
            "The person who uploaded this master sheet cannot approve it.",
        )
    if (
        source.state != MasterSheetImport.State.SUBMITTED
        or source.revision != request.revision
        or review_hash(source) != request.reviewed_hash
    ):
        raise Refusal(
            "REVISION_SUPERSEDED",
            "The import changed after it was submitted.",
            status=409,
        )
    if context.decision != "approve":
        source.state = MasterSheetImport.State.REVIEW
        source.approval_request = None
        source.save(update_fields=["state", "approval_request"])
        return {"state": source.state}
    if is_stale(source, run.now):
        raise Refusal(
            "REVISION_SUPERSEDED",
            "The lists changed after this import was submitted; it must be refreshed and "
            "submitted again.",
            status=409,
        )
    result = apply(run, source, checker_id=context.checker_id)
    source.state = MasterSheetImport.State.APPROVED
    source.approved_by_id = context.checker_id
    source.result = result
    source.save(update_fields=["state", "approved_by", "result"])
    return {"state": source.state, **result}


def apply(
    run: CommandRun, source: MasterSheetImport, *, checker_id: uuid.UUID
) -> dict[str, Any]:
    """Write one reviewed plan through the product's writers, in one command."""
    from masters.goods_config import normalise_scope
    from masters.goods_services import (
        config_scope_key,
        create_brand_like,
        freeze_config_version,
        validate_config_payload,
    )

    plan = source.plan
    versions: dict[str, str] = {}
    for dimension in plan["dimensions"]:
        if not dimension["changed"]:
            continue
        name = dimension["dimension"]
        owner = (
            ConfigVersion.objects.filter(
                tenant_id=run.tenant_id, kind="vocabulary", payload__dimension=name
            )
            .order_by("-effective_from", "-version")
            .first()
        )
        scope = normalise_scope(owner.scope if owner else {"scope_kind": "tenant"})
        scope_key = owner.scope_key if owner else config_scope_key(scope)
        payload = {
            "dimension": name,
            "values": dimension["values"],
            "effective_from": run.now.isoformat(),
        }
        validate_config_payload(
            "vocabulary",
            payload,
            tenant_id=run.tenant_id,
            scope_key=scope_key,
            as_of=run.now,
            scope=scope,
        )
        draft = ConfigDraft.objects.create(
            tenant_id=run.tenant_id,
            kind="vocabulary",
            scope=scope,
            scope_key=scope_key,
            payload=payload,
            effective_from=run.now,
            maker_id=source.uploaded_by_id,
            state=ConfigDraft.State.APPROVED,
        )
        version, _superseded = freeze_config_version(
            run,
            draft,
            checker_id=checker_id,
            source_revision=source.revision,
            source_hash=source.reviewed_hash,
            not_before=run.now,
        )
        versions[name] = str(version.pk)

    seasons = [
        str(
            create_brand_like(
                run,
                kind="season",
                family="master:season",
                model=Season,
                body={"code": row["code"], "name": row["name"]},
            ).pk
        )
        for row in plan["seasons"]["to_create"]
        if row["include"]
    ]
    brands = [
        str(
            create_brand_like(
                run,
                kind="brand",
                family="master:brand",
                model=Brand,
                body={"code": row["code"], "name": row["name"]},
            ).pk
        )
        for row in plan["brands"]["to_create"]
        if row["include"]
    ]

    current_versions = {
        dim: versions.get(dim) or vid for dim, vid in source.base["version_ids"].items()
    }
    created: list[str] = []
    corrected: list[str] = []
    for entry in [*plan["item_rules"]["new"], *plan["item_rules"]["changed"]]:
        for dimension in ("sub_category", "type"):
            rule = entry.get(dimension)
            if not rule:
                continue
            if rule["rule_id"]:
                row = run.lock(
                    LockRank.DOCUMENT,
                    SourceCrosswalk.objects.filter(
                        tenant_id=run.tenant_id, pk=rule["rule_id"]
                    ),
                )[0]
                if row.target_key == rule["target_id"]:
                    continue
                row.target_key = rule["target_id"]
                row.revision += 1
                save_master(
                    row,
                    conflict="That rule already exists.",
                    update_fields=["target_key", "revision"],
                )
                record_master_version(run, "crosswalk", row, reason_code=REASON)
                sync_crosswalk_exception(run, row)
                corrected.append(str(row.pk))
                continue
            version_id = current_versions.get(dimension)
            row = write_crosswalk(
                run,
                kind=dimension,
                issuer_key=ITEM_ISSUER,
                source_key=entry["item"][:240],
                target_key=rule["target_id"],
                config_version_id=uuid.UUID(version_id) if version_id else None,
                governance_state=GovernanceState.EFFECTIVE,
                reason_code=REASON,
            )
            created.append(str(row.pk))
    run.audit_after = {
        "master_sheet_import_id": str(source.pk),
        "config_versions": versions,
        "brands": len(brands),
        "seasons": len(seasons),
        "rules_created": len(created),
        "rules_corrected": len(corrected),
    }
    return {
        "config_versions": versions,
        "brand_ids": brands,
        "season_ids": seasons,
        "rule_ids": created,
        "corrected_rule_ids": corrected,
    }


def install() -> None:
    register_subject_handler(SUBJECT, APPROVE_ACTION, _decide, policy_governed=False)
