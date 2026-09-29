"""Stable merchandise identity for goods-v1 (design §3.4, §5.2, §5.3, §5.5).

A barcode is an alias, never the identity:

* ``identity_key`` hashes a style and its canonical, sorted distinguishing
  attribute tuple. Omitted size stays unknown; an explicit size must be a mapped
  vocabulary value; a defining field cannot be unknown.
* ``resolve_alias`` answers resolved / unknown / ambiguous from effective,
  in-period, site-or-unscoped aliases. It never chooses between several SKUs.
* ``record_pick`` freezes one person's choice against a draft revision or a scan
  and the exact candidate set they saw. SKUs are never merged or changed.
* A C-WHO proposal is a ``pending`` master bound to the PT draft being prepared,
  selectable only by that draft until C-PMO decides its ``master_proposal``
  approval through the one approvals decision command.

Vocabulary values carry no stored ID, so a value's ID is derived from its
dimension and stable ``value_key``: it survives new vocabulary versions, which
keeps identity keys stable.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from django.apps import apps
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils.dateparse import parse_datetime

from approvals.goods_models import ApprovalRequest
from approvals.goods_services import DecisionContext, create_request, register_subject_handler
from core.canonical import content_hash
from core.commands import CommandRun, LockRank, register_integrity_refusal
from core.goods_documents import lock_heads
from core.kernel_models import DocumentHead, DraftRevision
from core.refusals import Refusal, issue
from masters.goods_identity_models import (
    GovernanceState,
    IdentityPick,
    ProductSku,
    SkuAlias,
    SourceCrosswalk,
    Style,
)
from masters.goods_models import AliasRangeCounter, ConfigVersion
from masters.goods_models import MasterVersion as MasterVersionRow

MANAGE_ACTION = "product.master.manage"
PROPOSE_ACTION = "product.master.propose"
PROPOSAL_SUBJECT = "master_proposal"
PT_PURPOSES = frozenset({"receipt", "opening"})
ALIAS_TYPES = frozenset({"barcode", "vendor_code", "generated"})
CROSSWALK_KINDS = frozenset({"vendor", "brand", "subbrand"})

#: Attribute rules (store and warehouse operations PRD §5.4, OPS-14). A crosswalk
#: whose ``kind`` is a governed vocabulary dimension maps a source's words to one
#: approved value of that dimension; its ``issuer_key`` says whose words it reads:
#:
#: * ``*`` - any brand's file (a rule for everybody);
#: * ``brand:<id>`` - one brand's files only, checked before ``*``. With the
#:   source key ``*`` it is that brand's default when its file says nothing;
#: * ``keyword`` - a default carried by the description keyword that chose the
#:   ITEM (the source key is that keyword);
#: * ``item`` - read from the row's own ITEM value: the master sheet's
#:   ITEM -> SUB CATEGORY / TYPE helper (the source key is the ITEM value key).
#:
#: Master kinds keep their own issuer meaning (the vendor whose file it is).
ANY_ISSUER = "*"
BRAND_ISSUER_PREFIX = "brand:"
KEYWORD_ISSUER = "keyword"
ITEM_ISSUER = "item"
#: The source key of a brand's default (``brand:<id>`` issuer only).
DEFAULT_SOURCE = "*"

#: Database constraints that mean "this master already exists" (design §5.2).
CONFLICT_CONSTRAINTS = (
    "uq_style_code",
    "uq_productsku_identity",
    "ex_skualias_same_sku_overlap",
    "uq_sourcecrosswalk",
    # Inherited masters, unique within a tenant (change PRD §14.2).
    "uq_role_tenant_code",
    "uq_vendor_tenant_code",
    "uq_vendor_tenant_gstin",
    "uq_brand_tenant_code",
    "uq_legalentity_tenant_code",
    "uq_legalentity_tenant_pan",
    "uq_gstin_tenant_gstin",
    "uq_store_tenant_code",
)

VOCABULARY_NAMESPACE = uuid.UUID("6f1d3c52-8a4e-4f7b-9c1e-5b2a7d9e0c41")
ATTR_KEYS = frozenset({"field_id", "vocabulary_value_id", "supplied_text", "unknown"})


# -- small parsers ------------------------------------------------------------


def invalid(message: str, *, field: str | None = None) -> Refusal:
    return Refusal(
        "INVALID_REQUEST",
        message,
        issues=[issue("INVALID", message, field=field)] if field else None,
    )


def master_invalid(message: str, *, field: str | None = None, code: str = "INVALID") -> Refusal:
    return Refusal(
        "MASTER_INVALID", message, status=422, issues=[issue(code, message, field=field)]
    )


def parse_timestamp(value: Any, field: str) -> datetime:
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None or parsed.tzinfo is None:
        raise invalid(f"{field} must be a timestamp with a time zone offset.", field=field)
    return parsed.astimezone(UTC)


def bounded_text(value: Any, field: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise invalid(f"{field} must be text.", field=field)
    text = value.strip()
    if (not text and not allow_empty) or len(text) > maximum:
        raise invalid(f"{field} must be 1 to {maximum} characters.", field=field)
    return text


def ts(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def opt_id(value: Any) -> str | None:
    return None if value is None else str(value)


# -- vocabulary and profiles -------------------------------------------------


def vocabulary_value_id(dimension: str, value_key: str) -> uuid.UUID:
    """The stable ID of one vocabulary value (its dimension and ``value_key``)."""
    return uuid.uuid5(VOCABULARY_NAMESPACE, f"{dimension}\x1f{value_key}")


@dataclass(frozen=True)
class VocabularyValue:
    id: uuid.UUID
    dimension: str
    value_key: str
    label: str
    sort_order: int
    retired: bool
    config_version_id: uuid.UUID
    version: int


def effective_configs(tenant_id: uuid.UUID, kind: str, as_of: datetime) -> list[ConfigVersion]:
    from masters.goods_config import effective_at

    return effective_at(tenant_id, kind, as_of)


def _payload(version: ConfigVersion) -> dict[str, Any]:
    return version.payload if isinstance(version.payload, dict) else {}


def _values(version: ConfigVersion) -> list[VocabularyValue]:
    payload = _payload(version)
    dimension = str(payload.get("dimension") or "")
    out: list[VocabularyValue] = []
    for raw in payload.get("values") or []:
        if not isinstance(raw, dict) or not raw.get("value_key"):
            continue
        key = str(raw["value_key"])
        out.append(
            VocabularyValue(
                id=vocabulary_value_id(dimension, key),
                dimension=dimension,
                value_key=key,
                label=str(raw.get("label") or key),
                sort_order=int(raw.get("sort_order") or 0),
                retired=bool(raw.get("retired")),
                config_version_id=version.pk,
                version=version.version,
            )
        )
    out.sort(key=lambda v: (v.sort_order, v.value_key))
    return out


def vocabulary(
    tenant_id: uuid.UUID, as_of: datetime, dimension: str | None = None
) -> dict[str, list[VocabularyValue]]:
    """Each dimension's values from its latest effective ``vocabulary`` version."""
    latest: dict[str, ConfigVersion] = {}
    for version in effective_configs(tenant_id, "vocabulary", as_of):
        name = str(_payload(version).get("dimension") or "")
        if not name or (dimension is not None and name != dimension):
            continue
        if name in latest:
            # One vocabulary per dimension is in force at a time; two means history that
            # was never reconciled, and no value list is guessed from it.
            raise Refusal(
                "CONFIG_INVALID",
                f"More than one {name} vocabulary is in force.",
                status=422,
                issues=[issue("CONFIG_AMBIGUOUS", "more than one vocabulary", field=name)],
            )
        latest[name] = version
    return {name: _values(version) for name, version in latest.items()}


