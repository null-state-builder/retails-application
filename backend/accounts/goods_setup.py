"""Deployment bootstrap, role templates and grants (design §8.1, §4.2).

Bootstrap is a protected CLI operation, not a public endpoint: it binds one
tenant to one deployment key, creates the named administrator as a person with a
login and an explicit tenant grant, and records setup evidence through the same
command kernel as every other official change. It refuses to rebind a deployment
and creates no business stock.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from accounts.actions import ROLE_TEMPLATES, RoleTemplate
from accounts.role_lists import NON_STAFF_ROLES
from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from core.refusals import Refusal
from core.tenancy import tenant_context

#: The legacy sidebar role whose section access each PRD role borrows, so a
#: goods-v1 person can still reach the existing shell's sections.
LEGACY_NAV_ROLE = {
    "X-PLT": "it_admin",
    "C-OWN": "owner",
    "M-STR": "store_person",
    "M-CSH": "store_person",
    "C-WHO": "warehouse",
    "C-PMO": "data_steward",
    "C-INV": "ho_ops",
    "C-BUY": "ho_ops",
    "C-CAO": "accounts",
    "C-STO": "ho_ops",
}

BOOTSTRAP_SERVICE = "bootstrap"

#: The service principals that seed a deployment. A grant one of these writes is
#: the seed's own, so a later re-seed may append what a widened role template
#: added to it; a grant anybody else wrote is not (see :func:`grant_is_seeded`).
SEED_SERVICES = frozenset({"seed", BOOTSTRAP_SERVICE})


def grant_is_seeded(run: CommandRun) -> bool:
    """Is this grant being written by the deployment's own seed?

    Only a seed's own grants may be widened by a later re-seed. A grant an
    administrator makes on the People screen is somebody's deliberate decision
    about one person's authority, however wide or narrow it happens to be, and a
    re-seed has no business turning it into the whole role template.

    The answer is read off the running command - the seed's service principal,
    or a ``seed.`` action - rather than taken from the caller, because a flag the
    caller sets is exactly the hole this closes: the admin API calls the same
    :func:`add_grant`, and could set it too.
    """
    return (
        run.spec.action.startswith("seed.") or (run.principal.service_code or "") in SEED_SERVICES
    )


def is_tenant_staff(role_codes: Iterable[str]) -> bool:
    """Does holding these roles make someone a member of the tenant's staff?

    Anything outside :data:`NON_STAFF_ROLES` does. Someone who holds `X-PLT`
    *alongside* a business role - the bootstrap owner, for instance - is staff on
    the strength of that business role; `X-PLT` alone is not.

    No roles at all is *not* a claim that someone is platform-only: it is a
    person nobody has granted anything to yet, so they are ordinary staff. Only
    holding a platform or service role and nothing else keeps someone out.
    """
    codes = set(role_codes)
    return not codes or bool(codes - NON_STAFF_ROLES)


def sync_tenant_staff(*, tenant_id: Any, human_id: Any, tenant_staff: bool) -> None:
    """Make a seeded person's ``Staff`` row match :func:`is_tenant_staff`.

    Seeding is idempotent and re-runnable, so a deployment seeded before
    GSA-T03's rule still carries a ``Staff`` row for a platform-only
    administrator; skipping the create on a re-seed would leave them on the
    People screen for ever. Removing such a row is a fixture correction, not
    the business deletion Phase 1 forbids - but only when it still looks like
    the untouched seed row it was created as.

    ``tenant_staff`` names what the caller's own configuration wants; a person
    can also have become staff since through a live grant the seed config knows
    nothing about (GSA-T03 lets a tenant-wide staff.manage holder grant a real
    role to someone the seed only ever gave X-PLT), so this also checks the
    human's *effective* grants - :func:`accounts.principal.effective_grants`,
    the same source E062's ``create_staff`` checks, so a revoked, expired or
    inactive-role grant (and a revocation row) does not count - and never
    removes a row that answer would keep. Only an effective grant counts: a
    brand-new person being seeded for the first time is called with
    ``tenant_staff=False`` and no grants at all yet (they are granted
    immediately afterwards, in the same seed pass), and :func:`is_tenant_staff`
    reads "no roles at all" as ordinary staff - the right answer for someone
    the goods setup has never touched, but the wrong one for a fresh
    platform-only login mid-creation. Requiring at least one effective grant
    before this check can override the caller keeps that case exactly as it
    was.

    A row is left alone - never deleted - once it carries a real
    ``StaffAssignment``, has been edited or retired (``revision`` past 1, or
    retired at all), or is named by a ``MasterVersion`` of its own: any of
    those means something else now references this ``Staff`` record, which is
    a fact this function has no standing to erase.
    """
    from accounts.goods_models import Staff, StaffAssignment
    from accounts.principal import effective_grants
    from masters.goods_models import MasterVersion

    if not tenant_staff:
        live_codes = {grant.role_code for grant in effective_grants(human_id)}
        if live_codes and is_tenant_staff(live_codes):
            tenant_staff = True
    if tenant_staff:
        Staff.objects.get_or_create(tenant_id=tenant_id, human_id=human_id)
        return
    staff = Staff.objects.filter(tenant_id=tenant_id, human_id=human_id).first()
    if staff is None:
        return
    if staff.revision != 1 or staff.retired_at is not None:
        return
    if StaffAssignment.objects.filter(staff=staff).exists():
        return
    if MasterVersion.objects.filter(
        tenant_id=tenant_id, kind=MasterVersion.Kind.STAFF, target_key=str(staff.pk)
    ).exists():
        return
    staff.delete()


def ensure_goods_roles() -> dict[str, Any]:
    """One ``Role`` row per Phase 1 role template (idempotent)."""
    from accounts.models import Role
    from accounts.rbac_matrix import section_access_for

    roles: dict[str, Any] = {}
    for code, template in ROLE_TEMPLATES.items():
        try:
            access = section_access_for(LEGACY_NAV_ROLE.get(code, ""))
        except Exception:
            access = {}
        role, _ = Role.objects.get_or_create(
            code=code,
            defaults={
                "name": template.name,
                "description": f"Phase 1 role {code}",
                "section_access": access,
                "is_system": True,
            },
        )
        roles[code] = role
    return roles


def service_principal(tenant_id: uuid.UUID, service_code: str = BOOTSTRAP_SERVICE) -> Principal:
    return Principal(tenant_id=tenant_id, service_code=service_code)


@dataclass
class GrantRequest:
    role_code: str
    scope_kind: str = "tenant"
    entity_id: int | None = None
    site_id: int | None = None
    sbu_id: uuid.UUID | None = None
    brand_id: int | None = None
    actions: list[str] | None = None
    fields: list[str] | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    #: Deliberately narrower than the template; a re-seed leaves it alone.
    narrowed: bool = False


def add_grant(run: CommandRun, human: Any, request: GrantRequest) -> Any:
    """Append one ``RoleGrant`` inside a running command, validated against its template."""
    from accounts.goods_models import RoleGrant
    from accounts.models import Role

    template: RoleTemplate | None = ROLE_TEMPLATES.get(request.role_code)
    if template is None:
        raise Refusal("INVALID_REQUEST", f"Unknown role {request.role_code}.")
    if request.scope_kind not in template.scope_kinds:
        raise Refusal(
            "INVALID_REQUEST",
            f"{request.role_code} cannot be granted at {request.scope_kind} scope.",
        )
    actions = sorted(request.actions if request.actions is not None else template.actions)
    beyond = [a for a in actions if a not in template.actions]
    if beyond:
        raise Refusal("INVALID_REQUEST", f"{request.role_code} may not hold {', '.join(beyond)}.")
    fields = sorted(request.fields if request.fields is not None else template.fields)
    if request.scope_kind == "sbu" and request.sbu_id is not None:
        from masters.goods_models import Sbu

        # A retired unit takes no new access (Anand, 18 September 2026).
        if Sbu.objects.filter(pk=request.sbu_id, retired_at__isnull=False).exists():
            raise Refusal("SBU_RETIRED", "That SBU is retired, so no access can be granted to it.")
    role = Role.objects.get(code=request.role_code)
    grant = RoleGrant(
        human_id=human.pk,
        role_id=role.pk,
        scope_kind=request.scope_kind,
        entity_id=request.entity_id if request.scope_kind == "entity" else None,
        site_id=request.site_id if request.scope_kind == "site" else None,
        sbu_id=request.sbu_id if request.scope_kind == "sbu" else None,
        brand_id=request.brand_id if request.scope_kind == "brand" else None,
        effective_from=request.effective_from or run.now,
        effective_to=request.effective_to,
        action_set={
            "actions": actions,
            "scope_kind": request.scope_kind,
            **({"narrowed": True} if request.narrowed else {}),
            **({"seeded": True} if grant_is_seeded(run) else {}),
        },
        field_set=fields,
    )
    return run.record(grant)


def pending_top_ups(
    human_id: Any, now: datetime | None = None, *, seed_owned_only: bool = False
) -> list[tuple[Any, list[str]]]:
    """Role-and-scope pairs this person holds less of than the role template says.

    A grant stores the actions it was written with, so widening a role template
    never reaches the people already holding it. This says what is missing, and
    answers per *role and scope* rather than per row: a previous top-up is a
    second grant at the same scope holding only the actions it added, and reading
    access is a union, so the two together are what the person actually holds.
    Counting them separately would make every top-up look stale for ever.

    An administrator who narrowed the role (E080) caps the answer, so nothing
    here ever names an action that role may no longer hold.

    ``seed_owned_only`` answers the narrower question a re-seed asks: which of
    these shortfalls belong to grants *the seed itself wrote*. That is what
    :func:`top_up_grants` acts on, because a grant made on the People screen -
    narrow or wide, marked or not - is somebody's decision about one person's
    authority, and a re-seed widening it to the full role template would undo
    that decision silently. A role-and-scope pair counts as the seed's when any
    grant in it was seed-written; the admin API refuses an overlapping grant at a
    role and scope somebody already holds, so nothing of anybody else's can join
    a pair the seed owns.
    """
    from accounts.principal import configured_role_access, effective_grants

    grants = effective_grants(human_id, now)
    maxima = configured_role_access(grant.role_code for grant in grants)
    held: dict[tuple[Any, ...], set[str]] = {}
    first: dict[tuple[Any, ...], Any] = {}
    narrowed: set[tuple[Any, ...]] = set()
    seeded: set[tuple[Any, ...]] = set()
    for grant in grants:
        key = (
            grant.role_code,
            grant.scope_kind,
            grant.entity_id,
            grant.site_id,
            grant.sbu_id,
            grant.brand_id,
        )
        held[key] = held.get(key, set()) | set(grant.actions)
        first.setdefault(key, grant)
        if grant.narrowed:
            narrowed.add(key)
        if grant.seeded:
            seeded.add(key)
    pending: list[tuple[Any, list[str]]] = []
    for key, actions in held.items():
        if key in narrowed:
            continue  # narrow on purpose: never widened by a re-seed
        if seed_owned_only and key not in seeded:
            continue  # somebody else's grant: the seed does not widen it
        template = ROLE_TEMPLATES.get(str(key[0]))
        if template is None:
            continue
        wanted = set(template.actions)
        maximum = maxima.get(str(key[0]))
        if maximum is not None:
            wanted &= set(maximum.actions)
        missing = sorted(wanted - actions)
        if missing:
            pending.append((first[key], missing))
    return pending


def top_up_grants(run: CommandRun, human: Any) -> list[Any]:
    """Append what each of this person's grants is short of, inside a running command.

    Append-only: one extra grant per short grant - same role, same scope, only
    the missing actions - and never an edit to the row already there. Reading
    grants is a union, so the person ends up holding the whole template again.

    Only for grants a seed wrote itself. A grant somebody deliberately narrowed
    is not short of anything; it is narrow on purpose, and topping it up would
    widen it. Neither is a grant an administrator made on the People screen: it
    is theirs, whatever its width, and this never touches it.
    """
    added: list[Any] = []
    for grant, missing in pending_top_ups(human.pk, run.now, seed_owned_only=True):
        added.append(
            add_grant(
                run,
                human,
                GrantRequest(
                    role_code=grant.role_code,
                    scope_kind=grant.scope_kind,
                    entity_id=grant.entity_id,
                    site_id=grant.site_id,
                    sbu_id=grant.sbu_id,
                    brand_id=grant.brand_id,
                    actions=missing,
                ),
            )
        )
    return added


def top_up_person_grants(tenant: Any, human: Any, label: str) -> list[str]:
    """Run :func:`top_up_grants` for one seeded person, once, through the kernel.

    Does nothing when nothing is missing, so a re-seed that changes no template
    writes no row. The command id is derived from the person *and* the exact set
    of actions being added: replaying the same top-up is recognised and skipped,
    while a later widening of the same template still gets a command of its own.
    """
    from core.canonical import content_hash

    pending = pending_top_ups(human.pk, seed_owned_only=True)
    if not pending:
        return []
    summary = sorted(
        f"{grant.role_code}|{grant.scope_kind}|"
        f"{grant.entity_id or grant.site_id or grant.sbu_id or grant.brand_id or ''}|"
        f"{','.join(missing)}"
        for grant, missing in pending
    )

    def handler(run: CommandRun) -> CommandResult:
        top_up_grants(run, human)
        return CommandResult(resource_type="human", resource_id=str(human.pk))

    execute_command(
        service_principal(tenant.pk, "seed"),
        CommandSpec(
            action="seed.grant_top_up",
            command_id=uuid.uuid5(
                tenant.deployment_key, f"grant-top-up:{label}:{content_hash(summary)[:16]}"
            ),
            business_input={"staff_code": label, "actions": summary},
        ),
        handler,
    )
    return summary


def create_person(
    run: CommandRun,
    *,
    email: str,
    display_name: str,
    staff_code: str,
    password: str | None,
    legacy_role_code: str | None = None,
    must_change_password: bool = False,
    tenant_staff: bool = True,
) -> tuple[Any, Any]:
    """A person with a login (email + password) inside a running command.

    ``must_change_password`` defaults to ``False`` so the ~2000 existing tests that
    log straight in through this fixture keep working; a test proving GSA-T03's
    restricted-session behaviour opts in explicitly.

    ``tenant_staff`` says whether this person is a member of the tenant's
    workforce and so gets a ``Staff`` row. Callers that know the person's roles
    pass :func:`is_tenant_staff`; a platform-only administrator passes ``False``
    and stays off the People screen (GSA-T03). The ``Staff`` row carries no
    assignment either way - a seeded person's authority is their grants.
    """
    from accounts.goods_models import HumanIdentity, SecurityGuard
    from accounts.models import Role, User

    if User.objects.filter(email__iexact=email).exists():
        raise Refusal("STATE_CONFLICT", f"A login already uses {email}.")
    human = HumanIdentity.objects.create(
        tenant_id=run.tenant_id, staff_code=staff_code, display_name=display_name
    )
    SecurityGuard.objects.create(tenant_id=run.tenant_id, human=human)
    sync_tenant_staff(tenant_id=run.tenant_id, human_id=human.pk, tenant_staff=tenant_staff)
    legacy_role = Role.objects.filter(code=legacy_role_code).first() if legacy_role_code else None
    user = User(
        username=email.lower()[:60],
        full_name=display_name[:120],
        email=email.lower(),
        tenant_id=run.tenant_id,
        human=human,
        role=legacy_role,
        scope_type="all",
        is_active=True,
        must_change_password=must_change_password,
    )
    if password:
        user.set_password(password)
    else:
        user.set_unusable_password()
    user.save()
    return human, user


def bootstrap_deployment(
    *,
    deployment_key: uuid.UUID,
    code: str,
    name: str,
    timezone_name: str,
    currency: str,
    locale: str,
    synthetic: bool,
    admin_email: str,
    admin_name: str,
    admin_staff_code: str,
    admin_password: str,
) -> Any:
    from masters.goods_models import Tenant

    with transaction.atomic():
        existing = Tenant.objects.filter(deployment_key=deployment_key).first()
        if existing is not None:
            if existing.code != code:
                raise Refusal(
                    "STATE_CONFLICT",
                    f"Deployment {deployment_key} is already bound to tenant {existing.code}.",
                )
            return existing
        if Tenant.objects.exists():
            raise Refusal("STATE_CONFLICT", "This database already serves a different deployment.")
        tenant = Tenant.objects.create(
            code=code,
            name=name,
            deployment_key=deployment_key,
            timezone=timezone_name,
            currency=currency,
            locale=locale,
            synthetic=synthetic,
        )
        with tenant_context(tenant.pk):
            ensure_goods_roles()

    def handler(run: CommandRun) -> CommandResult:
        human, _user = create_person(
            run,
            email=admin_email,
            display_name=admin_name,
            staff_code=admin_staff_code,
            password=admin_password,
            legacy_role_code="it_admin",
            # C-OWN alongside X-PLT: the bootstrap administrator does business
            # work too, so they are tenant staff.
            tenant_staff=is_tenant_staff(["X-PLT", "C-OWN"]),
        )
        add_grant(run, human, GrantRequest(role_code="X-PLT"))
        add_grant(run, human, GrantRequest(role_code="C-OWN"))
        run.audit_after = [{"field": "tenant", "redacted": False, "value": code}]
        return CommandResult(resource_type="tenant", resource_id=str(tenant.pk), status_code=201)

    with tenant_context(tenant.pk):
        execute_command(
            service_principal(tenant.pk),
            CommandSpec(
                action="deployment.bootstrap",
                command_id=uuid.uuid5(deployment_key, "bootstrap"),
                business_input={
                    "code": code,
                    "admin_email": admin_email.lower(),
                    "synthetic": synthetic,
                },
                subject_key=f"tenant:{tenant.pk}",
            ),
            handler,
        )
    return tenant


def now() -> datetime:
    return timezone.now()
