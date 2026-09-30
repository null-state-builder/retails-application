"""The goods-v1 action and field registry, and the Phase 1 §9.2 role templates.

An action is a named thing a person may do; a grant gives one role's actions over
one scope. The registry is code, not configuration: a grant naming an action this
module does not know is refused. Role templates are the default maximum a role
can be granted - the §9.2 matrix restated as data the seed and the access matrix
editor both read.

``cost_own_pt`` is the C-WHO field predicate: cost cells only on PTs this person
prepared, never stock or layer value.

On 22 September 2026 the store and warehouse operations PRD §3.2 widened
``C-OWN`` additively: in that flow the Owner carries the inventory controller's
approving responsibility, so the owner template now also holds the three PT
approvals, the opening variance approval, the receipt disposition and
counter-GRN approvals, transfer allocation and count review. ``M-STR`` and
``C-WHO`` gained ``transfer.allocate`` in the same pass, because either site may
draft or request a transfer for itself; approving one stays with ``C-OWN`` and
``C-INV``. Nothing was taken away from any template.
"""

from __future__ import annotations

from dataclasses import dataclass
from accounts.sections import CAPABILITY_ORDER, SECTIONS

ACTIONS: dict[str, str] = {
    "org.tenant.manage": "Edit tenant profile",
    "org.entity.manage": "Create or edit entities and registrations",
    "org.site.manage": "Create or edit sites, SBUs and locations",
    "org.site.lifecycle.run": "Run site readiness checklists",
    "org.site.lifecycle.approve": "Approve site readiness, closure and non-trading",
    "org.site.route": "See internal routing destinations",
    "org.location.manage": "Create, edit and retire internal locations at any site in scope",
    "org.location.store.manage": "Create, edit and retire internal locations at an assigned store",
    "org.location.warehouse_bin.manage": "Create, edit and retire bins at an assigned warehouse",
    "access.manage": "Manage users, roles and grants",
    "access.review": "Review and acknowledge privileged changes",
    "staff.manage": "Manage staff and assignments",
    "staff.retire": "Retire staff",
    "config.draft": "Draft configuration",
    "config.approve": "Approve configuration",
    "product.master.manage": "Create effective styles, SKUs and aliases",
    "product.master.propose": "Propose styles, SKUs and aliases while preparing a PT",
    "crosswalk.manage": "Manage source crosswalks",
    "crosswalk.propose": "Propose source crosswalks",
    "identity.resolve": "Resolve identity ambiguity",
    "vendor.manage": "Create and edit vendors, brands, seasons and subbrands",
    "master.retire": (
        "Retire entities, registrations, sites, vendors, brands, seasons, subbrands, "
        "styles, SKUs, aliases and crosswalks"
    ),
    "booking.manage": "Create, confirm, correct and close bookings",
    "arrival.no_booking.confirm": "Confirm an arrival has no booking",
    "receive.arrival": "Record arrivals, counts and GRNs",
    "receipt.counter_grn.approve": "Approve counter-GRNs",
    "receipt.disposition.decide": "Decide receipt dispositions",
    "pt.view": "View PTs",
    "pt.prepare": "Prepare and submit receipt PTs",
    "pt.prepare.opening": "Prepare opening PTs and manifests",
    "pt.approve.receipt": "Approve receipt PTs",
    "pt.approve.opening": "Approve opening PTs",
    "pt.approve.transfer": "Approve transfer PTs",
    "pt.reversal.request": "Request PT reversal or reissue",
    "pt.reversal.approve": "Approve PT reversal or reissue",
    "opening.manifest.approve": "Approve opening manifests",
    "opening.import.stage": "Upload and reconcile an opening source without authoring valuation",
    "opening.variance.approve": "Approve opening variances",
    "label.print": "Print and reprint labels",
    "stock.view": "View stock",
    "stock.accept": "Accept and put away goods",
    "transfer.allocate": "Allocate transfers",
    "transfer.move": "Dispatch, record arrival and receive transfers",
    "movement.draft": "Draft movements, holds and releases",
    "movement.approve": "Approve movements, releases and write-offs",
    "rtv.execute": "Dispatch and confirm RTVs",
    "count.run": "Run counts",
    "count.review": "Review and adjust counts",
    "exception.view": "View owned exceptions",
    "exception.manage": "Assign and note exceptions",
    "approvals.view": "View approvals",
    "approval.decide": "Decide approvals under a pinned workflow policy",
    "export.run": "Run exports",
    "recovery.run": "Run restore and recovery",
    "ops.health.view": "View operations health",
    "audit.view": "Read the audit trail",
    "payroll.view": "View private payroll records",
    "brand_terms.view": "View brand commercial terms",
    "billing.identity.view": "View customer identity for billing",
}

