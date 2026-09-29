"""The nine-role access table, ratified as the seed default (issues #85, #130).

Source of truth: ``ERP_DASHBOARD_V1.xlsx`` → sheet "SIDEBAR RBAC" (24 Jul 2026)
for six of the nine personas, and **PRD #104** (ratified 26 Jul 2026) for the
other two rows plus the corrections recorded below. Each cell is the exact sheet
wording where the sheet had one; the leading rung is the normalised capability
(see ``sections`` for the ladder).

There is **one table**. ``ho_ops`` and ``data_steward`` used to sit in a separate
``DERIVED_ACCESS`` block that disclaimed itself - "NOT the RBAC matrix, retune
freely" - which meant a role's access could only be answered by knowing which of
two structures it came from. Anand ratified both rows on 26 July, so they are
persona rows like the other seven and the disclaimer is gone.

This module is only the **default** — ``seed_foundation`` writes it into each
``Role.section_access`` row, and the live API reads it back from the DB. So a
trained admin retunes access by editing a Role (data), never by shipping code
(Rule 12); this table just says where every fresh install starts.

Ratified corrections to the client's original Sheet-1 matrix, baked in here:
  · the store person gets receive-at-own-store for direct receipts (Sheet 1
    said NO on GRN; the booking-less direct-delivery decision wins). PT-making
    for those receipts was granted here too and withdrawn by #119: Anand ruled
    PT-making is warehouse work, so the warehouse rose one rung on this same
    section instead, and the store's cell was retitled to what it actually
    keeps - the bill upload;
  · Admin gets **no** Money access - Sheet-1 note (2), kept deliberately;
  · the store person **views** bookings (Sheet 1 said "No"). A store plans space
    and staff against goods headed its way, so the section opens read-only:
    ``view`` grants the screen, never the create (PRD #104, 26 Jul 2026). The
    cell says nothing about *which* bookings - narrowing them to the store's own
    is the record-scope axis, and #101's work.

One section has **no sheet row at all**: ``hrms`` (named ``staff`` until #118
renamed it in place). The sheet predates KDPS's hand-drawn store sidebar, which
puts attendance in the store person's daily screen (spec #84, user story 27),
so #87 adds the section with derived access — marked ``(derived)`` in the label
so nobody mistakes it for the sheet. It is data like every other cell: retune
it on the Role row, no release.

That same sheetless section is the one cell ``ROLE_OVERRIDES`` carries: the
sketch's "Member Details" — add/remove people, bank details, monthly target vs
achievement — is store work the sheet never priced, so the store role holds
``hrms: manage`` there. An override may only touch a sheetless section, so a
persona's ratified cells stay the sheet's word.

**22 Sep 2026 — the six initial roles** (``docs/features/rbac/rbac-v1-initial-roles.md``).
Anand ruled that Sheet 2's six personas are the first RBAC and that "Store
Person" is one role: the two codes that used to share its row, ``store_manager``
and ``store_staff``, were merged into ``store_person`` (migration 0020 moves the
users). Nothing was deleted - ``ho_ops``, ``promo``, ``data_steward`` and the
``hrms`` section stay exactly as ratified earlier; they are simply not part of
the six being defined yet.

Scope words in the cells ("Own store", "Assigned brands", "All (network)") are
*display context only* — the acting scope is the separate ``scope_type``
dimension (ADR-0003), not the capability. They are preserved verbatim as the
label so the payload can show "why" a role sees what it sees.
"""

from __future__ import annotations

from accounts.sections import (
    CAP_APPROVE,
    CAP_MANAGE,
    CAP_NONE,
    CAP_OPERATE,
    CAP_VIEW,
    SECTION_CODES,
    is_valid_capability,
    is_valid_section,
    meets,
)

