"""Business logic behind the masters goods-v1 endpoints (design §5.2, §5.3, §5.5, §6.1).

Legacy ``LegalEntity``/``Gstin``/``Store``/``Brand``/``Season`` rows stay the
readable identities that legacy callers already use. The full, versioned
``MasterPayload`` for each of them (and for ``subbrand``, which has no legacy
identity row at all) lives only in the immutable ``MasterVersion`` chain, keyed
by ``(kind, target_key)``. A legacy convenience column is kept in sync where one
already exists (``code``/``name``/``pan``/``is_active`` and similar) so legacy
readers keep answering from the same row; nothing here invents a new legacy
column or migration.

Every mutable master here that has no explicit revision column of its own
(entity, registration, site, brand, season, subbrand) is tracked through
``core.kernel_models.SubjectRevision`` — one counter per ``(family, subject_key)``,
exactly as the shared schema note in design §5.5 describes.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.commands import CommandRun, LockRank
from core.goods_money import MoneyInvalid, paise_from_json
from core.kernel_models import SubjectRevision
from core.refusals import Refusal, issue
from masters.goods_models import (
    ConfigDraft,
    ConfigVersion,
    EffectiveVersionPeriod,
    Location,
    MasterVersion,
    Sbu,
    SiteGuard,
)
from masters.models import Brand, LegalEntity, Store

# --------------------------------------------------------------------------
# MasterPayload field sets (design §5.3)
# --------------------------------------------------------------------------

ENTITY_FIELDS = frozenset({"code", "name", "pan", "address", "books_code"})
REGISTRATION_FIELDS = frozenset(
    {"entity_id", "gstin", "state_code", "state_name", "effective_from"}
)
SITE_FIELDS = frozenset(
    {
        "code",
        "name",
        "aliases",
        "city",
        "state",
        "country",
        "type",
        "entity_id",
        "registration_id",
        "counter_count",
        "partner_ref",
        "opening_date",
        "linked_warehouse_id",
        "permitted_operations",
        "brand_ids",
    }
)
BRAND_FIELDS = frozenset({"code", "name", "parent_id"})
SEASON_FIELDS = frozenset({"code", "name", "parent_id"})
SUBBRAND_FIELDS = frozenset({"code", "name", "parent_id"})
LOCATION_FIELDS = frozenset({"site_id", "parent_id", "name", "kind"})

SITE_TYPES = frozenset({"warehouse", "store", "DC", "concession", "virtual"})
PERMITTED_OPERATIONS = frozenset({"receive", "hold", "transfer", "sell"})


def parse_dt(value: Any, field: str, *, required: bool = True) -> datetime | None:
    if value in (None, ""):
        if required:
            raise Refusal(
                "MASTER_INVALID",
                f"{field} is required.",
                issues=[issue("REQUIRED", f"{field} is required", field=field)],
            )
        return None
    parsed = parse_datetime(str(value)) if isinstance(value, str) else None
    if parsed is None:
        raise Refusal(
            "MASTER_INVALID",
            f"{field} must be an RFC3339 timestamp.",
            issues=[issue("INVALID", "not a timestamp", field=field)],
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.get_current_timezone())
    return parsed


def parse_day(value: Any, field: str) -> date | None:
    if value in (None, ""):
        return None
    from django.utils.dateparse import parse_date

    parsed = parse_date(str(value))
    if parsed is None:
        raise Refusal(
            "MASTER_INVALID",
            f"{field} must be a date (YYYY-MM-DD).",
            issues=[issue("INVALID", "not a date", field=field)],
        )
    return parsed


# --------------------------------------------------------------------------
# SubjectRevision — the revision head for legacy-anchored masters
# --------------------------------------------------------------------------


def start_revision(tenant_id: uuid.UUID, family: str, subject_key: str) -> None:
    SubjectRevision.objects.create(
        tenant_id=tenant_id, family=family[:60], subject_key=subject_key[:100], revision=1
    )


def lock_revision(
    run: CommandRun, family: str, subject_key: str, *, rank: LockRank
) -> SubjectRevision:
    rows = run.lock(
        rank,
        SubjectRevision.objects.filter(
            tenant_id=run.tenant_id, family=family, subject_key=subject_key
        ),
    )
    if not rows:
        raise Refusal("NOT_FOUND", "That record was not found.")
    return cast(SubjectRevision, rows[0])


def bump_revision(head: SubjectRevision) -> int:
    head.revision += 1
    head.save(update_fields=["revision"])
    return head.revision


def current_revision(family: str, subject_key: str) -> int | None:
    row = SubjectRevision.objects.filter(family=family, subject_key=subject_key).first()
    return row.revision if row else None


# --------------------------------------------------------------------------
# MasterVersion — the versioned payload chain
# --------------------------------------------------------------------------


def append_master_version(
    run: CommandRun,
    *,
    kind: str,
    target_key: str,
    revision: int,
    payload: dict[str, Any],
    effective_from: datetime | None = None,
    retired: bool = False,
    reason_code: str | None = None,
) -> MasterVersion:
    return cast(
        MasterVersion,
        run.record(
            MasterVersion(
                kind=kind,
                target_key=target_key[:100],
                revision=revision,
                payload=payload,
                retired=retired,
                reason_code=reason_code,
                effective_from=effective_from or run.now,
            )
        ),
    )


def latest_master_version(tenant_id: uuid.UUID, kind: str, target_key: str) -> MasterVersion | None:
    return (
        MasterVersion.objects.filter(tenant_id=tenant_id, kind=kind, target_key=str(target_key))
        .order_by("-revision")
        .first()
    )


def latest_versions_by_kind(tenant_id: uuid.UUID, kind: str) -> list[MasterVersion]:
    """The current (highest-revision) version for each distinct ``target_key`` of ``kind``."""
    rows = MasterVersion.objects.filter(tenant_id=tenant_id, kind=kind).order_by(
        "target_key", "-revision"
    )
    latest: dict[str, MasterVersion] = {}
    for row in rows:
        latest.setdefault(row.target_key, row)
    return list(latest.values())


def master_state(latest: MasterVersion | None) -> str:
    if latest is None:
        return "active"
    return "retired" if latest.retired else "active"


# --------------------------------------------------------------------------
# GSTIN validation (design §5.2 masters.Gstin)
# --------------------------------------------------------------------------


def validate_gstin_number(value: str, state_code: str) -> None:
    text = str(value or "").strip().upper()
    if len(text) != 15:
        raise Refusal(
            "MASTER_INVALID",
            "A GSTIN is exactly 15 characters.",
            issues=[issue("INVALID", "must be 15 characters", field="gstin")],
        )
    if text[:2] != str(state_code or "").strip():
        raise Refusal(
            "MASTER_INVALID",
            "The GSTIN's first two digits must match its state code.",
            issues=[issue("INVALID", "state code mismatch", field="gstin")],
        )


# --------------------------------------------------------------------------
# Site setup: system locations and SBUs (design §5.2 masters.Location/Sbu)
# --------------------------------------------------------------------------


def ensure_system_locations(tenant_id: uuid.UUID, site: Store) -> list[Location]:
    """Create the six protected system locations once per site (idempotent)."""
    existing = set(Location.objects.filter(site=site, system=True).values_list("kind", flat=True))
    created = []
    for kind in Location.SYSTEM_KINDS:
        if kind in existing:
            continue
        created.append(
            Location.objects.create(
                tenant_id=tenant_id,
                site=site,
                parent=None,
                name=kind.replace("_", " ").title(),
                kind=kind,
                system=True,
            )
        )
    return created


def ensure_site_sbus(tenant_id: uuid.UUID, site: Store, brand_ids: list[int]) -> list[Sbu]:
    if site.tenant_id != tenant_id or Brand.objects.filter(
        tenant_id=tenant_id, pk__in=brand_ids, is_active=True,
    ).count() != len(set(brand_ids)):
        raise Refusal("MASTER_INVALID", "Each site brand must be an active identity in this company.")
    created = []
    fallback, made = Sbu.objects.get_or_create(
        site=site, brand=None, defaults={"tenant_id": tenant_id, "code": f"{site.code}-ALL"[:40]}
    )
    if made:
        created.append(fallback)
    for brand_id in brand_ids:
        sbu, made = Sbu.objects.get_or_create(
            site=site,
            brand_id=brand_id,
            defaults={"tenant_id": tenant_id, "code": f"{site.code}-{brand_id}"[:40]},
        )
        if made:
            created.append(sbu)
    return created


def site_guard_for(site: Store) -> SiteGuard | None:
    return SiteGuard.objects.filter(site=site).first()


# --------------------------------------------------------------------------
# Readiness checks (design §3.2/§3.3, E069)
# --------------------------------------------------------------------------


#: The numbering series receiving issues: GRN, counter-GRN and receipt PT.
READINESS_SERIES = ("GRN", "CGRN", "RPT")
#: Stock residual quantities a closure must see decided, in reporting order.
STOCK_RESIDUALS = ("physical_qty", "transit_qty", "unvalued_qty", "reserved_qty")
DECISION_KEYS = frozenset({"code", "message", "field", "line_key", "quantity"})


def _check(key: str, passed: bool, reason: str, *, overridable: bool) -> dict[str, Any]:
    return {
        "key": key,
        "passed": passed,
        "required": True,
        "overridable": overridable,
        "reason": None if passed else reason,
    }


def _version_effective(version: MasterVersion | None, now: datetime) -> bool:
    """The latest master record exists, is not retired and is in force at ``now``."""
    return (
        version is not None
        and not version.retired
        and version.effective_from <= now
        and (version.effective_to is None or version.effective_to > now)
    )


def compute_readiness_checks(site: Store, now: datetime) -> list[dict[str, Any]]:
    """What the site's effective setup satisfies at ``now`` (E069 step 10).

    Every answer is read from approved, in-force records. A requirement with no
    record to prove it fails: an unknown is never taken as satisfied.
    """
    tenant_id = site.tenant_id
    gstin = site.gstin if site.gstin_id else None
    entity = gstin.legal_entity if gstin is not None else None
    checks = [
        _check(
            "legal_entity",
            entity is not None
            and entity.is_active
            and _version_effective(latest_master_version(tenant_id, "entity", str(entity.pk)), now),
            "The site's legal entity has no active, effective record.",
            overridable=False,
        ),
        _check(
            "registration",
            gstin is not None
            and gstin.is_active
            and _version_effective(
                latest_master_version(tenant_id, "registration", str(gstin.pk)), now
            ),
            "The site's registration has no active, effective record.",
            overridable=False,
        ),
        _series_check(entity, now),
        _locations_check(site),
        _business_units_check(site),
        _business_profile_check(tenant_id, now),
        _working_calendar_check(tenant_id, now),
        _receipt_configuration_check(site, now),
        _staff_check(site, now),
    ]
    return checks


def _series_check(entity: LegalEntity | None, now: datetime) -> dict[str, Any]:
    from core.documents import VoucherSeries
    from core.fiscal import financial_year
    from core.numbering import confirmed_ceiling, series_store_code

    missing = list(READINESS_SERIES)
    if entity is not None:
        fy = financial_year(timezone.localtime(now).date())
        ready = {
            series.doc_type
            for series in VoucherSeries.objects.filter(
                store_code=series_store_code(entity.pk),
                doc_type__in=READINESS_SERIES,
                fy=fy,
                scope_version="entity_v1",
            )
            if confirmed_ceiling(series.pk) > 0
        }
        missing = [doc_type for doc_type in READINESS_SERIES if doc_type not in ready]
    return _check(
        "document_series",
        not missing,
        f"No published numbering series this financial year for: {', '.join(missing)}.",
        overridable=False,
    )


def _locations_check(site: Store) -> dict[str, Any]:
    have = set(
        Location.objects.filter(site=site, system=True, retired_at__isnull=True).values_list(
            "kind", flat=True
        )
    )
    missing = [kind for kind in Location.SYSTEM_KINDS if kind not in have]
    return _check(
        "system_locations",
        not missing,
        f"The site's system locations are not set up: {', '.join(missing)}.",
        overridable=False,
    )


def _business_units_check(site: Store) -> dict[str, Any]:
    latest = latest_master_version(site.tenant_id, "site", str(site.pk))
    brand_ids = {int(b) for b in ((latest.payload if latest else {}) or {}).get("brand_ids") or []}
    live = list(
        Sbu.objects.filter(site=site, retired_at__isnull=True).values_list("brand_id", flat=True)
    )
    missing = sorted(brand_ids - {b for b in live if b is not None})
    passed = bool(live) and not missing
    reason = (
        "The site has no business unit."
        if not live
        else f"Brands without a business unit at this site: {', '.join(map(str, missing))}."
    )
    return _check("business_units", passed, reason, overridable=True)


def _business_profile_check(tenant_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    from masters.goods_config import in_force
    from masters.goods_models import Tenant

    tenant = Tenant.objects.select_related("business_profile_version").get(pk=tenant_id)
    profile = tenant.business_profile_version
    passed = profile is not None and profile.kind == "business_profile" and in_force(profile, now)
    return _check(
        "business_profile",
        passed,
        "The business has no approved business profile in force.",
        overridable=True,
    )


def _working_calendar_check(tenant_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    """GSA-T08, design §5.8: working-day deadlines must have an approved calendar.

    Two gaps fail this, and both are named rather than counted:

    * the business has no ``working_calendar`` in force. This is the one that
      bites: every built-in working-day SLA
      (``alerts.goods_services.EXCEPTION_DEFAULTS``) then opens its exceptions
      with no deadline at all, because GSA-T08 forbids inferring a week.
    * an effective ``working_days`` notification policy names no approved
      calendar, or one withdrawn for invalidity. A policy naming a calendar that
      a later approval merely *superseded* is not a gap - that is ordinary
      supersession, and the deadlines the system actually computes come from the
      calendar in force, not from the policy's pin.

    Not overridable (design §1.3: "goods readiness cannot be approved without its
    approved compatible calendar"). It is read tenant-wide while the rest of this
    list is per-site, because a calendar and a notification policy are the
    business's, not one site's; a policy scoped to other sites still fails here,
    which is over-inclusive on purpose - a policy naming a withdrawn calendar is
    a configuration error wherever it points.
    """
    from masters.goods_config import approved_working_calendar

    gaps: list[str] = []
    if approved_working_calendar(tenant_id, now) is None:
        gaps.append("the business has approved no working calendar in force")
    uncovered = sorted(_policies_without_a_calendar(tenant_id, now))
    gaps += [f"the {event} notification policy names no approved calendar" for event in uncovered]
    return _check(
        "working_calendar",
        not gaps,
        "Working-day deadlines are not governed: " + "; ".join(gaps) + ".",
        overridable=False,
    )


def _policies_without_a_calendar(tenant_id: uuid.UUID, now: datetime) -> set[str]:
    """The ``event`` of each effective ``working_days`` notification policy whose
    ``calendar_version_id`` names no approved, unwithdrawn ``working_calendar``.

    Both kinds are read in one query each, so this costs two queries however many
    policies the business has.
    """
    from masters.goods_config import candidates

    wanted: dict[str, set[str]] = {}
    for found in candidates(
        ConfigVersion.objects.filter(tenant_id=tenant_id, kind="notifications")
    ):
        if found.state(now) != "effective":
            continue
        payload = found.version.payload if isinstance(found.version.payload, dict) else {}
        if payload.get("sla_unit") != "working_days":
            continue
        wanted.setdefault(str(payload.get("calendar_version_id")), set()).add(
            str(payload.get("event"))
        )
    if not wanted:
        return set()
    usable = {
        str(found.version.pk)
        for found in candidates(
            ConfigVersion.objects.filter(tenant_id=tenant_id, kind="working_calendar")
        )
        if found.withdrawn_at is None
    }
    return {event for named, events in wanted.items() if named not in usable for event in events}


def _receipt_configuration_check(site: Store, now: datetime) -> dict[str, Any]:
    """A receipt PT profile (with its pinned rates and tax) and approval policy apply here."""
    from masters.goods_config import ConfigTarget, candidates, check_pinned

    brands = [
        b
        for b in Sbu.objects.filter(site=site, retired_at__isnull=True).values_list(
            "brand_id", flat=True
        )
        if b is not None
    ]
    scopes: list[int | None] = list(brands) or [None]
    targets = [
        ConfigTarget.of(now, site_id=site.pk, brand_ids=[brand] if brand else [], purpose="receipt")
        for brand in scopes
    ]
    profiles = candidates(ConfigVersion.objects.filter(tenant_id=site.tenant_id, kind="profile"))
    policies = candidates(
        ConfigVersion.objects.filter(
            tenant_id=site.tenant_id, kind="approval", payload__action="pt.approve.receipt"
        )
    )
    gaps: list[str] = []
    for target in targets:
        usable = False
        for found in profiles:
            if found.failure(target) is not None:
                continue
            payload = found.version.payload if isinstance(found.version.payload, dict) else {}
            try:
                for kind, key in (("rates", "rates_version_id"), ("tax_rates", "tax_version_id")):
                    check_pinned(
                        site.tenant_id,
                        kind,
                        payload.get(key),
                        target,
                        code="READINESS_MISSING",
                        path=key,
                    )
            except Refusal:
                continue
            usable = True
            break
        label = f"brand {target.brand_ids[0]}" if target.brand_ids else "the site"
        if not usable:
            gaps.append(f"no receipt PT profile with effective rates and tax for {label}")
        if not any(policy.failure(target) is None for policy in policies):
            gaps.append(f"no receipt PT approval policy for {label}")
    return _check(
        "receipt_configuration",
        not gaps,
        "Receiving is not configured: " + "; ".join(gaps) + ".",
        overridable=True,
    )


def _staff_check(site: Store, now: datetime) -> dict[str, Any]:
    from django.db.models import Q

    from accounts.goods_models import StaffAssignment

    passed = (
        StaffAssignment.objects.filter(
            site=site,
            effective_from__lte=now,
            staff__human__active=True,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=now))
        .filter(Q(staff__retired_at__isnull=True) | Q(staff__retired_at__gt=now))
        .exists()
    )
    return _check(
        "staff_assigned",
        passed,
        "No active staff member is assigned to this site now.",
        overridable=True,
    )


def parse_decisions(raw: Any) -> list[dict[str, Any]]:
    """``residual_decisions``: closed ``Issue`` objects, each with a real reason."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 1000:
        raise Refusal("INVALID_REQUEST", "residual_decisions must be a list of decisions.")
    decisions: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        path = f"residual_decisions[{index}]"
        if not isinstance(item, dict) or set(item) - DECISION_KEYS:
            raise Refusal(
                "INVALID_REQUEST", f"{path} must be {{code, message, field?, quantity?}}."
            )
        code, message = item.get("code"), item.get("message")
        field, quantity = item.get("field"), item.get("quantity")
        if not isinstance(code, str) or not code or len(code) > 80:
            raise Refusal("INVALID_REQUEST", f"{path}.code must name the item decided.")
        if not isinstance(message, str) or not message.strip() or len(message) > 500:
            raise Refusal("INVALID_REQUEST", f"{path}.message must give the reason.")
        if field is not None and (not isinstance(field, str) or len(field) > 100):
            raise Refusal("INVALID_REQUEST", f"{path}.field must be text.")
        if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, int)):
            raise Refusal("INVALID_REQUEST", f"{path}.quantity must be a whole number.")
        decisions.append(
            {"code": code, "field": field, "quantity": quantity, "reason": message.strip()}
        )
    return decisions


