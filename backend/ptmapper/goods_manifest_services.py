"""Opening manifest, variance and opening PT services for synthetic tenants.

GSA-T10 (design §5.2-§5.8, §7 P05/P06, §8.3 flow 2; change PRD §5.5, §14.1, §14.5
F1/R20). A synthetic-tenant preparer loads an opening manifest and its physical
verification; the owner approves it. A row that disagrees with verification needs
its own distinct variance approval before it can enter an opening PT. Accepted
rows form an opening PT (``GoodsPt.manifest_version``, document kind ``OPT``,
purpose ``opening``) that a distinct checker approves: that approval is the one
moment "manifest-backed quantity and value" are created (design line 117) - it
opens a new custody lot (``engine.open_lot``) and covers it (``engine.cover``) in
the same P05 posting, unlike receipt where counting and PT approval are two
separate postings. A reissue after reversal reuses the retained lot rather than
opening a second one. No GRN, booking, invoice claim or GL/vendor/cash entry is
ever created.

Every route here refuses on a non-synthetic tenant or a site not
``opening_setup_ready`` with ``OPENING_NOT_READY`` (OQ-54): the block is a code
gate, not a configuration a real tenant could opt into.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple

from alerts.goods_services import open_exception, resolve_exceptions
from approvals.goods_models import ActionDraft, ApprovalRequest
from approvals.goods_policy import pin
from approvals.goods_services import (
    DecisionContext,
    create_request,
    register_subject_cells,
    register_subject_handler,
    supersede_pending,
)
from core.canonical import content_hash
from core.commands import CommandRun, LockRank, register_integrity_refusal
from core.goods_documents import (
    append_revision,
    lock_heads,
    new_document,
    officialise,
    record_event,
    revision_lines,
    set_state,
)
from core.goods_fields import portion as portion_range
from core.goods_money import MoneyInvalid, paise_from_json
from core.kernel_models import DocumentHead, DocumentIdentity, OfficialLine, OfficialVersion
from core.numbering import allocate
from core.refusals import Refusal, issue
from masters.goods_config import ConfigTarget
from masters.goods_models import SiteGuard, Tenant
from masters.hsn import refuse_missing_hsn
from masters.models import Season, Store
from ptmapper import goods_calc
from ptmapper.goods_models import (
    GoodsPt,
    OpeningClaim,
    OpeningManifest,
    OpeningManifestRow,
    OpeningManifestVersion,
    OpeningSeasonCorrection,
    OpeningVariance,
)
from ptmapper.goods_pt_services import (
    ProfileContext,
    _attributes,
    _identity_problems,
    _int_or_none,
    _previous_origins,
    _record_posting,
    _RowFacts,
    _unique_keys,
    _uuid_or_none,
    checked_lines,
    current_lines,
    load_profile,
    normalise_line,
    price_line,
    pt_amounts,
    pt_cells_many,
    require_complete_scope,
)
from stockledger import goods_engine as engine
from stockledger.goods_models import CustodyLot, Origin

OPENING = "opening"
OPENING_DOC_TYPE = "OPT"
#: Policy-governed: a distinct checker approves the opening PT (design §7 P05),
#: exactly like receipt's ``pt.approve.receipt`` but scoped to its own purpose.
APPROVE_OPENING = "pt.approve.opening"
#: Fixed-role: the manifest owner and the variance approver are named by the
#: accepted contract itself, not a configurable approval policy.
MANIFEST_ACTION = "opening.manifest.approve"
VARIANCE_ACTION = "opening.variance.approve"
MAX_ROWS = 5_000
MONEY_FIELDS = frozenset({"basic_paise", "mrp_paise"})


# ---------------------------------------------------------------------------
# OQ-54 / opening-setup gate
# ---------------------------------------------------------------------------


def require_opening_ready(tenant_id: uuid.UUID, site_id: int) -> SiteGuard:
    """Every opening route's own fence: a synthetic tenant, an opening-ready site.

    Real opening stock stays blocked while OQ-54 is open (change PRD §14.1, F1):
    a non-synthetic tenant is refused here regardless of what any site guard or
    approval elsewhere would otherwise allow.
    """
    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None or not tenant.synthetic:
        raise Refusal(
            "OPENING_NOT_READY",
            "Real opening stock stays blocked until OQ-54 is resolved.",
        )
    guard = SiteGuard.objects.filter(site_id=site_id).first()
    if guard is None or guard.stock_contract != SiteGuard.StockContract.GOODS_V1:
        raise Refusal("CONTRACT_DISABLED", "This site still runs the legacy stock contract.")
    if not guard.opening_setup_ready:
        raise Refusal("OPENING_NOT_READY", "This site is not approved for opening-stock setup.")
    if guard.freeze_id:
        raise Refusal("UNDER_COUNT", "A count freezes this site.")
    return guard


def manifest_target(
    cutoff_at: datetime, site_id: int, brand_ids: Iterable[int | None] = ()
) -> ConfigTarget:
    return ConfigTarget.of(
        cutoff_at, site_id=site_id, brand_ids={None, *brand_ids}, purpose=OPENING
    )


# ---------------------------------------------------------------------------
# OpeningRow / PhysicalVerification parsing
# ---------------------------------------------------------------------------


def _invalid(message: str, field: str | None = None) -> Refusal:
    return Refusal(
        "MANIFEST_INVALID",
        message,
        status=422,
        issues=[issue("INVALID", message, field=field)],
    )


def _money(raw: Any, field: str) -> int:
    try:
        parsed = paise_from_json(raw)
    except MoneyInvalid as refused:
        raise _invalid(f"{field} {refused.message}.", field) from None
    if parsed is None:
        raise _invalid(f"{field} is required.", field)
    return parsed


@dataclass
class ParsedRow:
    source_row_key: str
    site_id: int
    location_id: uuid.UUID | None
    condition: str
    identity: dict[str, Any]
    identity_key: tuple[str | None, ...] | None
    qty: int
    basic_paise: int
    mrp_paise: int
    hsn: str
    tax_version_id: str | None
    season_id: str
    #: The loader's deliberate "this cohort cannot be established" declaration.
    season_unknown_historical: bool
    older_origin_at: str | None
    older_origin_ref: str | None
    commercial_label: str | None
    verification: dict[str, Any]


_ROW_KEYS = frozenset(
    {
        "source_row_key",
        "site_id",
        "location_id",
        "condition",
        "identity",
        "qty",
        "basic_paise",
        "mrp_paise",
        "hsn",
        "tax_version_id",
        "season_id",
        "season_unknown_historical",
        "older_origin_at",
        "older_origin_ref",
        "commercial_label",
    }
)
_IDENTITY_KEYS = frozenset({"sku_id", "attributes", "raw_alias", "description"})
_VERIFICATION_KEYS = frozenset({"observed_qty", "observed_condition", "notes"})
_CONDITIONS = frozenset({"good", "damaged", "wrong", "unidentified"})


def _identity(raw: Any, index: int) -> tuple[dict[str, Any], tuple[str | None, ...] | None]:
    if not isinstance(raw, dict) or set(raw) - _IDENTITY_KEYS:
        raise _invalid(f"rows[{index}].row.identity is not a valid ObservedIdentity.")
    sku_id = _uuid_or_none(raw.get("sku_id"))
    key = uuid.uuid4()  # only used for _attributes' issue-shaping, never persisted
    attributes = _attributes(raw.get("attributes"), key)
    sku_str = str(sku_id) if sku_id else None
    raw_alias = raw.get("raw_alias")
    raw_alias_str = str(raw_alias)[:128] if raw_alias else None
    description = raw.get("description")
    out: dict[str, Any] = {
        "sku_id": sku_str,
        "attributes": attributes,
        "raw_alias": raw_alias_str,
        "description": str(description)[:240] if description else None,
    }
    # An unresolved identity (no sku_id) is not yet a real identity to compare -
    # two rows both awaiting resolution are not necessarily the same physical
    # item, so they are exempt from the duplicate-identity check below.
    identity_key = (sku_str, content_hash(attributes), raw_alias_str) if sku_id else None
    return out, identity_key


def _verification(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - _VERIFICATION_KEYS:
        raise _invalid(f"rows[{index}].verification is not a valid PhysicalVerification.")
    qty = raw.get("observed_qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
        raise _invalid(f"rows[{index}].verification.observed_qty must be a non-negative integer.")
    condition = raw.get("observed_condition")
    if condition not in _CONDITIONS:
        raise _invalid(f"rows[{index}].verification.observed_condition is not valid.")
    notes = raw.get("notes")
    return {
        "observed_qty": qty,
        "observed_condition": condition,
        "notes": str(notes)[:500] if notes else None,
    }


def _older_origin(row: dict[str, Any], index: int) -> tuple[str | None, str | None]:
    """The older origin is preserved exactly as supplied - present, or left
    unknown. It is never invented, defaulted or backfilled from anything else
    on the row."""
    older_at = row.get("older_origin_at")
    older_ref = row.get("older_origin_ref")
    if older_at is not None and not isinstance(older_at, str):
        raise _invalid(f"rows[{index}].row.older_origin_at must be a timestamp or absent.")
    if older_ref is not None and (not isinstance(older_ref, str) or len(older_ref) > 160):
        raise _invalid(
            f"rows[{index}].row.older_origin_ref must be text of at most 160 characters."
        )
    return older_at, older_ref


def _season(row: dict[str, Any], index: int, source_row_key: str) -> tuple[int, bool]:
    """The row's season and whether the loader declared it the unknown historical one.

    A blank season is still refused outright: the unknown historical season is a
    value somebody chooses, not the absence of one.
    """
    season_id = _int_or_none(row.get("season_id"))
    if season_id is None:
        raise Refusal(
            "MANIFEST_INVALID",
            "Every opening row needs a real mapped season.",
            status=422,
            issues=[
                issue("SEASON_REQUIRED", "A mapped season is required", line_key=source_row_key)
            ],
        )
    declared = row.get("season_unknown_historical", False)
    if not isinstance(declared, bool):
        raise _invalid(
            f"rows[{index}].row.season_unknown_historical must be true or false.",
            "season_unknown_historical",
        )
    return season_id, declared


def _parse_row(raw: Any, index: int, *, site_id: int) -> ParsedRow:
    if not isinstance(raw, dict) or set(raw) - {"row", "verification"} or "row" not in raw:
        raise _invalid(f"rows[{index}] must be {{row, verification}}.")
    row = raw["row"]
    if not isinstance(row, dict) or set(row) - _ROW_KEYS:
        raise _invalid(f"rows[{index}].row is not a valid OpeningRow.")
    source_row_key = row.get("source_row_key")
    if not isinstance(source_row_key, str) or not 1 <= len(source_row_key) <= 100:
        raise _invalid(f"rows[{index}].row.source_row_key is required.")
    if _int_or_none(row.get("site_id")) != site_id:
        raise _invalid(f"rows[{index}].row.site_id must match the manifest's site.")
    location_id = _uuid_or_none(row.get("location_id"))
    condition = row.get("condition")
    if condition not in _CONDITIONS:
        raise _invalid(f"rows[{index}].row.condition is not valid.")
    identity, identity_key = _identity(row.get("identity"), index)
    qty = row.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 999_999:
        raise _invalid(f"rows[{index}].row.qty must be 1 to 999,999.")
    basic_paise = _money(row.get("basic_paise"), f"rows[{index}].row.basic_paise")
    mrp_paise = _money(row.get("mrp_paise"), f"rows[{index}].row.mrp_paise")
    hsn = row.get("hsn")
    if not isinstance(hsn, str) or not hsn:
        raise _invalid(f"rows[{index}].row.hsn is required.")
    season_id, season_unknown = _season(row, index, source_row_key)
    older_at, older_ref = _older_origin(row, index)
    label = row.get("commercial_label")
    return ParsedRow(
        source_row_key=source_row_key,
        site_id=site_id,
        location_id=location_id,
        condition=condition,
        identity=identity,
        identity_key=identity_key,
        qty=qty,
        basic_paise=basic_paise,
        mrp_paise=mrp_paise,
        hsn=hsn[:24],
        tax_version_id=str(_uuid_or_none(row.get("tax_version_id")) or "") or None,
        season_id=str(season_id),
        season_unknown_historical=season_unknown,
        older_origin_at=older_at,
        older_origin_ref=older_ref,
        commercial_label=str(label)[:200] if label else None,
        verification=_verification(raw.get("verification"), index),
    )


def _check_duplicates(rows: list[ParsedRow]) -> None:
    """Within one manifest submission: duplicate row and duplicate identity are refused,
    each naming the exact duplicate. Cross-batch duplicate physical stock is prevented
    later, at opening-PT approval, by ``OpeningClaim``'s exclusion constraint."""
    seen_rows: dict[str, int] = {}
    seen_identity: dict[tuple[str | None, ...], int] = {}
    for index, row in enumerate(rows):
        if row.source_row_key in seen_rows:
            raise Refusal(
                "MANIFEST_INVALID",
                f"Row {row.source_row_key} is duplicated in this manifest.",
                status=422,
                issues=[
                    issue(
                        "DUPLICATE_ROW",
                        f"Duplicate row: {row.source_row_key}",
                        line_key=row.source_row_key,
                    )
                ],
            )
        seen_rows[row.source_row_key] = index
        if row.identity_key is None:
            continue
        if row.identity_key in seen_identity:
            other = rows[seen_identity[row.identity_key]].source_row_key
            raise Refusal(
                "MANIFEST_INVALID",
                f"Rows {other} and {row.source_row_key} name the same identity.",
                status=422,
                issues=[
                    issue(
                        "DUPLICATE_IDENTITY",
                        f"Duplicate identity: {row.source_row_key} repeats {other}",
                        line_key=row.source_row_key,
                    )
                ],
            )
        seen_identity[row.identity_key] = index