# Persona code → { section_code: (capability, exact sheet label) }.
# Every persona states all thirteen cells, so "no access" is written down rather
# than inferred from an omission. A section absent anyway is CAP_NONE
# (fail-closed), and ``_validate`` refuses to let a row ship incomplete.
MATRIX: dict[str, dict[str, tuple[str, str]]] = {
    "owner": {
        "home": (CAP_VIEW, "All (network)"),
        "sell": (CAP_VIEW, "View"),
        "booking": (CAP_APPROVE, "Approve"),
        "receive_goods": (CAP_VIEW, "View all"),
        "transfer": (CAP_APPROVE, "Override"),
        "stock_count": (CAP_APPROVE, "View all + approve big variances"),
        "return_to_brand": (CAP_APPROVE, "Approve"),
        "stock": (CAP_MANAGE, "Full (all locations)"),
        "money": (CAP_MANAGE, "Full"),
        "offers_price": (CAP_APPROVE, "Approve / Override"),
        "hrms": (CAP_MANAGE, "Full (derived)"),
        "reports": (CAP_VIEW, "All"),
        "setup": (CAP_MANAGE, "Full"),
    },
    "store_person": {
        "home": (CAP_VIEW, "Own store"),
        "sell": (CAP_OPERATE, "Create (bill, return, customer)"),
        # Ratified correction to the sheet's "No" (PRD #104, 26 Jul 2026): a
        # store reads the bookings headed to it so it can plan space and staff.
        # Read-only - placing one stays HO's and the brand manager's job.
        "booking": (CAP_VIEW, "View bookings for own store"),
        # PT making moved to the warehouse (#119, Anand's ruling of 25 Jul
        # 2026): a store may still receive at its own store and upload the
        # brand's bill, but it may not author or upload a PT.
        "receive_goods": (CAP_OPERATE, "Receive + bill upload (own store)"),
        "transfer": (CAP_OPERATE, "Request / Send / Receive"),
        "stock_count": (CAP_OPERATE, "Count own store"),
        "return_to_brand": (CAP_OPERATE, "Mark damage only"),
        "stock": (CAP_VIEW, "Own store"),
        "money": (CAP_OPERATE, "Expenses only (create)"),
        "offers_price": (CAP_VIEW, "View"),
        # The hand-drawn store sidebar puts biometric check-in in the daily
        # screen — so the store person *operates* HRMS (own attendance), even
        # though employee records and payroll stay a back-office promise.
        "hrms": (CAP_OPERATE, "Own attendance (derived)"),
        "reports": (CAP_VIEW, "Own store only"),
        "setup": (CAP_NONE, "No"),
    },
    "warehouse": {
        "home": (CAP_VIEW, "Warehouse"),
        "sell": (CAP_NONE, "No"),
        "booking": (CAP_OPERATE, "Draft"),
        # Raised one rung above the store (#119): PT-making now needs
        # `approve` on this section, and warehouse is the only operating role
        # that reaches it. Checked against every `receive_goods` gate in the
        # codebase - this grants the warehouse nothing beyond PT-making.
        "receive_goods": (CAP_APPROVE, "Create (GRN, PT)"),
        "transfer": (CAP_OPERATE, "Execute (distribute, dispatch)"),
        "stock_count": (CAP_OPERATE, "Count warehouse"),
        "return_to_brand": (CAP_OPERATE, "Create & execute"),
        "stock": (CAP_MANAGE, "Full"),
        "money": (CAP_OPERATE, "Expenses only (create)"),
        "offers_price": (CAP_VIEW, "View"),
        "hrms": (CAP_OPERATE, "Own attendance (derived)"),
        "reports": (CAP_VIEW, "All"),
        "setup": (CAP_OPERATE, "Products only"),
    },
    "brand_manager": {
        "home": (CAP_VIEW, "Assigned brands"),
        "sell": (CAP_VIEW, "View"),
        "booking": (CAP_OPERATE, "Create"),
        "receive_goods": (CAP_VIEW, "View"),
        "transfer": (CAP_APPROVE, "Approve"),
        "stock_count": (CAP_VIEW, "View assigned brands"),
        "return_to_brand": (CAP_VIEW, "View own brands"),
        "stock": (CAP_VIEW, "Assigned brands"),
        "money": (CAP_NONE, "No"),
        "offers_price": (CAP_APPROVE, "Recommend + approve within limit"),
        # A brand manager's scope is brands, not people — no staff surface.
        "hrms": (CAP_NONE, "No (derived)"),
        "reports": (CAP_VIEW, "Own brands only"),
        "setup": (CAP_OPERATE, "Edit assigned products"),
    },
    "accounts": {
        "home": (CAP_VIEW, "Finance view"),
        "sell": (CAP_VIEW, "View"),
        "booking": (CAP_VIEW, "View"),
        "receive_goods": (CAP_VIEW, "View"),
        "transfer": (CAP_VIEW, "View"),
        "stock_count": (CAP_VIEW, "View"),
        "return_to_brand": (CAP_VIEW, "View (credit notes)"),
        "stock": (CAP_VIEW, "View"),
        "money": (CAP_MANAGE, "Full"),
        "offers_price": (CAP_VIEW, "View"),
        # Payroll inputs and sales incentives are an Accounts read, not an edit.
        "hrms": (CAP_VIEW, "View (payroll inputs) (derived)"),
        "reports": (CAP_VIEW, "All"),
        "setup": (CAP_VIEW, "View"),
    },
    "admin": {
        "home": (CAP_VIEW, "All"),
        "sell": (CAP_MANAGE, "All"),
        "booking": (CAP_MANAGE, "Configure"),
        "receive_goods": (CAP_MANAGE, "All"),
        "transfer": (CAP_VIEW, "Monitor"),
        "stock_count": (CAP_MANAGE, "All"),
        "return_to_brand": (CAP_MANAGE, "All"),
        "stock": (CAP_MANAGE, "Full"),
        "money": (CAP_NONE, "No"),
        "offers_price": (CAP_MANAGE, "Configure"),
        "hrms": (CAP_MANAGE, "Full (derived)"),
        "reports": (CAP_VIEW, "All"),
        "setup": (CAP_MANAGE, "Full (incl. Users & Roles)"),
    },
    # HO Operations / Buyer - the network operator. Ratified 26 Jul 2026 (#130).
    # Books goods and chases arrivals but never inwards one: the buyer must not
    # confirm receipt of their own order, which is what keeps the three-way match
    # honest (PRD #104). No money, no setup ownership.
    "ho_ops": {
        "home": (CAP_VIEW, "All (network)"),
        "sell": (CAP_NONE, "No"),
        "booking": (CAP_OPERATE, "Create"),
        "receive_goods": (CAP_VIEW, "View all"),
        "transfer": (CAP_APPROVE, "Approve"),
        "stock_count": (CAP_APPROVE, "Approve variances"),
        "return_to_brand": (CAP_VIEW, "View"),
        "stock": (CAP_VIEW, "All locations"),
        "money": (CAP_NONE, "No"),
        "offers_price": (CAP_OPERATE, "Plan"),
        "hrms": (CAP_VIEW, "All (network)"),
        "reports": (CAP_VIEW, "All"),
        "setup": (CAP_NONE, "No"),
    },
    # HO Data Steward - the single owner of master data. Ratified 26 Jul 2026
    # (#130). Setup stays at `operate` (masters only): Users & Roles admin needs
    # `setup: manage`, which only Owner and Admin hold, and a steward must not
    # gain it (issue #85 review). Reads stock; posts nothing.
    # Promo / Marketing - the tenth row, ratified 3 Aug 2026 (D11 R2). The person
    # who writes the offers and publishes them to the shops: the digital
    # replacement for the placard that used to go up on the shutter. Authoring is
    # `operate`, deliberately not `approve` - an offer is a standing instruction
    # to give money away at every till, so somebody answerable for the brand or
    # the chain countersigns it (D11 §7). Reads stock because you cannot plan a
    # markdown without seeing what is ageing; touches no money, no stock document
    # and no booking.
    "promo": {
        "home": (CAP_VIEW, "All (network)"),
        "sell": (CAP_NONE, "No"),
        "booking": (CAP_NONE, "No"),
        "receive_goods": (CAP_NONE, "No"),
        "transfer": (CAP_NONE, "No"),
        "stock_count": (CAP_NONE, "No"),
        "return_to_brand": (CAP_NONE, "No"),
        "stock": (CAP_VIEW, "All locations"),
        "money": (CAP_NONE, "No"),
        "offers_price": (CAP_OPERATE, "Author & publish"),
        "hrms": (CAP_NONE, "No"),
        "reports": (CAP_VIEW, "All"),
        "setup": (CAP_NONE, "No"),
    },
    "data_steward": {
        "home": (CAP_VIEW, "All (network)"),
        "sell": (CAP_NONE, "No"),
        "booking": (CAP_NONE, "No"),
        "receive_goods": (CAP_NONE, "No"),
        "transfer": (CAP_NONE, "No"),
        "stock_count": (CAP_NONE, "No"),
        "return_to_brand": (CAP_NONE, "No"),
        "stock": (CAP_VIEW, "All locations"),
        "money": (CAP_NONE, "No"),
        "offers_price": (CAP_NONE, "No"),
        "hrms": (CAP_NONE, "No"),
        "reports": (CAP_VIEW, "All"),
        "setup": (CAP_OPERATE, "Masters"),
    },
}

