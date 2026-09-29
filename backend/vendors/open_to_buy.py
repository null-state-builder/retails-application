"""Open-to-buy (store operations PRD ST-BUY-1, overall PRD R-BUY-005; ticket 39).

1. **A budget** is money at cost that may be spent on one brand in one season,
   at one site or across the whole company. The Owner sets it (B161).
2. **Open-to-buy** is the budget less the open bookings and the goods received
   against them. Both are goods-v1 bookings of that brand and season valued at
   each line's cost: open is the pieces still to come on a confirmed booking that
   is not closed; received is the pieces received against any confirmed booking,
   less any reversed (B162). A line with no cost is never valued at zero: its
   pieces are listed as "cost missing" (B163).
3. **A booking that goes over it** - more than what is left of any budget that
   covers it, or with a line whose cost is missing - is confirmed only after the
   Owner approves it in the approvals inbox. The buyer asks; the Owner, a
   different person, decides; the buyer then confirms. An approval covers exactly
   the draft it was asked for (B166).

It is switched per site with ticket 01's switch: a booking is checked for the
destination sites where it is on (B165). Every write is one audited command with
the record before and after; money sits under ``cost_figures`` so the Audit Log
hides it from anyone who may not see cost.

The rules (``position``, ``check``) are pure; the rest reads or writes through a
running command.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from django.contrib.contenttypes.models import ContentType

from accounts.principal import AccessContext
from accounts.role_lists import OPEN_TO_BUY_APPROVER_ROLES, OPEN_TO_BUY_BUDGET_EDITOR_ROLES
from approvals.models import Approval, ApprovalStatus
from approvals.services import AlreadyPendingError, ApprovalError, request_approval
from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    execute_command,
)
from core.kernel_models import DocumentHead
from core.refusals import Refusal, issue
from core.tenancy import current_tenant_id
from masters.models import Brand, Season, Store
from masters.scoping import actionable_store_ids
from masters.store_feature_registry import OPEN_TO_BUY
from masters.store_features import feature as registered_feature
from masters.store_features import is_feature_on, require_feature, switch_states
from vendors.goods_models import BookingReceiptLink, GoodsBooking
from vendors.open_to_buy_models import OpenToBuyAsk, OpenToBuyBudget

FEATURE_KEY = OPEN_TO_BUY

#: The approvals-spine family (``Approval.kind``) and how the inbox names it.
APPROVAL_KIND = "open_to_buy"
APPROVAL_LABEL = "Booking over open-to-buy"

#: Audit actions. Budgets are ``open_to_buy_budget:<id>``, asks ``open_to_buy_ask:<id>``.
SET_ACTION = "vendors.open_to_buy.budget.set"
ASK_ACTION = "vendors.open_to_buy.ask"
APPROVE_ACTION = "vendors.open_to_buy.approve"
REJECT_ACTION = "vendors.open_to_buy.reject"

#: The largest budget a person may type: Rs 1,000 crore.
MAX_BUDGET_PAISE = 1_000_00_00_000_00

#: Where an ask stands.
NOT_ASKED = "not_asked"
WAITING = "waiting"
APPROVED = "approved"
REJECTED = "rejected"
#: Asked and decided, but for a draft that has changed since.
STALE = "stale"


# ---------------------------------------------------------------------------
# The rules, without a database
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineFact:
    """One confirmed booking line, as open-to-buy counts it."""

    site_id: int | None
    #: Pieces still to come: 0 once the booking is closed.
    open_qty: int
    #: Pieces received against it, less any reversed.
    received_qty: int
    cost_paise: int | None


@dataclass(frozen=True)
class Position:
    budget_paise: int
    open_paise: int
    received_paise: int
    open_to_buy_paise: int
    #: Pieces open or received whose line has no cost: not valued, never zero.
    cost_missing_pieces: int


def in_scope(site_id: int | None, budget_site_id: int | None) -> bool:
    """A company budget covers every line; a site's budget only the lines to that site."""
    return budget_site_id is None or site_id == budget_site_id