def _season_refusal(code: str, message: str, source_row_key: str, detail: str) -> Refusal:
    return Refusal(
        "MANIFEST_INVALID",
        message,
        status=422,
        issues=[issue(code, detail, field="season_id", line_key=source_row_key)],
    )


def _check_seasons(rows: list[ParsedRow]) -> None:
    """Every row's season must resolve to a real, tenant-governed season - never
    invented (ticket 10: "a missing season is refused, never invented").

    The unknown historical season is one of those real seasons, and the only one
    a row may name only when it also says so (store and warehouse operations PRD
    §4): the choice is deliberate, so naming it silently is refused, and so is
    claiming it over any other season.
    """
    wanted = {int(row.season_id) for row in rows}
    known = dict(Season.objects.filter(pk__in=wanted).values_list("pk", "historical_unknown"))
    for row in rows:
        season_id = int(row.season_id)
        if season_id not in known:
            raise _season_refusal(
                "SEASON_REQUIRED",
                f"Row {row.source_row_key} names a season that does not exist.",
                row.source_row_key,
                "A real mapped season is required",
            )
        if known[season_id] and not row.season_unknown_historical:
            raise _season_refusal(
                "SEASON_UNKNOWN_NOT_DECLARED",
                f"Row {row.source_row_key} names the unknown historical season "
                "without choosing it.",
                row.source_row_key,
                "Choosing the unknown historical season is a deliberate declaration",
            )
        if row.season_unknown_historical and not known[season_id]:
            raise _season_refusal(
                "SEASON_UNKNOWN_MISMATCH",
                f"Row {row.source_row_key} claims an unknown historical season "
                "but names a real one.",
                row.source_row_key,
                "Only the unknown historical season may be declared unknown",
            )


def _row_payload(row: ParsedRow) -> dict[str, Any]:
    return {
        "source_row_key": row.source_row_key,
        "site_id": row.site_id,
        "location_id": str(row.location_id) if row.location_id else None,
        "condition": row.condition,
        "identity": row.identity,
        "qty": row.qty,
        "basic_paise": str(row.basic_paise),
        "mrp_paise": str(row.mrp_paise),
        "hsn": row.hsn,
        "tax_version_id": row.tax_version_id,
        "season_id": row.season_id,
        # Stored on the row itself, so the declaration is auditable with the
        # row's own maker, command and time - never reconstructed later.
        "season_unknown_historical": row.season_unknown_historical,
        "older_origin_at": row.older_origin_at,
        "older_origin_ref": row.older_origin_ref,
        "commercial_label": row.commercial_label,
    }


# ---------------------------------------------------------------------------
# E137 create, E235 revise
# ---------------------------------------------------------------------------