# Scoped specialist operations; authority lives in versioned role step actions.
ACTIONS.update({'store.feature.manage': 'Store feature manage', 'tax.settings.manage': 'Tax settings manage', 'document.series.manage': 'Document series manage', 'consent.wording.manage': 'Consent wording manage', 'count.schedule.manage': 'Count schedule manage', 'staff.salesperson.resolve': 'Staff salesperson resolve', 'debit_note.manage': 'Debit_note manage', 'brand_claim.manage': 'Brand_claim manage', 'payable.manage': 'Payable manage', 'sor.settlement.manage': 'Sor settlement manage', 'booking.budget.manage': 'Booking budget manage', 'checklist.template.manage': 'Checklist template manage', 'brand_report.layout.manage': 'Brand_report layout manage', 'till.pin.admin': 'Till pin admin', 'brand_terms.propose': 'Brand_terms propose', 'brand_terms.approve': 'Brand_terms approve'})

FIELDS: dict[str, str] = {
    "cost": "Cost",
    "margin": "Margin",
    "layer_value": "Layer value",
    "personal": "Personal data",
    "cost_own_pt": "Cost on own prepared PTs",
}

# Supported section operations use the same versioned workflow thresholds as
# specialised commands. These are actions, not session-wide section unions.
SECTION_ACTIONS = {
    (section, level): f"section.{section}.{level}"
    for section, _label in SECTIONS for level in CAPABILITY_ORDER if level != "none"
}
ACTIONS.update({
    SECTION_ACTIONS[(section, level)]: f"{label}: {level}"
    for section, label in SECTIONS for level in CAPABILITY_ORDER if level != "none"
})


@dataclass(frozen=True)
class RoleTemplate:
    code: str
    name: str
    actions: frozenset[str]
    fields: frozenset[str]
    scope_kinds: frozenset[str]


def _t(code: str, name: str, actions: set[str], fields: set[str], scopes: set[str]) -> RoleTemplate:
    unknown = actions - set(ACTIONS)
    if unknown:  # pragma: no cover - programmer error
        raise ValueError(f"{code} names unknown actions {sorted(unknown)}")
    return RoleTemplate(code, name, frozenset(actions), frozenset(fields), frozenset(scopes))


_VIEWING = {"stock.view", "pt.view", "exception.view", "approvals.view", "org.site.route"}