def brand_issuer(brand_id: int) -> str:
    return f"{BRAND_ISSUER_PREFIX}{brand_id}"


def parse_rule_issuer(issuer_key: str) -> tuple[str, int | None] | None:
    """(``any``/``brand``/``keyword``/``item``, brand ID) of an attribute rule's issuer.

    None when the issuer is not one an attribute rule may name.
    """
    if issuer_key == ANY_ISSUER:
        return "any", None
    if issuer_key in (KEYWORD_ISSUER, ITEM_ISSUER):
        return issuer_key, None
    if issuer_key.startswith(BRAND_ISSUER_PREFIX):
        raw = issuer_key[len(BRAND_ISSUER_PREFIX) :]
        if raw.isdigit() and not raw.startswith("0"):
            return "brand", int(raw)
    return None


def is_attribute_kind(kind: str) -> bool:
    """A crosswalk kind that names a vocabulary dimension rather than a master."""
    return kind not in CROSSWALK_KINDS


def rule_brand(kind: str, issuer_key: str, target_key: str) -> int | None:
    """The one brand a crosswalk belongs to, for scope checks; None when tenant-wide.

    A brand crosswalk belongs to its target brand; an attribute rule to the brand
    whose files it reads (``brand:<id>``). Every other crosswalk is tenant-wide.
    """
    if kind == "brand":
        return int(target_key) if target_key.isdigit() else None
    if not is_attribute_kind(kind):
        return None
    parsed = parse_rule_issuer(issuer_key)
    return parsed[1] if parsed is not None else None


def attribute_dimensions(tenant_id: uuid.UUID, as_of: datetime) -> frozenset[str]:
    """The governed vocabulary dimensions an attribute rule may name now."""
    return frozenset(set(vocabulary(tenant_id, as_of)) - CROSSWALK_KINDS)


def attribute_target(
    tenant_id: uuid.UUID, dimension: str, target_key: str, as_of: datetime
) -> VocabularyValue | None:
    """The approved, unretired vocabulary value an attribute rule's target names."""
    for value in vocabulary(tenant_id, as_of, dimension).get(dimension, []):
        if str(value.id) == target_key and not value.retired:
            return value
    return None


def value_labels(tenant_id: uuid.UUID) -> dict[str, str]:
    """Labels for every value ever published; retired values stay readable."""
    labels: dict[str, str] = {}
    for version in ConfigVersion.objects.filter(tenant_id=tenant_id, kind="vocabulary").order_by(
        "effective_from", "version"
    ):
        for value in _values(version):
            labels[str(value.id)] = value.label
    return labels