def match_decisions(
    required: list[dict[str, Any]], decisions: list[dict[str, Any]], *, code: str, message: str
) -> list[dict[str, Any]]:
    """Pair every required item with exactly one decision naming it as it stands now.

    ``required`` items carry ``code`` and, where they apply, ``field`` and ``quantity``.
    A missing, duplicated, stale or unrelated decision refuses the whole request:
    only a decision about each current item, and nothing else, decides anything.
    Returns the required items with their ``decision``.
    """

    def identity(item: dict[str, Any]) -> tuple[Any, Any, Any]:
        return (item["code"], item.get("field"), item.get("quantity"))

    wanted = {identity(item): item for item in required}
    problems: list[dict[str, Any]] = []
    seen: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for decision in decisions:
        key = identity(decision)
        if key not in wanted:
            problems.append(
                issue("DECISION_UNMATCHED", f"{decision['code']} is not a current item to decide.")
            )
        elif key in seen:
            problems.append(issue("DECISION_DUPLICATED", f"{decision['code']} is decided twice."))
        else:
            seen[key] = decision
    for key, item in wanted.items():
        if key not in seen:
            problems.append(
                issue(
                    "DECISION_MISSING",
                    item.get("reason") or f"{item['code']} needs an explicit decision.",
                    field=item.get("field") or item["code"],
                    quantity=item.get("quantity"),
                )
            )
    if problems:
        raise Refusal(code, message, issues=problems)
    return [{**item, "decision": {"reason": seen[identity(item)]["reason"]}} for item in required]