ROLE_TEMPLATES: dict[str, RoleTemplate] = {
    t.code: t
    for t in [
        _t(
            "X-PLT",
            "Platform administrator",
            {
                "org.tenant.manage",
                "org.entity.manage",
                "org.site.manage",
                "access.manage",
                "config.draft",
                "export.run",
                "recovery.run",
                "ops.health.view",
                "audit.view",
                *_VIEWING,
            },
            {"cost", "margin", "layer_value"},
            {"tenant"},
        ),
        _t(
            "C-OWN",
            "Company owner",
            {
                "org.tenant.manage",
                "org.entity.manage",
                "org.site.manage",
                "org.site.lifecycle.approve",
                "access.manage",
                "access.review",
                "staff.manage",
                "staff.retire",
                "config.draft",
                "config.approve",
                "pt.reversal.approve",
                "opening.manifest.approve",
                "movement.approve",
                # PRD §3.2 (22 September 2026): the Owner approves what the
                # warehouse prepares, as a distinct human from the preparer.
                "pt.approve.receipt",
                "pt.approve.opening",
                "pt.approve.transfer",
                "opening.variance.approve",
                "receipt.disposition.decide",
                "receipt.counter_grn.approve",
                "transfer.allocate",
                "count.review",
                "export.run",
                "recovery.run",
                "ops.health.view",
                "audit.view",
                "exception.manage",
                "master.retire",
                "org.location.manage",
                *_VIEWING,
            },
            {"cost", "margin", "layer_value", "personal"},
            {"tenant", "entity"},
        ),
        _t(
            "M-STR",
            "Store manager",
            {
                "staff.manage",
                "org.location.store.manage",
                "receive.arrival",
                "stock.accept",
                # A site drafts or requests a transfer for itself (PRD §3.2);
                # approving one is not here.
                "transfer.allocate",
                "transfer.move",
                "movement.draft",
                "rtv.execute",
                "count.run",
                "label.print",
                "exception.manage",
                *_VIEWING,
            },
            {"personal"},
            {"site", "sbu"},
        ),
        _t(
            "M-CSH",
            "Cashier with receiving grant",
            {
                "receive.arrival",
                "stock.accept",
                "transfer.move",
                "count.run",
                "stock.view",
                "org.site.route",
            },
            set(),
            {"site", "sbu"},
        ),
        _t(
            "C-WHO",
            "Warehouse operator",
            {
                "product.master.propose",
                # OPS-16 (Anand, 23 September 2026): a preparer proposes a mapping
                # rule from a PT grid cell. A proposal applies nowhere until the
                # product-master owner confirms it; confirming is not here.
                "crosswalk.propose",
                "org.location.warehouse_bin.manage",
                "receive.arrival",
                "pt.prepare",
                "stock.accept",
                # As for the store: the warehouse drafts or requests a transfer
                # for itself, and never approves one (PRD §3.2).
                "transfer.allocate",
                "transfer.move",
                "movement.draft",
                "rtv.execute",
                "count.run",
                "label.print",
                *_VIEWING,
            },
            {"cost_own_pt"},
            {"site", "sbu"},
        ),
        _t(
            "C-PMO",
            "Product master owner",
            {
                "config.draft",
                "product.master.manage",
                "crosswalk.manage",
                "identity.resolve",
                "pt.prepare",
                "vendor.manage",
                *_VIEWING,
            },
            {"cost", "margin"},
            {"tenant"},
        ),
        _t(
            "C-INV",
            "Inventory controller and PT approver",
            {
                "receipt.counter_grn.approve",
                "receipt.disposition.decide",
                "pt.prepare.opening",
                "pt.approve.receipt",
                "pt.approve.opening",
                "pt.approve.transfer",
                "pt.reversal.request",
                "pt.reversal.approve",
                "opening.variance.approve",
                "transfer.allocate",
                "movement.draft",
                "movement.approve",
                "rtv.execute",
                "count.review",
                "label.print",
                "exception.manage",
                *_VIEWING,
            },
            {"cost", "margin", "layer_value"},
            {"tenant", "entity", "site", "sbu"},
        ),
        _t(
            "C-BUY",
            "Buyer",
            {
                "booking.manage",
                "arrival.no_booking.confirm",
                "crosswalk.propose",
                "transfer.allocate",
                *_VIEWING,
            },
            {"cost", "margin"},
            {"tenant", "brand"},
        ),
        _t(
            "C-CAO",
            "Commercial agreement owner",
            {"pt.view", "org.site.route"},
            {"cost"},
            {"tenant", "brand"},
        ),
        _t(
            "C-STO",
            "Site transition owner",
            {"org.site.lifecycle.run", *_VIEWING},
            set(),
            {"tenant", "site"},
        ),
    ]
}