@dataclass(frozen=True)
class IdentityProfile:
    version_id: uuid.UUID
    family: str
    distinguishing: tuple[str, ...]
    size_dimension: str
    colour_dimension: str | None
    grade_dimension: str | None
    allowed: dict[str, frozenset[str]]

    @property
    def identity_fields(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.distinguishing) | {self.size_dimension}))

    @property
    def dimensions(self) -> tuple[str, ...]:
        extra = {d for d in (self.colour_dimension, self.grade_dimension) if d}
        return tuple(sorted(set(self.identity_fields) | extra))


def _id_set(raw: Any) -> frozenset[str]:
    out: set[str] = set()
    items: list[Any] = raw if isinstance(raw, list) else []
    for item in items:
        try:
            out.add(str(uuid.UUID(str(item))))
        except ValueError:
            continue
    return frozenset(out)


def profile_from_version(version: ConfigVersion) -> IdentityProfile | None:
    if version.kind != "identity_profile":
        return None
    payload = _payload(version)
    size = str(payload.get("size_dimension") or "")
    family = str(payload.get("family") or "")
    if not size or not family:
        return None
    colour = payload.get("colour_dimension") or None
    grade = payload.get("grade_dimension") or None
    allowed: dict[str, frozenset[str]] = {size: _id_set(payload.get("allowed_size_values"))}
    if colour:
        allowed[str(colour)] = _id_set(payload.get("allowed_colour_values"))
    if grade:
        allowed[str(grade)] = _id_set(payload.get("allowed_grade_values"))
    return IdentityProfile(
        version_id=version.pk,
        family=family,
        distinguishing=tuple(str(d) for d in payload.get("distinguishing_dimensions") or []),
        size_dimension=size,
        colour_dimension=str(colour) if colour else None,
        grade_dimension=str(grade) if grade else None,
        allowed=allowed,
    )


def profile_context(
    tenant_id: uuid.UUID, version_id: uuid.UUID
) -> tuple[ConfigVersion, str, tuple[str, ...]] | None:
    """(version, family, vocabulary dimensions) of an identity or PT profile version."""
    version = (
        ConfigVersion.objects.filter(
            tenant_id=tenant_id, pk=version_id, kind__in=["identity_profile", "profile"]
        )
        .order_by()
        .first()
    )
    if version is None:
        return None
    from masters.goods_config import candidate

    if candidate(version).withdrawn_at is not None:
        return None
    profile = profile_from_version(version)
    if profile is not None:
        return version, profile.family, profile.dimensions
    payload = _payload(version)
    raw_columns = payload.get("columns")
    columns: list[Any] = raw_columns if isinstance(raw_columns, list) else []
    dimensions = tuple(
        sorted(
            str(column.get("key"))
            for column in columns
            if isinstance(column, dict)
            and column.get("logical_type") == "vocabulary"
            and column.get("key")
        )
    )
    return version, str(payload.get("family") or ""), dimensions


# -- attribute values and the identity key -----------------------------------