def position(budget_paise: int, facts: Iterable[LineFact], site_id: int | None) -> Position:
    """Budget less open bookings less goods received, over the lines in the budget's scope."""
    open_paise = received_paise = missing = 0
    for fact in facts:
        if not in_scope(fact.site_id, site_id):
            continue
        if fact.cost_paise is None:
            missing += fact.open_qty + fact.received_qty
            continue
        open_paise += fact.open_qty * fact.cost_paise
        received_paise += fact.received_qty * fact.cost_paise
    return Position(
        budget_paise=budget_paise,
        open_paise=open_paise,
        received_paise=received_paise,
        open_to_buy_paise=budget_paise - open_paise - received_paise,
        cost_missing_pieces=missing,
    )


@dataclass(frozen=True)
class DraftLine:
    site_id: int | None
    qty: int
    cost_paise: int | None


@dataclass(frozen=True)
class Over:
    """One budget a booking goes over, and by how much."""

    budget_id: int
    #: The budget's site; None for the whole company.
    site_id: int | None
    open_to_buy_paise: int
    #: The booking's lines in this budget's scope, at cost.
    booking_paise: int
    #: How far past open-to-buy the booking takes it (0 when only a cost is missing).
    over_paise: int
    #: The booking's pieces in scope whose line has no cost.
    cost_missing_pieces: int


def check(
    budgets: Iterable[tuple[int, int | None, Position]], lines: list[DraftLine]
) -> list[Over]:
    """Each budget ``(id, site, position)`` the draft goes over.

    A budget none of the draft's lines fall in is not touched. The draft goes over
    one when its lines cost more than what is left, or when a line in scope has
    no cost and so cannot be judged (B163).
    """
    out: list[Over] = []
    for budget_id, site_id, pos in budgets:
        scoped = [line for line in lines if in_scope(line.site_id, site_id)]
        if not scoped:
            continue
        value = sum(line.qty * line.cost_paise for line in scoped if line.cost_paise is not None)
        missing = sum(line.qty for line in scoped if line.cost_paise is None)
        over = max(0, value - pos.open_to_buy_paise)
        if over > 0 or missing > 0:
            out.append(
                Over(
                    budget_id=budget_id,
                    site_id=site_id,
                    open_to_buy_paise=pos.open_to_buy_paise,
                    booking_paise=value,
                    over_paise=over,
                    cost_missing_pieces=missing,
                )
            )
    return out


def over_json(over: Over) -> dict[str, Any]:
    """An ``Over`` as stored and sent: every paise figure as text."""
    row = asdict(over)
    for key in ("open_to_buy_paise", "booking_paise", "over_paise"):
        row[key] = str(row[key])
    return row


def money(paise: int) -> str:
    """``Rs 12,34,567.50``: Indian grouping, paise shown only when there are some."""
    rupees, rest = divmod(abs(paise), 100)
    digits = str(rupees)
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    grouped = ",".join(part for part in [head, *groups, tail] if part)
    sign = "-" if paise < 0 else ""
    return f"{sign}Rs {grouped}" + (f".{rest:02d}" if rest else "")


# ---------------------------------------------------------------------------
# Reading bookings
# ---------------------------------------------------------------------------


