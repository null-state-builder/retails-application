"""The money floor under the editable access matrix (#173, PRD #104).

`Role.section_access` is data an administrator retunes without a release (Rule
12), and #173 gives them a screen to do it on. That screen is the one place a
careless edit could open a hole in the money path, so four rules were ratified
on 26 July 2026 as **locked in code and unswitchable by anyone, including a
superuser editing Setup**:

  1. Nobody approves their own document.
  2. A store never posts value to the books.
  3. Money owed to a brand always requires a named head-office human.
  4. Users and roles may be changed only by Owner or IT Admin, and never by one
     person alone.

Three of the four can be crossed by a *cell* of the matrix, and those are the
``FLOORS`` below: each names the rung a role may not reach on a section, who is
allowed to reach it, and the sentence the screen shows over the greyed cell.

Rule 1 has no cell - self-approval is refused by the approvals engine on the
document, and no combination of section grants can express "their own". Rule
4's second half, "never by one person alone", has no cell either: it is why a
matrix edit is a *proposal* a second access administrator applies
(``accounts.access_changes``) rather than a save. Both are carried in ``RULES``
so the screen can state all four, and nobody has to wonder whether the missing
ones were forgotten.

**These floors are screen honesty as much as security.** The GL engine already
refuses a store-scoped actor underneath every API
(``core.posting._refuse_actor_outside_floor``), so an edit crossing rule 2
would only produce a role holding a button the server rejects. The one place a
cell floor is the *only* guard is rule 3 at ``money: manage``: that rung opens
the finledger books, and the engine's own brand-liability check asks merely for
a named non-store person - it would let a Warehouse seat granted ``money:
manage`` post what a brand is owed. So the floor is enforced here, on the way
in, exactly as ``ActorPolicySerializer.FLOOR_BOUND_ACTIONS`` narrows who may
inward a PT.

The two role sets rules 3 and 4 turn on are **the engine's own** -
``HEAD_OFFICE_VALUE_ACTORS`` and ``ACCESS_ADMINISTRATORS``, reused rather than
retyped, so the matrix floor and the posting floor cannot drift apart.

Both doors are checked: the matrix editor (``RoleAccessView``) and the older
whole-role editor (``AdminRoleSerializer``) run the same ``floor_violations``,
because a floor with one gate is not a floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from accounts.matrix import capability_of
from accounts.role_lists import (
    ACCESS_ADMINISTRATORS,
    HEAD_OFFICE_VALUE_ACTORS,
    declare_role_list,
)
from accounts.sections import (
    CAP_APPROVE,
    CAP_MANAGE,
    CAPABILITY_ORDER,
    CAPABILITY_RANK,
    CAPABILITY_WORDS,
    SECTION_CODES,
    SECTION_LABELS,
)

#: The seats that work a shop floor. Rule 2 is about a *store*, and store-ness
#: is a property of the user's scope, not of a role - the GL engine reads
#: ``scope_type`` and is the real enforcement. What the matrix can say is which
#: role rows are the store's, and it has to say it by name: no rung, no scope
#: field and no derived query on the role distinguishes "the seat a cashier
#: sits in" from "a head-office seat that happens to hold the same rung".
STORE_SEATS = declare_role_list(
    "accounts.store_seats_floor",
    ("store_person",),
    reason=(
        "Rule 2 says a store never posts value to the books, and the books enforce "
        "it on the user's scope. The access matrix is per role, so capping Money for "
        "the store seat is the only way the *screen* can keep the same promise; "
        "naming them is unavoidable because no ladder rung means 'this is a shop "
        "floor seat'. Widening this list hands a store seat the books."
    ),
)


@dataclass(frozen=True)
class Floor:
    """One ratified rule, expressed as a rung the matrix may not grant."""

    #: Which of the four ratified rules this cell floor protects.
    rule: int
    section: str
    #: The rung a role outside ``held_by`` may not reach on ``section``.
    locked_rung: str
    #: The role codes the floor does allow to reach it. May be empty.
    held_by: frozenset[str]
    #: The role codes the floor applies to - ``None`` means every role.
    applies_to: frozenset[str] | None
    #: Shown on the greyed cell, in the language the business ratified it in.
    reason: str

    def binds(self, role_code: str) -> bool:
        if role_code in self.held_by:
            return False
        return self.applies_to is None or role_code in self.applies_to

    def ceiling(self) -> str:
        """The highest rung a bound role may still hold - one below ``locked_rung``."""
        return CAPABILITY_ORDER[CAPABILITY_RANK[self.locked_rung] - 1]


#: The ratified rule texts, quoted from PRD #104 (26 Jul 2026).
RULES: dict[int, str] = {
    1: "Nobody approves their own document.",
    2: "A store never posts value to the books.",
    3: "Money owed to a brand always requires a named head-office human.",
    4: ("Users and roles may be changed only by Owner or IT Admin, and never by one person alone."),
}

FLOORS: tuple[Floor, ...] = (
    Floor(
        rule=2,
        section="money",
        locked_rung=CAP_APPROVE,
        held_by=frozenset(),
        applies_to=STORE_SEATS,
        reason=(
            "A store never posts value to the books. A store seat may raise its own "
            "expenses and nothing more, so Money stops at Operate here - the books "
            "refuse a store-scoped person underneath every screen anyway, and Setup "
            "must not hand out a button the server will reject."
        ),
    ),
    Floor(
        rule=3,
        section="money",
        locked_rung=CAP_MANAGE,
        held_by=HEAD_OFFICE_VALUE_ACTORS,
        applies_to=None,
        reason=(
            "Money owed to a brand always requires a named head-office human. Full "
            "Money opens the books - vendor bills, payments, the value ledgers - so "
            "it stays with Owner and Accounts."
        ),
    ),
    Floor(
        rule=4,
        section="setup",
        locked_rung=CAP_MANAGE,
        held_by=ACCESS_ADMINISTRATORS,
        applies_to=None,
        reason=(
            "Users and roles may be changed only by Owner or IT Admin. Full Setup is "
            "the power to grant power, so no other role may be given it - a matrix "
            "that could hand it out could configure this floor away."
        ),
    ),
)


def cell_limits(role_code: str) -> dict[str, dict[str, str]]:
    """``{section: {max_capability, rule, reason}}`` for this role's locked cells.

    What the grid greys out, and the one answer both the screen and the refusal
    below are computed from. A section absent from the map is the role's to set
    to anything on the ladder. Where two floors bind the same cell the tighter
    ceiling wins, so the screen never offers a rung the server would refuse.
    """
    limits: dict[str, dict[str, str]] = {}
    for floor in FLOORS:
        if not floor.binds(role_code):
            continue
        ceiling = floor.ceiling()
        held = limits.get(floor.section)
        if held is not None and CAPABILITY_RANK[held["max_capability"]] <= CAPABILITY_RANK[ceiling]:
            continue
        limits[floor.section] = {
            "max_capability": ceiling,
            "rule": RULES[floor.rule],
            "reason": floor.reason,
        }
    return limits


def floor_violations(role_code: str, section_access: dict[str, Any]) -> list[dict[str, str]]:
    """Every cell of ``section_access`` that would cross a floor, one per cell.

    Cell by cell on purpose: an administrator who moved five cells and tripped
    one is told which one and why, not handed a blanket refusal. One entry per
    *cell*, not per floor - Money is capped twice for a store seat, and the
    screen has one square there, so the tighter ceiling answers for it.
    """
    limits = cell_limits(role_code)
    violations: list[dict[str, str]] = []
    for section in SECTION_CODES:
        limit = limits.get(section)
        if limit is None:
            continue
        asked = capability_of(section_access.get(section))
        if CAPABILITY_RANK.get(asked, 0) > CAPABILITY_RANK[limit["max_capability"]]:
            violations.append({"section": section, "requested": asked, **limit})
    return violations


def clamp_to_floors(
    role_code: str, section_access: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The row with every floor-crossing cell pulled down to its ceiling.

    Both doors refuse a crossing edit on the way in, so a stored row can only
    cross a floor one way: the floor moved under it. Floors are ratified in
    code, so a release can tighten one - add a role to ``STORE_SEATS``, narrow
    ``HEAD_OFFICE_VALUE_ACTORS`` - and every row already stored at the old
    ceiling is suddenly above the new one, with no edit to refuse.

    Until #224 the seed papered over that by heart: it wrote the sheet, which is
    floor-clean, over every row on every deploy. Seeding the grid additively
    keeps an administrator's retune, which is the point - but it would also keep
    a rung the floor has since locked. So the seed narrows, out loud, on the
    same deploy that ships the tighter floor.

    Returns the row to store and the violations it corrected, so the caller can
    say which cells it moved rather than moving them silently. Narrowing only:
    this never raises a cell.
    """
    crossed = floor_violations(role_code, section_access)
    if not crossed:
        return section_access, []
    out = dict(section_access)
    for violation in crossed:
        ceiling = violation["max_capability"]
        out[violation["section"]] = {
            "capability": ceiling,
            "label": CAPABILITY_WORDS.get(ceiling, ceiling),
        }
    return out, crossed


def describe_floors(violations: list[dict[str, str]]) -> list[str]:
    """One plain sentence per refused cell, for a form-error body."""
    return [
        f"{SECTION_LABELS[v['section']]}: {v['reason']} "
        f"The highest this role may hold here is {v['max_capability']}."
        for v in violations
    ]


def _validate() -> None:
    """Guard the transcription the same way ``rbac_matrix`` guards its own.

    A floor naming a section or rung the system does not have would silently
    never bind, which is the one failure mode a floor cannot have.
    """
    for floor in FLOORS:
        assert floor.section in SECTION_CODES, f"floor/{floor.rule}: bad section {floor.section!r}"
        assert floor.locked_rung in CAPABILITY_RANK, (
            f"floor/{floor.rule}: bad rung {floor.locked_rung!r}"
        )
        assert CAPABILITY_RANK[floor.locked_rung] > 0, (
            f"floor/{floor.rule}: cannot lock the bottom rung"
        )
        assert floor.rule in RULES, f"floor/{floor.rule}: not one of the ratified rules"
        assert floor.reason.strip(), f"floor/{floor.rule}: a locked cell needs a reason"


_validate()