def parse_attrs(raw: Any, field: str = "attrs") -> list[dict[str, Any]]:  # noqa: C901 - one check per closed-schema rule
    """A closed ``AttributeValues`` list: exactly one value form or explicit unknown."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 1000:
        raise invalid(f"{field} must be a list of attribute values.", field=field)
    out: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw):
        where = f"{field}[{index}]"
        if not isinstance(item, dict) or set(item) - ATTR_KEYS or "field_id" not in item:
            raise invalid(f"{where} must be an attribute value object.", field=where)
        field_id = bounded_text(item["field_id"], f"{where}.field_id", 60)
        unknown = item.get("unknown", False)
        if not isinstance(unknown, bool):
            raise invalid(f"{where}.unknown must be true or false.", field=where)
        value_id = item.get("vocabulary_value_id")
        text = item.get("supplied_text")
        if (value_id is not None) + (text is not None) + unknown != 1:
            raise invalid(
                f"{where} needs exactly one of vocabulary_value_id, supplied_text or unknown.",
                field=where,
            )
        entry: dict[str, Any] = {"field_id": field_id, "unknown": unknown}
        if value_id is not None:
            try:
                entry["vocabulary_value_id"] = str(uuid.UUID(str(value_id)))
            except ValueError:
                raise invalid(f"{where}.vocabulary_value_id must be an ID.", field=where) from None
        if text is not None:
            entry["supplied_text"] = bounded_text(text, f"{where}.supplied_text", 240)
        if field_id in out:
            raise invalid(f"{field_id} appears more than once.", field=where)
        out[field_id] = entry
    return [out[key] for key in sorted(out)]


def normalise_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def identity_key(style_id: uuid.UUID, attrs: list[dict[str, Any]]) -> str:
    """SHA-256 of the style and its canonical, sorted distinguishing attribute tuple.

    ``attrs`` are the distinguishing entries only; an entry with neither a
    vocabulary value nor supplied text is unknown.
    """
    parts: list[list[str]] = []
    for entry in attrs:
        field_id = str(entry["field_id"])
        if entry.get("vocabulary_value_id"):
            parts.append([field_id, "value", str(uuid.UUID(str(entry["vocabulary_value_id"])))])
        elif entry.get("supplied_text") is not None:
            parts.append([field_id, "text", normalise_text(str(entry["supplied_text"]))])
        else:
            parts.append([field_id, "unknown", ""])
    parts.sort()
    return content_hash({"style_id": str(style_id), "attrs": parts})


def check_vocabulary_refs(
    tenant_id: uuid.UUID, attrs: list[dict[str, Any]], as_of: datetime
) -> list[dict[str, Any]]:
    """Issues for vocabulary references that are not selectable effective values."""
    wanted = {e["field_id"] for e in attrs if e.get("vocabulary_value_id")}
    if not wanted:
        return []
    current = vocabulary(tenant_id, as_of)
    problems: list[dict[str, Any]] = []
    for entry in attrs:
        value_id = entry.get("vocabulary_value_id")
        if not value_id:
            continue
        values = {str(v.id): v for v in current.get(entry["field_id"], [])}
        value = values.get(value_id)
        if value is None or value.retired:
            problems.append(
                issue(
                    "VOCABULARY_VALUE_INVALID",
                    f"That {entry['field_id']} value is not an effective vocabulary value.",
                    field=f"attrs.{entry['field_id']}",
                )
            )
    return problems


def sku_identity(
    tenant_id: uuid.UUID,
    style: Style,
    profile: IdentityProfile,
    attrs: list[dict[str, Any]],
    as_of: datetime,
) -> tuple[str, list[dict[str, Any]]]:
    """Validate SKU attributes under the profile: (identity_key, stored attributes)."""
    if style.profile_family != profile.family:
        raise master_invalid(
            "The identity profile is for a different product family than the style.",
            field="profile_version_id",
        )
    by_field = {entry["field_id"]: dict(entry) for entry in attrs}
    problems = check_vocabulary_refs(tenant_id, attrs, as_of)
    for entry in attrs:
        allowed = profile.allowed.get(entry["field_id"])
        value_id = entry.get("vocabulary_value_id")
        if allowed and value_id and value_id not in allowed:
            problems.append(
                issue(
                    "VALUE_NOT_ALLOWED",
                    f"That {entry['field_id']} value is not allowed by the identity profile.",
                    field=f"attrs.{entry['field_id']}",
                )
            )
    size = by_field.get(profile.size_dimension)
    if size is None:
        by_field[profile.size_dimension] = {"field_id": profile.size_dimension, "unknown": True}
    elif size.get("supplied_text") is not None:
        problems.append(
            issue(
                "SIZE_NOT_MAPPED",
                "A size must be a mapped vocabulary value; leave it out to keep size unknown.",
                field=f"attrs.{profile.size_dimension}",
            )
        )
    for dimension in profile.distinguishing:
        if dimension == profile.size_dimension:
            continue
        defining = by_field.get(dimension)
        if defining is None or defining.get("unknown"):
            problems.append(
                issue(
                    "DEFINING_FIELD_UNKNOWN",
                    f"{dimension} defines the SKU and cannot be unknown.",
                    field=f"attrs.{dimension}",
                )
            )
    if problems:
        raise Refusal(
            "MASTER_INVALID",
            "The SKU attributes do not satisfy the identity profile.",
            status=422,
            issues=problems,
        )
    stored = [by_field[key] for key in sorted(by_field)]
    distinguishing = [by_field[key] for key in profile.identity_fields if key in by_field]
    return identity_key(style.pk, distinguishing), stored


# -- DTO data ------------------------------------------------------------------


def style_data(row: Style) -> dict[str, Any]:
    return {
        "brand_id": str(row.brand_id),
        "style_code": row.style_code,
        "profile_family": row.profile_family,
        "attrs": list(row.attrs or []),
    }


def sku_data(row: ProductSku) -> dict[str, Any]:
    return {
        "style_id": str(row.style_id),
        "profile_version_id": opt_id(row.identity_profile_id),
        "attrs": list(row.attrs or []),
    }


def alias_data(row: SkuAlias) -> dict[str, Any]:
    return {
        "sku_id": str(row.sku_id),
        "issuer_key": row.issuer_key,
        "alias_type": row.alias_type,
        "value": row.value,
        "site_id": opt_id(row.site_id),
        "effective_from": ts(row.effective_from),
        "effective_to": ts(row.effective_to),
    }


def crosswalk_data(row: SourceCrosswalk) -> dict[str, Any]:
    return {
        "kind": row.kind,
        "issuer_key": row.issuer_key,
        "source_key": row.source_key,
        "target_key": row.target_key,
        "config_version_id": opt_id(row.config_version_id),
    }


MASTER_MODELS: dict[str, type[models.Model]] = {
    "style": Style,
    "sku": ProductSku,
    "alias": SkuAlias,
    "crosswalk": SourceCrosswalk,
}


def master_data(kind: str, row: Any) -> dict[str, Any]:
    if kind == "style":
        return style_data(row)
    if kind == "sku":
        return sku_data(row)
    if kind == "alias":
        return alias_data(row)
    return crosswalk_data(row)


def record_master_version(
    run: CommandRun,
    kind: str,
    row: Any,
    *,
    retired: bool = False,
    reason_code: str | None = None,
    approval_id: uuid.UUID | None = None,
    effective_from: datetime | None = None,
) -> None:
    run.record(
        MasterVersionRow(
            kind=kind,
            target_key=str(row.pk),
            revision=row.revision,
            payload={"data": master_data(kind, row), "governance_state": row.governance_state},
            retired=retired,
            reason_code=reason_code,
            effective_from=effective_from or run.now,
            approval_id=approval_id,
        )
    )


def save_master(
    row: models.Model, *, conflict: str, update_fields: list[str] | None = None
) -> None:
    """Write a master projection row; a uniqueness/overlap violation is MASTER_CONFLICT."""
    try:
        with transaction.atomic():
            if update_fields is None:
                row.save()
            else:
                row.save(update_fields=update_fields)
    except IntegrityError as exc:
        if any(name in str(exc) for name in CONFLICT_CONSTRAINTS):
            raise Refusal("MASTER_CONFLICT", conflict, status=409) from exc
        raise


# -- draft lineage and selectability -------------------------------------------


def lineage_revision_ids(tenant_id: uuid.UUID, revision_id: uuid.UUID | None) -> list[uuid.UUID]:
    """Revisions of the same draft: a proposal stays usable while that draft is edited."""
    if revision_id is None:
        return []
    row = (
        DraftRevision.objects.filter(tenant_id=tenant_id, pk=revision_id)
        .values("document_id")
        .first()
    )
    if row is None:
        return []
    if row["document_id"] is None:
        return [revision_id]
    return list(
        DraftRevision.objects.filter(
            tenant_id=tenant_id, document_id=row["document_id"]
        ).values_list("pk", flat=True)
    )


def usable_q(
    as_of: datetime, lineage: list[uuid.UUID], *, prefix: str = "", retired_field: bool = True
) -> Q:
    state = Q(**{f"{prefix}governance_state": GovernanceState.EFFECTIVE})
    if lineage:
        state |= Q(
            **{
                f"{prefix}governance_state": GovernanceState.PENDING,
                f"{prefix}originating_revision_id__in": lineage,
            }
        )
    if not retired_field:
        return state
    return state & (
        Q(**{f"{prefix}retired_at__isnull": True}) | Q(**{f"{prefix}retired_at__gt": as_of})
    )


def is_usable(row: Any, as_of: datetime, lineage: list[uuid.UUID]) -> bool:
    retired_at = getattr(row, "retired_at", None)
    if retired_at is not None and retired_at <= as_of:
        return False
    if row.governance_state == GovernanceState.EFFECTIVE:
        return True
    return bool(
        row.governance_state == GovernanceState.PENDING
        and row.originating_revision_id in set(lineage)
    )


def preparing_revision(run: CommandRun, revision: DraftRevision) -> DocumentHead:
    """Lock the PT draft a proposal cites and require it to be the one being prepared."""
    document_id = revision.document_id
    head = lock_heads(run, [document_id]).get(document_id) if document_id else None
    document = revision.document
    if (
        head is None
        or document is None
        or document.purpose not in PT_PURPOSES
        or head.state != DocumentHead.State.DRAFT
        or head.draft_revision_id != revision.pk
    ):
        raise master_invalid(
            "A proposal can only be made while preparing the current draft of a PT.",
            field="originating_revision_id",
        )
    return head


# -- master proposals and their approval ---------------------------------------


def proposal_subject_key(kind: str, row_id: Any) -> str:
    return f"{kind}:{row_id}"


def open_proposal(
    run: CommandRun,
    *,
    kind: str,
    row: Any,
    site_id: int | None,
    brand_id: int | None,
    new_subject: bool = False,
) -> ApprovalRequest:
    data = master_data(kind, row)
    label = data.get("style_code") or data.get("value") or str(row.pk)
    return create_request(
        run,
        subject_kind=PROPOSAL_SUBJECT,
        subject_key=proposal_subject_key(kind, row.pk),
        revision=row.revision,
        reviewed_hash=content_hash(data),
        requested_action=MANAGE_ACTION,
        site_id=site_id,
        brand_id=brand_id,
        title=f"Proposed {kind} {label}",
        new_subject=new_subject,
    )


def latest_proposal_request(kind: str, row_id: Any) -> ApprovalRequest | None:
    return (
        ApprovalRequest.objects.filter(
            subject_kind=PROPOSAL_SUBJECT, subject_key=proposal_subject_key(kind, row_id)
        )
        .order_by("-created_at")
        .first()
    )


def decide_master_proposal(run: CommandRun, context: DecisionContext) -> dict[str, Any] | None:
    """E234 subject command for ``master_proposal``: approve makes it effective."""
    kind, _, raw_id = context.request.subject_key.partition(":")
    model = MASTER_MODELS.get(kind)
    if model is None or kind == "crosswalk":
        raise Refusal("NOT_FOUND", "That proposal was not found.")
    try:
        row_id = uuid.UUID(raw_id)
    except ValueError:
        raise Refusal("NOT_FOUND", "That proposal was not found.") from None
    rows = run.lock(LockRank.DOCUMENT, model._default_manager.filter(pk=row_id))
    if not rows:
        raise Refusal("NOT_FOUND", "That proposal was not found.")
    row: Any = rows[0]
    if row.governance_state != GovernanceState.PENDING:
        raise Refusal("STATE_CONFLICT", "This proposal has already been decided.")
    if content_hash(master_data(kind, row)) != context.request.reviewed_hash:
        raise Refusal("APPROVAL_STALE", "The proposal changed after it was submitted.")
    if context.decision == "approve":
        if kind == "sku" and row.style.governance_state != GovernanceState.EFFECTIVE:
            raise master_invalid("Confirm the style this SKU belongs to first.")
        if kind == "alias" and row.sku.governance_state != GovernanceState.EFFECTIVE:
            raise master_invalid("Confirm the SKU this alias points to first.")
        row.governance_state = GovernanceState.EFFECTIVE
        fields = ["governance_state", "revision"]
    else:
        row.governance_state = GovernanceState.RETIRED
        fields = ["governance_state", "revision"]
        if kind in ("style", "sku"):
            row.retired_at = run.now
            fields.append("retired_at")
    row.revision += 1
    row.save(update_fields=fields)
    record_master_version(
        run,
        kind,
        row,
        retired=context.decision != "approve",
        reason_code=context.reason_code,
        approval_id=context.request.pk,
    )
    run.audit_after = {"kind": kind, "id": str(row.pk), "governance_state": row.governance_state}
    return {"kind": kind, "id": str(row.pk), "governance_state": row.governance_state}


# -- generated aliases ---------------------------------------------------------


def range_bounds(version: ConfigVersion) -> tuple[str, str, int, int]:
    payload = _payload(version)
    try:
        issuer = str(payload["issuer"])
        prefix = str(payload.get("prefix") or "")
        start = int(payload["start"])
        end = int(payload["end"])
    except (KeyError, TypeError, ValueError):
        raise master_invalid(
            "That barcode range is not a valid approved range.", field="range_version_id"
        ) from None
    if not issuer or start < 0 or end < start or len(prefix) + len(str(end)) > 128:
        raise master_invalid(
            "That barcode range is not a valid approved range.", field="range_version_id"
        )
    return issuer, prefix, start, end


def allocate_generated_value(run: CommandRun, version: ConfigVersion) -> tuple[str, str]:
    """Issue the next unused value of an approved range and advance its counter once.

    The counter is a config-version guard, so it is locked at DRAFT rank (design
    §4.1, §5.2 AliasRangeCounter) - after any document identities the command holds.
    """
    issuer, prefix, start, end = range_bounds(version)
    run.advisory_lock(LockRank.DRAFT, [f"alias-range:{version.pk}"])
    counters = run.lock(
        LockRank.DRAFT,
        AliasRangeCounter.objects.filter(tenant_id=run.tenant_id, range_version_id=version.pk),
    )
    counter = (
        counters[0]
        if counters
        else AliasRangeCounter.objects.create(
            tenant_id=run.tenant_id, range_version_id=version.pk, next_value=start
        )
    )
    width = len(str(end))
    candidate = max(counter.next_value, start)
    value = ""
    while candidate <= end:
        value = f"{prefix}{candidate:0{width}d}"
        if not SkuAlias.objects.filter(
            tenant_id=run.tenant_id, issuer_key=issuer, value=value
        ).exists():
            break
        candidate += 1
    else:
        raise Refusal(
            "ALIAS_RANGE_EXHAUSTED",
            "Every value in this barcode range has been issued; an owner must approve a new range.",
            status=409,
        )
    counter.next_value = candidate + 1
    counter.save(update_fields=["next_value"])
    return issuer, value


# -- resolution and picks ------------------------------------------------------


@dataclass(frozen=True)
class IdentityResolution:
    result: str  # "resolved" | "unknown" | "ambiguous"
    candidates: list[dict[str, Any]]  # {sku_id, brand, style, colour?, grade?, size}
    candidate_hash: str
    chosen_sku_id: uuid.UUID | None
    issues: list[dict[str, Any]]

    def as_dto(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "candidate_hash": self.candidate_hash,
            "candidates": self.candidates,
            "chosen_sku_id": opt_id(self.chosen_sku_id),
            "issues": self.issues,
        }


def candidate_set_hash(value: str, sku_ids: Iterable[Any]) -> str:
    return content_hash({"value": value, "sku_ids": sorted(str(s) for s in sku_ids)})


def candidates_for(tenant_id: uuid.UUID, sku_ids: Iterable[Any]) -> list[dict[str, Any]]:
    ids = sorted({str(s) for s in sku_ids})
    if not ids:
        return []
    labels = value_labels(tenant_id)
    skus = {
        str(sku.pk): sku
        for sku in ProductSku.objects.select_related(
            "style", "style__brand", "identity_profile"
        ).filter(tenant_id=tenant_id, pk__in=ids)
    }
    out: list[dict[str, Any]] = []
    for sku_id in ids:
        sku = skus.get(sku_id)
        if sku is None:
            continue
        profile = profile_from_version(sku.identity_profile) if sku.identity_profile else None
        by_field = {e.get("field_id"): e for e in sku.attrs or [] if isinstance(e, dict)}

        def label_of(dimension: str | None, attrs: dict[Any, Any] = by_field) -> str | None:
            entry = attrs.get(dimension) if dimension else None
            if entry is None or entry.get("unknown"):
                return None
            if entry.get("vocabulary_value_id"):
                value_id = str(entry["vocabulary_value_id"])
                return labels.get(value_id, value_id)
            return str(entry.get("supplied_text") or "") or None

        item: dict[str, Any] = {
            "sku_id": sku_id,
            "brand": sku.style.brand.name,
            "style": sku.style.style_code,
            "size": (label_of(profile.size_dimension) if profile else None) or "unknown",
        }
        colour = label_of(profile.colour_dimension) if profile else None
        grade = label_of(profile.grade_dimension) if profile else None
        if colour is not None:
            item["colour"] = colour
        if grade is not None:
            item["grade"] = grade
        out.append(item)
    return out


@dataclass(frozen=True)
class SkuBookingFacts:
    """What a booking line is matched on: the item's style, brand, size and colour.

    ``size_value_id`` and ``colour_value_id`` are the governed values the item's
    identity profile names for its size and colour, or ``None`` when the item does
    not say (unknown, free text, or no such dimension).
    """

    style_code: str
    brand_id: int
    size_value_id: str | None
    colour_value_id: str | None


def sku_booking_facts(tenant_id: uuid.UUID, sku_ids: Iterable[Any]) -> dict[str, SkuBookingFacts]:
    """Each SKU's :class:`SkuBookingFacts`, keyed by SKU id; unknown ids are left out."""
    ids = sorted({str(s) for s in sku_ids})
    if not ids:
        return {}
    out: dict[str, SkuBookingFacts] = {}
    for sku in ProductSku.objects.select_related("style", "identity_profile").filter(
        tenant_id=tenant_id, pk__in=ids
    ):
        profile = profile_from_version(sku.identity_profile) if sku.identity_profile else None
        by_field = {e.get("field_id"): e for e in sku.attrs or [] if isinstance(e, dict)}

        def governed(dimension: str | None, attrs: dict[Any, Any] = by_field) -> str | None:
            entry = attrs.get(dimension) if dimension else None
            if entry is None or entry.get("unknown") or not entry.get("vocabulary_value_id"):
                return None
            return str(entry["vocabulary_value_id"])

        out[str(sku.pk)] = SkuBookingFacts(
            style_code=sku.style.style_code,
            brand_id=sku.style.brand_id,
            size_value_id=governed(profile.size_dimension) if profile else None,
            colour_value_id=governed(profile.colour_dimension) if profile else None,
        )
    return out