def _int_or_none(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


def _line_site(line: dict[str, Any], header: dict[str, Any]) -> int | None:
    return _int_or_none(line.get("destination_site_id") or header.get("destination_site_id"))


def _links_by_line(booking_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    """Pieces received less reversed, per booking and linked line key, in one query."""
    received: dict[uuid.UUID, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for booking_pk, key, qty, counter in BookingReceiptLink.objects.filter(
        booking_id__in=booking_ids
    ).values_list("booking_id", "booking_line_key", "linked_qty", "counter_of_id"):
        received[booking_pk][str(key)] += -qty if counter else qty
    return received


def line_facts(
    booking: GoodsBooking,
    header: dict[str, Any],
    lines: list[dict[str, Any]],
    root: dict[str, str],
    links: dict[str, int],
) -> list[LineFact]:
    """One confirmed booking's lines as open-to-buy counts them."""
    net: dict[str, int] = defaultdict(int)
    for line_key, qty in links.items():
        net[root.get(line_key, line_key)] += qty
    closed = booking.closed_at is not None
    facts: list[LineFact] = []
    for line in lines:
        got = max(0, net[root.get(line["line_key"], line["line_key"])])
        booked = int(line["qty"])
        facts.append(
            LineFact(
                site_id=_line_site(line, header),
                open_qty=0 if closed else max(0, booked - got),
                received_qty=got,
                cost_paise=_int_or_none(line.get("cost_paise")),
            )
        )
    return facts


def booking_facts(
    brand_id: int, season_id: int, *, leave_out: uuid.UUID | None = None
) -> list[LineFact]:
    """Every confirmed goods-v1 booking line of this brand and season (B162),
    but ``leave_out``'s.

    Drafts count for nothing. A closed booking keeps what it received and has
    nothing still to come. Receipt links are read in one query.
    """
    from vendors.goods_services import booking_head, booking_lines

    bookings = [
        b
        for b in GoodsBooking.objects.select_related("document").filter(brand_id=brand_id)
        if b.pk != leave_out
    ]
    received = _links_by_line([b.pk for b in bookings])
    facts: list[LineFact] = []
    for booking in bookings:
        head = booking_head(booking)
        if head.live_version_id is None:
            continue
        header, lines, root = booking_lines(booking, head)
        if _int_or_none(header.get("season_id")) != season_id:
            continue
        facts += line_facts(booking, header, lines, root, received[booking.pk])
    return facts


def draft_lines(header: dict[str, Any], lines: list[dict[str, Any]]) -> list[DraftLine]:
    return [
        DraftLine(
            site_id=_line_site(line, header),
            qty=int(line["qty"]),
            cost_paise=_int_or_none(line.get("cost_paise")),
        )
        for line in lines
    ]


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------


def reaches(access: AccessContext, site_id: int | None, brand_id: int) -> bool:
    """A goods grant carrying ``cost`` over this site and brand (B160).

    The same field that hides a booking's cost. A site's budget needs a grant
    reaching that site; the company's needs one reaching no particular site
    (tenant or brand scope).
    """
    return "cost" in access.field_grants(site_id=site_id, brand_id=brand_id)


def reaches_site(access: AccessContext, site_id: int) -> bool:
    """A grant carrying ``cost`` reaches this site, for some brand."""
    return any("cost" in g.fields and access.reaches_site(g, site_id) for g in access.grants)


def sees_cost_somewhere(access: AccessContext) -> bool:
    return any("cost" in grant.fields for grant in access.grants)


def _role(user: Any) -> str:
    return str(getattr(getattr(user, "role", None), "code", "") or "")


def may_set(user: Any) -> bool:
    """The Owner (B161), or break-glass; the cost reach is checked per budget."""
    if getattr(user, "is_superuser", False):
        return True
    return _role(user) in OPEN_TO_BUY_BUDGET_EDITOR_ROLES


def sites_switched_on(site_ids: Iterable[int]) -> set[int]:
    """Which of these sites have open-to-buy on."""
    stores = list(Store.objects.filter(pk__in=list(site_ids)))
    return {
        state.site_id
        for state in switch_states(stores, [registered_feature(FEATURE_KEY)])
        if state.enabled
    }


def on_anywhere(user: Any | None = None) -> bool:
    """On at an active site - one the person acts at, when a person is given."""
    stores = Store.objects.filter(is_active=True)
    if user is not None:
        ids = actionable_store_ids(user)
        if ids is not None:
            stores = stores.filter(pk__in=ids)
    return bool(sites_switched_on(stores.values_list("pk", flat=True)))


def readable_budgets(access: AccessContext) -> list[OpenToBuyBudget]:
    rows = OpenToBuyBudget.objects.filter(tenant_id=access.tenant_id).select_related(
        "brand", "season", "site", "set_by"
    )
    return [row for row in rows if reaches(access, row.site_id, row.brand_id)]


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


def positions(budgets: list[OpenToBuyBudget]) -> dict[int, Position]:
    """Each budget's position, reading each brand and season's bookings once."""
    facts: dict[tuple[int, int], list[LineFact]] = {}
    out: dict[int, Position] = {}
    for budget in budgets:
        key = (budget.brand_id, budget.season_id)
        if key not in facts:
            facts[key] = booking_facts(*key)
        out[budget.pk] = position(budget.budget_paise, facts[key], budget.site_id)
    return out


def snapshot(budget: OpenToBuyBudget | None) -> dict[str, Any] | None:
    """A budget as its audit records hold it; the money under ``cost_figures``."""
    if budget is None:
        return None
    return {
        "brand_id": budget.brand_id,
        "season_id": budget.season_id,
        "site_id": budget.site_id,
        "scope": "site" if budget.site_id else "company",
        "revision": budget.revision,
        "cost_figures": {"budget_paise": str(budget.budget_paise)},
    }


# ---------------------------------------------------------------------------
# 1. Setting a budget
# ---------------------------------------------------------------------------


def _check_target(
    user: Any,
    access: AccessContext,
    brand_id: int,
    season_id: int,
    site_id: int | None,
    budget_paise: int,
) -> None:
    """Who may set this budget, for what, and whether the switch allows it."""
    if not may_set(user):
        raise Refusal(
            "ACTION_DENIED",
            "The Owner sets open-to-buy budgets. The buyer reads them here.",
        )
    if not Brand.objects.filter(pk=brand_id, is_active=True).exists():
        raise Refusal("INVALID_REQUEST", "No active brand has that ID.", status=422)
    if not Season.objects.filter(pk=season_id).exists():
        raise Refusal("INVALID_REQUEST", "No season has that ID.", status=422)
    if site_id is not None:
        site = Store.objects.filter(pk=site_id, is_active=True).first()
        if site is None:
            raise Refusal("INVALID_REQUEST", "No active site has that ID.", status=422)
    if not reaches(access, site_id, brand_id):
        raise Refusal("NOT_FOUND", "That site or brand is not one you may plan for.")
    if site_id is not None:
        require_feature(site_id, FEATURE_KEY)
    elif not on_anywhere(user):
        raise Refusal(
            "FEATURE_OFF",
            "Open-to-buy is switched off at every site you work at. "
            "Admin can switch it on in Setup, Feature Switches.",
            status=403,
        )
    if budget_paise < 0 or budget_paise > MAX_BUDGET_PAISE:
        raise Refusal(
            "INVALID_REQUEST",
            "A budget is zero or more, and at most Rs 1,000 crore.",
            status=422,
        )


def set_budget(
    run: CommandRun,
    *,
    user: Any,
    access: AccessContext,
    brand_id: int,
    season_id: int,
    site_id: int | None,
    budget_paise: int,
    expected_revision: int | None,
) -> OpenToBuyBudget:
    """Set or change one budget. A stale screen is refused; nothing is deleted."""
    _check_target(user, access, brand_id, season_id, site_id, budget_paise)
    _guard(run, brand_id, season_id)
    existing = (
        OpenToBuyBudget.objects.select_for_update()
        .filter(tenant_id=run.tenant_id, brand_id=brand_id, season_id=season_id, site_id=site_id)
        .first()
    )
    if existing is None and expected_revision is not None:
        raise Refusal("STALE_REVISION", "That budget is not set yet. Reload and try again.")
    if existing is not None and expected_revision != existing.revision:
        raise Refusal(
            "STALE_REVISION",
            "Someone changed this budget since you opened it. Reload and try again.",
        )
    before = snapshot(existing)
    if existing is None:
        budget = OpenToBuyBudget.objects.create(
            tenant_id=run.tenant_id,
            brand_id=brand_id,
            season_id=season_id,
            site_id=site_id,
            budget_paise=budget_paise,
            set_by=user,
        )
    else:
        budget = existing
        budget.budget_paise = budget_paise
        budget.revision += 1
        budget.set_by = user
        budget.save(update_fields=["budget_paise", "revision", "set_by", "updated_at"])
    run.audit_before = before
    run.audit_after = snapshot(budget)
    run.audit_site_id = site_id
    run.audit_subject_key = f"open_to_buy_budget:{budget.pk}"
    return budget


def _guard(run: CommandRun, brand_id: int, season_id: int) -> None:
    """Budgets of a brand and season, and the confirmations judged against them,
    are taken in turn under one advisory key, so two bookings confirmed at the
    same moment cannot both fit into what only one of them fits."""
    run.advisory_lock(LockRank.CHAIN, [f"open-to-buy:{brand_id}:{season_id}"])


# ---------------------------------------------------------------------------
# 2. A booking against open-to-buy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """What open-to-buy says about one draft booking."""

    #: The switch is on at a destination (or anywhere, for a booking with none),
    #: and a budget covers the brand and season.
    applies: bool
    overs: list[Over]
    budgets: dict[int, tuple[OpenToBuyBudget, Position]]


def checked_sites(lines: list[DraftLine]) -> tuple[set[int], bool]:
    """The draft's destination sites where the switch is on, and whether the
    company budget applies (B165): on at any destination, or - for a booking that
    names no site yet - on at any active site."""
    named = {line.site_id for line in lines if line.site_id is not None}
    if not named:
        return set(), on_anywhere()
    on = sites_switched_on(named)
    return on, bool(on)


def verdict(booking: GoodsBooking, header: dict[str, Any], lines: list[dict[str, Any]]) -> Verdict:
    drafts = draft_lines(header, lines)
    season_id = _int_or_none(header.get("season_id"))
    on_sites, company = checked_sites(drafts)
    if season_id is None or not (on_sites or company):
        return Verdict(applies=False, overs=[], budgets={})
    rows = OpenToBuyBudget.objects.filter(
        tenant_id=booking.tenant_id, brand_id=booking.brand_id, season_id=season_id
    ).select_related("site")
    chosen = [row for row in rows if (row.site_id is None and company) or row.site_id in on_sites]
    if not chosen:
        return Verdict(applies=False, overs=[], budgets={})
    figures = positions(chosen)
    budgets = {row.pk: (row, figures[row.pk]) for row in chosen}
    # A line to a site whose switch is off is left out of that site's check but
    # still counts towards the company's budget.
    overs = check(((row.pk, row.site_id, figures[row.pk]) for row in chosen), drafts)
    return Verdict(applies=True, overs=overs, budgets=budgets)


def asks_of(booking: GoodsBooking) -> list[OpenToBuyAsk]:
    return list(OpenToBuyAsk.objects.filter(booking_id=booking.pk).order_by("-created_at", "-id"))


def approvals_of(ask_ids: Iterable[int]) -> dict[int, Approval]:
    """The approval of each ask, in one query."""
    ids = list(ask_ids)
    if not ids:
        return {}
    newest: dict[int, Approval] = {}
    for row in (
        Approval.objects.filter(
            kind=APPROVAL_KIND,
            content_type=ContentType.objects.get_for_model(OpenToBuyAsk),
            object_id__in=ids,
        )
        .select_related("requested_by", "decided_by")
        .order_by("object_id", "-created_at", "-id")
    ):
        newest.setdefault(int(row.object_id), row)
    return newest


def stage_of(ask: OpenToBuyAsk | None, approval: Approval | None, draft_hash: str | None) -> str:
    """Where the newest ask stands for the draft as it is now."""
    if ask is None or approval is None:
        return NOT_ASKED
    if approval.status == ApprovalStatus.PENDING:
        # Still with the Owner, even for an earlier draft: nothing more to ask yet.
        return WAITING
    if ask.draft_hash != draft_hash:
        return STALE
    if approval.status == ApprovalStatus.APPROVED:
        return APPROVED
    if approval.status == ApprovalStatus.REJECTED:
        return REJECTED
    return NOT_ASKED


def newest_ask(booking: GoodsBooking) -> tuple[OpenToBuyAsk | None, Approval | None]:
    asks = asks_of(booking)
    if not asks:
        return None, None
    return asks[0], approvals_of([asks[0].pk]).get(asks[0].pk)


def approved_ask(booking: GoodsBooking, draft_hash: str) -> OpenToBuyAsk | None:
    """An ask the Owner approved for exactly this draft, if any."""
    asks = [ask for ask in asks_of(booking) if ask.draft_hash == draft_hash]
    decided = approvals_of(ask.pk for ask in asks)
    for ask in asks:
        approval = decided.get(ask.pk)
        if approval is not None and approval.status == ApprovalStatus.APPROVED:
            return ask
    return None


def guard_confirmation(
    run: CommandRun,
    booking: GoodsBooking,
    head: DocumentHead,
    header: dict[str, Any],
    lines: list[dict[str, Any]],
) -> OpenToBuyAsk | None:
    """Called by the booking's confirmation: refuse a booking over open-to-buy
    unless the Owner approved this very draft. Returns the approval used, if any."""
    assert head.draft_revision is not None
    season_id = _int_or_none(header.get("season_id"))
    if season_id is None:
        return None
    _guard(run, booking.brand_id, season_id)
    found = verdict(booking, header, lines)
    if not found.overs:
        return None
    draft_hash = head.draft_revision.content_hash
    ask = approved_ask(booking, draft_hash)
    if ask is not None:
        return ask
    latest, approval = newest_ask(booking)
    stage = stage_of(latest, approval, draft_hash)
    if stage == WAITING:
        raise Refusal(
            "OPEN_TO_BUY_WAITING",
            "This booking goes over open-to-buy and is waiting for the Owner's approval.",
            status=409,
        )
    raise Refusal(
        "OPEN_TO_BUY_OVER",
        "This booking goes over open-to-buy. Ask the Owner to approve it, then confirm it.",
        status=409,
        issues=[_issue(over, found.budgets[over.budget_id][0]) for over in found.overs],
    )


def guard_correction(
    run: CommandRun,
    booking: GoodsBooking,
    before: tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]],
    after: tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]],
) -> None:
    """Refuse a correction that takes a confirmed booking over open-to-buy (B169).

    Only a correction that uses more of a budget than the booking already did is
    judged: more pieces, a higher cost, a new destination or a cost taken away.
    It is refused when what is then left of any budget covering it goes below
    zero. There is no approval of a correction: the Owner raises the budget, or
    the buyer books the extra separately and asks.
    """
    header, lines, root = after
    season_id = _int_or_none(header.get("season_id"))
    if season_id is None:
        return
    _guard(run, booking.brand_id, season_id)
    drafts = draft_lines(header, lines)
    on_sites, company = checked_sites(drafts)
    if not (on_sites or company):
        return
    rows = [
        row
        for row in OpenToBuyBudget.objects.filter(
            tenant_id=booking.tenant_id, brand_id=booking.brand_id, season_id=season_id
        ).select_related("site")
        if (row.site_id is None and company) or row.site_id in on_sites
    ]
    if not rows:
        return
    links = _links_by_line([booking.pk])[booking.pk]
    old = line_facts(booking, *before, links)
    new = line_facts(booking, header, lines, root, links)
    others = booking_facts(booking.brand_id, season_id, leave_out=booking.pk)
    problems: list[dict[str, Any]] = []
    for row in rows:
        was = position(0, old, row.site_id)
        now = position(0, new, row.site_id)
        used_before = -was.open_to_buy_paise
        used_now = -now.open_to_buy_paise
        left = position(row.budget_paise, others, row.site_id).open_to_buy_paise - used_now
        if now.cost_missing_pieces > was.cost_missing_pieces:
            problems.append(
                issue(
                    "COST_MISSING",
                    f"The correction leaves pieces for {_scope_name(row)} with no cost, so it "
                    "cannot be judged against open-to-buy.",
                )
            )
        elif used_now > used_before and left < 0:
            problems.append(issue("OVER_BUDGET", f"Goes over open-to-buy for {_scope_name(row)}."))
    if problems:
        raise Refusal(
            "OPEN_TO_BUY_OVER",
            "This correction takes the booking over open-to-buy. The Owner can raise the "
            "budget; or book the extra pieces as a new booking and ask the Owner.",
            status=409,
            issues=problems,
        )