def enforce_checks_for_approval(
    checks: list[dict[str, Any]], raw_decisions: Any
) -> list[dict[str, Any]]:
    """Approval needs every mandatory check passing and one reasoned decision per other gap.

    Returns the checks with each override decision attached, for the capability event.
    """
    decisions = parse_decisions(raw_decisions)
    failing = [c for c in checks if c["required"] and not c["passed"]]
    unoverridable = [c for c in failing if not c["overridable"]]
    if unoverridable:
        raise Refusal(
            "READINESS_UNOVERRIDABLE",
            "This site is missing a mandatory readiness requirement.",
            issues=[
                issue("READINESS", c["reason"] or c["key"], field=c["key"]) for c in unoverridable
            ],
        )
    decided = match_decisions(
        [{"code": c["key"], "reason": c["reason"]} for c in failing],
        decisions,
        code="READINESS_MISSING",
        message="Each unresolved readiness item needs its own reasoned owner decision.",
    )
    reasons = {item["code"]: item["decision"] for item in decided}
    return [{**c, "decision": reasons.get(c["key"])} for c in checks]


#: The two readiness actions that open a capability rather than close one.
APPROVAL_ACTIONS = frozenset({"approve_opening_setup", "approve_goods", "approve_sell"})


def apply_readiness_approval(
    run: CommandRun,
    *,
    store: Store,
    guard: SiteGuard,
    action: str,
    checks: list[dict[str, Any]],
    reason_code: str,
    residual_decisions: Any,
    approver_id: Any,
    evidence_id: Any = None,
) -> Any:
    """Approve a site's opening setup or its goods work, and record why.

    The one statement of what approving a site means: every mandatory
    requirement passing, one reasoned decision against each remaining gap, the
    guard flag opened, and a capability event carrying the checks exactly as
    they stood - so anyone reading the event afterwards can see what was
    satisfied and what was decided over.

    The readiness screen's own command calls this, and so does the development
    seed that opens its fixture pair, which is the point: a fixture site becomes
    ready by the route a real one takes, refusals included, rather than by
    having its guard written to.
    """
    from masters.goods_models import SiteCapabilityEvent

    if action not in APPROVAL_ACTIONS:
        raise Refusal("INVALID_REQUEST", f"{action} is not a readiness approval.")
    finishing_opening = (
        action == "approve_goods"
        and guard.lifecycle == SiteGuard.Lifecycle.OPENING
        and guard.selling_mode == SiteGuard.SellingMode.ONLINE_ALPHA
    )
    if finishing_opening:
        from ptmapper.soh_services import is_reconciled

        checks = [*checks, _check(
            "opening_reconciled", is_reconciled(store),
            "Approve and physically accept every opening batch before activating this store.",
            overridable=False,
        )]
    recorded = enforce_checks_for_approval(checks, residual_decisions)
    if action == "approve_opening_setup":
        guard.opening_setup_ready = True
    elif action == "approve_sell":
        guard.sell_ready = True
    else:
        guard.goods_ready = True
        if guard.lifecycle == SiteGuard.Lifecycle.PLANNED or finishing_opening:
            guard.lifecycle = SiteGuard.Lifecycle.ACTIVE
    event = run.record(
        SiteCapabilityEvent(
            site=store,
            operation=(
                SiteCapabilityEvent.Operation.OPENING_SETUP
                if action == "approve_opening_setup"
                else SiteCapabilityEvent.Operation.SELL
                if action == "approve_sell"
                else SiteCapabilityEvent.Operation.GOODS
            ),
            outcome=SiteCapabilityEvent.Outcome.APPROVED,
            site_revision=guard.revision,
            checks=recorded,
            reason_code=reason_code,
            evidence_id=evidence_id,
            approver_id=approver_id,
        )
    )
    guard.capability_event = event
    guard.revision += 1
    guard.save()
    return event


#: GSA-T04 (ticket 04B): the governed setup command that fixes each readiness
#: check, by the real goods-v1 API path the resolving screen already calls. A
#: check with no entry has no operator-facing command in this stage — numbering
#: series are published by activation and seeds, never by a request — so its
#: exception names no action rather than an invented one, and the centre says so.
SETUP_RESOLUTION_ACTIONS: dict[str, list[str]] = {
    "legal_entity": ["masters/entities/{id}"],
    "registration": ["masters/gstins/{id}"],
    "system_locations": ["masters/stores/{id}/locations"],
    "business_units": ["masters/stores/{id}/sbus"],
    "business_profile": ["masters/configurations"],
    "working_calendar": ["masters/configurations"],
    "receipt_configuration": ["masters/configurations"],
    "staff_assigned": ["accounts/admin/staff/{id}/assign"],
}