def resolve_alias(
    tenant_id: uuid.UUID,
    *,
    value: str,
    site_id: int | None,
    as_of: datetime,
    issuer_key: str | None = None,
    alias_type: str | None = None,
    subject_revision_id: uuid.UUID | None = None,
    brand_ids: set[int] | None = None,
    profile_family: str | None = None,
) -> IdentityResolution:
    """Candidates for a scanned or typed code; never chooses between several SKUs.

    Effective aliases (and pending ones only for their originating draft) whose
    period contains ``as_of``: site-scoped ones for ``site_id`` plus unscoped ones.
    ``brand_ids``/``profile_family`` narrow to the caller's authorised scope.
    """
    lineage = lineage_revision_ids(tenant_id, subject_revision_id)
    aliases = SkuAlias.objects.filter(
        tenant_id=tenant_id, value=value, effective_from__lte=as_of
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=as_of))
    if site_id is None:
        aliases = aliases.filter(site__isnull=True)
    else:
        aliases = aliases.filter(Q(site__isnull=True) | Q(site_id=site_id))
    if issuer_key is not None:
        aliases = aliases.filter(issuer_key=issuer_key)
    if alias_type is not None:
        aliases = aliases.filter(alias_type=alias_type)
    aliases = (
        aliases.filter(usable_q(as_of, lineage, retired_field=False))
        .filter(usable_q(as_of, lineage, prefix="sku__"))
        .filter(usable_q(as_of, lineage, prefix="sku__style__"))
    )
    if brand_ids is not None:
        aliases = aliases.filter(sku__style__brand_id__in=sorted(brand_ids))
    if profile_family is not None:
        aliases = aliases.filter(sku__style__profile_family=profile_family)
    sku_ids = sorted({str(s) for s in aliases.values_list("sku_id", flat=True)})
    candidates = candidates_for(tenant_id, sku_ids)
    digest = candidate_set_hash(value, [c["sku_id"] for c in candidates])
    if not candidates:
        return IdentityResolution(
            "unknown",
            [],
            digest,
            None,
            [issue("IDENTITY_UNKNOWN", "No SKU is known by this code here and now.")],
        )
    if len(candidates) == 1:
        return IdentityResolution(
            "resolved", candidates, digest, uuid.UUID(candidates[0]["sku_id"]), []
        )
    return IdentityResolution(
        "ambiguous",
        candidates,
        digest,
        None,
        [issue("IDENTITY_AMBIGUOUS", "Several SKUs share this code; choose the right one.")],
    )