def matches_verification(row: OpeningManifestRow) -> bool:
    """Whether physical verification agreed with the manifest row on both the count
    and the condition. A row that does not match needs a variance decision before it
    can enter an opening PT."""
    verification = row.verification
    return bool(
        verification["observed_qty"] == row.payload["qty"]
        and verification["observed_condition"] == row.payload["condition"]
    )


def _close_superseded_investigations(run: CommandRun, superseded: list[OpeningManifestRow]) -> None:
    """A revision replaces the rows it supersedes, so their open investigations are
    about rows that no longer exist. Left open their SLA clocks keep running against
    an owner who can no longer act on them; the new version opens its own."""
    for row in superseded:
        subject_key = f"manifest_row:{row.pk}"
        for kind in ("opening_variance", "opening_origin_unavailable"):
            resolve_exceptions(
                run, kind=kind, subject_key=subject_key, reason_code="MANIFEST_REVISED"
            )


def _register_investigations(
    run: CommandRun,
    manifest: OpeningManifest,
    version: OpeningManifestVersion,
    rows: list[OpeningManifestRow],
) -> None:
    for row in rows:
        payload = row.payload
        if not payload.get("older_origin_at") and not payload.get("older_origin_ref"):
            open_exception(
                run,
                kind="opening_origin_unavailable",
                site_id=manifest.site_id,
                subject_key=f"manifest_row:{row.pk}",
                reason_code="OLDER_ORIGIN_UNKNOWN",
                source_event_key=row.pk,
                allowed_resolution_actions=["exceptions/{id}/confirm-origin-unavailable"],
            )
        if not matches_verification(row):
            open_exception(
                run,
                kind="opening_variance",
                site_id=manifest.site_id,
                subject_key=f"manifest_row:{row.pk}",
                reason_code="VERIFICATION_MISMATCH",
                source_event_key=row.pk,
                allowed_resolution_actions=["ptmapper/opening-manifests/{id}/variances"],
            )


def create_manifest(run: CommandRun, *, body: dict[str, Any]) -> OpeningManifest:
    """E137: a preparer's opening manifest, with its physical verification, awaiting
    the owner's approval of this exact revision."""
    site_id = body["site_id"]
    require_opening_ready(run.tenant_id, site_id)
    batch_key = str(body["batch_key"])[:100]
    if OpeningManifest.objects.filter(
        tenant_id=run.tenant_id, site_id=site_id, batch_key=batch_key
    ).exists():
        raise Refusal(
            "MANIFEST_INVALID",
            f"Batch {batch_key} already has an opening manifest at this site.",
            status=422,
            issues=[issue("DUPLICATE_BATCH", f"Duplicate batch: {batch_key}", field="batch_key")],
        )
    raw_rows = body.get("rows")
    if not isinstance(raw_rows, list) or not 1 <= len(raw_rows) <= MAX_ROWS:
        raise _invalid(f"rows must be a list of 1 to {MAX_ROWS} entries.")
    parsed = [_parse_row(raw, index, site_id=site_id) for index, raw in enumerate(raw_rows)]
    _check_duplicates(parsed)
    _check_seasons(parsed)
    dataset_key = str(body["dataset_key"])[:100]
    manifest = OpeningManifest.objects.create(
        tenant_id=run.tenant_id,
        site_id=site_id,
        dataset_key=dataset_key,
        batch_key=batch_key,
        revision=1,
    )
    version, rows = _append_version(
        run, manifest, body=body, parsed=parsed, dataset_key=dataset_key, revision=1
    )
    manifest.current_version = version
    manifest.save(update_fields=["current_version"])
    _register_investigations(run, manifest, version, rows)
    _request_manifest_approval(run, manifest, version)
    run.audit_after = {"manifest_id": str(manifest.pk), "rows": len(rows)}
    return manifest


def _cutoff_at(raw: Any) -> datetime:
    from django.utils.dateparse import parse_datetime

    moment = parse_datetime(raw) if isinstance(raw, str) else None
    if moment is None or moment.tzinfo is None:
        raise _invalid("cutoff_at must be a timestamp with an offset.", "cutoff_at")
    return moment


def _source_evidence(run: CommandRun, raw: Any) -> uuid.UUID:
    from files.goods_models import EvidenceObject

    evidence_id = _uuid_or_none(raw)
    if (
        evidence_id is None
        or not EvidenceObject.objects.filter(tenant_id=run.tenant_id, pk=evidence_id).exists()
    ):
        raise _invalid("source_evidence_id must be a known evidence file.", "source_evidence_id")
    return evidence_id


def _append_version(
    run: CommandRun,
    manifest: OpeningManifest,
    *,
    body: dict[str, Any],
    parsed: list[ParsedRow],
    dataset_key: str,
    revision: int,
) -> tuple[OpeningManifestVersion, list[OpeningManifestRow]]:
    assert run.principal.human_id is not None
    payloads = [{**_row_payload(row), "dataset_key": dataset_key} for row in parsed]
    digest = content_hash(
        {"header": {"site_id": manifest.site_id, "batch_key": manifest.batch_key}, "rows": payloads}
    )
    profile_version_id = _uuid_or_none(body.get("profile_version_id"))
    version = run.record(
        OpeningManifestVersion(
            tenant_id=run.tenant_id,
            manifest=manifest,
            site_id=manifest.site_id,
            revision=revision,
            cutoff_at=_cutoff_at(body["cutoff_at"]),
            source_evidence_id=_source_evidence(run, body["source_evidence_id"]),
            profile_version_id=profile_version_id,
            content_hash=digest,
            maker_id=run.principal.human_id,
            row_count=len(payloads),
        )
    )
    rows = [
        run.record(
            OpeningManifestRow(
                tenant_id=run.tenant_id,
                manifest_version=version,
                site_id=manifest.site_id,
                source_row_key=row.source_row_key,
                payload=payload,
                verification=row.verification,
            )
        )
        for row, payload in zip(parsed, payloads, strict=True)
    ]
    return version, rows


def _request_manifest_approval(
    run: CommandRun, manifest: OpeningManifest, version: OpeningManifestVersion
) -> None:
    create_request(
        run,
        subject_kind=ApprovalRequest.SubjectKind.MANIFEST,
        subject_key=str(manifest.pk),
        revision=manifest.revision,
        reviewed_hash=version.content_hash,
        requested_action=MANIFEST_ACTION,
        site_id=manifest.site_id,
        require_distinct=True,
        required_roles=["C-OWN"],
        title=f"Opening manifest {manifest.batch_key}",
    )


def patch_manifest(
    run: CommandRun,
    manifest: OpeningManifest,
    *,
    body: dict[str, Any],
    expected_revision: int | None,
) -> OpeningManifest:
    """E235: a complete new revision of an unconsumed manifest; editing supersedes
    any pending approval and asks for a fresh one of this exact revision."""
    require_opening_ready(run.tenant_id, manifest.site_id)
    locked = run.lock(LockRank.DOCUMENT, OpeningManifest.objects.filter(pk=manifest.pk))
    if not locked:
        raise Refusal("NOT_FOUND", "That opening manifest was not found.")
    manifest = locked[0]
    if expected_revision != manifest.revision:
        raise Refusal("REVISION_SUPERSEDED", "The manifest changed after you loaded it.")
    # E235: "Original C-INV preparer within site scope" - holding the opening
    # preparer action at the site is not enough; a revision is a new version of
    # this preparer's own evidence.
    first = OpeningManifestVersion.objects.filter(manifest=manifest).order_by("revision").first()
    if first is not None and first.maker_id != run.principal.human_id:
        raise Refusal(
            "ACTION_DENIED",
            "Only the preparer who loaded this manifest can revise it.",
            status=403,
        )
    if OpeningClaim.objects.filter(
        manifest_row__manifest_version=manifest.current_version
    ).exists():
        raise Refusal(
            "MANIFEST_IN_USE",
            "This manifest already has a live opening PT; use the variance or PT reversal route.",
            status=409,
        )
    raw_rows = body.get("rows")
    if not isinstance(raw_rows, list) or not 1 <= len(raw_rows) <= MAX_ROWS:
        raise _invalid(f"rows must be a list of 1 to {MAX_ROWS} entries.")
    parsed = [
        _parse_row(raw, index, site_id=manifest.site_id) for index, raw in enumerate(raw_rows)
    ]
    _check_duplicates(parsed)
    _check_seasons(parsed)
    revision = manifest.current_version.revision + 1 if manifest.current_version else 1
    superseded = list(
        OpeningManifestRow.objects.filter(manifest_version=manifest.current_version)
        if manifest.current_version
        else []
    )
    version, rows = _append_version(
        run, manifest, body=body, parsed=parsed, dataset_key=manifest.dataset_key, revision=revision
    )
    manifest.current_version = version
    manifest.revision += 1
    # A revision retracts whatever approval an earlier version had: the opening
    # PT can only ever be built from a version the owner has approved by name,
    # never from a stale ``approved_version`` a later edit left behind.
    manifest.approved_version = None
    manifest.save(update_fields=["current_version", "revision", "approved_version"])
    supersede_pending(run, ApprovalRequest.SubjectKind.MANIFEST, str(manifest.pk), MANIFEST_ACTION)
    _close_superseded_investigations(run, superseded)
    _register_investigations(run, manifest, version, rows)
    _request_manifest_approval(run, manifest, version)
    run.audit_after = {"manifest_id": str(manifest.pk), "revision": revision}
    return manifest