#: A stable name for each site's setup exceptions, so re-running the readiness
#: check names the same work rather than a second row (GSA-T04: "repeating a
#: producer does not duplicate the same unresolved work").
SETUP_EXCEPTION_NAMESPACE = uuid.UUID("6f1a4d52-2b8e-5c7a-9d31-8e0f4a6b1c25")


def sync_setup_exceptions(run: CommandRun, site: Store, checks: list[dict[str, Any]]) -> None:
    """Open owned work for each failed required setup item, and close it once it passes.

    GSA-T04: "missing or invalid required setup routes to the site-transition/owner
    readiness queue". The readiness command is both the producer and the observer —
    an item is discovered failing here and discovered fixed here — so this runs on
    every readiness action, `check` included. Nothing else opens this kind: an
    ordinary master version, retirement or withdrawal that leaves the site's setup
    still valid changes no check, and so raises nothing.

    A check that fails again after it was fixed reopens its own exception rather
    than leaving a resolved row behind a live problem.
    """
    from alerts.goods_services import open_exception, resolve_exceptions

    for check in checks:
        if not check.get("required"):
            continue
        key = str(check["key"])
        subject_key = f"setup:{site.pk}:{key}"
        source_event_key = uuid.uuid5(SETUP_EXCEPTION_NAMESPACE, f"{run.tenant_id}:{site.pk}:{key}")
        if check.get("passed"):
            resolve_exceptions(
                run,
                kind="setup_configuration_invalid",
                subject_key=subject_key,
                reason_code=key.upper(),
                source_event_key=source_event_key,
            )
            continue
        open_exception(
            run,
            kind="setup_configuration_invalid",
            site_id=site.pk,
            subject_key=subject_key,
            reason_code=key.upper(),
            source_event_key=source_event_key,
            allowed_resolution_actions=list(SETUP_RESOLUTION_ACTIONS.get(key, [])),
            note=check.get("reason"),
            reopen=True,
        )


def readiness_residuals(site: Store) -> dict[str, Any]:
    """ReadinessDTO.residuals (design §5.8): quantities across every range, or null if unknowable.

    Physical, unvalued and reserved quantities are measured over the site's physical
    custody ranges; transit covers goods leaving or heading to the site. Stock held
    only in the legacy ledger is invisible to these ranges, so while any remains the
    quantities it could affect are unknown and the residual is partial - never a
    false zero.
    """
    from django.db.models import Q

    from alerts.goods_models import GoodsException
    from core.goods_fields import bounds
    from stockledger import ranges
    from stockledger.goods_models import ActiveReservation, Position
    from stockledger.models import InTransitStock, QuarantineStock, StockOnHand

    exceptions = sorted(
        str(pk)
        for pk in GoodsException.objects.filter(site=site, state="open").values_list(
            "pk", flat=True
        )
    )
    held_legacy = (
        StockOnHand.objects.filter(store=site).exclude(net_qty=0).exists()
        or QuarantineStock.objects.filter(store=site).exclude(qty=0).exists()
    )
    moving_legacy = (
        InTransitStock.objects.filter(Q(source_store=site) | Q(destination_store=site))
        .exclude(qty=0)
        .exists()
    )
    physical: dict[Any, list[tuple[int, int]]] = {}
    quantities = dict.fromkeys(STOCK_RESIDUALS, 0)
    for position in Position.objects.filter(site=site, boundary="physical"):
        interval = bounds(position.portion)
        physical.setdefault(position.lot_id, []).append(interval)
        quantities["physical_qty"] += ranges.length(interval)
        if position.origin_id is None and position.value_basis_origin_id is None:
            quantities["unvalued_qty"] += ranges.length(interval)
    for position in Position.objects.filter(boundary="transit").filter(
        Q(transfer__source_site=site) | Q(transfer__destination_site=site)
    ):
        quantities["transit_qty"] += ranges.length(bounds(position.portion))
    reserved: dict[Any, list[tuple[int, int]]] = {}
    for reservation in ActiveReservation.objects.filter(lot_id__in=list(physical)):
        reserved.setdefault(reservation.lot_id, []).append(bounds(reservation.portion))
    quantities["reserved_qty"] = sum(
        ranges.total(ranges.intersect(physical[lot], intervals))
        for lot, intervals in reserved.items()
    )
    # Legacy-ledger stock is invisible to these ranges: the quantities it can touch are
    # unknown, and the ones it cannot are kept.
    reasons: list[dict[str, Any]] = []
    unknown: list[str] = []
    if held_legacy:
        unknown += ["physical_qty", "unvalued_qty", "reserved_qty"]
        reasons.append(
            issue(
                "LEGACY_STOCK_NOT_ASSESSED",
                "The site still holds legacy-ledger stock, which these quantities cannot see.",
            )
        )
    if moving_legacy:
        unknown.append("transit_qty")
        reasons.append(
            issue(
                "LEGACY_TRANSIT_NOT_ASSESSED",
                "Legacy-ledger stock is in transit to or from the site.",
            )
        )
    if not unknown:
        completeness = "complete"
    elif len(set(unknown)) >= len(STOCK_RESIDUALS):
        # Every range is unknowable - not merely some of them - so the honest
        # word is "unknown", not "partial" (GSA-T02: complete/partial/unknown).
        completeness = "unknown"
    else:
        completeness = "partial"
    return {
        **quantities,
        **dict.fromkeys(unknown),
        "open_exception_ids": exceptions,
        "completeness": completeness,
        "reasons": reasons,
        "cash_assessment": "not_assessed",
    }


def closure_items(residuals: dict[str, Any], *, stock_allowed: bool) -> list[dict[str, Any]]:
    """The residuals a closure step must decide, refusing what cannot be decided.

    Unknown residuals cannot be decided at all. At final closure stock cannot be
    decided away either: it must leave through its own documents first.
    """
    if residuals["completeness"] != "complete":
        raise Refusal(
            "CLOSURE_UNRESOLVED",
            "The site's residual stock cannot be fully measured, so it cannot be closed.",
            issues=list(residuals["reasons"]),
        )
    stock = [{"code": key, "quantity": residuals[key]} for key in STOCK_RESIDUALS if residuals[key]]
    if stock and not stock_allowed:
        raise Refusal(
            "CLOSURE_UNRESOLVED",
            "The site still holds stock; move or dispose of it before closing.",
            issues=[
                issue("RESIDUAL_STOCK", item["code"], field=item["code"], quantity=item["quantity"])
                for item in stock
            ],
        )
    return stock + [
        {"code": "open_exception", "field": exception_id}
        for exception_id in residuals["open_exception_ids"]
    ]


def trading_not_excluded(site: Store) -> list[dict[str, Any]]:
    """Why the site cannot be affirmed non-trading; unknown trading activity is not none."""
    from sell.models import HeldBill, Return, Sale

    problems: list[dict[str, Any]] = []
    latest = latest_master_version(site.tenant_id, "site", str(site.pk))
    tills = ((latest.payload if latest else None) or {}).get("counter_count")
    if isinstance(tills, bool) or not isinstance(tills, int):
        problems.append(issue("TILLS_UNKNOWN", "No site record says how many tills this site has."))
    elif tills > 0:
        problems.append(issue("TILLS_ENROLLED", f"The site records {tills} till(s)."))
    if (
        Sale.objects.filter(store=site).exists()
        or Return.objects.filter(store=site).exists()
        or HeldBill.objects.filter(store=site).exists()
    ):
        problems.append(
            issue("TRADING_HISTORY", "The site has recorded bills, returns or held bills.")
        )
    return problems


def readiness_dto(site: Store, guard: SiteGuard, checks: list[dict[str, Any]]) -> dict[str, Any]:
    from masters.first_store_readiness import selling_checks

    return {
        "site_id": str(site.pk),
        "lifecycle": guard.lifecycle,
        "opening_setup_ready": guard.opening_setup_ready,
        "goods_ready": guard.goods_ready,
        "sell_ready": guard.sell_ready,
        "selling_mode": guard.selling_mode,
        "selling_checks": selling_checks(site, timezone.now())
        if guard.selling_mode == SiteGuard.SellingMode.ONLINE_ALPHA else [],
        "non_trading_confirmed": guard.non_trading_confirmed,
        "checks": checks,
        "residuals": readiness_residuals(site),
    }


def require_sell_ready(site: Store) -> None:
    from masters.first_store_readiness import require_ready

    require_ready(site, timezone.now())


def location_holds_stock(location: Location) -> bool:
    from stockledger.goods_models import Position

    return Position.objects.filter(location=location).exists()


# --------------------------------------------------------------------------
# Configuration payload validation (design §5.3 ConfigPayload, §5.5, E086-E088)
# --------------------------------------------------------------------------