#: Command action names that are single-identity step-up changes, listed on the
#: privileged-change review screen (Phase 1 §9.3, design E084).
#:
#: These are **command** action names - what an ``AuditEvent.action`` holds -
#: not the grant actions in ``ACTIONS`` above. Discovery is by registered kind:
#: an action appears on that review screen because it is written here, never
#: because its name happens to begin with an approved prefix. The prefix
#: adapter change PRD §14.5 P6 allowed until these kinds existed is removed
#: (GSA-T18), so a new privileged command family is one entry here. Each succeeded
#: one also opens an owned review item in the shared exceptions centre
#: (``goods_admin_services.open_privileged_follow_up``, registered in ``apps.py``;
#: ticket 03D). The access kinds are mirrored for history links in the frontend's
#: ``lib/administrativeHistory.ts``, checked by ``tests/test_goods_access_history.py``.
PRIVILEGED_COMMAND_ACTIONS: dict[str, str] = {
    "masters.store_feature.switch": "A store's operating feature policy was changed",
    "access.brand_identity.bind": "Legacy brand ownership was reconciled",
    "staff.retire": "A staff member was retired",
    "staff.assign": "A staff placement was changed",
    "accounts.till_pin.admin_set": "A till PIN was set by an administrator",
    "accounts.till_pin.reset": "A till PIN was reset by an administrator",
    "access.assignment.replace": "A person's scoped roles were changed",
    "access.role.policy": "A role policy was changed",
    "access.workflow.policy": "Workflow access thresholds were changed",
    "access.user.create": "A login was created",
    "access.user.update": "A login was changed",
    "access.grant.change": "Role grants were changed",
    "access.role.create": "A role was created",
    "access.role.update": "A role was changed",
    "access.role.access": "A role's maximum actions or fields were changed",
    # Phase 8 command families, registered by GSA-T18 in place of the prefixes.
    "exports.request": "A durable export was requested",
    "operations.recovery": "Restore or recovery was run",
    # Two-identity till enrolment (Phase 1 §9.3) appears on the same review
    # screen. Selling is out of this stage's scope, so no command writes it
    # yet; the kind is registered so the fact is not carried as a prefix.
    "till.enrol": "A till was enrolled",
}

#: Actions that need a fresh password confirmation on the same session.
STEP_UP_ACTIONS = frozenset(
    {
        "org.tenant.manage",
        "org.entity.manage",
        "org.site.manage",
        "master.retire",
        "access.manage",
        "access.review",
        "config.approve",
        "org.site.lifecycle.approve",
        "export.run",
        "recovery.run",
    }
)


#: Two-person floors no grant, role setting or approval policy relaxes (Phase 1 §9.3, design §5.5).
DISTINCT_IDENTITY_ACTIONS = (
    "pt.approve.receipt",
    "pt.approve.opening",
    "pt.approve.transfer",
    "pt.reversal.approve",
    "receipt.counter_grn.approve",
    "movement.approve",
    "config.approve",
    "opening.variance.approve",
    "opening.manifest.approve",
)

#: Grants that may read tenant-wide masters: vendors, brands, seasons and subbrands
#: (design E021/E023, E026/E028, E031/E033, E036/E038 "authenticated read grant").
TENANT_MASTER_READ_ACTIONS = frozenset(
    {
        "vendor.manage",
        "master.retire",
        "product.master.manage",
        "product.master.propose",
        "crosswalk.manage",
        "crosswalk.propose",
        "identity.resolve",
        "booking.manage",
        "arrival.no_booking.confirm",
        "receive.arrival",
        "receipt.counter_grn.approve",
        "receipt.disposition.decide",
        "pt.view",
        "pt.prepare",
        "pt.prepare.opening",
        "stock.view",
        "stock.accept",
        "transfer.allocate",
    }
)


def validate_action_set(role_code: str, actions: list[str]) -> list[str]:
    template = ROLE_TEMPLATES.get(role_code)
    problems = [a for a in actions if a not in ACTIONS]
    if template is not None:
        problems += [a for a in actions if a in ACTIONS and a not in template.actions]
    return problems