def _decide_manifest(run: CommandRun, context: DecisionContext) -> dict[str, Any] | None:
    manifest_id = _uuid_or_none(context.request.subject_key)
    manifest = (
        OpeningManifest.objects.filter(pk=manifest_id).select_related("current_version").first()
        if manifest_id
        else None
    )
    if manifest is None:
        raise Refusal("NOT_FOUND", "That opening manifest was not found.")
    context.access.require(MANIFEST_ACTION, site_id=manifest.site_id)
    version = manifest.current_version
    if version is None or version.content_hash != context.request.reviewed_hash:
        raise Refusal(
            "REVISION_SUPERSEDED", "The manifest changed after this approval was requested."
        )
    if context.decision == "reject":
        return {"state": "rejected"}
    require_opening_ready(run.tenant_id, manifest.site_id)
    locked = run.lock(LockRank.DOCUMENT, OpeningManifest.objects.filter(pk=manifest.pk))
    if not locked:
        raise Refusal("NOT_FOUND", "That opening manifest was not found.")
    manifest = locked[0]
    if manifest.current_version_id != version.pk:
        raise Refusal(
            "REVISION_SUPERSEDED", "The manifest changed after this approval was requested."
        )
    manifest.approved_version = version
    manifest.save(update_fields=["approved_version"])
    return {"state": "approved", "version": version.revision}


# ---------------------------------------------------------------------------
# E138 variance propose + decide
# ---------------------------------------------------------------------------


def propose_variance(
    run: CommandRun,
    manifest: OpeningManifest,
    *,
    body: dict[str, Any],
    expected_revision: int | None,
) -> ActionDraft:
    require_opening_ready(run.tenant_id, manifest.site_id)
    locked = run.lock(LockRank.DOCUMENT, OpeningManifest.objects.filter(pk=manifest.pk))
    if not locked:
        raise Refusal("NOT_FOUND", "That opening manifest was not found.")
    manifest = locked[0]
    if expected_revision != manifest.revision:
        raise Refusal("REVISION_SUPERSEDED", "The manifest changed after you loaded it.")
    row_id = _uuid_or_none(body.get("manifest_row_id"))
    row = (
        OpeningManifestRow.objects.filter(
            pk=row_id, manifest_version=manifest.current_version
        ).first()
        if row_id
        else None
    )
    if row is None:
        raise Refusal("NOT_FOUND", "That manifest row was not found.")
    original_qty = row.payload["qty"]
    accepted_qty = body.get("accepted_qty")
    if (
        isinstance(accepted_qty, bool)
        or not isinstance(accepted_qty, int)
        or not 0 <= accepted_qty <= original_qty
    ):
        raise Refusal(
            "OPENING_VARIANCE_INVALID",
            "accepted_qty must be a whole number from 0 up to the manifest row's quantity.",
            status=422,
        )
    # GSA-T10: the decision carries the condition the row opens under. Verification is
    # what the counter actually saw, so that is the default; a quantity-only decision
    # must never let a row verified damaged open as good stock.
    observed_condition = row.verification["observed_condition"]
    accepted_condition = body.get("accepted_condition", observed_condition)
    if accepted_condition not in _CONDITIONS:
        raise Refusal(
            "OPENING_VARIANCE_INVALID",
            "accepted_condition is not a valid condition.",
            status=422,
        )
    reason_code = body.get("reason_code")
    if not isinstance(reason_code, str) or not reason_code:
        raise Refusal("INVALID_REQUEST", "reason_code is required.")
    evidence_ids = body.get("evidence_ids") or []
    if not isinstance(evidence_ids, list) or len(evidence_ids) > 20:
        raise Refusal("INVALID_REQUEST", "evidence_ids must be a list of at most 20 IDs.")
    assert run.principal.human_id is not None
    payload = {
        "manifest_row_id": str(row.pk),
        "original_qty": original_qty,
        "observed_qty": row.verification["observed_qty"],
        "accepted_qty": accepted_qty,
        "observed_condition": observed_condition,
        "accepted_condition": str(accepted_condition),
        "reason_code": str(reason_code)[:60],
        "evidence_ids": [str(_uuid_or_none(e)) for e in evidence_ids],
    }
    draft = ActionDraft.objects.create(
        tenant_id=run.tenant_id,
        subject_kind="opening_variance",
        subject_key=f"manifest_row:{row.pk}:{run.spec.command_id}",
        revision=1,
        maker_id=run.principal.human_id,
        payload=payload,
        content_hash=content_hash(payload),
    )
    create_request(
        run,
        subject_kind="opening_variance",
        subject_key=f"action_draft:{draft.pk}",
        revision=manifest.revision,
        reviewed_hash=draft.content_hash,
        requested_action=VARIANCE_ACTION,
        site_id=manifest.site_id,
        require_distinct=True,
        required_roles=["C-INV", "C-OWN"],
        title=f"Opening variance on {row.source_row_key}",
    )
    run.audit_after = {"variance_request": str(draft.pk), "manifest_row_id": str(row.pk)}
    return draft


def _decide_variance(run: CommandRun, context: DecisionContext) -> dict[str, Any] | None:
    draft_id = _uuid_or_none(context.request.subject_key.removeprefix("action_draft:"))
    draft = (
        ActionDraft.objects.filter(pk=draft_id, subject_kind="opening_variance").first()
        if draft_id
        else None
    )
    if draft is None:
        raise Refusal("NOT_FOUND", "That variance request was not found.")
    context.access.require(VARIANCE_ACTION, site_id=context.request.site_id)
    if draft.content_hash != context.request.reviewed_hash:
        raise Refusal("REVISION_SUPERSEDED", "The variance changed after it was requested.")
    row = OpeningManifestRow.objects.get(pk=uuid.UUID(draft.payload["manifest_row_id"]))
    if context.decision == "reject":
        return {"state": "rejected"}
    assert context.request.site_id is not None  # propose_variance always names one
    require_opening_ready(run.tenant_id, context.request.site_id)
    variance = run.record(
        OpeningVariance(
            tenant_id=run.tenant_id,
            manifest_row=row,
            decision=draft.payload,
            approved_by_id=context.checker_id,
            maker_id=draft.maker_id,
        )
    )
    resolve_exceptions(run, kind="opening_variance", subject_key=f"manifest_row:{row.pk}")
    return {"state": "approved", "variance_id": str(variance.pk)}


# ---------------------------------------------------------------------------
# Accepted rows: manifest quantity, after any approved variance
# ---------------------------------------------------------------------------


class AcceptedRow(NamedTuple):
    """What an opening row was finally accepted as: how many, in what condition."""

    qty: int
    condition: str