def _scope_name(budget: OpenToBuyBudget) -> str:
    return budget.site.name if budget.site is not None else "the whole company"


def _issue(over: Over, budget: OpenToBuyBudget) -> dict[str, Any]:
    """What the refusal says, without money: its reader may not see cost."""
    if over.over_paise > 0:
        return issue("OVER_BUDGET", f"Goes over open-to-buy for {_scope_name(budget)}.")
    return issue(
        "COST_MISSING",
        f"{over.cost_missing_pieces} piece(s) for {_scope_name(budget)} have no cost, so the "
        "booking cannot be judged against open-to-buy.",
    )


def headline(booking: GoodsBooking, overs: list[Over], season: Season) -> str:
    """What the inbox shows. No money: the inbox is read by logins that may not see
    cost; the Owner opens the request for the figures."""
    missing = any(over.cost_missing_pieces for over in overs) and not any(
        over.over_paise for over in overs
    )
    tail = "a line with no cost" if missing else "over open-to-buy"
    return f"{booking.brand.name} {season.code} · booking draft · {tail}"


def ask_owner(
    run: CommandRun,
    booking: GoodsBooking,
    *,
    user: Any,
    reviewed_hash: str,
    expected_revision: int | None,
) -> OpenToBuyAsk:
    """The buyer asks the Owner to approve confirming this draft over open-to-buy."""
    from core.goods_documents import lock_heads
    from inbound import goods_input as inp
    from vendors.goods_services import booking_lines

    head = lock_heads(run, [booking.document_id])[booking.document_id]
    inp.check_revision(expected_revision, head.revision)
    if head.live_version_id is not None or head.draft_revision is None:
        raise Refusal("STATE_CONFLICT", "Only a draft booking is asked about.")
    draft_hash = head.draft_revision.content_hash
    if reviewed_hash != draft_hash:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "What you reviewed is no longer the current draft. Reload and review it again.",
        )
    header, lines, _root = booking_lines(booking, head)
    season_id = _int_or_none(header.get("season_id"))
    if season_id is None:
        raise Refusal("STATE_CONFLICT", "The booking has no season.")
    _guard(run, booking.brand_id, season_id)
    found = verdict(booking, header, lines)
    if not found.applies:
        raise Refusal(
            "FEATURE_OFF",
            "Open-to-buy does not apply to this booking: it is switched off where it goes, "
            "or no budget is set for its brand and season.",
            status=403,
        )
    if not found.overs:
        raise Refusal("NOT_OVER", "This booking fits within open-to-buy. Confirm it.")
    latest, approval = newest_ask(booking)
    stage = stage_of(latest, approval, draft_hash)
    if approval is not None and approval.status == ApprovalStatus.PENDING:
        # Also for an earlier draft (B170): one request waits in the Owner's inbox
        # at a time, so none is left there that nothing would act on.
        raise Refusal(
            "STATE_CONFLICT",
            "The Owner has not decided the earlier request yet. They decide it first; then "
            "ask again if the booking changed.",
        )
    if stage == APPROVED:
        raise Refusal("STATE_CONFLICT", "The Owner has already approved this draft. Confirm it.")
    season = Season.objects.get(pk=season_id)
    ask = OpenToBuyAsk.objects.create(
        tenant_id=run.tenant_id,
        booking=booking,
        draft_hash=draft_hash,
        brand_id=booking.brand_id,
        season=season,
        site_id=booking.document.site_id,
        figures=[over_json(over) for over in found.overs],
        over_paise=max(over.over_paise for over in found.overs),
        asked_by=user,
    )
    try:
        approval = request_approval(
            ask,
            kind=APPROVAL_KIND,
            kind_label=APPROVAL_LABEL,
            title=headline(booking, found.overs, season),
            made_by=user,
            requested_by=user,
            approver_roles=sorted(OPEN_TO_BUY_APPROVER_ROLES),
            store=booking.document.site,
            brand=booking.brand.name,
            # Stated, not the amount over: the inbox is not a cost reader's alone.
            value_paise=0,
        )
    except AlreadyPendingError as exc:  # pragma: no cover - a new ask has no approval yet
        raise Refusal("STATE_CONFLICT", str(exc)) from exc
    run.audit_before = {"booking_id": str(booking.document_id), "stage": stage}
    run.audit_after = {
        "booking_id": str(booking.document_id),
        "stage": WAITING,
        "approval_id": approval.pk,
        "draft_hash": draft_hash,
        "cost_figures": {"overs": ask.figures},
    }
    run.audit_site_id = booking.document.site_id
    run.audit_subject_key = f"open_to_buy_ask:{ask.pk}"
    return ask