def _context_site(context: dict[str, Any]) -> int | None:
    raw = context.get("site_id")
    if raw is None:
        return None
    try:
        return int(str(raw))
    except ValueError:
        raise invalid("context.site_id must be an ID.", field="context.site_id") from None


def _context_uuid(raw: Any, field: str) -> uuid.UUID | None:
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise invalid(f"{field} must be an ID.", field=field) from None


def record_pick(
    run: CommandRun,
    *,
    value: str,
    candidate_hash: str,
    chosen_sku_id: uuid.UUID,
    subject_revision_id: uuid.UUID | None = None,
    scan_event_id: uuid.UUID | None = None,
    context: dict[str, Any],
    brand_ids: set[int] | None = None,
    profile_family: str | None = None,
) -> IdentityPick:
    """Freeze one choice among the reviewed candidates against a revision or scan."""
    if (subject_revision_id is None) == (scan_event_id is None):
        raise invalid("A pick belongs to exactly one draft revision or one scan.")
    if (
        subject_revision_id is not None
        and not DraftRevision.objects.filter(
            tenant_id=run.tenant_id, pk=subject_revision_id
        ).exists()
    ):
        raise Refusal("NOT_FOUND", "That draft revision was not found.")
    if scan_event_id is not None:
        scans = apps.get_model("inbound", "ScanObservation")._default_manager
        if not scans.filter(tenant_id=run.tenant_id, pk=scan_event_id).exists():
            raise Refusal("NOT_FOUND", "That scan was not found.")
    raw_as_of = context.get("as_of")
    as_of = raw_as_of if isinstance(raw_as_of, datetime) else parse_timestamp(raw_as_of, "as_of")
    lineage_revision = subject_revision_id or _context_uuid(
        context.get("subject_revision_id"), "context.subject_revision_id"
    )
    current = resolve_alias(
        run.tenant_id,
        value=value,
        site_id=_context_site(context),
        as_of=as_of,
        issuer_key=context.get("issuer_key"),
        alias_type=context.get("alias_type"),
        subject_revision_id=lineage_revision,
        brand_ids=brand_ids,
        profile_family=profile_family,
    )
    ids = [c["sku_id"] for c in current.candidates]
    if current.candidate_hash != candidate_hash or str(chosen_sku_id) not in ids:
        raise Refusal(
            "IDENTITY_PICK_STALE",
            "The possible SKUs changed since you looked; look the code up again and choose.",
            status=409,
        )
    frozen_context = {
        key: (ts(item) if isinstance(item, datetime) else opt_id(item))
        for key, item in context.items()
    }
    pick = IdentityPick(
        subject_revision_id=subject_revision_id,
        scan_event_id=scan_event_id,
        value=value,
        candidates=ids,
        candidate_hash=candidate_hash,
        chosen_sku_id=chosen_sku_id,
        context=frozen_context,
    )
    run.record(pick)
    if scan_event_id is not None:
        # GSA-T04: the choice this scan was waiting for. The owned work closes
        # through the governed pick itself, never through a note about it.
        from alerts.goods_services import resolve_exceptions

        resolve_exceptions(
            run,
            kind="identity_ambiguity",
            subject_key=f"identity:{scan_event_id}",
            reason_code="IDENTITY_CHOSEN",
        )
    run.audit_after = {"value": value, "candidates": ids, "chosen_sku_id": str(chosen_sku_id)}
    return pick