def _row_resolution(
    manifest: OpeningManifest,
) -> tuple[dict[uuid.UUID, AcceptedRow], list[OpeningManifestRow]]:
    """Every manifest row's accepted quantity and condition, and every row still
    awaiting a variance decision. A row that disagrees with verification and has no
    variance decision yet is neither accepted nor silently dropped - it comes
    back as unresolved, so the opening PT cannot be built around it and quietly
    leave it stranded (design R-INV-011: excluded rows "remain visible")."""
    if manifest.approved_version_id is None:
        return {}, []
    rows = list(OpeningManifestRow.objects.filter(manifest_version_id=manifest.approved_version_id))
    variances = {
        v.manifest_row_id: v
        for v in OpeningVariance.objects.filter(manifest_row__in=rows).order_by("recorded_at")
    }
    out: dict[uuid.UUID, AcceptedRow] = {}
    unresolved: list[OpeningManifestRow] = []
    for row in rows:
        if matches_verification(row):
            out[row.pk] = AcceptedRow(row.payload["qty"], row.payload["condition"])
        elif row.pk in variances:
            decision = variances[row.pk].decision
            # GSA-T10: the decision names the condition the row opens under. Older
            # decisions carry only a quantity; those fall back to what verification
            # observed, never to the manifest's unverified claim.
            out[row.pk] = AcceptedRow(
                decision["accepted_qty"],
                decision.get("accepted_condition") or row.verification["observed_condition"],
            )
        else:
            unresolved.append(row)
    return {row_id: a for row_id, a in out.items() if a.qty > 0}, unresolved


def accepted_rows(manifest: OpeningManifest) -> dict[uuid.UUID, int]:
    """Every manifest row's accepted quantity: the manifest's own count when
    verification agreed, or its latest approved variance's accepted_qty when it
    did not. A row with an unresolved difference and no approved variance is
    excluded - it stays visible, never silently included (design R-INV-011)."""
    accepted, _ = _row_resolution(manifest)
    return {row_id: a.qty for row_id, a in accepted.items()}


def accepted_conditions(manifest: OpeningManifest) -> dict[uuid.UUID, str]:
    """Every accepted row's condition: the manifest's own when verification agreed,
    otherwise the approved variance's accepted condition."""
    accepted, _ = _row_resolution(manifest)
    return {row_id: a.condition for row_id, a in accepted.items()}


# ---------------------------------------------------------------------------
# Opening PT: create draft from an approved manifest
# ---------------------------------------------------------------------------


def _opening_line(row: OpeningManifestRow, qty: int, profile: ProfileContext) -> dict[str, Any]:
    payload = row.payload
    line = normalise_line(
        {
            "line_key": str(uuid.uuid4()),
            "sku_id": payload["identity"].get("sku_id"),
            "attributes": payload["identity"].get("attributes") or [],
            "season_id": payload["season_id"],
            # The row's own declaration travels with the line, so the PT, its
            # official version and every stock read after it say the same thing.
            "season_unknown_historical": bool(payload.get("season_unknown_historical")),
            "alias_as_used": payload["identity"].get("raw_alias"),
            "qty": qty,
            "coverage_requests": [{"manifest_row_id": str(row.pk), "qty": qty}],
            "hsn": payload["hsn"],
            "supplied": {"basic_paise": payload["basic_paise"], "mrp_paise": payload["mrp_paise"]},
            "source_ref": payload["identity"].get("description"),
        }
    )
    return price_line(line, profile, goods_calc.BOTH_SUPPLIED)


def create_opening_draft(
    run: CommandRun,
    *,
    manifest: OpeningManifest,
    profile_version_id: Any,
    evidence_id: uuid.UUID | None,
) -> tuple[DocumentIdentity, DocumentHead, GoodsPt]:
    """The from-manifest analogue of E123: a numberless draft prefilled 1:1 from
    every accepted row of the manifest's approved version; no free-typed lines."""
    require_opening_ready(run.tenant_id, manifest.site_id)
    # Lock the manifest before reading its approved version and asking whether that
    # version already has a PT: two concurrent creates would both see no PT and both
    # build one, and nothing downstream could tell which of the two is the opening.
    # `uq_goodspt_per_manifest_version` is the database's own last word on it.
    locked = run.lock(LockRank.DOCUMENT, OpeningManifest.objects.filter(pk=manifest.pk))
    if not locked:
        raise Refusal("NOT_FOUND", "That opening manifest was not found.")
    manifest = locked[0]
    if manifest.approved_version_id is None:
        raise Refusal(
            "OPENING_NOT_READY", "This manifest has no owner-approved version yet.", status=422
        )
    if GoodsPt.objects.filter(manifest_version_id=manifest.approved_version_id).exists():
        raise Refusal(
            "PT_PARENT_INVALID",
            "This manifest version already has its opening PT.",
            status=422,
        )
    accepted, unresolved = _row_resolution(manifest)
    if unresolved:
        raise Refusal(
            "OPENING_NOT_READY",
            "Every row that disagrees with verification needs its variance decided "
            "before the opening PT can be created - a row cannot be first excluded and "
            "then added to a later opening PT.",
            status=422,
            issues=[
                issue(
                    "OPENING_VARIANCE_PENDING",
                    f"Row {row.source_row_key} needs a variance decision",
                    line_key=row.source_row_key,
                )
                for row in unresolved
            ],
        )
    if not accepted:
        raise Refusal(
            "OPENING_NOT_READY",
            "No manifest row has a resolved accepted quantity yet.",
            status=422,
        )
    version = manifest.approved_version
    assert version is not None
    profile = load_profile(
        run.tenant_id,
        profile_version_id,
        manifest_target(version.cutoff_at, manifest.site_id),
    )
    rows = {row.pk: row for row in OpeningManifestRow.objects.filter(pk__in=list(accepted))}
    lines = [
        _opening_line(rows[row_id], entry.qty, profile)
        for row_id, entry in sorted(accepted.items(), key=str)
    ]
    _unique_keys(lines)
    site = manifest.site_id
    entity_id = _site_entity_id(site)
    identity, head = new_document(
        run, kind=OPENING_DOC_TYPE, purpose=OPENING, entity_id=entity_id, site_id=site
    )
    goods_pt = GoodsPt.objects.create(
        tenant_id=run.tenant_id,
        document=identity,
        manifest_version=version,
        preparer_id=identity.maker_id,
    )
    header = {
        "purpose": OPENING,
        "manifest_version_id": str(version.pk),
        "site_id": str(site),
        "profile_version_id": str(profile.version.pk),
        "direction": goods_calc.BOTH_SUPPLIED,
        "source": "typed",
        "source_evidence_ids": [str(evidence_id)] if evidence_id else [],
        "disposition_ids": [],
    }
    append_revision(
        run,
        head,
        header=header,
        replace_lines=[(uuid.UUID(line["line_key"]), line) for line in lines],
    )
    record_event(run, identity.pk, "draft_created", payload={"to_state": "draft", "details": []})
    run.audit_subject_key = f"document:{identity.pk}"
    run.audit_site_id = site
    return identity, head, goods_pt


def _site_entity_id(site_id: int) -> int:
    return Store.objects.filter(pk=site_id).values_list("gstin__legal_entity_id", flat=True).get()


# ---------------------------------------------------------------------------
# Submit (E127 dispatch target) and reconcile
# ---------------------------------------------------------------------------


def approved_manifest(manifest_version_id: uuid.UUID) -> OpeningManifest:
    """The manifest this version is still the approved version of. A revision clears
    `approved_version`, so a superseded version has no manifest to open against."""
    manifest = OpeningManifest.objects.filter(approved_version_id=manifest_version_id).first()
    if manifest is None:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "This opening PT's manifest was revised and approved again; "
            "it can no longer be officialised.",
        )
    return manifest


