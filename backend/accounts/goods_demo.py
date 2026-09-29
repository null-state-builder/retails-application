"""The synthetic goods-v1 development fixture (ticket 01, GSA-T01).

Every later goods ticket needs people and places to test with. This module seeds
them inside the deployment's existing synthetic tenant: one warehouse and one
store of their own, each with its own legal entity, registration, protected
system locations and fallback SBU, plus one login for every role the design's
goods journey names.

Three rules shape it, and each is load-bearing:

* **It is additive.** The KDPS-DEMO legacy sites, users and fixtures are left
  exactly as they are; goods work gets its own tenant-neutral pair so a goods
  test can never be read as a statement about the client's data.
* **Setup is not activation.** The pair is `planned` and goods-unapproved — on
  the goods-v1 stock contract like every site (OPS-13), but an empty goods-v1
  operating scope. Seeding can log in and configure; it cannot receive, post or
  open stock. Enabling a non-empty scope is ticket 21's job, behind its own
  proofs (change PRD §14.1).
* **It is explicitly synthetic.** Names, entities and login domain all say so, so
  nobody mistakes a demo GRN for a real one.

OPS-02 (22 September 2026) adds a *second* pair beside that one: `OPS-WH` and
`OPS-ST`, which the store and warehouse operations PRD §3.1 needs open for
business - on the goods-v1 contract, with their locations, their own logins and
one registered till at the store. The first pair stays exactly as it is, because
what it proves is that seeding a site does not open it.

`X-SVC` is deliberately absent: it is a service principal, never a human, and it
must never be reachable by signing in. `syn.platform` is here but is not tenant
staff: it holds `X-PLT` alone, which administers the deployment rather than works
in the business, so it gets no `Staff` row and no place on the People screen
(GSA-T03). Every other persona does, with no assignment - authority is grants.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction

from accounts.goods_setup import (
    GrantRequest,
    add_grant,
    create_person,
    ensure_goods_roles,
    is_tenant_staff,
    service_principal,
    sync_tenant_staff,
    top_up_person_grants,
)
from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from core.refusals import Refusal
from core.tenancy import tenant_context

#: Logins say what they are. Nothing here resolves to a real mailbox.
SYNTHETIC_DOMAIN = "synthetic.demo"

#: One password for every synthetic persona: this is a development fixture, and a
#: table of different fake passwords buys nothing but transcription mistakes.
SYNTHETIC_PASSWORD = "Synthetic@123"


@dataclass(frozen=True)
class SiteSpec:
    code: str
    name: str
    kind: str
    entity_code: str
    entity_name: str
    pan: str
    state_code: str
    state_name: str
    city: str

    @property
    def gstin(self) -> str:
        # 15 characters: state code, PAN, entity number, Z, checksum placeholder.
        return f"{self.state_code}{self.pan}1Z{self.state_code[-1]}"


@dataclass(frozen=True)
class PersonaSpec:
    staff_code: str
    display_name: str
    role_code: str
    #: Site code for a site-scoped grant; ``None`` means tenant scope.
    site_code: str | None = None
    #: Extra roles. Only the deliberate multi-role denial fixture has any.
    extra: tuple[tuple[str, str | None], ...] = field(default_factory=tuple)
    #: Sites where this person prepares PTs for the site's people without being
    #: one of them: a prepare-only warehouse grant, never counting or acceptance.
    prepares_for: tuple[str, ...] = ()
    note: str = ""

    @property
    def email(self) -> str:
        return f"{self.staff_code}@{SYNTHETIC_DOMAIN}"


WAREHOUSE = "SYN-GW"
STORE = "SYN-GS"

GOODS_SITES: tuple[SiteSpec, ...] = (
    SiteSpec(
        code=WAREHOUSE,
        name="Synthetic Goods Warehouse",
        kind="warehouse",
        entity_code="syn-north",
        entity_name="Synthetic Northern Retail Pvt Ltd",
        pan="AAASN1111A",
        state_code="10",
        state_name="Synthetic North",
        city="Synthetic North City",
    ),
    SiteSpec(
        code=STORE,
        name="Synthetic Goods Store",
        kind="store",
        entity_code="syn-south",
        entity_name="Synthetic Southern Retail Pvt Ltd",
        pan="AAASS2222B",
        state_code="20",
        state_name="Synthetic South",
        city="Synthetic South City",
    ),
)

GOODS_PERSONAS: tuple[PersonaSpec, ...] = (
    PersonaSpec("syn.platform", "Synthetic Platform Admin", "X-PLT"),
    PersonaSpec("syn.owner", "Synthetic Company Owner", "C-OWN"),
    PersonaSpec("syn.buyer", "Synthetic Buyer", "C-BUY"),
    PersonaSpec("syn.warehouse", "Synthetic Warehouse Operator", "C-WHO", site_code=WAREHOUSE),
    PersonaSpec("syn.storemanager", "Synthetic Store Manager", "M-STR", site_code=STORE),
    PersonaSpec(
        "syn.receiver",
        "Synthetic Store Receiver",
        "M-CSH",
        site_code=STORE,
        note="receiving grant at the store",
    ),
    PersonaSpec("syn.productmaster", "Synthetic Product Master Owner", "C-PMO"),
    PersonaSpec(
        "syn.controller1",
        "Synthetic Inventory Controller One",
        "C-INV",
        note="maker/checker pair with controller two",
    ),
    PersonaSpec(
        "syn.controller2",
        "Synthetic Inventory Controller Two",
        "C-INV",
        note="the distinct second human an approval needs",
    ),
    PersonaSpec("syn.commercial", "Synthetic Commercial Agreement Owner", "C-CAO"),
    PersonaSpec("syn.transition", "Synthetic Site Transition Owner", "C-STO"),
    PersonaSpec(
        "syn.multirole",
        "Synthetic Multi-Role Person",
        "C-WHO",
        site_code=WAREHOUSE,
        extra=(("C-INV", None),),
        note="deliberately holds two roles: proves one human cannot approve their own work",
    ),
)


#: OPS-02: the working pair the store and warehouse operations flow needs — one
#: warehouse and one store that are actually open for goods, unlike the
#: deliberately planned `SYN-GW`/`SYN-GS` above. Both belong to one synthetic
#: legal entity in one state, which is how a real chain's own store and
#: warehouse relate, and which is what a stock transfer between them assumes.
OPS_WAREHOUSE = "OPS-WH"
OPS_STORE = "OPS-ST"

#: The store's one counter. The till is registered against it at seed time so a
#: selling screen has something to bill from; OPS-09 owns registering a till
#: from the app and filling in its 24-hour authority.
OPS_TILL_COUNTER = "T1"

OPS_SITES: tuple[SiteSpec, ...] = (
    SiteSpec(
        code=OPS_WAREHOUSE,
        name="Synthetic Operations Warehouse",
        kind="warehouse",
        entity_code="syn-ops",
        entity_name="Synthetic Operations Retail Pvt Ltd",
        pan="AAASO3333C",
        state_code="30",
        state_name="Synthetic Ops State",
        city="Synthetic Ops City",
    ),
    SiteSpec(
        code=OPS_STORE,
        name="Synthetic Operations Store",
        kind="store",
        entity_code="syn-ops",
        entity_name="Synthetic Operations Retail Pvt Ltd",
        pan="AAASO3333C",
        state_code="30",
        state_name="Synthetic Ops State",
        city="Synthetic Ops City",
    ),
)

#: One login per job at the ready pair. The Owner is `syn.owner` above: the PRD
#: gives approval to one Owner across the business, and a second owner login
#: would only invite someone to approve their own site's work.
OPS_PERSONAS: tuple[PersonaSpec, ...] = (
    PersonaSpec(
        "ops.warehouse",
        "Operations Warehouse Operator",
        "C-WHO",
        site_code=OPS_WAREHOUSE,
        prepares_for=(OPS_STORE,),
        note="prepares and submits PTs at OPS-WH and for OPS-ST; approves none of them",
    ),
    PersonaSpec(
        "ops.store",
        "Operations Store Person",
        "M-STR",
        site_code=OPS_STORE,
        note="the store floor at OPS-ST: receives, accepts, counts, sells",
    ),
    PersonaSpec(
        "ops.cashier",
        "Operations Store Cashier",
        "M-CSH",
        site_code=OPS_STORE,
        note="bills at the registered till at OPS-ST",
    ),
)


#: What a warehouse operator may do at a store whose PTs they prepare: the PT
#: work and its labels, nothing physical. The store's own people count, accept
#: and put away (PT operational agreement: the warehouse uploads the store's PT).
PREPARE_ONLY_ACTIONS = ("pt.prepare", "pt.view", "label.print", "stock.view", "exception.view")


def _prepare_only_grant(site_id: int) -> GrantRequest:
    return GrantRequest(
        role_code="C-WHO",
        scope_kind="site",
        site_id=site_id,
        actions=list(PREPARE_ONLY_ACTIONS),
        narrowed=True,
    )


def _grant(role_code: str, site_code: str | None, sites: dict[str, Any]) -> GrantRequest:
    if site_code is None:
        return GrantRequest(role_code=role_code)
    return GrantRequest(role_code=role_code, scope_kind="site", site_id=sites[site_code].pk)


def _build_sites(tenant: Any, specs: tuple[SiteSpec, ...]) -> dict[str, Any]:
    """Entities, registrations, sites, system locations and a fallback unit.

    Every site starts planned and goods-unapproved on the goods-v1 stock contract
    - an empty goods-v1 operating scope, born the way the Organisation screen's
    own create-site command births one. No site stays on the legacy contract
    (store and warehouse operations PRD §3.1, OPS-13). Opening one is a separate,
    deliberate step.
    """
    from masters.goods_models import SiteGuard
    from masters.goods_services import ensure_site_sbus, ensure_system_locations
    from masters.models import Gstin, LegalEntity, Store

    sites: dict[str, Any] = {}
    for spec in specs:
        entity, _ = LegalEntity.objects.get_or_create(
            code=spec.entity_code, defaults={"name": spec.entity_name, "pan": spec.pan}
        )
        registration, _ = Gstin.objects.get_or_create(
            gstin=spec.gstin,
            defaults={
                "legal_entity": entity,
                "state_code": spec.state_code,
                "state_name": spec.state_name,
            },
        )
        store, _ = Store.objects.get_or_create(
            code=spec.code,
            defaults={
                "name": spec.name,
                "store_type": spec.kind,
                "gstin": registration,
                "city": spec.city,
            },
        )
        SiteGuard.objects.get_or_create(
            site=store,
            defaults={
                "tenant_id": tenant.pk,
                "lifecycle": SiteGuard.Lifecycle.PLANNED,
                "stock_contract": SiteGuard.StockContract.GOODS_V1,
                "goods_ready": False,
                "sell_ready": False,
                "opening_setup_ready": False,
            },
        )
        ensure_system_locations(tenant.pk, store)
        ensure_site_sbus(tenant.pk, store, [])
        sites[spec.code] = store
    return sites


def seed_goods_sites(tenant: Any) -> dict[str, Any]:
    """The synthetic warehouse/store pair, planned and goods-unapproved.

    Left exactly as it is on purpose: tickets and browser specs rely on this pair
    being a site nobody has opened yet. The pair that *is* open is OPS-02's, below.
    """
    return _build_sites(tenant, GOODS_SITES)


def _ensure_working_locations(tenant_id: Any, site: Any) -> None:
    """A floor, a backstore and one bin inside that backstore (idempotent).

    Beside the protected system locations, these are the places goods actually
    sit: what is out for sale, what is held behind, and one named shelf inside
    the held space to move a carton to.
    """
    from masters.goods_models import Location

    Location.objects.get_or_create(
        site=site, name="Floor", defaults={"tenant_id": tenant_id, "kind": Location.Kind.FLOOR}
    )
    backstore, _ = Location.objects.get_or_create(
        site=site,
        name="Backstore",
        defaults={"tenant_id": tenant_id, "kind": Location.Kind.BACKSTORE},
    )
    Location.objects.get_or_create(
        site=site,
        name="Bin 1",
        defaults={"tenant_id": tenant_id, "kind": Location.Kind.BIN, "parent": backstore},
    )


def _owner_human_id(tenant: Any) -> Any:
    from accounts.models import User

    owner = User.objects.filter(email__iexact=f"syn.owner@{SYNTHETIC_DOMAIN}").first()
    if owner is None or owner.human_id is None:
        raise Refusal(
            "STATE_CONFLICT",
            "The synthetic owner must be seeded before the operations pair is opened.",
        )
    return owner.human_id


#: Why this pair's records exist, on every seeded row that carries a reason. A
#: plain word, so anyone reading an approval trail here knows whose it is.
SEED_REASON = "SYNTHETIC_SEED"

#: Every document the ready pair's own work produces needs its numbering series
#: before the first one can be written: goods received and counter-received,
#: receipt, opening and transfer PTs, bookings, movements, holds, releases and
#: transit-shortage corrections (GAP, goods ticket 14).
#: Readiness itself insists on three of them (`READINESS_SERIES`); the rest
#: would each fail at their first document instead, which is a worse place to
#: find out.
OPS_SERIES = ("BKG", "GRN", "CGRN", "RPT", "OPT", "TPT", "MOV", "HLD", "REL", "GAP")


def _ensure_ops_series(tenant: Any, store: Any) -> None:
    """The numbering series this pair's shared legal entity needs (idempotent).

    Numbering is the one piece of site setup with no operator command behind it:
    a series is published by activation and by seeds, never by a request
    (`masters.goods_services.SETUP_RESOLUTION_ACTIONS` says so in as many
    words). `prepare_series` is the product's own function for exactly this -
    create the series and publish its first ceiling - and it is what the seed
    uses rather than a hand-written row.

    Both operations sites share one legal entity, so the second site finds the
    first site's series already there.
    """
    from core.numbering import prepare_series

    for doc_type in OPS_SERIES:
        prepare_series(tenant.pk, store.gstin.legal_entity, doc_type)


def _publish_site_master_records(tenant: Any, spec: SiteSpec, store: Any) -> None:
    """Publish this site's legal entity and registration as master records.

    Readiness refuses to approve a site whose legal entity or registration has
    no approved, in-force master record, and neither refusal can be overridden -
    rightly, because a site that cannot say whose it is and what it is
    registered as has no business receiving anything. The ordinary way those
    records appear is somebody creating the entity and the registration on the
    Organisation screen, which publishes the first version of each.

    This pair's entity and registration were made as plain rows by an earlier
    seed, so they have no version to read. The same append the create command
    makes is made here instead, under the seed's own reason: the record says
    where it came from rather than pretending to be somebody's decision.

    Idempotent, and shared: both operations sites belong to one legal entity and
    one registration, so the second site finds the first site's records already
    published and writes nothing.
    """
    from core.kernel_models import SubjectRevision
    from masters.goods_services import append_master_version, latest_master_version, start_revision

    entity = store.gstin.legal_entity
    registration = store.gstin
    wanted = [
        (
            "entity",
            "master:entity",
            str(entity.pk),
            {
                "code": entity.code,
                "name": entity.name,
                "pan": entity.pan,
                "address": None,
                "books_code": None,
            },
        ),
        (
            "registration",
            "master:registration",
            str(registration.pk),
            {
                "entity_id": str(entity.pk),
                "gstin": registration.gstin,
                "state_code": registration.state_code,
                "state_name": registration.state_name,
                "effective_from": None,
            },
        ),
    ]
    missing = [row for row in wanted if latest_master_version(tenant.pk, row[0], row[2]) is None]
    if not missing:
        return

    def handler(run: CommandRun) -> CommandResult:
        for kind, family, target_key, payload in missing:
            if not SubjectRevision.objects.filter(
                tenant_id=run.tenant_id, family=family, subject_key=target_key
            ).exists():
                start_revision(run.tenant_id, family, target_key)
            append_master_version(
                run,
                kind=kind,
                target_key=target_key,
                revision=1,
                payload=payload,
                reason_code=SEED_REASON,
            )
        return CommandResult(resource_type="entity", resource_id=str(entity.pk))

    execute_command(
        service_principal(tenant.pk, "seed"),
        CommandSpec(
            action="seed.ops_site_masters",
            command_id=uuid.uuid5(tenant.deployment_key, f"ops-site-masters:{spec.entity_code}"),
            business_input={"entity_code": spec.entity_code},
            subject_key=f"entity:{entity.pk}",
        ),
        handler,
    )


def _open_ops_site(tenant: Any, spec: SiteSpec, store: Any, approver_id: Any) -> None:
    """Open one operations site for setup and for goods, the way a real site opens.

    Both steps run the product's own readiness approval - the same function the
    readiness screen's command calls - so this pair is held to every check a
    real site is held to. Nothing is waved through: the mandatory requirements
    have to pass, and each remaining gap is decided one at a time with a reason
    that says plainly this is the development pair.

    Which is what makes the refusals real too. A missing numbering series or an
    unpublished registration stops the seed here, loudly, instead of leaving a
    site that looks open and fails at the first receipt.

    Selling is the one flag with no command of its own: nothing in the product
    approves `sell_ready`, so the store's is set here and said so. The stock
    contract needs no step of its own: since OPS-13 every site this fixture
    builds is born on goods-v1, and the assignment inside the approval below
    only brings into line a pair a seed before that change left on the legacy
    contract and never opened.
    """
    from masters.goods_models import SiteGuard
    from masters.goods_services import apply_readiness_approval, compute_readiness_checks

    wants_sell = spec.kind == "store"
    guard = SiteGuard.objects.filter(site=store).first()
    if (
        guard is not None
        and guard.goods_ready
        and guard.opening_setup_ready
        and guard.sell_ready == wants_sell
        and guard.lifecycle == SiteGuard.Lifecycle.ACTIVE
        and guard.stock_contract == SiteGuard.StockContract.GOODS_V1
    ):
        return

    def approve(action: str) -> None:
        def handler(run: CommandRun) -> CommandResult:
            row = SiteGuard.objects.get(site=store)
            if action == "approve_opening_setup" and row.opening_setup_ready:
                return CommandResult(resource_type="readiness", resource_id=str(store.pk))
            if action == "approve_goods" and row.goods_ready:
                return CommandResult(resource_type="readiness", resource_id=str(store.pk))
            # The goods contract is what the rest of the readiness answers are
            # about, so it is set before they are computed rather than after.
            row.stock_contract = SiteGuard.StockContract.GOODS_V1
            checks = compute_readiness_checks(store, run.now)
            # Each gap that can be decided over gets its own reason. The ones
            # that cannot are not decided here - they refuse, and should.
            residuals = [
                {
                    "code": check["key"],
                    "message": "The development store and warehouse pair: opened without this.",
                }
                for check in checks
                if check["required"] and not check["passed"] and check["overridable"]
            ]
            apply_readiness_approval(
                run,
                store=store,
                guard=row,
                action=action,
                checks=checks,
                reason_code=SEED_REASON,
                residual_decisions=residuals,
                approver_id=approver_id,
            )
            return CommandResult(resource_type="readiness", resource_id=str(store.pk))

        execute_command(
            service_principal(tenant.pk, "seed"),
            CommandSpec(
                action=f"seed.ops_site_{action}",
                command_id=uuid.uuid5(tenant.deployment_key, f"ops-site-{action}:{spec.code}"),
                business_input={"code": spec.code},
                subject_key=f"site:{store.pk}",
                site_id=store.pk,
            ),
            handler,
        )

    approve("approve_opening_setup")
    approve("approve_goods")

    if wants_sell:

        def sell_handler(run: CommandRun) -> CommandResult:
            row = SiteGuard.objects.get(site=store)
            if not row.sell_ready:
                row.sell_ready = True
                row.revision += 1
                row.save()
            return CommandResult(resource_type="readiness", resource_id=str(store.pk))

        execute_command(
            service_principal(tenant.pk, "seed"),
            CommandSpec(
                action="seed.ops_site_sell_ready",
                command_id=uuid.uuid5(tenant.deployment_key, f"ops-site-sell:{spec.code}"),
                business_input={"code": spec.code},
                subject_key=f"site:{store.pk}",
                site_id=store.pk,
            ),
            sell_handler,
        )


def seed_ops_sites(tenant: Any) -> dict[str, Any]:
    """OPS-02: one warehouse and one store that are open for goods work.

    Both end up on the `goods_v1` stock contract, active and goods-ready, with
    the six system locations plus a floor, a backstore and one bin; the store is
    sell-ready as well. Idempotent: a re-seed finds them already open and writes
    nothing.
    """
    sites = _build_sites(tenant, OPS_SITES)
    approver_id = _owner_human_id(tenant)
    for spec in OPS_SITES:
        store = sites[spec.code]
        _ensure_working_locations(tenant.pk, store)
        _publish_site_master_records(tenant, spec, store)
        _ensure_ops_series(tenant, store)
        _open_ops_site(tenant, spec, store, approver_id)
    return sites


def seed_ops_till(tenant: Any, store: Any) -> Any:
    """The store's one registered till (idempotent).

    One till per store, so the row is keyed by the store itself. Its authority
    window is left empty: OPS-09 issues and renews the 24-hour authority an
    offline till bills under, and a seed has no business minting one.
    """
    from accounts.models import User
    from sell.models import RegisteredTill

    existing = RegisteredTill.objects.filter(store=store, active=True).first()
    if existing is not None:
        return existing
    owner = User.objects.filter(email__iexact=f"syn.owner@{SYNTHETIC_DOMAIN}").first()
    if owner is None:
        raise Refusal(
            "STATE_CONFLICT", "The synthetic owner must be seeded before the till is registered."
        )
    return RegisteredTill.objects.create(
        store=store,
        counter_id=OPS_TILL_COUNTER,
        registered_by=owner,
        series_prefix=f"{store.code}-{OPS_TILL_COUNTER}",
        active=True,
    )


def seed_ops_selling(store: Any) -> None:
    """What the ready store needs before its till can write a bill (idempotent).

    Two things, both of which the foundation seed gives every *legacy* selling
    store and neither of which reached this pair, because this pair is created
    afterwards:

    * **somebody to sell.** Every sold line carries the name of who sold it, and
      the counter refuses a bill that names nobody - so a store with no salesperson
      cannot sell at all, and the refusal reads like a defect when it is a
      missing row. The foundation seed says exactly this about its own stores.
    * **its selling series.** A sale's number is not the server's to create
      lazily: the till already holds a number when it calls, so a missing series
      turns a printed bill into a sync failure the store cannot fix. Next year's
      rows ride along for the same reason they do there - the roll happens at
      midnight on 1 April, when nobody is deploying.
    """
    from core.documents import VoucherSeries
    from core.fiscal import financial_year, next_financial_year
    from sell.services.salespeople import ensure_salesperson

    for code, name in (("S1", "Counter One"), ("S2", "Counter Two")):
        ensure_salesperson(store, f"{store.code}-{code}", f"{name} ({store.code})")
    for fy in (financial_year(), next_financial_year()):
        for doc_type in ("SAL", "CRN", "SRT"):
            VoucherSeries.objects.get_or_create(fy=fy, store_code=store.code, doc_type=doc_type)


def _wire_shell_access(
    user: Any, role_code: str, site_code: str | None, sites: dict[str, Any]
) -> None:
    """Give one ready-pair login the shell the PRD says its role reaches.

    The goods screens are drawn from a person's goods grants, so a goods-only
    login has always reached those. The *rest* of the shell is not: Sell, Money,
    Reports and Attendance are drawn from the legacy section ladder, which reads
    the login's `Role` row and its store scope - and a person seeded with goods
    grants alone has neither, so PRD §12's ten store rows came out as the goods
    lines and nothing else, and the counter had no store to bill at.

    The row to point them at is the RBAC v1 one their PRD role *is* -
    `LEGACY_NAV_ROLE` already states that correspondence, and the shell keys
    both its section ladder and its persona layouts (PRD §12's ten store rows,
    the warehouse's own) on those codes. A login carrying a goods-only code gets
    the sections but none of the layout, which is a sidebar nobody designed.
    Site personas are scoped to the one site they work at, so the counter knows
    which store it is standing in.

    Idempotent, and confined to the ready pair and the one Owner who approves
    their work: the `SYN-GW`/`SYN-GS` personas are deliberately goods-only and
    are left exactly as they are.
    """
    from accounts.goods_setup import LEGACY_NAV_ROLE
    from accounts.models import Role, ScopeType

    role = Role.objects.filter(code=LEGACY_NAV_ROLE.get(role_code, "")).first()
    site = sites.get(site_code or "")
    changed: list[str] = []
    if role is not None and user.role_id != role.pk:
        user.role = role
        changed.append("role")
    if site is not None and user.scope_type != ScopeType.STORE:
        user.scope_type = ScopeType.STORE
        changed.append("scope_type")
    if changed:
        user.save(update_fields=changed)
    if site is not None and not user.stores.filter(pk=site.pk).exists():
        user.stores.add(site)


def seed_goods_personas(
    tenant: Any, sites: dict[str, Any], personas: tuple[PersonaSpec, ...] = GOODS_PERSONAS
) -> list[PersonaSpec]:
    """One login per goods role, each added through the command kernel once."""
    from accounts.models import User

    # The ready pair's people work in the whole shell, not only in the goods
    # screens; the deliberately unopened `SYN-GW`/`SYN-GS` pair's people do not.
    wire_shell = personas is OPS_PERSONAS

    created: list[PersonaSpec] = []
    for persona in personas:
        # `syn.platform` holds X-PLT alone, so it gets no Staff row: platform
        # administration is not tenant employment (GSA-T03). Every other persona
        # is manageable on the People screen.
        staff = is_tenant_staff([persona.role_code, *(code for code, _ in persona.extra)])
        existing = User.objects.filter(email__iexact=persona.email).first()
        if existing is not None:
            # Already seeded, possibly before the rule above existed: bring the
            # row into line rather than leaving a stale one on the screen.
            if existing.human_id is not None:
                sync_tenant_staff(
                    tenant_id=tenant.pk, human_id=existing.human_id, tenant_staff=staff
                )
                # A grant keeps the actions it was written with, so a persona
                # seeded before a role template widened is short of the new
                # ones. Top the difference up rather than leaving a login that
                # cannot do what its role says (PRD §3.2 widened C-OWN).
                top_up_person_grants(tenant, existing.human, persona.staff_code)
            if wire_shell:
                _wire_shell_access(existing, persona.role_code, persona.site_code, sites)
            continue
        requests = [_grant(persona.role_code, persona.site_code, sites)]
        requests += [_grant(code, site_code, sites) for code, site_code in persona.extra]
        requests += [_prepare_only_grant(sites[code].pk) for code in persona.prepares_for]

        def handler(
            run: CommandRun,
            persona: PersonaSpec = persona,
            requests: Any = requests,
            staff: bool = staff,
        ) -> CommandResult:
            human, _user = create_person(
                run,
                email=persona.email,
                display_name=persona.display_name,
                staff_code=persona.staff_code,
                password=SYNTHETIC_PASSWORD,
                tenant_staff=staff,
            )
            for request in requests:
                add_grant(run, human, request)
            return CommandResult(resource_type="human", resource_id=str(human.pk), status_code=201)

        execute_command(
            service_principal(tenant.pk, "seed"),
            CommandSpec(
                action="seed.goods_persona",
                command_id=uuid.uuid5(tenant.deployment_key, f"goods-persona:{persona.staff_code}"),
                business_input={"staff_code": persona.staff_code},
            ),
            handler,
        )
        if wire_shell:
            fresh = User.objects.filter(email__iexact=persona.email).first()
            if fresh is not None:
                _wire_shell_access(fresh, persona.role_code, persona.site_code, sites)
        created.append(persona)
    return created


#: A permanent fixture brand, distinct from the throwaway brand each Playwright
#: run creates through `goods/bookings`. Its point is exactly that distinctness:
#: `syn.brandscoped` (GSA-T08) is granted only here, so a test proving "a
#: brand-scoped user cannot see another brand's exception" (ticket 08) has a
#: brand genuinely different from the one the exception under test belongs to,
#: without the fixture and the test having to coordinate a shared id.
BRAND_CODE = "SYN-BRAND-FIX"
BRAND_SCOPED_STAFF_CODE = "syn.brandscoped"


def seed_goods_brand(tenant: Any) -> Any:
    """The one permanent synthetic brand `syn.brandscoped` is granted at."""
    from masters.models import Brand

    brand, _ = Brand.objects.get_or_create(
        code=BRAND_CODE,
        defaults={
            "name": "Synthetic Fixture Brand",
            "ownership": Brand.Ownership.OWNED,
            "return_terms": Brand.ReturnTerms.NONE,
        },
    )
    return brand


def seed_brand_scoped_persona(tenant: Any, brand: Any) -> None:
    """`syn.brandscoped`: a C-BUY grant at brand scope and nothing else (GSA-T08).

    C-BUY is the one role template goods-v1 grants at `scope_kind="brand"`
    (`accounts/actions.py`), and it holds `exception.view` through `_VIEWING`.
    `GoodsException` carries a `site`, never a `brand` (`alerts/goods_models.py`)
    — `AccessContext.store_site_ids` (what the exceptions list reads scope from)
    does not expand a brand-only grant into any site at all, so this persona
    reads no exception, ever, from any site or brand. That is the proof: a
    brand-scoped grant leaks nobody's exceptions, this fixture brand's or
    another one's, because the record it would need to be scoped *by* does not
    exist on the model. `test_goods_exceptions_centre.py` and
    `e2e/goods-exceptions.spec.ts` assert this rather than merely relying on it.
    """
    from accounts.models import User

    email = f"{BRAND_SCOPED_STAFF_CODE}@{SYNTHETIC_DOMAIN}"
    if User.objects.filter(email__iexact=email).exists():
        return

    def handler(run: CommandRun) -> CommandResult:
        human, _user = create_person(
            run,
            email=email,
            display_name="Synthetic Brand-Scoped Buyer",
            staff_code=BRAND_SCOPED_STAFF_CODE,
            password=SYNTHETIC_PASSWORD,
            tenant_staff=is_tenant_staff(["C-BUY"]),
        )
        add_grant(
            run,
            human,
            GrantRequest(role_code="C-BUY", scope_kind="brand", brand_id=brand.pk),
        )
        return CommandResult(resource_type="human", resource_id=str(human.pk), status_code=201)

    execute_command(
        service_principal(tenant.pk, "seed"),
        CommandSpec(
            action="seed.goods_persona",
            command_id=uuid.uuid5(
                tenant.deployment_key, f"goods-persona:{BRAND_SCOPED_STAFF_CODE}"
            ),
            business_input={"staff_code": BRAND_SCOPED_STAFF_CODE},
        ),
        handler,
    )


#: GSA-T08's synthetic working calendar: Monday-Saturday, Asia/Kolkata, no
#: holidays. **A fixture only** (implementation-decisions.md GSA-T08, design
#: §5.8) — seeded for the synthetic tenant so its working-day exceptions have
#: an approved calendar to be dated against, and never read as a real-tenant
#: default or an inferred approval. A real deployment needs its own explicit
#: calendar approval before any working-day exception in it gets a deadline at
#: all; until then `alerts.goods_services.due_at` answers `None` (ticket 08C),
#: and goods activation readiness refuses to approve a site in that state.
WORKING_CALENDAR_PAYLOAD: dict[str, Any] = {
    "timezone": "Asia/Kolkata",
    "working_weekdays": [1, 2, 3, 4, 5, 6],
    "excluded_dates": [],
}


def seed_working_calendar(tenant: Any, approver_human_id: Any, *, starts_days_ago: int = 1) -> None:
    """Approve GSA-T08's synthetic calendar for the synthetic tenant (idempotent).

    A fixture, never a real-tenant default: a real tenant approves its own
    calendar, and without one a working-day deadline is refused rather than
    guessed (``alerts.goods_services.due_at``). ``starts_days_ago`` backdates the
    effective period, so a tenant that later approves a calendar of its own
    supersedes this one instead of clashing with it.
    """
    from datetime import timedelta

    from core.canonical import content_hash
    from core.refusals import Refusal
    from masters.goods_config import activate, normalise_scope
    from masters.goods_models import ConfigDraft, ConfigVersion

    if not tenant.synthetic:
        raise Refusal(
            "CONTRACT_DISABLED",
            "This Monday-Saturday calendar is a synthetic fixture. A real business "
            "approves its own working calendar.",
        )
    if ConfigVersion.objects.filter(tenant_id=tenant.pk, kind="working_calendar").exists():
        return

    def handler(run: CommandRun) -> CommandResult:
        canonical = normalise_scope({"scope_kind": "tenant"})
        starts = run.now - timedelta(days=starts_days_ago)
        draft = ConfigDraft.objects.create(
            tenant_id=run.tenant_id,
            kind="working_calendar",
            scope=canonical,
            scope_key=content_hash(canonical)[:64],
            payload=WORKING_CALENDAR_PAYLOAD,
            effective_from=starts,
            maker_id=approver_human_id,
            state=ConfigDraft.State.APPROVED,
        )
        version = run.record(
            ConfigVersion(
                draft_id=draft.pk,
                kind="working_calendar",
                version=1,
                scope=canonical,
                scope_key=draft.scope_key,
                payload=WORKING_CALENDAR_PAYLOAD,
                effective_from=starts,
                approved_by_id=approver_human_id,
                source_revision=1,
                source_hash=content_hash(WORKING_CALENDAR_PAYLOAD),
            )
        )
        activate(run, version, effective_to=None)
        return CommandResult(resource_type="configuration", resource_id=str(version.pk))

    execute_command(
        service_principal(tenant.pk, "seed"),
        CommandSpec(
            action="seed.working_calendar",
            command_id=uuid.uuid5(tenant.deployment_key, "goods-working-calendar"),
            business_input={"kind": "working_calendar"},
        ),
        handler,
    )


# ---------------------------------------------------------------------------
# OPS-11: a small product catalogue for the ready pair
# ---------------------------------------------------------------------------
#
# The operations pair had places, people and an open contract, and nothing to
# put on a shelf. Opening stock names a SKU outright, acceptance matches the
# scanned tag against that line's approved aliases, and the till resolves a
# barcode to a SKU - so a journey from opening to a bill needs a handful of
# real, governed products at this pair and nothing more.
#
# Everything here goes through the product's own master path: the same
# validation, the same identity key, the same append-only master version the
# Product Master screen's create writes. A seed is the one caller allowed to do
# that without a person behind it, and it says so in the command it runs.

#: The size list the profile wizard's own fashion starting draft carries, and
#: the one `e2e/goodsActivation.ts`'s baseline expects to find. Matching it
#: exactly matters: the browser fixture reuses a vocabulary equal to its own and
#: supersedes anything else, which would leave these SKUs pinned to a version
#: that had ended.
CATALOGUE_SIZES: list[dict[str, Any]] = [
    {"value_key": "XS", "label": "XS", "sort_order": 0, "retired": False},
    {"value_key": "S", "label": "S", "sort_order": 1, "retired": False},
    {"value_key": "M", "label": "M", "sort_order": 2, "retired": False},
    {"value_key": "L", "label": "L", "sort_order": 3, "retired": False},
    {"value_key": "XL", "label": "XL", "sort_order": 4, "retired": False},
    {"value_key": "XXL", "label": "XXL", "sort_order": 5, "retired": False},
    {"value_key": "FREE", "label": "Free Size", "sort_order": 6, "retired": False},
]

#: One product family for the whole fixture catalogue. Size alone distinguishes
#: one SKU from another in it, which is the smallest honest fashion identity.
CATALOGUE_FAMILY = "fashion"

CATALOGUE_IDENTITY_PROFILE: dict[str, Any] = {
    "family": CATALOGUE_FAMILY,
    "distinguishing_dimensions": [],
    "size_dimension": "size",
    "colour_dimension": None,
    "grade_dimension": None,
    # Explicitly empty keeps its meaning: unrestricted (GSA-T04).
    "allowed_size_values": [],
    "allowed_colour_values": [],
    "allowed_grade_values": [],
}

#: Who issued these barcodes. Not a vendor: nobody sent these goods: the
#: business printed its own codes for its own fixture stock.
CATALOGUE_ISSUER = "syn-ops"

#: Two styles, two sizes each. The barcodes are deliberately in a block of their
#: own so nothing here can be mistaken for the legacy demo's `8901…` codes,
#: which belong to other stores and other tests.
CATALOGUE_STYLES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("OPS-SHIRT", (("S", "8902000000010"), ("M", "8902000000020"))),
    ("OPS-TROUSER", (("S", "8902000000030"), ("M", "8902000000040"))),
)


def _publish_tenant_config(
    tenant: Any, approver_human_id: Any, *, kind: str, payload: dict[str, Any], label: str
) -> Any:
    """Approve one tenant-wide configuration line, once (idempotent).

    The same shape `seed_working_calendar` uses, for the two configuration lines
    a product catalogue cannot exist without: the size vocabulary its SKUs name,
    and the identity profile that says what makes one SKU different from
    another. Both are the business's own decisions in a real deployment; here
    they are the fixture's, recorded as such.
    """
    from core.canonical import content_hash
    from core.commands import database_now
    from masters.goods_config import activate, in_force
    from masters.goods_models import ConfigDraft, ConfigVersion

    def matches(version: Any) -> bool:
        current = version.payload if isinstance(version.payload, dict) else {}
        return all(current.get(key) == value for key, value in payload.items())

    live = [
        version
        for version in ConfigVersion.objects.filter(tenant_id=tenant.pk, kind=kind)
        if in_force(version, database_now()) and matches(version)
    ]
    if live:
        return live[0]

    holder: dict[str, Any] = {}

    def handler(run: CommandRun) -> CommandResult:
        from masters.goods_config import normalise_scope

        canonical = normalise_scope({"scope_kind": "tenant"})
        draft = ConfigDraft.objects.create(
            tenant_id=run.tenant_id,
            kind=kind,
            scope=canonical,
            scope_key=content_hash(canonical)[:64],
            payload=payload,
            effective_from=run.now,
            maker_id=approver_human_id,
            state=ConfigDraft.State.APPROVED,
        )
        version = run.record(
            ConfigVersion(
                draft_id=draft.pk,
                kind=kind,
                version=ConfigVersion.objects.filter(tenant_id=run.tenant_id, kind=kind).count()
                + 1,
                scope=canonical,
                scope_key=draft.scope_key,
                payload=payload,
                effective_from=run.now,
                approved_by_id=approver_human_id,
                source_revision=1,
                source_hash=content_hash(payload),
            )
        )
        activate(run, version, effective_to=None)
        holder["version"] = version
        return CommandResult(resource_type="configuration", resource_id=str(version.pk))

    execute_command(
        service_principal(tenant.pk, "seed"),
        CommandSpec(
            action="seed.ops_catalogue_config",
            command_id=uuid.uuid5(tenant.deployment_key, f"ops-catalogue-config:{label}"),
            business_input={"kind": kind, "label": label},
        ),
        handler,
    )
    return holder.get("version")


def _catalogue_style(run: CommandRun, brand: Any, style_code: str) -> Any:
    """One fixture style, created the way the Product Master screen creates one."""
    from masters.goods_identity_models import GovernanceState, Style
    from masters.goods_identity_services import record_master_version, save_master

    style = Style.objects.filter(
        tenant_id=run.tenant_id,
        brand_id=brand.pk,
        profile_family=CATALOGUE_FAMILY,
        style_code=style_code,
    ).first()
    if style is not None:
        return style
    style = Style(
        tenant_id=run.tenant_id,
        brand_id=brand.pk,
        style_code=style_code,
        profile_family=CATALOGUE_FAMILY,
        attrs=[],
        governance_state=GovernanceState.EFFECTIVE,
    )
    save_master(style, conflict="That style already exists for this brand.")
    record_master_version(run, "style", style)
    return style


def _catalogue_sku(run: CommandRun, style: Any, profile: Any, size: str) -> Any:
    """One SKU of that style in one size.

    The identity key is the product's own - `sku_identity` validates the
    attributes against the profile and computes the key - so two seeds of the
    same size land on the same SKU rather than on two that look alike.
    """
    from masters.goods_identity_models import GovernanceState, ProductSku
    from masters.goods_identity_services import (
        record_master_version,
        save_master,
        sku_identity,
        vocabulary_value_id,
    )

    attrs = [
        {
            "field_id": "size",
            "unknown": False,
            "vocabulary_value_id": str(vocabulary_value_id("size", size)),
        }
    ]
    key, stored = sku_identity(run.tenant_id, style, profile, attrs, run.now)
    sku = ProductSku.objects.filter(tenant_id=run.tenant_id, identity_key=key).first()
    if sku is not None:
        return sku
    sku = ProductSku(
        tenant_id=run.tenant_id,
        style_id=style.pk,
        identity_key=key,
        identity_profile_id=profile.version_id,
        attrs=stored,
        governance_state=GovernanceState.EFFECTIVE,
    )
    save_master(sku, conflict="A SKU with exactly these identity attributes already exists.")
    record_master_version(run, "sku", sku)
    return sku


def _catalogue_alias(run: CommandRun, sku: Any, profile: Any, barcode: str) -> None:
    """The barcode that piece will carry, unscoped by site.

    Unscoped on purpose: the warehouse and the store read the same code off the
    same piece, which is the whole point of a barcode, and a transfer between
    them would be nonsense if they did not.
    """
    from masters.goods_identity_models import GovernanceState, SkuAlias
    from masters.goods_identity_services import record_master_version, save_master

    if SkuAlias.objects.filter(
        tenant_id=run.tenant_id, alias_type=SkuAlias.AliasType.BARCODE, value=barcode
    ).exists():
        return
    alias = SkuAlias(
        tenant_id=run.tenant_id,
        sku_id=sku.pk,
        issuer_key=CATALOGUE_ISSUER,
        alias_type=SkuAlias.AliasType.BARCODE,
        value=barcode,
        site=None,
        effective_from=run.now,
        effective_to=None,
        config_version_id=profile.version_id,
        governance_state=GovernanceState.EFFECTIVE,
    )
    save_master(alias, conflict="That barcode is already in use for this SKU.")
    record_master_version(run, "alias", alias, effective_from=run.now)


def _catalogue_shelf(tenant: Any) -> dict[str, str]:
    """What of this catalogue already exists, as ``{barcode: sku_id}``."""
    from masters.goods_identity_models import SkuAlias

    wanted = [barcode for _code, sizes in CATALOGUE_STYLES for _size, barcode in sizes]
    return {
        barcode: str(sku_id)
        for barcode, sku_id in SkuAlias.objects.filter(
            tenant_id=tenant.pk, alias_type=SkuAlias.AliasType.BARCODE, value__in=wanted
        ).values_list("value", "sku_id")
    }


def seed_ops_catalogue(tenant: Any, brand: Any, approver_human_id: Any) -> dict[str, str]:
    """Two styles, four SKUs and a barcode each, for the ready pair (idempotent).

    Returns ``{barcode: sku_id}`` so a caller can say what it put on the shelf.

    Every row is written the way the Product Master screen writes one: the
    attributes validated against the identity profile, the identity key computed
    by the product's own function rather than made up here, the uniqueness the
    screen checks checked, and each row given its own append-only master
    version. What the seed does not do is pretend to be a person - these are
    created directly as effective masters, which is exactly what the screen's
    direct path does for the product master owner, and the command it runs says
    `seed.` so nobody reads them as somebody's decision.

    Idempotent by asking first: a second seed finds the four barcodes already
    issued and writes nothing.
    """
    from masters.goods_identity_services import profile_from_version

    _publish_tenant_config(
        tenant,
        approver_human_id,
        kind="vocabulary",
        payload={"dimension": "size", "values": CATALOGUE_SIZES},
        label="size-vocabulary",
    )
    profile_version = _publish_tenant_config(
        tenant,
        approver_human_id,
        kind="identity_profile",
        payload=CATALOGUE_IDENTITY_PROFILE,
        label="fashion-identity-profile",
    )
    if profile_version is None:
        raise Refusal(
            "STATE_CONFLICT", "The fixture identity profile must be in force before its SKUs."
        )
    profile = profile_from_version(profile_version)
    if profile is None:
        raise Refusal("STATE_CONFLICT", "The fixture identity profile does not read as one.")

    already = _catalogue_shelf(tenant)
    if len(already) == sum(len(sizes) for _code, sizes in CATALOGUE_STYLES):
        return already

    def handler(run: CommandRun) -> CommandResult:
        for style_code, sizes in CATALOGUE_STYLES:
            style = _catalogue_style(run, brand, style_code)
            for size, barcode in sizes:
                _catalogue_alias(run, _catalogue_sku(run, style, profile, size), profile, barcode)
        return CommandResult(resource_type="catalogue", resource_id=str(brand.pk))

    execute_command(
        service_principal(tenant.pk, "seed"),
        CommandSpec(
            action="seed.ops_catalogue",
            command_id=uuid.uuid5(tenant.deployment_key, f"ops-catalogue:{brand.pk}"),
            business_input={"brand_code": BRAND_CODE},
        ),
        handler,
    )
    return _catalogue_shelf(tenant)


def seed_goods_demo(tenant: Any) -> dict[str, Any]:
    """Seed the synthetic goods sites and personas (idempotent)."""
    with tenant_context(tenant.pk):
        with transaction.atomic():
            ensure_goods_roles()
            sites = seed_goods_sites(tenant)
        seed_goods_personas(tenant, sites)
        from accounts.models import User

        owner = User.objects.filter(email__iexact=f"syn.owner@{SYNTHETIC_DOMAIN}").first()
        if owner is not None and owner.human_id is not None:
            seed_working_calendar(tenant, owner.human_id)
        # OPS-02: the pair that is actually open for business, and the people who
        # work there. After the personas above, because the Owner who approves
        # the sites and registers the till is `syn.owner`.
        ops_sites = seed_ops_sites(tenant)
        seed_goods_personas(tenant, ops_sites, OPS_PERSONAS)
        # The Owner approves this pair's work, so the Owner is part of this
        # pair's flow (PRD §3.2) and needs the same shell the two site personas
        # now get - the approvals queue above all. Tenant-scoped: an Owner
        # belongs to no single site.
        if owner is not None:
            _wire_shell_access(owner, "C-OWN", None, ops_sites)
        seed_ops_till(tenant, ops_sites[OPS_STORE])
        seed_ops_selling(ops_sites[OPS_STORE])
        brand = seed_goods_brand(tenant)
        seed_brand_scoped_persona(tenant, brand)
        # OPS-11: something to actually put on the shelf. After the brand,
        # which its styles belong to, and after the sites, which its pieces
        # will stand in.
        if owner is not None and owner.human_id is not None:
            seed_ops_catalogue(tenant, brand, owner.human_id)
    return {**sites, **ops_sites}


def credentials_section() -> list[str]:
    """The synthetic goods logins, for ``app/test_credentials.md``."""
    lines = [
        "## Synthetic goods-v1 logins",
        "",
        "**These are synthetic.** They belong to a deliberately fake warehouse and",
        "store (`SYN-GW`, `SYN-GS`) inside the synthetic demo tenant, under synthetic",
        "legal entities and registrations. No real person, site or registration is",
        "described here, and no address at `@synthetic.demo` is a real mailbox.",
        "",
        "Both sites are **planned and goods-unapproved** — on the `goods_v1` stock",
        "contract like every site, but an empty goods-v1 operating scope. These logins",
        "can sign in and configure; they cannot receive, post or open stock.",
        "",
        "| Email | Password | Role | Scope | Note |",
        "|---|---|---|---|---|",
    ]
    for persona in GOODS_PERSONAS:
        roles = [(persona.role_code, persona.site_code), *persona.extra]
        role_text = ", ".join(code for code, _ in roles)
        scope_text = ", ".join(site or "tenant" for _, site in roles)
        lines.append(
            f"| {persona.email} | {SYNTHETIC_PASSWORD} | {role_text} | {scope_text} "
            f"| {persona.note} |"
        )
    lines += [
        f"| {BRAND_SCOPED_STAFF_CODE}@{SYNTHETIC_DOMAIN} | {SYNTHETIC_PASSWORD} | C-BUY | brand "
        f"`{BRAND_CODE}` | GSA-T08: brand scope only — reads no exception anywhere, proving one "
        "cannot leak across brands |",
        "",
        "`X-SVC` has no login on purpose: it is the noninteractive service principal",
        "for jobs and imports, and it never approves anything.",
        "",
        "GSA-T08's synthetic working calendar (Monday-Saturday, Asia/Kolkata, no",
        "holidays) is approved for this tenant only — a fixture for exception due-date",
        "tests, never a real-tenant default.",
        "",
    ]
    lines += _ops_credentials_lines()
    return lines


def _ops_credentials_lines() -> list[str]:
    """The OPS-02 pair and its logins, for ``app/test_credentials.md``."""
    lines = [
        "## Synthetic store and warehouse operations pair (OPS-02)",
        "",
        "**Also synthetic**, in the same synthetic demo tenant, under a synthetic",
        "legal entity and registration. Unlike `SYN-GW`/`SYN-GS` above, these two are",
        "**open**: `goods_v1` stock contract, active, goods-ready, and the store",
        "sell-ready. Each has the six protected system locations plus a `Floor`, a",
        "`Backstore` and one `Bin 1` inside that backstore.",
        "",
        "| Site | Code | Kind | Contract | State |",
        "|---|---|---|---|---|",
        f"| Synthetic Operations Warehouse | `{OPS_WAREHOUSE}` | warehouse | `goods_v1` "
        "| active, goods-ready |",
        f"| Synthetic Operations Store | `{OPS_STORE}` | store | `goods_v1` "
        "| active, goods-ready, sell-ready |",
        "",
        f"The store has one registered till on counter `{OPS_TILL_COUNTER}` "
        f"(series prefix `{OPS_STORE}-{OPS_TILL_COUNTER}`), registered by "
        "`syn.owner`. Its 24-hour authority window is empty until a device opens one: "
        "OPS-09's window is granted by `POST /api/sell/till/renew`, which only answers a "
        "live online session, so the counter opens its own window the first time it syncs. "
        "**No till token is seeded and none is needed** - the device identity "
        "`RegisteredTill.device_token` is minted at registration and is not a credential; "
        "the session is what authenticates every call the counter makes.",
        "",
        "| Email | Password | Role | Scope | Note |",
        "|---|---|---|---|---|",
    ]
    for persona in OPS_PERSONAS:
        lines.append(
            f"| {persona.email} | {SYNTHETIC_PASSWORD} | {persona.role_code} "
            f"| {persona.site_code} | {persona.note} |"
        )
    lines += [
        "",
        "The Owner for this pair is `syn.owner` above: one Owner approves across the",
        "business, and the site people never approve their own work.",
        "",
    ]
    return lines