# The nine seeded role *codes* map onto the nine persona rows; "Admin" is the
# it_admin role. Every canonical role must resolve to a persona, so the contract
# test can assert every one of them against the table.
ROLE_PERSONA = {
    "owner": "owner",
    "store_person": "store_person",
    "warehouse": "warehouse",
    "brand_manager": "brand_manager",
    "accounts": "accounts",
    "it_admin": "admin",
    "ho_ops": "ho_ops",
    "data_steward": "data_steward",
    "promo": "promo",
}

# Sections the SIDEBAR RBAC sheet never covered. Only these may be overridden
# per role code — everything else is the sheet's ratified word.
SHEETLESS_SECTIONS = frozenset({"hrms"})

# Per-role-code cells layered *on top of* the persona row, for a sheetless
# section a role code needs at a different rung than its persona row says.
#
# The hand-drawn Store Ops screen puts "Member Details" — add/remove members,
# contact **and bank** details, monthly target vs achievement, growth/de-growth
# — in the store's own daily list (settled 25 Jul 2026: members are staff
# scorecards, not loyalty customers; the POS still owns the customer). That was
# the store *manager's* cell while the store had two codes; since the merge of
# 22 Sep 2026 the one ``store_person`` code keeps the higher rung, so no store
# loses a screen it had. Scope stays "own store" through the separate
# ``scope_type`` dimension (ADR-0003), never the capability.
ROLE_OVERRIDES: dict[str, dict[str, tuple[str, str]]] = {
    "store_person": {
        "hrms": (CAP_MANAGE, "Own store members + attendance (derived)"),
    },
}