CONFIG_KINDS = frozenset(
    {
        "business_profile",
        "vocabulary",
        "identity_profile",
        "profile",
        "tax_rates",
        "rates",
        "approval",
        "reasons",
        "series",
        "barcode_range",
        "label",
        "notifications",
        "workflow",
        "non_trading",
        "working_calendar",
        "sell_policy",
    }
)

Checker = Callable[[Any, str], None]


def config_invalid(path: str, message: str) -> Refusal:
    return Refusal(
        "CONFIG_INVALID",
        f"{path}: {message}.",
        status=422,
        issues=[issue("INVALID", message, field=path)],
    )


def _text(maximum: int = 200, *, allow_empty: bool = False) -> Checker:
    def check(value: Any, path: str) -> None:
        if (
            not isinstance(value, str)
            or len(value) > maximum
            or (not allow_empty and not value.strip())
        ):
            raise config_invalid(path, f"must be text of at most {maximum} characters")

    return check


def _integer(lower: int | None = None, upper: int | None = None) -> Checker:
    def check(value: Any, path: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise config_invalid(path, "must be a whole number")
        if (lower is not None and value < lower) or (upper is not None and value > upper):
            raise config_invalid(path, f"must be between {lower} and {upper}")

    return check


def _boolean(value: Any, path: str) -> None:
    if not isinstance(value, bool):
        raise config_invalid(path, "must be true or false")


def _constant(expected: Any) -> Checker:
    def check(value: Any, path: str) -> None:
        if value != expected or type(value) is not type(expected):
            raise config_invalid(path, f"must be {expected!r}")

    return check


def _choice(*allowed: Any) -> Checker:
    def check(value: Any, path: str) -> None:
        if isinstance(value, bool) or value not in allowed:
            raise config_invalid(path, f"must be one of {', '.join(str(a) for a in allowed)}")

    return check


def _identifier(value: Any, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise config_invalid(path, "must be an ID")
    if len(str(value)) > 100:
        raise config_invalid(path, "must be an ID")


def _uuid_text(value: Any, path: str) -> None:
    try:
        uuid.UUID(str(value))
    except ValueError:
        raise config_invalid(path, "must be a UUID") from None
    if not isinstance(value, str):
        raise config_invalid(path, "must be a UUID")


def _timestamp(value: Any, path: str) -> None:
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None or parsed.tzinfo is None:
        raise config_invalid(path, "must be an RFC3339 timestamp with a time zone")


def _date(value: Any, path: str) -> None:
    from django.utils.dateparse import parse_date

    if not isinstance(value, str) or parse_date(value) is None:
        raise config_invalid(path, "must be a date (YYYY-MM-DD)")


def _timezone_name(value: Any, path: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not isinstance(value, str) or not value.strip():
        raise config_invalid(path, "must be an IANA time zone name")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise config_invalid(path, "must be a known IANA time zone name") from None


def _money(value: Any, path: str) -> None:
    try:
        paise_from_json(value, maximum=10**30 - 1)
    except MoneyInvalid:
        raise config_invalid(path, "must be whole paise as a base-10 string") from None


def _percent(value: Any, path: str) -> None:
    text = value if isinstance(value, str) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value)
    try:
        number = Decimal(text) if text is not None else None
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite() or not 0 <= number <= Decimal("99.99"):
        raise config_invalid(path, "must be a percentage from 0 to 99.99")
    if number.as_tuple().exponent < -2:  # type: ignore[operator]
        raise config_invalid(path, "must have at most two decimal places")


def _discount_percent(value: Any, path: str) -> None:
    if value in ("100", "100.0", "100.00", 100):
        return
    _percent(value, path)


def _items(item: Checker, *, maximum: int = 1000) -> Checker:
    def check(value: Any, path: str) -> None:
        if not isinstance(value, list) or len(value) > maximum:
            raise config_invalid(path, f"must be a list of at most {maximum} items")
        for index, entry in enumerate(value):
            item(entry, f"{path}[{index}]")

    return check


def _object(fields: dict[str, tuple[Checker, bool]]) -> Checker:
    """A closed object: every key known, required keys present and non-null."""

    def check(value: Any, path: str) -> None:
        if not isinstance(value, dict):
            raise config_invalid(path, "must be an object")
        unknown = sorted(set(value) - set(fields))
        if unknown:
            raise config_invalid(f"{path}.{unknown[0]}", "is not a field of this configuration")
        for name, (checker, required) in fields.items():
            if value.get(name) is None:
                if required:
                    raise config_invalid(f"{path}.{name}", "is required")
                continue
            checker(value[name], f"{path}.{name}")

    return check


def _required(checker: Checker) -> tuple[Checker, bool]:
    return checker, True


def _optional(checker: Checker) -> tuple[Checker, bool]:
    return checker, False


_TEXTS = _items(_text())
_IDS = _items(_identifier)

_PROFILE_COLUMN = _object(
    {
        "id": _required(_uuid_text),
        "key": _required(_text(60)),
        "label": _required(_text(80)),
        "order": _required(_integer(0)),
        "logical_type": _required(
            _choice("text", "vocabulary", "quantity", "money", "percent", "alias")
        ),
        "required": _required(_boolean),
        "mode": _required(_choice("supplied", "selected", "inherited", "looked_up", "derived")),
        "vocabulary_version_id": _optional(_identifier),
        "default_value": _optional(_text(500, allow_empty=True)),
        "dependency_keys": _required(_items(_text(60))),
        "rounding_scale": _required(_integer(0, 2)),
        "tolerance_minor_units": _required(_integer(0)),
        "currency": _optional(_constant("INR")),
        "unit": _optional(_constant("piece")),
        "table_visible": _required(_boolean),
        "export_visible": _required(_boolean),
    }
)

CONFIG_SCHEMAS: dict[str, Checker] = {
    "sell_policy": _object(
        {
            "manual_discount_cap_percent": _required(_discount_percent),
            "manual_discount_on_offer_lines": _required(_boolean),
            "return_window_days": _required(_integer(0, 365)),
        }
    ),
    "business_profile": _object(
        {
            "categories": _required(_TEXTS),
            "identity_profile_id": _required(_identifier),
            "expected_skus": _required(_integer(0)),
            "brands": _required(_integer(0)),
            "sites": _required(_integer(0)),
            "sbus": _required(_integer(0)),
            "staff": _required(_integer(0)),
            "documents_per_day": _required(_integer(0)),
            "evidence_bytes_per_year": _required(_integer(0)),
            "commercial_labels": _required(_TEXTS),
            "workforce_scope": _required(_text(500)),
            "tills_per_site": _required(
                _items(
                    _object({"site_id": _required(_identifier), "count": _required(_integer(0))})
                )
            ),
            "accounting_interface": _required(_text(160)),
        }
    ),
    "vocabulary": _object(
        {
            "dimension": _required(_text(60)),
            "values": _required(
                _items(
                    _object(
                        {
                            "value_key": _required(_text(100)),
                            "label": _required(_text(160)),
                            "sort_order": _required(_integer()),
                            "retired": _required(_boolean),
                        }
                    ),
                    maximum=5000,
                )
            ),
            "effective_from": _required(_timestamp),
        }
    ),
    "identity_profile": _object(
        {
            "family": _required(_text(60)),
            "distinguishing_dimensions": _required(_items(_text(60))),
            "size_dimension": _required(_text(60)),
            "colour_dimension": _optional(_text(60)),
            "grade_dimension": _optional(_text(60)),
            "allowed_size_values": _required(_items(_uuid_text, maximum=5000)),
            "allowed_colour_values": _required(_items(_uuid_text, maximum=5000)),
            "allowed_grade_values": _required(_items(_uuid_text, maximum=5000)),
        }
    ),
    "profile": _object(
        {
            "family": _required(_text(60)),
            "columns": _required(_items(_PROFILE_COLUMN, maximum=200)),
            "directions": _required(
                _items(_choice("base_to_ticket", "ticket_to_purchase", "both_supplied"))
            ),
            "allow_row_override": _required(_boolean),
            "rates_version_id": _required(_identifier),
            "tax_version_id": _required(_identifier),
            "opening_rules": _required(
                _object(
                    {
                        "season_required": _required(_constant(True)),
                        "both_supplied": _required(_constant(True)),
                    }
                )
            ),
            "barcode_range_id": _optional(_identifier),
        }
    ),
    "tax_rates": _object(
        {
            "currency": _required(_constant("INR")),
            "hsn_rules": _required(
                _items(
                    _object(
                        {
                            "hsn": _required(_text(20)),
                            "effective_from": _required(_timestamp),
                            "effective_to": _optional(_timestamp),
                            "slabs": _required(
                                _items(
                                    _object(
                                        {
                                            "lower_paise": _required(_money),
                                            "upper_paise": _optional(_money),
                                            "lower_inclusive": _required(_boolean),
                                            "upper_inclusive": _required(_boolean),
                                            "input_pct": _required(_percent),
                                            "output_pct": _required(_percent),
                                        }
                                    )
                                )
                            ),
                        }
                    ),
                    maximum=5000,
                )
            ),
        }
    ),
    "rates": _object(
        {
            "transport_pct": _required(_percent),
            "pricing_margin_pct": _required(_percent),
            "brand_id": _optional(_identifier),
        }
    ),
    "approval": _object(
        {
            "action": _required(_text(100)),
            "roles": _required(_items(_text(40))),
            "steps": _optional(_items(_object({
                "label": _required(_text(64)),
                "roles": _required(_items(_text(40))),
                "qty_max": _optional(_integer(0)),
                "value_max": _optional(_money),
                "unknown_value": _optional(_choice("refuse", "quantity_only")),
            }))),
            "purpose": _optional(_text(60)),
            "site_ids": _required(_IDS),
            "brand_ids": _required(_IDS),
            "require_distinct": _required(_boolean),
            "qty_max": _optional(_integer(0)),
            "value_max": _optional(_money),
            "step_up": _optional(_boolean),
            "unknown_value": _optional(_choice("refuse", "quantity_only")),
        }
    ),
    "reasons": _object(
        {
            "action": _required(_text(100)),
            "codes": _required(
                _items(
                    _object(
                        {
                            "code": _required(_text(60)),
                            "label": _required(_text(160)),
                            "retired": _required(_boolean),
                        }
                    )
                )
            ),
        }
    ),
    "series": _object(
        {
            "entity_id": _required(_identifier),
            "type": _required(_text(20)),
            "fy": _required(_text(5)),
            "ceiling_block_size": _required(_integer(1, 100_000)),
        }
    ),
    "barcode_range": _object(
        {
            "issuer": _required(_text(100)),
            "prefix": _required(_text(30, allow_empty=True)),
            "start": _required(_integer(0)),
            "end": _required(_integer(0)),
            "symbology": _required(_constant("code128")),
        }
    ),
    "label": _object(
        {
            "width_mm": _required(_integer(20, 150)),
            "height_mm": _required(_integer(20, 150)),
            "symbology": _required(_constant("code128")),
            "copies_limit": _required(_integer(1, 5000)),
            "fields": _required(_items(_text(60))),
            "printer_dpi": _required(_choice(203, 300, 600)),
            "min_module_dots": _required(_integer(2, 10)),
            "max_payload_characters": _required(_integer(1, 128)),
        }
    ),
    "notifications": _object(
        {
            "event": _required(_text(100)),
            "roles": _required(_items(_text(40))),
            "email": _required(_boolean),
            "sla_value": _required(_integer(1)),
            "sla_unit": _required(_choice("working_days", "days", "hours")),
            "calendar_version_id": _required(_identifier),
        }
    ),
    "workflow": _object(
        {
            "operation": _required(_text(100)),
            "enabled": _required(_boolean),
            "prerequisites": _required(_items(_text(100))),
        }
    ),
    "non_trading": _object(
        {
            "site_id": _required(_identifier),
            "reason": _required(_text(500)),
            "evidence_id": _required(_identifier),
            "confirmed_no_external_trading": _required(_constant(True)),
        }
    ),
    # GSA-T08, design §5.8 "Working calendar". Tenant-wide (`TENANT_WIDE_KINDS`)
    # and exclusive (`EXCLUSIVE_BY["working_calendar"] = ()`), so at most one
    # version is ever in force: exactly the "one approved calendar" the
    # exception system reads. A `notifications` policy's `calendar_version_id`
    # is cross-checked against this kind (`_check_rules`), never invented.
    "working_calendar": _object(
        {
            "timezone": _required(_timezone_name),
            "working_weekdays": _required(_items(_integer(1, 7), maximum=7)),
            "excluded_dates": _required(_items(_date, maximum=3660)),
        }
    ),
}


def _approved_version(tenant_id: uuid.UUID, kind: str, raw_id: Any) -> ConfigVersion | None:
    try:
        version_id = uuid.UUID(str(raw_id))
    except ValueError:
        return None
    return ConfigVersion.objects.filter(tenant_id=tenant_id, pk=version_id, kind=kind).first()


def _require_approved(tenant_id: uuid.UUID, kind: str, raw_id: Any, path: str) -> None:
    from masters.goods_config import candidate

    version = _approved_version(tenant_id, kind, raw_id)
    if version is None:
        raise config_invalid(path, f"must name an approved {kind} configuration version")
    if candidate(version).withdrawn_at is not None:
        raise config_invalid(path, f"names a withdrawn {kind} configuration version")


def _require_in_force(
    tenant_id: uuid.UUID, kind: str, raw_id: Any, path: str, at: datetime
) -> None:
    """The named approved version is also effective at ``at`` — not future, ended
    or withdrawn."""
    from masters.goods_config import in_force

    version = _approved_version(tenant_id, kind, raw_id)
    if version is None or not in_force(version, at):
        raise config_invalid(
            path, f"must name a {kind} configuration in force when this configuration takes effect"
        )


def _tax_refusal(path: str, code: str, message: str) -> Refusal:
    return Refusal(
        "CONFIG_INVALID",
        f"{path}: {message}.",
        status=422,
        issues=[issue(code, message, field=path)],
    )


def _check_tax_rules(payload: dict[str, Any]) -> None:
    """HSN rules for one HSN may not overlap in time; slabs of one rule may not overlap.

    Periods are half-open ``[effective_from, effective_to)``: a rule ending at an instant
    and the next starting at that same instant touch without overlapping.
    """
    seen: dict[str, list[tuple[datetime, datetime | None, int]]] = {}
    for index, rule in enumerate(payload["hsn_rules"]):
        path = f"payload.hsn_rules[{index}]"
        starts = parse_datetime(str(rule["effective_from"]))
        ends = parse_datetime(str(rule["effective_to"])) if rule.get("effective_to") else None
        assert starts is not None
        if ends is not None and ends <= starts:
            raise config_invalid(f"{path}.effective_to", "must be after effective_from")
        hsn = str(rule["hsn"])
        for other_start, other_end, other in seen.get(hsn, []):
            if (other_end is None or starts < other_end) and (ends is None or other_start < ends):
                raise _tax_refusal(
                    f"{path}.effective_from",
                    "TAX_RULE_OVERLAP",
                    f"overlaps hsn_rules[{other}] for HSN {hsn}",
                )
        seen.setdefault(hsn, []).append((starts, ends, index))
        slabs = sorted(
            enumerate(rule["slabs"]),
            key=lambda pair: (int(pair[1]["lower_paise"]), not pair[1]["lower_inclusive"]),
        )
        for position, slab in slabs:
            upper = slab.get("upper_paise")
            if upper is not None and int(upper) <= int(slab["lower_paise"]):
                raise config_invalid(
                    f"{path}.slabs[{position}].upper_paise", "must be above lower_paise"
                )
        for (_, lower), (position, higher) in zip(slabs, slabs[1:], strict=False):
            upper = lower.get("upper_paise")
            touching = upper is not None and int(upper) == int(higher["lower_paise"])
            if (
                upper is None
                or int(upper) > int(higher["lower_paise"])
                or (touching and lower["upper_inclusive"] and higher["lower_inclusive"])
            ):
                raise _tax_refusal(
                    f"{path}.slabs[{position}]",
                    "SLAB_OVERLAP",
                    "overlaps another slab of this rule",
                )


def check_scope_payload(kind: str, payload: dict[str, Any], scope: dict[str, Any]) -> None:
    """One meaning for scope: payload fields that repeat a scope dimension must agree with it."""
    if kind == "approval":
        pairs = (
            ("payload.site_ids", "site_ids", payload.get("site_ids") or []),
            ("payload.brand_ids", "brand_ids", payload.get("brand_ids") or []),
        )
        for path, name, values in pairs:
            if sorted({str(v) for v in values}) != scope[name]:
                raise config_invalid(path, f"must equal scope.{name}")
        purpose = payload.get("purpose")
        if (scope["purposes"] or None) != ([str(purpose)] if purpose else None):
            raise config_invalid("payload.purpose", "must equal the one scope purpose")
    if kind == "rates" and payload.get("brand_id") is not None:
        if scope["brand_ids"] != [str(payload["brand_id"])]:
            raise config_invalid("payload.brand_id", "must equal scope.brand_ids")


def _check_vocabulary(
    payload: dict[str, Any], tenant_id: uuid.UUID, scope_key: str | None, as_of: datetime
) -> None:
    from masters.goods_identity_services import vocabulary

    keys = [str(value["value_key"]) for value in payload["values"]]
    seen: set[str] = set()
    for index, key in enumerate(keys):
        if key in seen:
            raise config_invalid(f"payload.values[{index}].value_key", "repeats another value key")
        seen.add(key)
    dimension = str(payload["dimension"])
    owners = (
        ConfigVersion.objects.filter(
            tenant_id=tenant_id, kind="vocabulary", payload__dimension=dimension
        )
        .exclude(scope_key=scope_key or "")
        .exists()
    )
    if owners:
        raise config_invalid("payload.dimension", "is already owned by another approved vocabulary")
    published = vocabulary(tenant_id, max(as_of, timezone.now()), dimension).get(dimension, [])
    missing = sorted({value.value_key for value in published} - seen)
    if missing:
        raise config_invalid(
            "payload.values",
            f"must keep published value key {missing[0]}; retire a value instead of removing it",
        )


def _check_identity_profile(payload: dict[str, Any], tenant_id: uuid.UUID, as_of: datetime) -> None:
    from masters.goods_identity_services import vocabulary

    governed = vocabulary(tenant_id, as_of)
    named = {
        "size_dimension": payload["size_dimension"],
        "colour_dimension": payload.get("colour_dimension"),
        "grade_dimension": payload.get("grade_dimension"),
    }
    for index, dimension in enumerate(payload["distinguishing_dimensions"]):
        if dimension not in governed:
            raise config_invalid(
                f"payload.distinguishing_dimensions[{index}]", "names no governed vocabulary"
            )
    for field, allowed_field in (
        ("size_dimension", "allowed_size_values"),
        ("colour_dimension", "allowed_colour_values"),
        ("grade_dimension", "allowed_grade_values"),
    ):
        dimension = named[field]
        allowed = [str(v) for v in payload[allowed_field]]
        if dimension is None:
            if allowed:
                raise config_invalid(f"payload.{allowed_field}", f"needs a {field} first")
            continue
        if dimension not in governed:
            raise config_invalid(f"payload.{field}", "names no governed vocabulary")
        selectable = {str(value.id) for value in governed[dimension] if not value.retired}
        for index, value_id in enumerate(allowed):
            if value_id not in selectable:
                raise config_invalid(
                    f"payload.{allowed_field}[{index}]",
                    f"is not a current value of the {dimension} vocabulary",
                )


def _check_rules(  # noqa: C901 - one branch per configuration kind with cross-field rules
    kind: str,
    payload: dict[str, Any],
    tenant_id: uuid.UUID | None,
    scope_key: str | None,
    as_of: datetime,
) -> None:
    from accounts.actions import ACTIONS, DISTINCT_IDENTITY_ACTIONS

    if kind in ("approval", "reasons") and payload["action"] not in ACTIONS:
        raise config_invalid("payload.action", "is not a registered action")
    if (
        kind == "approval"
        and payload["require_distinct"] is False
        and payload["action"] in DISTINCT_IDENTITY_ACTIONS
    ):
        raise config_invalid(
            "payload.require_distinct", "cannot relax the fixed two-person rule for this action"
        )
    if kind == "approval":
        if not payload["roles"] or any(not step["roles"] for step in payload.get("steps") or []):
            raise config_invalid("payload.roles", "every approval route needs explicit roles")
    if kind == "reasons":
        codes = [code["code"] for code in payload["codes"]]
        if len(set(codes)) != len(codes):
            raise config_invalid("payload.codes", "repeats a reason code")
    if kind == "series":
        from core.numbering import DOC_TYPES

        if payload["type"] not in DOC_TYPES:
            raise config_invalid("payload.type", "is not a goods-v1 document type")
        if not re.fullmatch(r"\d{2}-\d{2}", str(payload["fy"])):
            raise config_invalid("payload.fy", "must look like 26-27")
    if kind == "barcode_range":
        if payload["end"] < payload["start"]:
            raise config_invalid("payload.end", "must not be below start")
        if len(payload["prefix"]) + len(str(payload["end"])) > 128:
            raise config_invalid("payload.prefix", "makes the generated value too long")
    if kind == "working_calendar":
        weekdays = payload["working_weekdays"]
        if len(set(weekdays)) != len(weekdays):
            raise config_invalid("payload.working_weekdays", "repeats a weekday")
        if not weekdays:
            raise config_invalid("payload.working_weekdays", "must name at least one working day")
        dates = payload["excluded_dates"]
        if len(set(dates)) != len(dates):
            raise config_invalid("payload.excluded_dates", "repeats a date")
    if kind == "tax_rates":
        _check_tax_rules(payload)
    if tenant_id is None:
        return
    if kind == "vocabulary":
        _check_vocabulary(payload, tenant_id, scope_key, as_of)
    elif kind == "identity_profile":
        _check_identity_profile(payload, tenant_id, as_of)
    elif kind == "series":
        if not LegalEntity.objects.filter(pk=str(payload["entity_id"])).exists():
            raise config_invalid("payload.entity_id", "names no known entity")
    elif kind == "profile":
        _require_approved(
            tenant_id, "rates", payload["rates_version_id"], "payload.rates_version_id"
        )
        _require_approved(
            tenant_id, "tax_rates", payload["tax_version_id"], "payload.tax_version_id"
        )
        if payload.get("barcode_range_id") is not None:
            _require_approved(
                tenant_id, "barcode_range", payload["barcode_range_id"], "payload.barcode_range_id"
            )
        for index, column in enumerate(payload["columns"]):
            if column.get("vocabulary_version_id") is not None:
                _require_approved(
                    tenant_id,
                    "vocabulary",
                    column["vocabulary_version_id"],
                    f"payload.columns[{index}].vocabulary_version_id",
                )
    elif kind == "business_profile":
        _require_approved(
            tenant_id,
            "identity_profile",
            payload["identity_profile_id"],
            "payload.identity_profile_id",
        )
    elif kind == "notifications":
        # Design §5.8: "Notifications.calendar_version_id references an approved
        # compatible calendar" — never a server-local heuristic.
        _require_approved(
            tenant_id,
            "working_calendar",
            payload["calendar_version_id"],
            "payload.calendar_version_id",
        )
        if payload["sla_unit"] == "working_days":
            # "Refuse approval of a `working_days` notification policy without
            # that approved compatible calendar" (design §5.8): compatible means
            # in force when this policy itself takes effect, so the deadlines it
            # governs have a calendar from their first day. A day- or hour-unit
            # policy measures elapsed time and asks nothing of the calendar it
            # names, so its existing meaning is left alone.
            _require_in_force(
                tenant_id,
                "working_calendar",
                payload["calendar_version_id"],
                "payload.calendar_version_id",
                as_of,
            )


def validate_config_payload(
    kind: str,
    payload: Any,
    *,
    tenant_id: uuid.UUID | None = None,
    scope_key: str | None = None,
    as_of: datetime | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Closed-schema and rule validation for one ``ConfigPayload`` (design §5.3, E086 step 8).

    Unknown keys are invalid, never free-form JSON. With ``tenant_id`` the checks
    that read approved configuration run too: vocabulary keys stay stable and a
    dimension has one owner, an identity profile names only governed vocabulary
    values, and profile references name approved versions.
    """
    if kind not in CONFIG_KINDS:
        raise Refusal(
            "CONFIG_INVALID",
            f"{kind} is not a registered configuration kind.",
            status=422,
            issues=[issue("INVALID", "unregistered kind", field="kind")],
        )
    if not isinstance(payload, dict):
        raise config_invalid("payload", "must be an object")
    CONFIG_SCHEMAS[kind](payload, "payload")
    _check_rules(kind, payload, tenant_id, scope_key, as_of or timezone.now())
    if scope is not None:
        from masters.goods_config import TENANT_WIDE_KINDS, normalise_scope

        canonical = normalise_scope(scope)
        if kind in TENANT_WIDE_KINDS and (
            canonical["scope_kind"] != "tenant" or canonical["purposes"]
        ):
            raise config_invalid("scope", f"a {kind} configuration applies tenant-wide")
        check_scope_payload(kind, payload, canonical)
    return payload


def config_draft_data(draft: ConfigDraft) -> dict[str, Any]:
    """The exact reviewable content of a draft - its DTO data and its reviewed hash."""
    return {
        "kind": draft.kind,
        "scope": draft.scope,
        "payload": draft.payload,
        "effective_from": draft.effective_from.isoformat(),
        "effective_to": draft.effective_to.isoformat() if draft.effective_to else None,
    }


def config_draft_hash(draft: ConfigDraft) -> str:
    from core.canonical import content_hash

    return content_hash(config_draft_data(draft))


def config_subject_key(draft_id: Any) -> str:
    return f"configdraft:{draft_id}"


def config_drafters(tenant_id: uuid.UUID, draft: ConfigDraft) -> set[uuid.UUID]:
    """Everyone who created, edited or submitted any revision of ``draft`` (Phase 1 §9.3)."""
    from core.kernel_models import AuditEvent

    editors = set(
        AuditEvent.objects.filter(
            tenant_id=tenant_id,
            subject_key=config_subject_key(draft.pk),
            action="config.draft",
            outcome="succeeded",
            actor__isnull=False,
        ).values_list("actor_id", flat=True)
    )
    return {*editors, draft.maker_id}


def start_of_today(tenant_id: uuid.UUID, now: datetime) -> datetime:
    from zoneinfo import ZoneInfo

    from masters.goods_models import Tenant

    zone_name = Tenant.objects.filter(pk=tenant_id).values_list("timezone", flat=True).first()
    local = now.astimezone(ZoneInfo(zone_name or "Asia/Kolkata"))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def is_backdated(
    tenant_id: uuid.UUID, draft: ConfigDraft, now: datetime, today: datetime | None = None
) -> bool:
    """A version starting before today, in the tenant's own time zone (Phase 1 P1-CFG).

    ``today`` lets a caller that needs both this and ``backdate_impact`` read the
    tenant's day boundary once instead of twice.
    """
    return draft.effective_from < (today or start_of_today(tenant_id, now))


def backdate_impact(
    tenant_id: uuid.UUID, draft: ConfigDraft, now: datetime, today: datetime | None = None
) -> list[dict[str, Any]]:
    """E088 step 10: the impact list of a backdated version, or BACKDATE_INVALID.

    A version starting before today is a backdate (Phase 1 P1-CFG). It may not
    start at or before the approved version it follows for the same kind and
    scope, because that would re-time an approved version and so change which
    configuration official documents used. Documents recorded since it starts
    are listed; every official one keeps the version it pinned.
    """
    if draft.effective_from >= (today or start_of_today(tenant_id, now)):
        return []
    previous = (
        ConfigVersion.objects.filter(
            tenant_id=tenant_id, kind=draft.kind, scope_key=draft.scope_key
        )
        .order_by("-effective_from", "-version")
        .first()
    )
    if previous is not None and draft.effective_from <= previous.effective_from:
        raise Refusal(
            "BACKDATE_INVALID",
            "A backdated version cannot start at or before the approved version it follows.",
            status=409,
            issues=[
                issue(
                    "BACKDATE_BEFORE_APPROVED",
                    f"The approved version {previous.version} starts "
                    f"{previous.effective_from.isoformat()}.",
                    field="effective_from",
                )
            ],
        )
    from core.kernel_models import DocumentIdentity

    documents = DocumentIdentity.objects.filter(
        tenant_id=tenant_id, created_at__gte=draft.effective_from
    )
    scope = draft.scope if isinstance(draft.scope, dict) else {}
    if scope.get("scope_kind") == "sites" and scope.get("site_ids"):
        site_ids = [int(str(s)) for s in scope["site_ids"] if str(s).isdigit()]
        documents = documents.filter(site_id__in=site_ids)
    elif scope.get("scope_kind") == "entity" and str(scope.get("entity_id") or "").isdigit():
        documents = documents.filter(entity_id=int(str(scope["entity_id"])))
    impact = []
    for document_id, number in documents.order_by("created_at", "id").values_list(
        "pk", "official_number"
    )[:1000]:
        impact.append(
            {
                "resource_key": f"document:{document_id}",
                "effect": (
                    "Official version keeps the configuration it used."
                    if number
                    else "Unofficial draft is validated under this version from now on."
                ),
                "official_unchanged": True,
            }
        )
    return impact


def config_scope_key(scope: dict[str, Any]) -> str:
    from masters.goods_config import scope_hash

    return scope_hash(scope)[:64]


def apply_series_configuration(run: CommandRun, version: ConfigVersion) -> None:
    """A ``series`` configuration, once approved, publishes its first ceiling."""
    from core.documents import VoucherSeries
    from core.numbering import _request_ceiling, ensure_series

    payload = version.payload
    entity = LegalEntity.objects.filter(pk=payload["entity_id"]).first()
    if entity is None:
        raise Refusal("CONFIG_INVALID", "The series configuration names an unknown entity.")
    series = ensure_series(
        run.tenant_id,
        entity,
        payload["type"],
        payload.get("fy"),
        block_size=int(payload.get("ceiling_block_size") or 1000),
    )
    locked = run.lock(LockRank.SERIES, VoucherSeries.objects.filter(pk=series.pk))[0]
    _request_ceiling(run, locked, 0)


def build_query_terms(params: dict[str, str]) -> str:
    return (params.get("q") or "").strip()


# --------------------------------------------------------------------------
# The registered approval-subject handler for configuration drafts
# (registered from ``masters.apps.MastersConfig.ready``, design §3.1, §6.1).
# --------------------------------------------------------------------------


def handle_configuration_approval(run: CommandRun, context: Any) -> dict[str, Any] | None:
    """Turn an approved configuration draft into a frozen ``ConfigVersion``.

    Only the exact content that was submitted can be frozen: a draft edited after
    submission no longer matches the request's revision and reviewed hash. Nobody
    who drafted or edited any revision may approve it (Phase 1 §9.3). A rejection
    needs no extra effect - the kernel's decision path records it. A ``series``
    kind additionally ensures the numbering series exists and queues its ceiling.
    """
    request = context.request
    if not request.subject_key.startswith("configdraft:"):
        raise Refusal("STATE_CONFLICT", "This approval does not name a configuration draft.")
    draft_id = request.subject_key.split(":", 1)[1]
    locked = run.lock(LockRank.DRAFT, ConfigDraft.objects.filter(pk=draft_id))
    if not locked:
        raise Refusal("NOT_FOUND", "That configuration draft was not found.")
    draft = locked[0]
    if context.decision != "approve":
        return None
    if draft.state == ConfigDraft.State.APPROVED:
        raise Refusal("STATE_CONFLICT", "This configuration has already been approved.")
    if (
        draft.state != ConfigDraft.State.SUBMITTED
        or draft.revision != request.revision
        or config_draft_hash(draft) != request.reviewed_hash
    ):
        raise Refusal(
            "REVISION_SUPERSEDED",
            "The configuration changed after it was submitted; it must be submitted again.",
        )
    if context.checker_id in config_drafters(run.tenant_id, draft):
        raise Refusal(
            "SELF_APPROVAL",
            "A person who drafted or edited this configuration cannot also approve it.",
        )
    validate_config_payload(
        draft.kind,
        draft.payload,
        tenant_id=run.tenant_id,
        scope_key=draft.scope_key,
        as_of=draft.effective_from,
        scope=draft.scope,
    )
    impact = backdate_impact(run.tenant_id, draft, run.now)
    last_version = (
        ConfigVersion.objects.filter(
            tenant_id=run.tenant_id, kind=draft.kind, scope_key=draft.scope_key
        )
        .order_by("-version")
        .first()
    )
    next_version = (last_version.version + 1) if last_version else 1
    version = run.record(
        ConfigVersion(
            draft=draft,
            kind=draft.kind,
            version=next_version,
            scope=draft.scope,
            scope_key=draft.scope_key,
            payload=draft.payload,
            effective_from=draft.effective_from,
            approved_by_id=context.checker_id,
            source_revision=draft.revision,
            source_hash=request.reviewed_hash,
            backdate_impact=impact,
        )
    )
    from masters.goods_config import activate

    # A successor takes over no earlier than its approval, unless it is a reviewed backdate
    # (a start before today, with its impact list frozen on the version): approving later
    # than the drafted start must not rewrite what decisions under the predecessor relied on.
    backdated = draft.effective_from < start_of_today(run.tenant_id, run.now)
    superseded = activate(
        run,
        version,
        effective_to=draft.effective_to,
        not_before=None if backdated else run.now,
    )
    starts = EffectiveVersionPeriod.objects.get(
        target_kind="configuration", target_id=version.pk
    ).effective_from
    draft.state = ConfigDraft.State.APPROVED
    draft.save(update_fields=["state"])
    if draft.kind == "series":
        apply_series_configuration(run, version)
    return {
        "config_version_id": str(version.pk),
        "effective_from": starts.isoformat(),
        "effective_to": draft.effective_to.isoformat() if draft.effective_to else None,
        "superseded_version_ids": superseded,
    }
