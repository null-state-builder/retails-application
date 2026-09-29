"""The register of hand-kept role lists — every gate that is *not* the ladder (#94).

One gate: an API permission is a section plus a minimum capability, read from the
role's stored access (``accounts.permissions.require_section``). A hand-kept list
of role codes survives only where the ladder provably cannot express the rule —
and then the reason is written down here rather than left as a set literal in a
view module for the next reader to reverse-engineer.

Declaring one is the whole mechanism::

    PATNA_ROLES = declare_role_list(
        "ptmapper.post_pt",
        ("accounts", "owner", "it_admin"),
        reason="...why the ladder cannot say this...",
    )

The contract test reads ``REGISTERED_ROLE_LISTS`` and asserts two things: every
entry carries a reason, and no role-list constant anywhere in the backend was
written *without* coming through here. So "we kept a role list" is a recorded
decision, never an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleListException:
    """One gate that stayed a role list, and why the ladder could not take it."""

    #: ``app.gate`` — the module and the thing it guards, e.g. ``masters.writes``.
    name: str
    roles: frozenset[str]
    reason: str


#: Declaration order, which is also the order the contract test reports them in.
REGISTERED_ROLE_LISTS: dict[str, RoleListException] = {}


def declare_role_list(name: str, roles: tuple[str, ...], *, reason: str) -> frozenset[str]:
    """Register a hand-kept role list and hand back the set to gate with.

    The reason is mandatory and non-empty by construction: a list nobody could
    justify in a sentence is one that belongs on the ladder instead.
    """
    if not reason.strip():  # pragma: no cover - programmer error
        raise ValueError(f"{name}: a hand-kept role list needs a reason")
    if name in REGISTERED_ROLE_LISTS:  # pragma: no cover - programmer error
        raise ValueError(f"{name}: already declared")
    frozen = frozenset(roles)
    REGISTERED_ROLE_LISTS[name] = RoleListException(name=name, roles=frozen, reason=reason)
    return frozen


# Floor rule #4 is deliberately the one role list Setup cannot edit.  It stays
# in this registry so the existing contract requires the written reason.
ACCESS_ADMINISTRATORS = declare_role_list(
    "accounts.access_administrators_floor",
    ("owner", "it_admin"),
    reason=(
        "Changing users, roles or permission policy is itself the power to grant "
        "power. The ratified floor reserves proposals and second-person decisions "
        "to Owner or IT Admin; storing this list in editable policy would let that "
        "same policy configure the floor away."
    ),
)

#: Not a gate at all, and registered here for exactly that reason: the guard
#: catches every ``*_ROLES`` constant in the backend, and the honest answer for
#: this one is a written note rather than a rename that would hide it.
NON_STAFF_ROLES = declare_role_list(
    "accounts.non_staff_roles",
    ("X-PLT", "X-SVC"),
    reason=(
        "These two codes say who is *not* a member of the tenant's workforce - a "
        "platform administrator and a service principal - so that seeding gives "
        "them no Staff row and the People screen never lists them (GSA-T03). It "
        "decides no API access, so there is no section capability to hang it on: "
        "the ladder answers what a role may reach, never whether its holder is "
        "one of the chain's people. The pair is a property of the deployment's "
        "own roles, which is why it is hand-kept."
    ),
)

HEAD_OFFICE_VALUE_ACTORS = declare_role_list(
    "accounts.head_office_value_actors_floor",
    ("accounts", "owner"),
    reason=(
        "PT inwarding and V-flip create or change brand liability. The ratified "
        "segregation-of-duties floor reserves those postings to Accounts or Owner; "
        "an editable actor policy may narrow this pair but cannot add another role."
    ),
)

#: Store operations feature switches (ST-OPS-6): the PRD says "only Admin" may
#: switch a feature per store. The ladder cannot say it: ``setup: manage`` is held
#: by Owner as well as Admin, and baseline B6 forbids widening a check, so the one
#: Admin code is named here, narrowing ``setup: manage`` rather than replacing it.
STORE_FEATURE_EDITOR_ROLES = declare_role_list(
    "masters.store_feature_editors",
    ("it_admin",),
    reason=(
        "The store operations PRD (ST-OPS-6, section 4) gives switching a feature "
        "per store to Admin alone. The only rung that reaches it, setup: manage, is "
        "also Owner's, so the section ladder cannot separate the two; baseline B6 "
        "says narrow rather than widen. Letting Owner switch too is Anand's call and "
        "a one-word change here."
    ),
)

#: Store operations tax settings (§6, ticket 03): "Accounts can view. Only Admin
#: can change them." Same shape as the switch list above, for the same reason:
#: ``setup: manage`` is Owner's too, and baseline B6 says narrow, never widen.
TAX_SETTING_EDITOR_ROLES = declare_role_list(
    "masters.tax_setting_editors",
    ("it_admin",),
    reason=(
        "The store operations PRD ticket 03 gives changing the versioned tax "
        "settings to Admin alone. The only rung that reaches it, setup: manage, is "
        "also Owner's, so the section ladder cannot separate the two; baseline B6 "
        "says narrow rather than widen. Letting Owner save a version too is "
        "Anand's call and a one-word change here."
    ),
)

DOCUMENT_SERIES_EDITOR_ROLES = declare_role_list(
    "masters.document_series_editors",
    ("it_admin",),
    reason=(
        "The store operations PRD ticket 04 gives setting site prefixes and the "
        "date the new number format starts to Admin alone. The only rung that "
        "reaches it, setup: manage, is also Owner's, so the section ladder cannot "
        "separate the two; baseline B6 says narrow rather than widen. Letting Owner "
        "change them too is Anand's call and a one-word change here."
    ),
)

TILL_PIN_RESETTERS = declare_role_list(
    "accounts.till_pin_resetters",
    ("it_admin",),
    reason=(
        "The store operations PRD ticket 06 gives setting or resetting a manager's "
        "counter PIN to Admin (baseline B76). The only action that reaches a login, "
        "access.manage, is also Owner's, so the grant ladder cannot separate the two; "
        "baseline B6 says narrow rather than widen. Letting Owner set or reset a PIN "
        "too is Anand's call and a one-word change here."
    ),
)

#: Store operations brand terms (ST-BRD-1, section 4; ticket 23): "Brand Manager,
#: with Owner approval". Two lists, because the ladder reaches neither rule alone.
BRAND_TERM_EDITOR_ROLES = declare_role_list(
    "masters.brand_term_editors",
    ("brand_manager",),
    reason=(
        "The store operations PRD (section 4, ticket 23) gives proposing a brand's "
        "commercial terms to the Brand Manager. The rung it holds, setup: operate, "
        "is also the warehouse's and the data steward's, so the section ladder "
        "cannot separate them; baseline B6 says narrow rather than widen."
    ),
)

BRAND_TERM_READER_ROLES = declare_role_list(
    "masters.brand_term_readers",
    ("owner", "brand_manager", "accounts"),
    reason=(
        "Brand terms carry each brand's margin (store operations PRD section 4, "
        "section 15, ticket 23). They are the Brand Manager's and the Owner's work, "
        "and Accounts settles on them. setup: view is also held by the warehouse, "
        "the data steward and Admin for other Setup work, so the ladder cannot keep "
        "margins from them; baseline B6 says narrow rather than widen."
    ),
)

BRAND_TERM_APPROVER_ROLES = declare_role_list(
    "masters.brand_term_approvers",
    ("owner",),
    reason=(
        "The store operations PRD (section 4, ticket 23) needs the Owner to approve "
        "a change of brand terms (overall PRD R-BUY-006). The only rung that reaches "
        "it, setup: manage, is also Admin's, so the section ladder cannot separate "
        "the two; baseline B6 says narrow rather than widen."
    ),
)

#: Store operations scheduled counts (ST-INV-3; ticket 35): "The Owner sets a
#: schedule per store".
COUNT_SCHEDULE_EDITOR_ROLES = declare_role_list(
    "outbound.count_schedule_editors",
    ("owner",),
    reason=(
        "The store operations PRD (ST-INV-3, ticket 35) has the Owner set each "
        "store's count schedule. The rung the Owner holds on Stock Count, approve, "
        "is also Operations' and below Admin's manage, so the section ladder cannot "
        "separate them; baseline B6 says narrow rather than widen. Adding Admin "
        "(section 4 lists schedules under Admin) is a one-word change here."
    ),
)

SALESPERSON_MATCH_RESOLVERS = declare_role_list(
    "sell.salesperson_match_resolvers",
    ("it_admin",),
    reason=(
        "The store operations PRD (section 29, ticket 07) gives resolving an old "
        "salesperson row that the matching rule could not place to Admin. The "
        "rungs that reach staff records, hrms: manage and setup: manage, are also "
        "Owner's and the store's, so the section ladder cannot separate them; "
        "baseline B6 says narrow rather than widen. Adding Owner is Anand's call "
        "and a one-word change here."
    ),
)

#: Store operations customer consent (ticket 15): "Admin can change the wording,
#: which creates a new version." Same shape as the lists above, for the same
#: reason: ``setup: manage`` is Owner's too, and baseline B6 says narrow.
CONSENT_WORDING_EDITOR_ROLES = declare_role_list(
    "masters.consent_wording_editors",
    ("it_admin",),
    reason=(
        "The store operations PRD ticket 15 gives changing the customer consent "
        "wording to Admin. The only rung that reaches it, setup: manage, is also "
        "Owner's, so the section ladder cannot separate the two; baseline B6 says "
        "narrow rather than widen. Letting Owner save a wording too is Anand's call "
        "and a one-word change here."
    ),
)

#: Store operations debit notes for shortages (ST-REC-3, section 4; ticket 38):
#: "Accounts reviews and issues it"; issuing needs the Owner's approval, by a
#: different person. Both lists narrow ``money: manage``, which Owner and Accounts
#: hold today, so neither widens a check (baseline B6).
DEBIT_NOTE_REVIEWER_ROLES = declare_role_list(
    "inbound.debit_note_reviewers",
    ("accounts",),
    reason=(
        "The store operations PRD (section 4, ST-REC-3, ticket 38) gives reviewing and "
        "issuing a debit note to Accounts. The only rung that reaches money work, money: "
        "manage, is also the Owner's, who must approve the note as a different person, so "
        "the ladder cannot separate the two; baseline B6 says narrow rather than widen."
    ),
)

#: Store operations brand discount claims (ST-BRD-3, section 4; ticket 26): Accounts
#: raises the month's claims and records the brand accepting and settling them.
#: Narrows ``money: manage``, which Owner and Accounts hold today (baseline B6).
BRAND_CLAIM_EDITOR_ROLES = declare_role_list(
    "sell.brand_claim_editors",
    ("accounts",),
    reason=(
        "The store operations PRD (section 4, ST-BRD-3, ticket 26) has Accounts settle "
        "brand claims against the brand's credit notes. The only rung that reaches money "
        "work, money: manage, is also the Owner's, so the ladder cannot separate the two; "
        "baseline B6 says narrow rather than widen. Letting the Owner act too is a "
        "one-word change here."
    ),
)

#: Store operations brand payables (ST-MNY-4, section 4; ticket 28): Accounts
#: records the vendor invoices of outright brands and the payments made against
#: them. Narrows ``money: manage``, which Owner and Accounts hold (baseline B6).
PAYABLE_EDITOR_ROLES = declare_role_list(
    "finledger.payable_editors",
    ("accounts",),
    reason=(
        "The store operations PRD (section 4, ST-MNY-4, ticket 28) gives payables to "
        "Accounts. The only rung that reaches money work, money: manage, is also the "
        "Owner's, so the ladder cannot separate the two; baseline B6 says narrow rather "
        "than widen. Letting the Owner record too is a one-word change here."
    ),
)

DEBIT_NOTE_APPROVER_ROLES = declare_role_list(
    "inbound.debit_note_approvers",
    ("owner",),
    reason=(
        "The store operations PRD (section 4, ticket 38) needs the Owner to approve issuing "
        "a debit note, as a different person from Accounts who asked. The approvals inbox "
        "routes by role code (approver_roles on the approval row), and money: manage is "
        "also Accounts', so the ladder cannot name the approver; baseline B6 says narrow."
    ),
)

#: Store operations petty cash (ST-MNY-3, section 4; ticket 42): "a spend over
#: Rs 2,000 needs Owner approval", by a different person, through the approvals
#: inbox, which routes by role code. ``money: manage`` is Owner and Accounts alike.
PETTY_CASH_APPROVER_ROLES = declare_role_list(
    "sell.petty_cash_approvers",
    ("owner",),
    reason=(
        "The store operations PRD (section 4, ST-MNY-3, ticket 42) needs the Owner to approve "
        "a petty cash spend over the limit, as a different person from the one who recorded "
        "it. The approvals inbox routes by role code (approver_roles on the approval row), and "
        "money: manage is also Accounts', so the ladder cannot name the approver; baseline B6 "
        "says narrow."
    ),
)

#: Store operations SOR ageing (ST-BRD-5, section 15; ticket 24): Accounts records
#: a delivery's dispatch date after receiving and the brand's invoice that settles
#: its SOR pieces. Narrows ``money: manage``, which Owner and Accounts hold today.
SOR_AGEING_EDITOR_ROLES = declare_role_list(
    "inbound.sor_ageing_editors",
    ("accounts",),
    reason=(
        "The store operations PRD (section 4, section 15, ticket 24) has Accounts settle "
        "with brands; recording a brand's invoice, or a dispatch date missed at receiving, "
        "is that work. The only rung that reaches it, money: manage, is also the Owner's, "
        "so the ladder cannot separate the two; baseline B6 says narrow rather than widen. "
        "Letting the Owner act too is a one-word change here."
    ),
)

#: Store operations open-to-buy (ST-BUY-1, section 4; ticket 39): "a booking that
#: goes over it needs Owner approval", by a different person from the buyer who
#: asked, through the approvals inbox, which routes by role code. Setting the budget
#: a booking is judged against is the same authority: a buyer who set their own
#: budget would approve their own bookings.
OPEN_TO_BUY_APPROVER_ROLES = declare_role_list(
    "vendors.open_to_buy_approvers",
    ("owner",),
    reason=(
        "The store operations PRD (section 4, ST-BUY-1, ticket 39) needs the Owner to approve "
        "a booking over open-to-buy, as a different person from the buyer who asked. The "
        "approvals inbox routes by role code (approver_roles on the approval row), and "
        "booking: approve is also Admin's (manage is above it), so the ladder cannot name the "
        "approver; baseline B6 says narrow."
    ),
)

OPEN_TO_BUY_BUDGET_EDITOR_ROLES = declare_role_list(
    "vendors.open_to_buy_budget_editors",
    ("owner",),
    reason=(
        "The store operations PRD (ST-BUY-1, ticket 39) has the Owner decide bookings over "
        "open-to-buy, so the budget they are judged against is the Owner's to set. The goods "
        "cost field that opens the screen is also the buyer's, Accounts' and Admin's, so no "
        "existing check separates them; baseline B6 says narrow. Letting Accounts set budgets "
        "too is a one-word change here."
    ),
)

#: Store operations task checklists (ST-OPS-4, ticket 49): the PRD's role table
#: (section 4) gives setting checklists to Admin. Same shape as the switch list:
#: ``setup: manage`` is Owner's too, and baseline B6 says narrow, never widen.
CHECKLIST_TEMPLATE_EDITOR_ROLES = declare_role_list(
    "storefront.checklist_template_editors",
    ("it_admin",),
    reason=(
        "The store operations PRD (section 4, ST-OPS-4) gives setting checklist "
        "templates to Admin. The only rung that reaches it, setup: manage, is also "
        "Owner's, so the section ladder cannot separate the two; baseline B6 says "
        "narrow rather than widen. Letting Owner set templates too is Anand's call "
        "and a one-word change here."
    ),
)

#: Store operations brand reports (ST-BRD-2, section 15; ticket 29): Accounts saves
#: each brand's report layout. Reading and making the reports is the existing
#: ``reports: view``; no rung means "may change how a brand's report is laid out".
BRAND_LAYOUT_EDITOR_ROLES = declare_role_list(
    "reporting.brand_layout_editors",
    ("accounts",),
    reason=(
        "The store operations PRD (section 15, ST-BRD-2, ticket 29) puts brand reports "
        "under brand settlement, which is Accounts' work (baseline B314). Every role holds "
        "reports: view and none holds reports: manage, so the ladder cannot name who may "
        "change a brand's layout; baseline B6 says narrow rather than widen. Letting "
        "another role save layouts too is a one-word change here."
    ),
)