def reconcile_opening(
    manifest_version_id: uuid.UUID, lines: list[dict[str, Any]], revision_hash: str
) -> dict[str, Any]:
    """Every accepted manifest row's exact quantity must be covered once - no
    more, no less (design: "opening quantity must equal the matching approved
    manifest quantity")."""
    manifest_row_ids = {
        row.pk: row
        for row in OpeningManifestRow.objects.filter(manifest_version_id=manifest_version_id)
    }
    manifest = approved_manifest(manifest_version_id)
    accepted = accepted_rows(manifest)
    requested: dict[uuid.UUID, int] = defaultdict(int)
    issues: list[dict[str, Any]] = []
    for line in lines:
        for request in line.get("coverage_requests") or []:
            row_id = _uuid_or_none(request.get("manifest_row_id"))
            if row_id is None or row_id not in manifest_row_ids:
                issues.append(
                    issue("COVERAGE_SOURCE_INVALID", "Coverage must use an accepted manifest row.")
                )
                continue
            requested[row_id] += int(request["qty"])
    out_lines: list[dict[str, Any]] = []
    for row_id, accepted_qty in sorted(accepted.items(), key=str):
        proposed = requested.pop(row_id, 0)
        row_issues = []
        if proposed != accepted_qty:
            row_issues.append(
                issue(
                    "OPENING_QTY_MISMATCH",
                    f"This row's approved quantity is {accepted_qty}; the PT proposes {proposed}.",
                    quantity=accepted_qty,
                )
            )
        issues.extend(row_issues)
        out_lines.append(
            {
                "line_key": None,
                # The shared PT reconciliation panel (PtPrepare.tsx) reads
                # every purpose's row identifier as `lot_id`; opening has no
                # lot at reconciliation time (design line 117: the lot opens
                # only at approval), so this names the manifest row instead.
                "lot_id": str(row_id),
                "manifest_row_id": str(row_id),
                "claimed_qty": accepted_qty,
                "counted_qty": accepted_qty,
                "proposed_qty": proposed,
                "already_covered_qty": 0,
                "held_uncovered_qty": 0,
                "disposed_uncovered_qty": 0,
                "issues": row_issues,
            }
        )
    for _row_id in requested:
        issues.append(
            issue("COVERAGE_SOURCE_INVALID", "That manifest row is not accepted for opening.")
        )
    totals = {
        "counted_qty": sum(int(line["counted_qty"]) for line in out_lines),
        "proposed_qty": sum(int(line["proposed_qty"]) for line in out_lines),
        "already_covered_qty": 0,
        "held_uncovered_qty": 0,
        "disposed_uncovered_qty": 0,
        "claimed_qty": None,
        "value_paise": None,
    }
    return {
        "purpose": OPENING,
        "revision_hash": revision_hash,
        "source_hashes": [],
        "lines": out_lines,
        "totals": totals,
        "passed": not issues,
        "issues": issues,
    }