# ---------------------------------------------------------------------------
# 3. The Owner's decision, in the approvals inbox
# ---------------------------------------------------------------------------


def _decision_principal(ask: OpenToBuyAsk, actor: Any) -> Principal:
    """The Owner as a named person: a login with no person behind it never decides."""
    human_id = getattr(actor, "human_id", None)
    if human_id is None:
        raise ApprovalError("A booking over open-to-buy is approved by a named person.")
    tenant_id = current_tenant_id() or ask.tenant_id
    return Principal(tenant_id=tenant_id, human_id=human_id, user_id=getattr(actor, "pk", None))


def _record_decision(ask: OpenToBuyAsk, actor: Any, *, approved: bool, reason: str) -> None:
    """The Owner's decision as its own audited command, inside the decision's transaction.

    The approvals spine has already checked the role, and that the Owner is not
    the buyer who asked. The decision is recorded whatever the switch says now:
    it changes nothing until the buyer confirms, and confirming checks again.
    """
    action = APPROVE_ACTION if approved else REJECT_ACTION
    booking_id = str(ask.booking.document_id)

    def handler(run: CommandRun) -> CommandResult:
        run.audit_before = {"booking_id": booking_id, "stage": WAITING}
        run.audit_after = {
            "booking_id": booking_id,
            "stage": APPROVED if approved else REJECTED,
            "decided_by": getattr(actor, "pk", None),
            "draft_hash": ask.draft_hash,
            **({} if approved else {"reason": reason}),
        }
        return CommandResult(resource_type="open_to_buy_ask", resource_id=str(ask.pk))

    try:
        execute_command(
            _decision_principal(ask, actor),
            CommandSpec(
                action=action,
                command_id=uuid.uuid4(),
                business_input={"ask_id": ask.pk, "approved": approved, "reason": reason},
                subject_key=f"open_to_buy_ask:{ask.pk}",
                site_id=ask.site_id,
            ),
            handler,
        )
    except Refusal as refusal:
        raise ApprovalError(refusal.message) from refusal


def on_approved(ask: OpenToBuyAsk, *, actor: Any) -> None:
    _record_decision(ask, actor, approved=True, reason="")


def on_rejected(ask: OpenToBuyAsk, *, actor: Any, reason: str) -> None:
    _record_decision(ask, actor, approved=False, reason=reason)


def register_approval_hooks() -> None:
    from approvals.hooks import register_on_approved, register_on_rejected

    register_on_approved(OpenToBuyAsk, on_approved)
    register_on_rejected(OpenToBuyAsk, on_rejected)


def switched_on_for(site_id: int | None) -> bool:
    """The switch at a budget's site; for the company's, anywhere."""
    return is_feature_on(site_id, FEATURE_KEY) if site_id is not None else on_anywhere()