#: A stable name for each crosswalk's unresolved-mapping work, so correcting a
#: row twice names the same work rather than a second row.
CROSSWALK_EXCEPTION_NAMESPACE = uuid.UUID("3d9b7c14-8a25-5f60-b4e7-21c0d5938ab6")


def sync_crosswalk_exception(run: CommandRun, row: Any) -> None:
    """Own the work a source key mapped to nothing leaves behind, and close it when it is done.

    GSA-T04: "unresolved master, crosswalk or identity work routes to C-PMO".
    A crosswalk with an empty ``target_key`` is exactly what the rest of the
    product already calls unresolved (`crosswalk_choice`, E227's
    ``pending_crosswalks``): a source key somebody has seen in a supplier's file
    and cannot yet name. That is work for the product master owner however the row
    got there — the buyer who proposed it cannot finish it, and a crosswalk
    proposal has no approval to be rejected at (`decide_master_proposal` refuses
    the kind outright), so E059 giving it a target is the one and only way out.

    Creating or correcting a crosswalk that already has a target raises nothing:
    an ordinary version of a valid mapping is not an exception.
    """
    from alerts.goods_services import open_exception, resolve_exceptions

    subject_key = f"crosswalk:{row.pk}"
    source_event_key = uuid.uuid5(CROSSWALK_EXCEPTION_NAMESPACE, f"{run.tenant_id}:{row.pk}")
    if str(row.target_key or "") != "":
        resolve_exceptions(
            run,
            kind="master_resolution_required",
            subject_key=subject_key,
            reason_code="CROSSWALK_MAPPED",
            source_event_key=source_event_key,
        )
        return
    open_exception(
        run,
        # Tenant-wide on purpose: a source key belongs to an issuer's file, not
        # to one store. C-PMO is tenant-scoped and so reads it; a site-scoped
        # reader never does, which widens no access.
        kind="master_resolution_required",
        site_id=None,
        subject_key=subject_key,
        reason_code="CROSSWALK_UNMAPPED",
        source_event_key=source_event_key,
        allowed_resolution_actions=["masters/crosswalks/{id}"],
        note=f"{row.kind} source key {row.source_key} from {row.issuer_key} maps to nothing yet.",
    )


def install() -> None:
    """Register the proposal approval command and the conflict constraint names."""
    register_subject_handler(
        PROPOSAL_SUBJECT, MANAGE_ACTION, decide_master_proposal, policy_governed=False
    )
    for name in CONFLICT_CONSTRAINTS:
        register_integrity_refusal(name, "MASTER_CONFLICT", "That master already exists.")
