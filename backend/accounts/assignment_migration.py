"""Conservative, repeatable conversion of old access to scoped role assignments.

The preview is also the reconciliation report. A person's assignments are
omitted together when any legacy role or scope is unclear. Legacy rows remain
untouched, and every converted row points back to its source grant or login.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from accounts.goods_models import HumanIdentity, RoleAssignment, RoleGrant, ServerSession
from accounts.models import Role, User
from accounts.rbac_matrix import section_access_for
from accounts.role_assignments import INITIAL_FIELD_ACCESS, INITIAL_ROLE_CODES
from accounts.sessions import bump_security_epoch, person_retired
from accounts.unified_policy import OWNER_APPROVAL_STEPS, initial_step_actions
from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from core.refusals import Refusal
from core.tenancy import require_tenant_id
from masters.goods_models import EffectiveVersionPeriod, MasterVersion, Sbu, Tenant
from masters.models import Brand, Store

GRANT_ROLE_ALIASES = {
    "C-OWN": "owner",
    "M-STR": "store_person",
    "M-CSH": "store_person",
    "C-WHO": "warehouse",
    "C-CAO": "accounts",
    # C-BUY also represented the old HO Buyer role. It is accepted for a
    # Brand Manager only when the login makes that intent explicit.
    "C-BUY": "brand_manager",
}


def _isolated_synthetic_proof_tenant(tenant_id: uuid.UUID) -> bool:
    """Narrow the seeded Admin migration exception to the named proof target."""
    db = connection.settings_dict
    isolated = (
        bool(getattr(settings, "PROOF_MODE", False))
        and db.get("NAME") == "kdps_proof"
        and db.get("USER") == "kdps_proof"
        and db.get("HOST") in {"127.0.0.1", "localhost"}
        and str(db.get("PORT")) == "55433"
    )
    return isolated and Tenant.objects.filter(pk=tenant_id, synthetic=True).exists()
USER_ROLE_ALIASES = {
    "owner": "owner",
    "store_person": "store_person",
    "store_manager": "store_person",
    "store_staff": "store_person",
    "warehouse": "warehouse",
    "brand_manager": "brand_manager",
    "accounts": "accounts",
    "it_admin": "it_admin",
    "admin": "it_admin",
}


@dataclass(frozen=True)
class Scope:
    all_sites: bool
    site_ids: tuple[int, ...]
    all_brands: bool
    brand_ids: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "all_sites": self.all_sites,
            "site_ids": list(self.site_ids),
            "all_brands": self.all_brands,
            "brand_ids": list(self.brand_ids),
        }

    def intersect(self, other: Scope) -> Scope:
        def axis(all_a: bool, ids_a: tuple[int, ...], all_b: bool, ids_b: tuple[int, ...]) -> tuple[bool, tuple[int, ...]]:
            if all_a and all_b:
                return True, ()
            if all_a:
                return False, ids_b
            if all_b:
                return False, ids_a
            return False, tuple(sorted(set(ids_a) & set(ids_b)))

        sites_all, sites = axis(self.all_sites, self.site_ids, other.all_sites, other.site_ids)
        brands_all, brands = axis(
            self.all_brands, self.brand_ids, other.all_brands, other.brand_ids
        )
        return Scope(sites_all, sites, brands_all, brands)

    def empty(self) -> bool:
        return (not self.all_sites and not self.site_ids) or (
            not self.all_brands and not self.brand_ids
        )


ALL_SCOPE = Scope(True, (), True, ())


@dataclass(frozen=True)
class Candidate:
    user_id: int
    human_id: uuid.UUID
    role_code: str
    scope: Scope
    effective_from: datetime
    effective_to: datetime | None
    legacy_grant_id: uuid.UUID | None = None
    revoked_at: datetime | None = None

    def source(self) -> str:
        return f"grant:{self.legacy_grant_id}" if self.legacy_grant_id else f"user:{self.user_id}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source(),
            "user_id": self.user_id,
            "human_id": str(self.human_id),
            "role": self.role_code,
            "scope": self.scope.as_dict(),
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }


@dataclass
class MigrationPlan:
    tenant_id: uuid.UUID
    candidates: list[Candidate]
    blocked: list[dict[str, Any]]
    retired_platform_grants: list[str]
    policy_changes: list[dict[str, Any]]
    before_after: list[dict[str, Any]]
    inventory: dict[str, int]
    allow_seeded_tenant_admin: bool = False
    target_state: list[dict[str, Any]] = field(default_factory=list)

    def source_fingerprint(self) -> str:
        """Hash every authority-relevant source projection, excluding run counts."""
        source = {
            "candidates": [row.as_dict() for row in self.candidates],
            "blocked": self.blocked,
            "retired_platform_grants": self.retired_platform_grants,
            "policy_changes": self.policy_changes,
            "before_after": self.before_after,
            "allow_seeded_tenant_admin": self.allow_seeded_tenant_admin,
            "target_state": self.target_state,
        }
        return hashlib.sha256(json.dumps(source, sort_keys=True, default=str).encode()).hexdigest()

    def report(self) -> dict[str, Any]:
        return {
            "tenant_id": str(self.tenant_id),
            "source_fingerprint": self.source_fingerprint(),
            "inventory": self.inventory,
            "candidate_count": len(self.candidates),
            "blocked_person_count": len({r["user_id"] for r in self.blocked if r.get("user_id")}),
            "blocked": self.blocked,
            "retired_platform_grants": self.retired_platform_grants,
            "policy_changes": self.policy_changes,
            "before_after": self.before_after,
            "candidates": [row.as_dict() for row in self.candidates],
        }


def _block(user: User, code: str, detail: str) -> dict[str, Any]:
    return {"user_id": user.pk, "human_id": str(user.human_id) if user.human_id else None, "code": code, "detail": detail}


def _known_ids(kind: str, tenant_id: uuid.UUID, ids: list[int]) -> tuple[int, ...] | None:
    model = Store if kind == "site" else Brand
    wanted = tuple(sorted(set(ids)))
    if len(wanted) != len(ids):
        return None
    found = set(model.objects.filter(tenant_id=tenant_id, pk__in=wanted).values_list("pk", flat=True))
    return wanted if found == set(wanted) else None


def _user_scope(user: User, tenant_id: uuid.UUID) -> Scope | None:
    scope = user.scope_type
    if scope == "all":
        return ALL_SCOPE
    if scope == "brand":
        brand_ids = list(user.brands.values_list("pk", flat=True))
        brands = _known_ids("brand", tenant_id, brand_ids)
        return Scope(True, (), False, brands) if brands is not None else None
    if scope == "entity":
        if user.entity_id is None or not user.entity or user.entity.tenant_id != tenant_id:
            return None
        entity_sites = tuple(
            Store.objects.filter(tenant_id=tenant_id, gstin__legal_entity_id=user.entity_id)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        return Scope(False, entity_sites, True, ())
    if scope in {"store", "store_group", "region"}:
        site_ids = list(user.stores.values_list("pk", flat=True))
        selected_sites = _known_ids("site", tenant_id, site_ids)
        return Scope(False, selected_sites, True, ()) if selected_sites is not None else None
    return None


def _grant_scope(grant: RoleGrant, tenant_id: uuid.UUID) -> Scope | None:
    kind = grant.scope_kind
    if kind == "tenant" and not any((grant.entity_id, grant.site_id, grant.sbu_id, grant.brand_id)):
        return ALL_SCOPE
    if kind == "site" and grant.site_id is not None and not any((grant.entity_id, grant.sbu_id, grant.brand_id)):
        ids = _known_ids("site", tenant_id, [grant.site_id])
        return Scope(False, ids, True, ()) if ids is not None else None
    if kind == "brand" and grant.brand_id is not None and not any((grant.entity_id, grant.site_id, grant.sbu_id)):
        ids = _known_ids("brand", tenant_id, [grant.brand_id])
        return Scope(True, (), False, ids) if ids is not None else None
    if kind == "entity" and grant.entity_id is not None and not any((grant.site_id, grant.sbu_id, grant.brand_id)):
        if grant.entity is None or grant.entity.tenant_id != tenant_id:
            return None
        sites = tuple(
            Store.objects.filter(tenant_id=tenant_id, gstin__legal_entity_id=grant.entity_id)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        return Scope(False, sites, True, ())
    if kind == "sbu" and grant.sbu_id is not None and grant.sbu is not None and not any((grant.entity_id, grant.site_id, grant.brand_id)):
        if grant.sbu.tenant_id != tenant_id:
            return None
        if not Store.objects.filter(tenant_id=tenant_id, pk=grant.sbu.site_id).exists():
            return None
        if grant.sbu.brand_id is None:
            return Scope(False, (grant.sbu.site_id,), True, ())
        if not Brand.objects.filter(tenant_id=tenant_id, pk=grant.sbu.brand_id).exists():
            return None
        return Scope(False, (grant.sbu.site_id,), False, (grant.sbu.brand_id,))
    return None


def _grant_end(grant: RoleGrant, revocations: dict[uuid.UUID, datetime], periods: dict[uuid.UUID, datetime]) -> datetime | None:
    ends = [date for date in (grant.effective_to, revocations.get(grant.pk), periods.get(grant.pk)) if date is not None]
    return min(ends) if ends else None


def build_migration_plan(
    tenant_id: uuid.UUID, *, allow_seeded_tenant_admin: bool = False
) -> MigrationPlan:
    """Read all legacy authority and return a deterministic, fail-closed plan."""

    if require_tenant_id() != tenant_id:
        raise ValueError("migration tenant does not match the bound tenant")
    roles = {r.code: r for r in Role.objects.filter(tenant_id=tenant_id, code__in=INITIAL_ROLE_CODES)}
    policy_changes = []
    for code in sorted(INITIAL_ROLE_CODES):
        role = roles.get(code)
        if role is None:
            policy_changes.append({"role": code, "missing": True})
            continue
        role_versions = MasterVersion.objects.filter(
            tenant_id=tenant_id, kind="role", target_key=code
        )
        original_cutover = role_versions.filter(
            reason_code="so03_access_cutover"
        ).order_by("-revision").first()
        current_steps = (role.permissions_map or {}).get("step_actions", [])
        before_policy = {
            "sections": role.section_access,
            "fields": role.field_access,
            "step_actions": current_steps,
        }
        reason_code = "so03_access_cutover"
        if original_cutover is not None:
            # A previously run SO-03 cutover may predate explicit Owner step
            # defaults. Upgrade that baseline only if nobody has edited the
            # role since; a later approved policy decision always wins.
            latest = role_versions.order_by("-revision").first()
            recorded_steps = (original_cutover.payload or {}).get("step_actions")
            if (
                code != "owner" or latest is None or latest.pk != original_cutover.pk
                or recorded_steps is not None or current_steps
            ):
                continue
            after_policy = {
                **before_policy,
                "step_actions": sorted(OWNER_APPROVAL_STEPS),
            }
            reason_code = "so03_owner_step_defaults"
        else:
            after_policy = {
                "sections": section_access_for(code),
                "fields": INITIAL_FIELD_ACCESS[code],
                "step_actions": initial_step_actions(code),
            }
        policy_changes.append({
            "role": code,
            "before": before_policy,
            "after": after_policy,
            "unchanged": before_policy == after_policy,
            "reason_code": reason_code,
        })

    grants_by_human: dict[uuid.UUID, list[RoleGrant]] = defaultdict(list)
    # A revocation is itself a RoleGrant row with ``revokes_id`` set and an
    # empty action set. Query original grants here, including those subsequently
    # revoked, so their end dates and source links remain visible. Filtering on
    # the reverse ``revokes`` relation would do the opposite: omit originals
    # with revocations and include the revocation markers as fresh authority.
    grants = list(
        RoleGrant.objects.select_related("role", "sbu", "entity")
        .filter(tenant_id=tenant_id, revokes_id__isnull=True)
        .order_by("effective_from", "pk")
    )
    for grant in grants:
        grants_by_human[grant.human_id].append(grant)
    revoked: dict[uuid.UUID, datetime] = {}
    for revoked_id, when in RoleGrant.objects.filter(tenant_id=tenant_id, revokes__isnull=False).values_list("revokes_id", "effective_from"):
        if revoked_id is not None and (revoked_id not in revoked or when < revoked[revoked_id]):
            revoked[revoked_id] = when
    periods = {
        row.target_id: row.effective_to
        for row in EffectiveVersionPeriod.objects.filter(
            tenant_id=tenant_id, target_kind="grant", target_id__in=[g.pk for g in grants]
        )
        if row.effective_to is not None
    }
    candidates: list[Candidate] = []
    blocked: list[dict[str, Any]] = []
    retired_platform: list[str] = []
    before_after: list[dict[str, Any]] = []
    seen_humans: set[uuid.UUID] = set()
    users = User.objects.select_related("human", "role", "entity").filter(tenant_id=tenant_id).order_by("pk")
    for user in users:
        problems: list[dict[str, Any]] = []
        if (
            not user.is_active
            or user.human is not None and not user.human.active
            or person_retired(user.human_id, timezone.now())
        ):
            problems.append(_block(
                user, "INACTIVE_IDENTITY",
                "Inactive or retired people cannot receive migrated authority.",
            ))
        if user.human_id is None or user.human is None or user.human.tenant_id != tenant_id:
            problems.append(_block(user, "MISSING_IDENTITY", "Login has no tenant-matched human identity."))
        elif user.human_id in seen_humans:
            problems.append(_block(user, "AMBIGUOUS_IDENTITY", "More than one login uses this human identity."))
        else:
            seen_humans.add(user.human_id)
        if user.is_superuser:
            problems.append(_block(user, "PLATFORM_IDENTITY", "A platform superuser needs separate support authority."))
        old_role_code = user.role.code if user.role else None
        role_code = USER_ROLE_ALIASES.get(old_role_code or "")
        if user.role is not None and user.role.tenant_id != tenant_id:
            problems.append(_block(
                user, "CROSS_TENANT_ROLE",
                "Legacy login role belongs to another tenant and cannot establish authority here.",
            ))
        if role_code is None:
            problems.append(_block(user, "UNMAPPED_ROLE", f"Legacy login role {old_role_code!r} is not one of the six roles."))
        elif role_code not in roles:
            problems.append(_block(user, "MISSING_TARGET_ROLE", f"Target role {role_code!r} does not exist."))
        scope = _user_scope(user, tenant_id)
        if scope is None:
            problems.append(_block(user, "AMBIGUOUS_SCOPE", "Legacy login scope cannot be mapped to stable IDs."))
        own_grants = grants_by_human.get(user.human_id, []) if user.human_id else []
        mapped: list[tuple[RoleGrant, str, Scope]] = []
        platform_grants = [grant for grant in own_grants if grant.role.code == "X-PLT"]
        if platform_grants and len(platform_grants) == len(own_grants):
            # X-PLT is a platform identity. The old login's `it_admin` label
            # cannot silently turn it into a tenant administrator. The known
            # synthetic `admin` seed is an explicit tenant-admin fixture.
            explicit_seed = (
                allow_seeded_tenant_admin
                and user.username == "admin"
                and role_code == "it_admin"
                and _isolated_synthetic_proof_tenant(tenant_id)
            )
            already_converted = RoleAssignment.objects.filter(
                tenant_id=tenant_id, legacy_user_id=user.pk, legacy_grant__isnull=True
            ).exists()
            if not explicit_seed and not already_converted:
                problems.append(_block(user, "PLATFORM_ONLY", "Platform-only X-PLT authority needs a separate tenant Admin decision."))
        for grant in own_grants:
            if grant.role.code == "X-PLT":
                retired_platform.append(str(grant.pk))
                continue
            grant_role_code = GRANT_ROLE_ALIASES.get(grant.role.code)
            if grant_role_code is None or (grant.role.code == "C-BUY" and role_code != "brand_manager"):
                problems.append(_block(user, "UNMAPPED_GRANT", f"Grant {grant.pk} role {grant.role.code!r} needs a chosen replacement."))
                continue
            if grant_role_code not in roles or grant.role.tenant_id != tenant_id:
                problems.append(_block(user, "MISSING_TARGET_ROLE", f"Grant {grant.pk} has no tenant-matched target role."))
                continue
            grant_scope = _grant_scope(grant, tenant_id)
            if grant_scope is None:
                problems.append(_block(user, "AMBIGUOUS_GRANT_SCOPE", f"Grant {grant.pk} scope is invalid or crosses tenants."))
                continue
            mapped.append((grant, grant_role_code, grant_scope))
        proposed: list[Candidate] = []
        if not problems and user.human_id and role_code and scope:
            if mapped:
                for grant, mapped_role_code, grant_scope in mapped:
                    combined = scope.intersect(grant_scope)
                    if combined.empty():
                        problems.append(_block(user, "EMPTY_INTERSECTION", f"Grant {grant.pk} and login scopes do not overlap."))
                        break
                    end = _grant_end(grant, revoked, periods)
                    revoked_before_start = revoked.get(grant.pk)
                    if revoked_before_start is not None and revoked_before_start <= grant.effective_from:
                        # Removing a scheduled legacy grant does not create an
                        # invalid period and must never turn into live access.
                        end = _grant_end(grant, {}, periods)
                    else:
                        revoked_before_start = None
                    if end is not None and end <= grant.effective_from:
                        problems.append(_block(user, "INVALID_PERIOD", f"Grant {grant.pk} ends before it begins."))
                        break
                    proposed.append(Candidate(user.pk, user.human_id, mapped_role_code, combined, grant.effective_from, end, grant.pk, revoked_before_start))
            elif not scope.empty():
                proposed.append(Candidate(user.pk, user.human_id, role_code, scope, user.date_joined, None))
            else:
                problems.append(_block(user, "EMPTY_SCOPE", "Legacy scope selects no site/brand cell."))
        if problems:
            blocked.extend(problems)
            proposed = []
        elif proposed and user.human_id:
            # A seeded or manually edited unified assignment is already the
            # person's authority. Never add old grants beside it merely because
            # their source link is absent; that can widen a scope or restore a
            # deliberately revoked target row.
            unlinked_target = RoleAssignment.objects.filter(
                tenant_id=tenant_id, human_id=user.human_id,
                legacy_user__isnull=True, legacy_grant__isnull=True,
            ).exists()
            missing_links = False
            for row in proposed:
                link: dict[str, Any] = (
                    {"legacy_grant_id": row.legacy_grant_id}
                    if row.legacy_grant_id
                    else {"legacy_user_id": row.user_id, "legacy_grant__isnull": True}
                )
                if not RoleAssignment.objects.filter(tenant_id=tenant_id, **link).exists():
                    missing_links = True
                    break
            if unlinked_target and missing_links:
                problems.append(_block(
                    user, "EXISTING_TARGET_AUTHORITY",
                    "A unified assignment already exists without a legacy source link; reconcile it before migration.",
                ))
                blocked.extend(problems)
                proposed = []
        candidates.extend(proposed)
        old_actions = sorted({action for g in own_grants for action in (g.action_set.get("actions", []) if isinstance(g.action_set, dict) else [])})
        old_fields = sorted({field for g in own_grants for field in (g.field_set if isinstance(g.field_set, list) else [])})
        before_after.append({
            "user_id": user.pk,
            "human_id": str(user.human_id) if user.human_id else None,
            "before": {
                "role": old_role_code,
                "scope_type": user.scope_type,
                "sections": user.role.section_access if user.role else {},
                "goods_grant_ids": [str(g.pk) for g in own_grants],
                "goods_actions": old_actions,
                "goods_fields": old_fields,
            },
            "after": [{"role": c.role_code, "scope": c.scope.as_dict(), "sections": section_access_for(c.role_code), "fields": INITIAL_FIELD_ACCESS[c.role_code]} for c in proposed],
            "field_added": sorted(set().union(*(INITIAL_FIELD_ACCESS[c.role_code] for c in proposed)) - set(old_fields)),
            "field_removed": sorted(set(old_fields) - set().union(*(INITIAL_FIELD_ACCESS[c.role_code] for c in proposed))),
            "blocked": bool(problems),
        })
    for human_id, human_grants in grants_by_human.items():
        if human_id not in seen_humans:
            blocked.append({"user_id": None, "human_id": str(human_id), "code": "MISSING_LOGIN", "detail": f"{len(human_grants)} grants have no tenant login."})
    inventory = {
        "roles": Role.objects.filter(tenant_id=tenant_id).count(),
        "users": User.objects.filter(tenant_id=tenant_id).count(),
        "unbound_users": User.objects.filter(tenant__isnull=True).count(),
        "humans": HumanIdentity.objects.filter(tenant_id=tenant_id).count(),
        "legacy_grants": RoleGrant.objects.filter(tenant_id=tenant_id).count(),
        "existing_assignments": RoleAssignment.objects.filter(tenant_id=tenant_id).count(),
        "active_sessions": ServerSession.objects.filter(
            tenant_id=tenant_id, revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).count(),
        "sites": Store.objects.filter(tenant_id=tenant_id).count(),
        "brands": Brand.objects.filter(tenant_id=tenant_id).count(),
    }
    return MigrationPlan(
        tenant_id, candidates, blocked, retired_platform, policy_changes, before_after,
        inventory, allow_seeded_tenant_admin,
        [dict(row) for row in RoleAssignment.objects.filter(tenant_id=tenant_id).order_by("pk").values(
            "id", "human_id", "role_id", "all_sites", "site_ids", "all_brands", "brand_ids",
            "effective_from", "effective_to", "revoked_at", "legacy_user_id", "legacy_grant_id",
            "role__section_access", "role__field_access", "role__permissions_map",
        )],
    )


def _apply_policy_change(tenant_id: uuid.UUID, item: dict[str, Any]) -> None:
    """Write the approved baseline through the command and master-version spine."""

    from accounts.goods_admin_services import record_version
    from accounts.goods_setup import service_principal

    code = str(item["role"])
    if item.get("missing"):
        return
    after = item["after"]
    digest = hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest()
    # Include the previous policy in the command identity. If an earlier
    # rehearsal was refused before saving, the same input remains retryable.
    before_digest = hashlib.sha256(json.dumps(item["before"], sort_keys=True).encode()).hexdigest()
    command_id = uuid.uuid5(tenant_id, f"so03-policy:{code}:{before_digest}:{digest}")

    def handler(run: CommandRun) -> CommandResult:
        role = Role.objects.select_for_update().get(tenant_id=tenant_id, code=code)
        before = {
            "sections": role.section_access,
            "fields": role.field_access,
            "step_actions": (role.permissions_map or {}).get("step_actions", []),
        }
        if before != item["before"]:
            raise Refusal(
                "STATE_CONFLICT", "Role policy changed after the migration preview; run it again."
            )
        revision = int(MasterVersion.objects.filter(tenant_id=tenant_id, kind="role", target_key=code).aggregate(top=Max("revision"))["top"] or 0) + 1
        if before != after:
            role.section_access = after["sections"]
            role.field_access = after["fields"]
            role.permissions_map = {
                **(role.permissions_map or {}),
                "step_actions": after["step_actions"],
            }
            role.save(update_fields=["section_access", "field_access", "permissions_map", "updated_at"])
        record_version(
            run, kind="role", target_key=code, revision=revision,
            payload={
                "code": code,
                "section_access": role.section_access,
                "field_access": role.field_access,
                "step_actions": (role.permissions_map or {}).get("step_actions", []),
            },
            reason_code=item.get("reason_code", "so03_access_cutover"),
        )
        run.audit_subject_key = f"role:{code}"
        run.audit_before = before
        run.audit_after = after
        return CommandResult(resource_type="role", resource_id=str(role.pk))

    execute_command(
        service_principal(tenant_id, "so03-migration"),
        CommandSpec(action="access.role.update", command_id=command_id, business_input={"code": code, "policy_hash": digest}),
        handler,
    )


def _apply_tenant_cutover(tenant_id: uuid.UUID) -> dict[str, Any]:
    """Invalidate every pre-cutover session once, including blocked people."""

    from accounts.goods_setup import service_principal
    from accounts.goods_admin_services import record_version

    target_key = "access-cutover:so03"
    marker = MasterVersion.objects.filter(
        tenant_id=tenant_id, kind="tenant", target_key=target_key
    )
    if marker.exists():
        return {"applied": False, "invalidated_humans": 0, "revoked_sessions": 0}

    result_counts = {"applied": False, "invalidated_humans": 0, "revoked_sessions": 0}

    def handler(run: CommandRun) -> CommandResult:
        if marker.exists():
            return CommandResult(resource_type="tenant", resource_id=str(tenant_id))
        from accounts.unified_policy import (
            ACTION_LEVELS, WORKFLOW_TARGET_KEY, serialise_workflow_levels,
        )

        if not MasterVersion.objects.filter(
            tenant_id=tenant_id, kind="tenant", target_key=WORKFLOW_TARGET_KEY
        ).exists():
            record_version(
                run, kind="tenant", target_key=WORKFLOW_TARGET_KEY, revision=1,
                payload={"action_levels": serialise_workflow_levels(ACTION_LEVELS)},
                reason_code="so03_access_cutover",
            )
        human_ids = set(
            User.objects.filter(tenant_id=tenant_id, human_id__isnull=False)
            .values_list("human_id", flat=True)
        )
        for human_id in sorted(human_ids):
            bump_security_epoch(human_id, tenant_id)
        revoked = ServerSession.objects.filter(
            tenant_id=tenant_id, revoked_at__isnull=True
        ).update(revoked_at=run.now, step_up_at=None)
        payload = {
            "cutover": "so03",
            "invalidated_humans": len(human_ids),
            "revoked_sessions": revoked,
        }
        record_version(
            run, kind="tenant", target_key=target_key, revision=1,
            payload=payload, reason_code="so03_access_cutover",
        )
        run.audit_subject_key = f"tenant:{tenant_id}:access-cutover"
        run.audit_after = payload
        result_counts.update({
            "applied": True,
            "invalidated_humans": len(human_ids),
            "revoked_sessions": revoked,
        })
        return CommandResult(resource_type="tenant", resource_id=str(tenant_id))

    execute_command(
        service_principal(tenant_id, "so03-migration"),
        CommandSpec(
            action="access.user.update",
            command_id=uuid.uuid5(tenant_id, target_key),
            business_input={"cutover": "so03"},
        ),
        handler,
    )
    return result_counts


def apply_migration_plan(plan: MigrationPlan, *, apply_policy_defaults: bool = True) -> dict[str, Any]:
    """Insert only absent source-linked rows; conflicts block instead of overwriting."""

    if require_tenant_id() != plan.tenant_id:
        raise ValueError("migration tenant does not match the bound tenant")
    # Rebuild after locking the existing source rows. A dry-run report is never
    # authority to apply an assignment if its identity, scope or revocation
    # changed while an operator was reviewing it.
    with transaction.atomic():
        list(User.objects.select_for_update().filter(tenant_id=plan.tenant_id).values_list("pk", flat=True))
        list(HumanIdentity.objects.select_for_update().filter(tenant_id=plan.tenant_id).values_list("pk", flat=True))
        list(Role.objects.select_for_update().filter(tenant_id=plan.tenant_id).values_list("pk", flat=True))
        list(RoleGrant.objects.select_for_update().filter(tenant_id=plan.tenant_id).values_list("pk", flat=True))
        list(RoleAssignment.objects.select_for_update().filter(tenant_id=plan.tenant_id).values_list("pk", flat=True))
        list(Store.objects.select_for_update().filter(tenant_id=plan.tenant_id).values_list("pk", flat=True))
        list(Brand.objects.select_for_update().filter(tenant_id=plan.tenant_id).values_list("pk", flat=True))
        list(Sbu.objects.select_for_update().filter(tenant_id=plan.tenant_id).values_list("pk", flat=True))
        list(EffectiveVersionPeriod.objects.select_for_update().filter(tenant_id=plan.tenant_id, target_kind="grant").values_list("pk", flat=True))
        user_ids = User.objects.filter(tenant_id=plan.tenant_id).values_list("pk", flat=True)
        list(User.stores.through.objects.select_for_update().filter(user_id__in=user_ids).values_list("pk", flat=True))
        list(User.brands.through.objects.select_for_update().filter(user_id__in=user_ids).values_list("pk", flat=True))
        current_plan = build_migration_plan(
            plan.tenant_id, allow_seeded_tenant_admin=plan.allow_seeded_tenant_admin
        )
        if current_plan.source_fingerprint() != plan.source_fingerprint():
            raise Refusal("STALE_MIGRATION_PLAN", "Legacy access changed after preview; run the dry run again.", status=409)
        return _apply_current_migration_plan(plan, apply_policy_defaults=apply_policy_defaults)


def _apply_current_migration_plan(plan: MigrationPlan, *, apply_policy_defaults: bool) -> dict[str, Any]:
    """Apply the source-checked plan within the caller's transaction."""
    created = 0
    unchanged = 0
    conflicts: list[dict[str, str]] = []
    outcomes: list[dict[str, str]] = [
        {"source": f"user:{row['user_id']}", "outcome": "excluded", "reason": row["code"]}
        for row in plan.blocked if row.get("user_id") is not None
    ] + [
        {"source": f"grant:{grant_id}", "outcome": "excluded", "reason": "PLATFORM_GRANT"}
        for grant_id in plan.retired_platform_grants
    ]
    touched_humans: set[uuid.UUID] = set()
    role_ids = dict(Role.objects.filter(tenant_id=plan.tenant_id, code__in=INITIAL_ROLE_CODES).values_list("code", "pk"))
    with transaction.atomic():
        for candidate in plan.candidates:
            if candidate.role_code not in role_ids:
                conflicts.append({
                    "source": candidate.source(), "code": "MISSING_TARGET_ROLE"
                })
                outcomes.append({"source": candidate.source(), "outcome": "conflict", "reason": "MISSING_TARGET_ROLE"})
                continue
            lookup: dict[str, Any] = {"legacy_grant_id": candidate.legacy_grant_id} if candidate.legacy_grant_id else {"legacy_user_id": candidate.user_id, "legacy_grant__isnull": True}
            existing = RoleAssignment.objects.filter(tenant_id=plan.tenant_id, **lookup).first()
            desired = {
                "human_id": candidate.human_id,
                "role_id": role_ids[candidate.role_code],
                "all_sites": candidate.scope.all_sites,
                "site_ids": list(candidate.scope.site_ids),
                "all_brands": candidate.scope.all_brands,
                "brand_ids": list(candidate.scope.brand_ids),
                "effective_from": candidate.effective_from,
                "effective_to": candidate.effective_to,
                "legacy_grant_id": candidate.legacy_grant_id,
                "legacy_user_id": candidate.user_id,
            }
            if existing is not None:
                preserves_revocation = candidate.revoked_at is None or (
                    existing.revoked_at is not None and existing.revoked_at <= candidate.revoked_at
                )
                if preserves_revocation and all(getattr(existing, key) == value for key, value in desired.items()):
                    unchanged += 1
                    outcome = "historical" if (
                        existing.revoked_at is not None or
                        candidate.effective_to is not None and candidate.effective_to <= timezone.now()
                    ) else "unchanged"
                    outcomes.append({"source": candidate.source(), "outcome": outcome})
                else:
                    conflicts.append({"source": candidate.source(), "code": "EXISTING_ASSIGNMENT_DIFFERS"})
                    outcomes.append({"source": candidate.source(), "outcome": "conflict", "reason": "EXISTING_ASSIGNMENT_DIFFERS"})
                continue
            RoleAssignment.objects.create(tenant_id=plan.tenant_id, revoked_at=candidate.revoked_at, **desired)
            created += 1
            outcome = "historical" if candidate.revoked_at is not None or candidate.effective_to and candidate.effective_to <= timezone.now() else "migrated"
            outcomes.append({"source": candidate.source(), "outcome": outcome})
            touched_humans.add(candidate.human_id)
        for human_id in touched_humans:
            bump_security_epoch(human_id, plan.tenant_id)
    if apply_policy_defaults:
        for item in plan.policy_changes:
            _apply_policy_change(plan.tenant_id, item)
        if plan.policy_changes:
            for human_id in RoleAssignment.objects.filter(tenant_id=plan.tenant_id, role__code__in=[item["role"] for item in plan.policy_changes]).values_list("human_id", flat=True).distinct():
                if human_id not in touched_humans:
                    bump_security_epoch(human_id, plan.tenant_id)
    cutover = _apply_tenant_cutover(plan.tenant_id)
    return {
        "created": created,
        "unchanged": unchanged,
        "conflicts": conflicts,
        "outcomes": outcomes,
        "policy_changes_applied": len([item for item in plan.policy_changes if not item.get("missing")]),
        "cutover": cutover,
    }