def validate_opening_rows(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    if not lines:
        problems.append(issue("NO_LINES", "An opening PT needs at least one line"))
    facts = _RowFacts.load(lines, set())
    for line in lines:
        key = line.get("line_key")
        problems.extend(_identity_problems(line, None, facts))
        calculated = line.get("calculated") or {}
        if not _int_or_none(calculated.get("p_rate_paise")) or not _int_or_none(
            calculated.get("mrp_paise")
        ):
            problems.append(
                issue(
                    "VALUE_MISSING", "P RATE and MRP must be calculated and positive", line_key=key
                )
            )
        season = str(line.get("season_id") or "")
        declared = bool(line.get("season_unknown_historical"))
        if not season:
            problems.append(
                issue(
                    "SEASON_REQUIRED",
                    "A real mapped season is required",
                    field="season_id",
                    line_key=key,
                )
            )
        elif season in facts.unknown_seasons and not declared:
            problems.append(
                issue(
                    "SEASON_UNKNOWN_NOT_DECLARED",
                    "Choosing the unknown historical season is a deliberate declaration",
                    field="season_id",
                    line_key=key,
                )
            )
        elif declared and season not in facts.unknown_seasons:
            problems.append(
                issue(
                    "SEASON_UNKNOWN_MISMATCH",
                    "Only the unknown historical season may be declared unknown",
                    field="season_id",
                    line_key=key,
                )
            )
        requests = line.get("coverage_requests") or []
        if len(requests) != 1 or not requests[0].get("manifest_row_id"):
            problems.append(
                issue(
                    "COVERAGE_SOURCE_INVALID",
                    "An opening line covers exactly one manifest row.",
                    line_key=key,
                )
            )
    return problems


def submit_opening(
    run: CommandRun, head: DocumentHead, *, reviewed_hash: str, goods_pt: GoodsPt
) -> tuple[dict[str, Any], ApprovalRequest]:
    """E127 for an opening PT: reconcile against the approved manifest's accepted
    quantities and request the distinct checker's P05 approval."""
    revision = head.draft_revision
    if revision is None or head.state != DocumentHead.State.DRAFT:
        raise Refusal("STATE_CONFLICT", "Only a draft can be submitted.")
    manifest_version = goods_pt.manifest_version
    if manifest_version is None:
        raise Refusal("PT_PARENT_INVALID", "An opening PT needs its approved manifest.", status=422)
    require_opening_ready(run.tenant_id, head.document.held_site_id)
    if reviewed_hash != revision.content_hash:
        raise Refusal("REVISION_SUPERSEDED", "What you reviewed is no longer the current draft.")
    lines = current_lines(run, head)
    profile = load_profile(
        run.tenant_id,
        revision.payload.get("profile_version_id"),
        manifest_target(manifest_version.cutoff_at, head.document.held_site_id),
        code="PT_ROWS_INVALID",
    )
    fresh = checked_lines(run, revision.payload, lines, profile, stale_code="REVIEW_INCOMPLETE")
    problems = validate_opening_rows(fresh)
    if problems:
        raise Refusal(
            "PT_ROWS_INVALID", "Some rows are not valid yet.", status=422, issues=problems[:1000]
        )
    reconciliation = reconcile_opening(manifest_version.pk, fresh, revision.content_hash)
    run.reconciliation = reconciliation
    if not reconciliation["passed"]:
        raise Refusal(
            "RECONCILIATION_FAILED",
            "The opening PT does not reconcile with the approved manifest.",
            issues=reconciliation["issues"][:1000],
        )
    set_state(run, head, DocumentHead.State.SUBMITTED, event="submitted")
    request = create_request(
        run,
        subject_kind="document",
        subject_key=str(head.document_id),
        revision=head.revision,
        reviewed_hash=revision.content_hash,
        requested_action=APPROVE_OPENING,
        site_id=head.document.site_id,
        reconciliation=reconciliation,
        title=f"Opening PT for manifest {manifest_version.manifest.batch_key}",
        policy=_pin_opening(run, head, manifest_version, fresh),
    )
    run.audit_subject_key = f"document:{head.document_id}"
    return reconciliation, request


def _pin_opening(
    run: CommandRun,
    head: DocumentHead,
    manifest_version: OpeningManifestVersion,
    lines: list[dict[str, Any]],
) -> Any:
    target = manifest_target(manifest_version.cutoff_at, head.document.held_site_id)
    return pin(
        run,
        action=APPROVE_OPENING,
        purpose=OPENING,
        site_id=head.document.site_id,
        brand_ids=list(target.brand_ids),
        amounts=pt_amounts(lines),
    )


# ---------------------------------------------------------------------------
# Approval: officialise, open the manifest-backed lot and cover it (P05)
# ---------------------------------------------------------------------------


def _submitted_opening_subject(
    run: CommandRun, context: DecisionContext
) -> tuple[DocumentHead, GoodsPt, Any]:
    document_id = _uuid_or_none(context.request.subject_key)
    heads = lock_heads(run, [document_id]) if document_id else {}
    head = heads.get(document_id) if document_id else None
    goods_pt = (
        GoodsPt.objects.select_related("manifest_version__manifest", "document")
        .filter(document_id=document_id)
        .first()
    )
    if head is None or goods_pt is None or goods_pt.manifest_version is None:
        raise Refusal("NOT_FOUND", "That opening PT was not found.")
    revision = head.draft_revision
    if head.state != DocumentHead.State.SUBMITTED or revision is None:
        raise Refusal("STATE_CONFLICT", "Only a submitted opening PT can be decided.")
    if revision.content_hash != context.request.reviewed_hash:
        raise Refusal("REVISION_SUPERSEDED", "The submitted PT changed after it was reviewed.")
    checker = context.checker_id
    makers = {goods_pt.preparer_id, head.document.maker_id}
    if str(checker) in {str(m) for m in makers if m}:
        raise Refusal("SELF_APPROVAL", "Someone who prepared this PT cannot also decide it.")
    return head, goods_pt, revision


def _approve_opening(run: CommandRun, context: DecisionContext) -> dict[str, Any] | None:
    head, goods_pt, revision = _submitted_opening_subject(run, context)
    require_complete_scope(context.access, head.document_id, APPROVE_OPENING)
    context.enforce_policy(run)
    site_id = head.document.held_site_id
    manifest_version = goods_pt.manifest_version
    assert manifest_version is not None  # _submitted_opening_subject already required it
    if context.decision == "reject":
        set_state(
            run, head, DocumentHead.State.DRAFT, event="rejected", reason_code=context.reason_code
        )
        return {"state": "draft"}
    require_opening_ready(run.tenant_id, site_id)
    profile = load_profile(
        run.tenant_id,
        revision.payload.get("profile_version_id"),
        manifest_target(manifest_version.cutoff_at, site_id),
        code="PT_ROWS_INVALID",
    )
    stored = [dict(state.payload) for state in revision_lines(head.document_id, revision.revision)]
    lines = checked_lines(run, revision.payload, stored, profile, stale_code="PT_ROWS_INVALID")
    # Ticket 12: where the switch is on, a line with no HSN stops approval, by name.
    refuse_missing_hsn(lines, site_id)
    problems = validate_opening_rows(lines)
    if problems:
        raise Refusal(
            "PT_ROWS_INVALID", "Some rows are not valid.", status=422, issues=problems[:1000]
        )
    row_ids = [
        uuid.UUID(request["manifest_row_id"])
        for line in lines
        for request in line.get("coverage_requests") or []
    ]
    engine.lock_lots(
        run,
        CustodyLot.objects.filter(source_manifest_row_id__in=row_ids).values_list("pk", flat=True),
    )
    reconciliation = reconcile_opening(manifest_version.pk, lines, revision.content_hash)
    run.reconciliation = reconciliation
    if not reconciliation["passed"]:
        raise Refusal(
            "RECONCILIATION_FAILED",
            "The opening PT no longer reconciles with the approved manifest.",
            issues=reconciliation["issues"][:1000],
        )
    identity = head.document
    number = identity.official_number or allocate(run, identity.entity, OPENING_DOC_TYPE)
    frozen = [{k: v for k, v in line.items() if k != "issues"} for line in lines]
    version, official_lines = officialise(
        run,
        head,
        approved_by_id=context.checker_id,
        canonical_header={**revision.payload, "number": number},
        lines=[(uuid.UUID(line["line_key"]), line) for line in frozen],
        authority={**run.authority, "maker_id": str(identity.maker_id)},
        reconciliation=reconciliation,
        profile_version_id=profile.version.pk,
        number=number,
    )
    rows = {row.pk: row for row in OpeningManifestRow.objects.filter(pk__in=row_ids)}
    _open_and_cover(
        run,
        version=version,
        official_lines=official_lines,
        lines=frozen,
        rows=rows,
        conditions=accepted_conditions(approved_manifest(manifest_version.pk)),
        site_id=site_id,
        manifest_version=manifest_version,
        profile=profile,
    )
    run.audit_subject_key = f"document:{head.document_id}"
    run.audit_site_id = site_id
    return {"number": number, "version": version.version, "official_version_id": str(version.pk)}


def _open_and_cover(
    run: CommandRun,
    *,
    version: OfficialVersion,
    official_lines: list[OfficialLine],
    lines: list[dict[str, Any]],
    rows: dict[uuid.UUID, OpeningManifestRow],
    conditions: dict[uuid.UUID, str],
    site_id: int,
    manifest_version: OpeningManifestVersion,
    profile: ProfileContext,
) -> None:
    """P05: manifest source boundary -> site held/quarantine custody, then
    origin-evidence boundary -> site stock, in one posting.

    A reissue's manifest row already has its ``CustodyLot`` (from the first
    approval); reuse it instead of opening a second physical lot for the same
    manifest row (design line 9663: "a reissue reuses retained manifest custody
    and reclassifies it rather than adding a second physical lot").

    A row whose accepted condition is not good opens quarantine custody and stops
    there: no origin, no coverage and no value leg. Ordinary coverage values only
    good goods (``engine.cover``), and damaged value belongs to ``value_damage``
    alone (change PRD 14.5 J5), so opening must not invent a value for it. This is
    the same treatment counted damaged goods already get at P01 receiving. Its
    ``OpeningClaim`` is still written, because duplicate protection is about the
    physical quantity, not about who valued it.
    """
    plan = engine.Plan("P05", version.pk, engine.event_key("P05", version.pk))
    previous = _previous_origins(version.document_id)
    existing_lots = {
        lot.source_manifest_row_id: lot
        for lot in CustodyLot.objects.filter(source_manifest_row_id__in=list(rows))
    }
    for official, line in zip(official_lines, lines, strict=True):
        request = (line.get("coverage_requests") or [])[0]
        row = rows[uuid.UUID(request["manifest_row_id"])]
        qty = int(request["qty"])
        payload = row.payload
        # The condition the row was accepted under - the manifest's own when
        # verification agreed, otherwise the approved variance's. Never the
        # manifest's unverified claim.
        condition = conditions.get(row.pk, payload["condition"])
        lot = existing_lots.get(row.pk)
        if lot is None:
            kind = "receiving" if condition == "good" else "quarantine"
            location = engine.system_location(site_id, kind)
            lot = engine.open_lot(
                run,
                plan,
                source_kind=CustodyLot.SourceKind.OPENING,
                site_id=site_id,
                qty=qty,
                identity={
                    **payload["identity"],
                    "condition": condition,
                    "older_origin_at": payload["older_origin_at"],
                    "older_origin_ref": payload["older_origin_ref"],
                },
                source_time=manifest_version.cutoff_at,
                address=engine.Address(
                    boundary="physical",
                    site_id=site_id,
                    location_id=location.pk,
                    condition=condition,
                    sku_id=uuid.UUID(str(line["sku_id"])) if line.get("sku_id") else None,
                ),
                source_manifest_row_id=row.pk,
                from_boundary="manifest",
            )
            existing_lots[row.pk] = lot
        if condition == "good":
            prior = previous.get(str(official.stable_line_key))
            origin = _opening_origin(
                official, line, row, site_id, manifest_version.cutoff_at, profile, prior
            )
            run.record(origin)
            engine.cover(
                run,
                plan,
                lot_id=lot.pk,
                interval=(0, qty),
                origin=origin,
                pt_version_id=version.pk,
                pt_line_id=official.pk,
                site_id=site_id,
            )
        claim, created = OpeningClaim.objects.get_or_create(
            manifest_row=row,
            dataset_key=row.payload["dataset_key"],
            site_id=site_id,
            source_row_key=row.source_row_key,
            portion=portion_range(0, qty),
            defaults={"tenant_id": run.tenant_id, "pt_version": version},
        )
        if not created and claim.pt_version_id != version.pk:
            # Reissue: the same manifest row's claim is rebound to the reissued
            # version, never duplicated into a second claim (design line 361).
            claim.pt_version = version
            claim.save(update_fields=["pt_version"])
    batch_id = engine.post(run, version, plan)
    _record_posting(run, version.document_id, version.pk, "P05", batch_id)


def _opening_origin(
    official: OfficialLine,
    line: dict[str, Any],
    row: OpeningManifestRow,
    site_id: int,
    source_time: datetime,
    profile: ProfileContext,
    prior: Origin | None,
) -> Origin:
    calculated = line["calculated"]
    cost = int(str(calculated["p_rate_paise"]))
    mrp = int(str(calculated.get("mrp_paise") or (line.get("supplied") or {}).get("mrp_paise")))
    payload = row.payload
    return Origin(
        lineage_key=prior.lineage_key if prior else uuid.uuid4(),
        official_line_id=official.pk,
        site_id=site_id,
        source_time=source_time,
        source_kind=Origin.SourceKind.OPENING,
        sku_id=uuid.UUID(str(line["sku_id"])),
        unit_cost=cost,
        mrp=mrp,
        opening_qty=sum(int(r["qty"]) for r in line.get("coverage_requests") or []),
        previous_origin_id=prior.pk if prior else None,
        frozen_evidence={
            "opening_site_id": str(site_id),
            "manifest_row_id": str(row.pk),
            "dataset_key": payload["dataset_key"],
            "source_row_key": row.source_row_key,
            "source_time_basis": "cutoff",
            # Never invented: absent here means the older origin stays unknown.
            "older_origin_at": payload.get("older_origin_at"),
            "older_origin_ref": payload.get("older_origin_ref"),
            "identity_as_used": {
                "sku_id": line["sku_id"],
                "attributes": line.get("attributes") or [],
            },
            "alias_as_used": line.get("alias_as_used"),
            "season_id": line.get("season_id"),
            # Frozen beside the season it qualifies: a stock read years later can
            # still say "unknown historical season" without asking the master.
            "season_unknown_historical": bool(payload.get("season_unknown_historical")),
            "hsn": line.get("hsn"),
            "profile_version_id": str(profile.version.pk),
            "rate_version_id": str(profile.rates_version.pk),
            "tax_version_id": str(profile.tax_version.pk),
            "cost_paise": str(cost),
            "mrp_paise": str(mrp),
        },
    )


# ---------------------------------------------------------------------------
# OPS-03: a later, governed season correction
# ---------------------------------------------------------------------------


def latest_season_correction(row_id: uuid.UUID) -> OpeningSeasonCorrection | None:
    """The correction in force for one manifest row, or none."""
    return (
        OpeningSeasonCorrection.objects.filter(manifest_row_id=row_id)
        .order_by("recorded_at", "event_at")
        .last()
    )


def latest_season_corrections(row_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Row id -> the season each row has been corrected to, for the rows that have one.

    One query for a page of rows: stock reads overlay this on every opening
    position, and asking row by row would cost a query per line on screen.
    """
    out: dict[uuid.UUID, int] = {}
    for correction in OpeningSeasonCorrection.objects.filter(
        manifest_row_id__in=list(row_ids)
    ).order_by("recorded_at", "event_at"):
        out[correction.manifest_row_id] = correction.to_season_id
    return out


def season_in_force(row: OpeningManifestRow) -> int:
    correction = latest_season_correction(row.pk)
    return correction.to_season_id if correction else int(row.payload["season_id"])


def record_season_correction(
    run: CommandRun,
    *,
    manifest: OpeningManifest,
    manifest_row_id: uuid.UUID,
    season_id: int,
    reason: str,
) -> OpeningSeasonCorrection:
    """Establish the real season of an opening row, without rewriting anything.

    The manifest row, its verification, the opening PT's official version and
    every bill, label or transfer snapshot already issued keep what they said;
    this only appends the fact that the cohort is now known, with who said so,
    when and why (store and warehouse operations PRD §4).
    """
    require_opening_ready(run.tenant_id, manifest.site_id)
    row = OpeningManifestRow.objects.filter(
        pk=manifest_row_id, manifest_version__manifest=manifest
    ).first()
    if row is None:
        raise Refusal("NOT_FOUND", "That manifest row was not found.")
    if not reason.strip():
        raise Refusal("INVALID_REQUEST", "A correction needs a reason.")
    season = Season.objects.filter(pk=season_id).first()
    if season is None:
        raise Refusal(
            "OPENING_SEASON_INVALID",
            "That season does not exist.",
            status=422,
            issues=[
                issue("SEASON_REQUIRED", "A real mapped season is required", field="season_id")
            ],
        )
    if season.historical_unknown:
        raise Refusal(
            "OPENING_SEASON_INVALID",
            "A correction establishes the real season; it cannot name the unknown one.",
            status=422,
            issues=[
                issue(
                    "SEASON_UNKNOWN_MISMATCH",
                    "A correction names a real mapped season",
                    field="season_id",
                )
            ],
        )
    from_season_id = season_in_force(row)
    if from_season_id == season.pk:
        raise Refusal(
            "OPENING_SEASON_INVALID",
            "This row is already in that season.",
            status=422,
        )
    correction: OpeningSeasonCorrection = run.record(
        OpeningSeasonCorrection(
            tenant_id=run.tenant_id,
            manifest_row=row,
            from_season_id=from_season_id,
            to_season_id=season.pk,
            reason=reason.strip()[:500],
            actor_id=run.principal.human_id,
        )
    )
    run.audit_after = {
        "manifest_row_id": str(row.pk),
        "from_season_id": str(from_season_id),
        "to_season_id": str(season.pk),
    }
    return correction


def correction_dto(correction: OpeningSeasonCorrection) -> dict[str, Any]:
    return {
        "id": str(correction.pk),
        "manifest_row_id": str(correction.manifest_row_id),
        "from_season_id": str(correction.from_season_id),
        "to_season_id": str(correction.to_season_id),
        "reason": correction.reason,
        "recorded_at": correction.recorded_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# E241: dedicated owner action for "historical origin unavailable"
# ---------------------------------------------------------------------------


def confirm_origin_unavailable(
    run: CommandRun,
    *,
    exception_id: uuid.UUID,
    manifest_row_id: uuid.UUID,
    reason_code: str,
    evidence_ids: list[uuid.UUID],
    reviewed_hash: str,
) -> Any:
    from alerts.goods_models import GoodsException, OriginInvestigationOutcome

    exception = GoodsException.objects.filter(
        pk=exception_id, kind="opening_origin_unavailable"
    ).first()
    if exception is None or exception.subject_key != f"manifest_row:{manifest_row_id}":
        raise Refusal(
            "EXCEPTION_ACTION_INVALID",
            "That is not an open origin-unavailable investigation for this row.",
        )
    row = (
        OpeningManifestRow.objects.select_related("manifest_version")
        .filter(pk=manifest_row_id)
        .first()
    )
    if row is None:
        raise Refusal("NOT_FOUND", "That manifest row was not found.")
    require_opening_ready(run.tenant_id, row.site_id)
    # E241/design 5.8: `reviewed_hash` is outcome evidence - it says which manifest
    # version the owner actually read before closing the investigation. Stored
    # unchecked it pins nothing, so compare it with that version's own hash.
    if reviewed_hash != row.manifest_version.content_hash:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "This manifest was revised after you reviewed it; reload the row.",
        )
    if exception.state != "open":
        raise Refusal("EXCEPTION_ACTION_INVALID", "This investigation is already resolved.")
    if row.payload.get("older_origin_at") or row.payload.get("older_origin_ref"):
        raise Refusal("EXCEPTION_ACTION_INVALID", "This row already names an older origin.")
    from files.goods_models import EvidenceObject

    known_evidence = set(
        EvidenceObject.objects.filter(tenant_id=run.tenant_id, pk__in=evidence_ids).values_list(
            "pk", flat=True
        )
    )
    if not evidence_ids or set(evidence_ids) - known_evidence:
        raise Refusal(
            "EXCEPTION_ACTION_INVALID",
            "Confirming an origin unavailable requires at least one known evidence file.",
            issues=[
                issue(
                    "ORIGIN_EVIDENCE_REQUIRED",
                    "Supporting evidence is required",
                    field="evidence_ids",
                )
            ],
        )
    outcome = run.record(
        OriginInvestigationOutcome(
            tenant_id=run.tenant_id,
            exception=exception,
            manifest_row=row,
            reviewed_hash=reviewed_hash,
            reason_code=reason_code[:60],
            evidence_ids=[str(e) for e in evidence_ids],
            actor_id=run.principal.human_id,
        )
    )
    resolve_exceptions(
        run,
        kind="opening_origin_unavailable",
        subject_key=exception.subject_key,
        reason_code="HISTORICAL_ORIGIN_UNAVAILABLE",
    )
    return outcome


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _opening_request_cells(
    requests: list[ApprovalRequest],
) -> dict[Any, frozenset[tuple[int | None, int | None]]]:
    """An opening PT's approval covers its complete scope too - every line SKU's
    brand, not just the manifest's site (mirrors receipt's ``_request_cells``)."""
    by_document: dict[uuid.UUID, list[ApprovalRequest]] = defaultdict(list)
    for request in requests:
        try:
            by_document[uuid.UUID(str(request.subject_key))].append(request)
        except ValueError:
            continue
    goods_pts = GoodsPt.objects.select_related("document", "manifest_version__manifest").filter(
        document_id__in=list(by_document)
    )
    return {
        request.pk: cells
        for document_id, cells in pt_cells_many(goods_pts).items()
        for request in by_document[document_id]
    }


def install() -> None:
    register_subject_handler(
        ApprovalRequest.SubjectKind.MANIFEST,
        MANIFEST_ACTION,
        _decide_manifest,
        policy_governed=False,
    )
    register_subject_handler(
        "opening_variance", VARIANCE_ACTION, _decide_variance, policy_governed=False
    )
    register_subject_handler("document", APPROVE_OPENING, _approve_opening)
    register_subject_cells("document", APPROVE_OPENING, _opening_request_cells)
    register_integrity_refusal(
        "uq_openingmanifest_batch",
        "MANIFEST_INVALID",
        "That batch already has an opening manifest.",
    )
    register_integrity_refusal(
        "uq_goodspt_per_manifest_version",
        "PT_PARENT_INVALID",
        "This manifest version already has its opening PT.",
    )
    register_integrity_refusal(
        "ex_openingclaim_overlap",
        "OPENING_VARIANCE_INVALID",
        "This row's accepted quantity no longer matches its original opening posting.",
    )