def section_access_for(role_code: str) -> dict[str, dict[str, str]]:
    """Full 13-section access map for a role, as stored in ``section_access``.

    Every section is present (missing → explicit ``none``), so the row is
    self-describing and fail-closed. Shape: ``{section: {capability, label}}``.

    A role code the table does not know reaches nothing at all - there is no
    second table to fall through to.
    """
    persona = ROLE_PERSONA.get(role_code)
    base = MATRIX.get(persona, {}) if persona else {}
    source = {**base, **ROLE_OVERRIDES.get(role_code, {})}
    out: dict[str, dict[str, str]] = {}
    for section in SECTION_CODES:
        capability, label = source.get(section, (CAP_NONE, "No"))
        out[section] = {"capability": capability, "label": label}
    return out


#: Every role code this table can answer for — the nine ``seed_foundation``
#: writes. Anything outside it resolves to no access at all (fail-closed).
KNOWN_ROLE_CODES: tuple[str, ...] = tuple(sorted(ROLE_PERSONA))


def roles_with_capability(section: str, minimum: str) -> tuple[str, ...]:
    """The role codes the matrix puts at ``minimum`` or above on ``section``.

    The seed-time answer to "who may approve a write-off?", so an approver list
    is *derived from* the ratified matrix rather than hand-kept beside it. Only a
    default: the value is written onto an ``ApprovalPolicy`` row on first use and
    the business retunes it there (Rule 12). What this removes is the drift — a
    default can no longer name a role the sheet gives only ``view``.
    """
    return tuple(
        code
        for code in KNOWN_ROLE_CODES
        if meets(section_access_for(code)[section]["capability"], minimum)
    )


def _check_cells_are_transcribed(
    source: str, cells_by_role: dict[str, dict[str, tuple[str, str]]]
) -> None:
    """Every section and capability named in a source must be one the system has."""
    for role, cells in cells_by_role.items():
        for section, (capability, _) in cells.items():
            assert is_valid_section(section), f"{source}/{role}: bad section {section!r}"
            assert is_valid_capability(capability), (
                f"{source}/{role}: bad capability {capability!r}"
            )


def _check_matrix_rows_are_complete() -> None:
    """A ratified row must state all 13 sections — it says where it says "no"."""
    for persona, cells in MATRIX.items():
        missing = set(SECTION_CODES) - set(cells)
        assert not missing, f"{persona}: matrix missing sections {sorted(missing)}"


def _check_every_role_has_a_row() -> None:
    """No role may be seeded into the blank that used to be the derived block's job."""
    for role, persona in ROLE_PERSONA.items():
        assert persona in MATRIX, f"role/{role}: no ratified row for persona {persona!r}"


def _check_overrides_stay_off_the_sheet() -> None:
    """An override must never restate a ratified cell — only the sections the
    sheet left blank are ours to vary per role."""
    for role, cells in ROLE_OVERRIDES.items():
        off_sheet = set(cells) - SHEETLESS_SECTIONS
        assert not off_sheet, (
            f"override/{role}: may not override sheet sections {sorted(off_sheet)}"
        )


def _validate() -> None:
    """Guard the transcription: catch a typo'd section/capability at import.

    Both seed sources are checked so a slip in a ``ROLE_OVERRIDES`` cell fails
    loudly here rather than silently fail-closing that role's access. Each rule
    below is one named check, so the assertion that fires names the rule it broke.
    """
    _check_cells_are_transcribed("matrix", MATRIX)
    _check_cells_are_transcribed("override", ROLE_OVERRIDES)
    _check_matrix_rows_are_complete()
    _check_every_role_has_a_row()
    _check_overrides_stay_off_the_sheet()


_validate()
