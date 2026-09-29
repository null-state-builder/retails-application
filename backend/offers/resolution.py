"""The offer engine - pure, deterministic, no ORM (D5 Q1, grill Q9).

This module and its TypeScript twin at `app/frontend/src/till/offers.ts` are the
same function written twice, because the same cart is priced twice: once at the
counter, offline, on a machine that may not have seen head office for a day, and
again on the server when the bill finally syncs. If the two ever disagree the
customer's receipt and the books disagree, and there is no third opinion to
appeal to - the receipt is already in a bag on a bus to Deoghar.

So the two are held together by `offers/vectors/*.json`: the same golden
carts, run through both engines, compared to the paisa. A divergence is a red
build, which is what makes "the same cart always prices the same" a tested
property rather than a hope.

Three rules shape everything below.

**Deterministic, never optimal.** Choosing the globally cheapest combination of
overlapping offers is a knapsack, and a knapsack on till hardware is a counter
that stalls with a queue behind it. The engine is greedy instead: it takes the
best proposal on the table, claims the lines that proposal covers, and asks the
rest to bid again on what is left. That converges in at most one round per rule,
gives the same answer every time, and can be explained to a store manager in one
sentence - which the optimum could not be.

**Integer paise throughout.** No floats, anywhere, on any path (ADR-0004).
Percentages arrive as two-decimal strings and become hundredths of a percent;
half-up rounding is written as integer arithmetic so that Python and TypeScript
round the same way rather than the same way *usually*.

**The rulebook only ever discounts.** No rule can raise a line, no line's
discount can exceed what the line costs, and a piece flagged `no_discount` is
untouchable by every rule at once (D5 Q3). Those three are enforced here rather
than trusted from the config, because the config is data an administrator edits.

The layer model (grill Q9, contract §Step 4): layer `brand` first, at most one
per line, best deal wins; then `storewide`, then `bank`, each computed on the
already-reduced base (D5 Q13) and each applying only when that rule is flagged
`combinable`. Layers never compete with one another, so there is no combinatorial
search. Residual ties break by `priority` (lower wins) then by `id`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

# --- the vocabulary --------------------------------------------------------

LAYER_BRAND = "brand"
LAYER_STOREWIDE = "storewide"
LAYER_BANK = "bank"

#: Layer 1 claims a line outright; the other two stack onto what it left, in
#: this order. The order is the contract's, and it is load-bearing: a bank
#: percentage taken before a storewide one is a different number.
ADD_ON_LAYERS = (LAYER_STOREWIDE, LAYER_BANK)

TRIGGER_NONE = "none"
TRIGGER_SPEND = "spend"
TRIGGER_QTY = "qty"
TRIGGER_GROUP = "group"

REWARD_PCT_OFF = "pct_off"
REWARD_AMT_OFF = "amt_off"
REWARD_ITEM_FREE = "item_free"
REWARD_FIXED_PRICE = "fixed_price"
REWARD_GIFT = "gift"

#: What an add-on layer may do. A "buy 2 get 1" or a "flat ₹2,599" is a claim on
#: the pieces themselves and cannot sensibly be applied a second time onto a
#: price that has already been reduced; an extra percentage or an extra rupee
#: amount can. A rule outside this set on an add-on layer makes no proposal at
#: all, rather than making a wrong one.
ADD_ON_REWARDS = frozenset({REWARD_PCT_OFF, REWARD_AMT_OFF})

#: What a gift may fall back to when the counter has none left (D5 Q11). Never
#: another gift: a fallback that could itself be out of stock is a loop.
FALLBACK_REWARDS = frozenset({REWARD_PCT_OFF, REWARD_AMT_OFF, REWARD_FIXED_PRICE})

# --- GST after discount (store operations ticket 11, ST-CMP-1, §16) --------
#
# Everything below changes a bill only when the cart is resolved with
# ``after_discount=True``, which the till and the server do only where the
# store's ``gst-after-discount`` switch is on (and the bill says so). Off, the
# engine is exactly what it always was (baseline B5), vectors and all.

#: A buy-X-get-Y offer's own setting (``reward_config["allocation"]``): how the
#: price of each group is shared across its pieces for tax (§6, baseline, CA to
#: confirm). ``by_mrp`` - the default - spreads the group's price in proportion
#: to MRP, so each piece takes its rate from its own share. ``free_piece`` keeps
#: the free piece at nought, as the engine did before this ticket.
ALLOCATION_BY_MRP = "by_mrp"
ALLOCATION_FREE_PIECE = "free_piece"
ALLOCATIONS = (ALLOCATION_BY_MRP, ALLOCATION_FREE_PIECE)

#: A bank-layer offer's own setting (``reward_config["reduces_value"]``). False -
#: the default - means a bank instant discount is not a price cut: it does not
#: reduce any line's taxable value, and the bill records it as a "bank offer"
#: payment instead (§6, ST-POS-1; baseline, CA to confirm).
REDUCES_VALUE = "reduces_value"


def allocation_of(rule: Rule) -> str:
    value = str((rule.reward_config or {}).get("allocation") or ALLOCATION_BY_MRP)
    return value if value in ALLOCATIONS else ALLOCATION_BY_MRP


def reduces_value(rule: Rule) -> bool:
    return (rule.reward_config or {}).get(REDUCES_VALUE) is True


# --- the inputs ------------------------------------------------------------


@dataclass(frozen=True)
class CartLine:
    """One row of the bill, as the rulebook needs to see it.

    Deliberately neither the till's `CartLine` nor the server's `SaleLine`: this
    is the intersection of the two, so that neither side has to hand the engine
    anything it would have to invent.
    """

    line_no: int
    brand: str = ""
    item: str = ""
    design: str = ""
    size: str = ""
    color: str = ""
    barcode: str = ""
    season: str = ""
    qty: int = 1
    mrp_paise: int = 0
    no_discount: bool = False

    @property
    def gross_paise(self) -> int:
        return self.mrp_paise * self.qty


@dataclass(frozen=True)
class Cart:
    lines: tuple[CartLine, ...]
    #: The till's own clock (grill Q3). Every rule's start and end date is judged
    #: against this and nothing else, so a counter that has not synced for two
    #: days still stops a dead offer on time.
    day: date
    #: Gift offers the counter could not honour, by offer id - the trolley ran
    #: out, so the pre-decided fallback discount applies instead (D5 Q11).
    declined_gifts: frozenset[int] = frozenset()


@dataclass(frozen=True)
class Rule:
    """One offer, flattened out of the ORM (or off the till's dataset)."""

    id: int
    name: str = ""
    layer: str = LAYER_BRAND
    brand: str = ""
    trigger_type: str = TRIGGER_NONE
    trigger_config: Mapping[str, Any] = field(default_factory=dict)
    reward_type: str = REWARD_PCT_OFF
    reward_config: Mapping[str, Any] = field(default_factory=dict)
    item_scope: Mapping[str, Any] = field(default_factory=dict)
    starts_on: date = date.min
    ends_on: date | None = None
    combinable: bool = False
    priority: int = 100


# --- the outputs -----------------------------------------------------------


@dataclass(frozen=True)
class Entitlement:
    """A gift the bill has earned. Not a discount on any line."""

    offer_id: int
    offer_name: str
    barcode: str
    token_price_paise: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "offer_name": self.offer_name,
            "barcode": self.barcode,
            "token_price_paise": self.token_price_paise,
        }


@dataclass(frozen=True)
class LineOutcome:
    line_no: int
    discount_paise: int
    #: The layer-`brand` winner, which is the one that becomes `SaleLine.offer`.
    #: An add-on is recorded in the evidence but is not "the rule that won".
    offer_id: int | None
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "discount_paise": self.discount_paise,
            "offer_id": self.offer_id,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class BankOffer:
    """A bank instant discount recorded as a payment, not a price cut (ticket 11)."""

    offer_id: int
    offer_name: str
    amount_paise: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "offer_name": self.offer_name,
            "amount_paise": self.amount_paise,
        }


@dataclass(frozen=True)
class Resolution:
    lines: tuple[LineOutcome, ...]
    entitlements: tuple[Entitlement, ...]
    #: Bank offers that do not reduce value, resolved with ``after_discount``
    #: only; each becomes a "bank offer" tender on the bill. Always empty off.
    bank_offers: tuple[BankOffer, ...] = ()
    after_discount: bool = False

    @property
    def total_discount_paise(self) -> int:
        return sum(line.discount_paise for line in self.lines)

    @property
    def bank_offer_paise(self) -> int:
        return sum(offer.amount_paise for offer in self.bank_offers)

    def by_line(self) -> dict[int, LineOutcome]:
        return {line.line_no: line for line in self.lines}

    def as_dict(self) -> dict[str, Any]:
        """The canonical form the two engines are compared in.

        ``bank_offers`` appears only on a cart resolved after discount, so every
        vector written before ticket 11 keeps exactly its shape.
        """
        body: dict[str, Any] = {
            "lines": [line.as_dict() for line in self.lines],
            "entitlements": [gift.as_dict() for gift in self.entitlements],
            "total_discount_paise": self.total_discount_paise,
        }
        if self.after_discount:
            body["bank_offers"] = [offer.as_dict() for offer in self.bank_offers]
        return body


# --- integer arithmetic, mirrored in TypeScript ----------------------------


def hundredths(value: Any) -> int:
    """A two-decimal percentage string as hundredths of a percent.

    `"7.50"` is 750. Parsed off the digits rather than through a float, for the
    reason ADR-0004 gives: a rate that arrived as 7.499999 puts the counter and
    the server on opposite sides of a rupee.
    """
    text = str(value).strip()
    if not text:
        return 0
    sign = -1 if text.startswith("-") else 1
    whole, _, frac = text.lstrip("+-").partition(".")
    whole = whole or "0"
    frac = (frac + "00")[:2]
    if not whole.isdigit() or not frac.isdigit():
        raise ValueError(f"'{value}' is not a percentage")
    return sign * (int(whole) * 100 + int(frac))


def percent_of(amount_paise: int, percent_hundredths: int) -> int:
    """`amount x percent`, rounded half-up, in integers only.

    Written as `(2ab + d) // 2d` because that is half-up on a non-negative
    rational without ever touching a float or a Decimal - and it is the same
    expression the TypeScript port runs, which is the whole point of it.
    """
    if amount_paise <= 0 or percent_hundredths <= 0:
        return 0
    denominator = 10_000
    return (2 * amount_paise * percent_hundredths + denominator) // (2 * denominator)


def normalise(text: Any) -> str:
    """A brand or category name reduced to what two spellings of it share.

    `Sku.brand` is free text off a brand's own price tag and `Brand.name` is what
    head office typed; "U.S. Polo" and "US POLO" are the same brand, and a rule
    that matched only one of them would silently under-discount a bill.
    """
    return "".join(ch for ch in str(text or "").upper() if ch.isalnum())


def _names(scope: Mapping[str, Any], key: str) -> set[str]:
    raw = scope.get(key)
    if not raw:
        return set()
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    return {normalise(item) for item in raw if normalise(item)}


# --- scope -----------------------------------------------------------------


def _within_dates(rule: Rule, day: date) -> bool:
    if day < rule.starts_on:
        return False
    return rule.ends_on is None or day <= rule.ends_on


#: Every axis a rule may aim at, from a whole brand down to one size in one
#: colour (D5 Q2: "any level ... brand -> category -> chosen styles -> specific
#: size/colour", plus season).
SCOPE_KEYS = ("brands", "categories", "styles", "sizes", "colors", "barcodes", "seasons")

#: The explicit "we do not know which buying cohort this came from" season
#: (store and warehouse operations PRD §4), by the two spellings a line can
#: carry it under - the season master's code and its name. The engine holds no
#: ORM, so the sentinel is named here and in the TypeScript twin rather than
#: looked up; `masters.Season.historical_unknown` marks the same row for
#: everything that *can* read the master.
UNKNOWN_HISTORICAL_SEASON_CODE = "UNKNOWN-HIST"
UNKNOWN_HISTORICAL_SEASON_NAME = "Unknown historical season"


def is_unknown_historical_season(value: Any) -> bool:
    return normalise(value) in {
        normalise(UNKNOWN_HISTORICAL_SEASON_CODE),
        normalise(UNKNOWN_HISTORICAL_SEASON_NAME),
    }


def _facets(line: CartLine) -> tuple[tuple[str, str], ...]:
    return (
        ("brands", line.brand),
        ("categories", line.item),
        ("styles", line.design),
        ("sizes", line.size),
        ("colors", line.color),
        ("barcodes", line.barcode),
        ("seasons", line.season),
    )


def covers(rule: Rule, line: CartLine, day: date) -> bool:
    """Is this line one of the pieces this rule is about?

    Every clause is a hard no; the order is for the reader, not the logic.
    """
    if not _within_dates(rule, day):
        return False
    # The AMM/NOD flag beats every rule there is, including a storewide one
    # (D5 Q3). It is a property of the piece, not a preference of the offer.
    if line.no_discount:
        return False
    if line.qty <= 0 or line.mrp_paise <= 0:
        return False
    if rule.brand and normalise(rule.brand) != normalise(line.brand):
        return False

    scope = rule.item_scope or {}
    # A piece whose cohort was never established is outside every offer that
    # names a season, including one that names the unknown season itself: the
    # offer was written about a buying cohort, and this piece has none. An
    # offer with no season scope still reaches it.
    if is_unknown_historical_season(line.season) and _names(scope, "seasons"):
        return False
    for key, value in _facets(line):
        wanted = _names(scope, key)
        if wanted and normalise(value) not in wanted:
            return False

    floor = scope.get("mrp_min_paise")
    if floor is not None and line.mrp_paise < int(floor):
        return False
    ceiling = scope.get("mrp_max_paise")
    if ceiling is not None and line.mrp_paise > int(ceiling):
        return False

    exclude = scope.get("exclude") or {}
    return not any(normalise(value) in _names(exclude, key) for key, value in _facets(line))


# --- proposals -------------------------------------------------------------


@dataclass(frozen=True)
class _Proposal:
    """What one rule would give, if it were the one that won."""

    rule: Rule
    shares: dict[int, int]

    @property
    def total(self) -> int:
        return sum(self.shares.values())

    @property
    def rank(self) -> tuple[int, int, int]:
        """Best deal first, then the contract's tie-break: priority, then id."""
        return (-self.total, self.rule.priority, self.rule.id)


def _slab(rule: Rule, spend_paise: int, units: int) -> Mapping[str, Any] | None:
    """The step of a laddered offer this cart has actually reached.

    Highest qualifying step wins - "buy 3 and above" beats "buy 1" when there
    are three. A ladder with no qualifying step is not a smaller reward, it is no
    offer at all, which is why this answers `None` rather than an empty dict.
    """
    if rule.trigger_type in (TRIGGER_NONE, TRIGGER_GROUP):
        return {}
    slabs = list(rule.trigger_config.get("slabs") or [])
    if rule.trigger_type == TRIGGER_SPEND:
        key, reached = "min_paise", spend_paise
    elif rule.trigger_type == TRIGGER_QTY:
        key, reached = "min_qty", units
    else:
        return None
    qualifying = sorted(
        (slab for slab in slabs if reached >= int(slab.get(key, 0))),
        key=lambda slab: int(slab.get(key, 0)),
    )
    return qualifying[-1] if qualifying else None


def _slab_for(rule: Rule, covered: Sequence[CartLine]) -> Mapping[str, Any] | None:
    """The step this set of lines reaches. Always measured on printed MRP (D5 Q13)."""
    return _slab(
        rule,
        sum(line.gross_paise for line in covered),
        sum(line.qty for line in covered),
    )


def _reward_params(rule: Rule, slab: Mapping[str, Any]) -> dict[str, Any]:
    """The reward's dials, with the reached step's own values on top.

    A ladder says "spend ₹7,000 → ₹600 off, spend ₹11,000 → ₹1,000 off": the
    reward *type* belongs to the rule and the *amount* belongs to the step.
    """
    params = dict(rule.reward_config or {})
    params.update({k: v for k, v in slab.items() if not k.startswith("min_")})
    return params


def _spread(
    amount_paise: int, weights: Sequence[tuple[int, int]], *, earlier_first: bool = False
) -> dict[int, int]:
    """Share a lump sum across lines in proportion to what they are worth.

    Largest-remainder, so the parts sum to exactly the lump and no paisa is
    invented or lost. The spare paisa goes to the dearest line first (then the
    earliest), which is arbitrary but has to be *decided*: two engines that round
    the spare paisa onto different lines are two engines that disagree.

    ``earlier_first`` is baseline B9 (GST after discount, ticket 11): a tie on
    the remainder goes to the earlier line, whatever the lines are worth.
    """
    total_weight = sum(weight for _, weight in weights)
    if amount_paise <= 0 or total_weight <= 0:
        return {}
    shares: dict[int, int] = {}
    remainders: list[tuple[int, int, int]] = []
    for line_no, weight in weights:
        numerator = amount_paise * weight
        shares[line_no] = numerator // total_weight
        remainders.append((numerator % total_weight, weight, line_no))
    spare = amount_paise - sum(shares.values())
    if earlier_first:
        remainders.sort(key=lambda row: (-row[0], row[2]))
    else:
        remainders.sort(key=lambda row: (-row[0], -row[1], row[2]))
    for _, _, line_no in remainders[:spare]:
        shares[line_no] += 1
    return shares


def _group_units(
    lines: Sequence[CartLine], base: Mapping[int, int], config: Mapping[str, Any]
) -> tuple[dict[int, int], dict[int, int]]:
    """Buy X get Y's groups: what goes free per line, and how many of each
    line's pieces are in a completed group at all.

    The pieces are taken dearest first and grouped ``buy + get`` at a time; the
    cheapest ``get`` of each group go free. Pieces left over after the last full
    group are in no group: they pay their own price, whatever the allocation.
    """
    buy = max(int(config.get("buy", 0)), 0)
    get = max(int(config.get("get", 0)), 0)
    if buy <= 0 or get <= 0:
        return {}, {}
    repeat = bool(config.get("repeat", True))

    units: list[tuple[int, int]] = []  # (what one piece costs, which line it is on)
    for line in lines:
        remaining = base.get(line.line_no, 0)
        if remaining <= 0:
            continue
        units.extend([(remaining // line.qty, line.line_no)] * line.qty)
    units.sort(key=lambda unit: (-unit[0], unit[1]))

    group = buy + get
    free: dict[int, int] = {}
    members: dict[int, int] = {}
    taken = 0
    while len(units) - taken >= group:
        for _, line_no in units[taken : taken + group]:
            members[line_no] = members.get(line_no, 0) + 1
        for price, line_no in units[taken + buy : taken + group]:
            free[line_no] = free.get(line_no, 0) + price
        taken += group
        if not repeat:
            break
    return free, members


def _spread_group_by_mrp(
    lines: Sequence[CartLine],
    base: Mapping[int, int],
    free: Mapping[int, int],
    members: Mapping[int, int],
) -> dict[int, int]:
    """Buy X get Y as several goods sold for one price (ticket 11, Circular 92).

    The price the customer pays for the grouped pieces is spread across every
    one of them in proportion to MRP, to the paisa, the spare paisa by largest
    remainder with ties to the earlier line (B9). Each line's discount is what
    its grouped pieces are worth less its share of that price. No piece is
    free; each takes its tax rate from its own share (§6; baseline, CA to confirm).
    """
    by_no = {line.line_no: line for line in lines}
    worth = {
        line_no: (base.get(line_no, 0) // by_no[line_no].qty) * units
        for line_no, units in sorted(members.items())
    }
    price = sum(worth.values()) - sum(free.values())
    shares = _spread(price, sorted(worth.items()), earlier_first=True)
    return {line_no: value - shares.get(line_no, 0) for line_no, value in worth.items()}


def _free_units(
    lines: Sequence[CartLine], base: Mapping[int, int], config: Mapping[str, Any]
) -> dict[int, int]:
    """Buy X get Y: dearest first, and the cheapest of each group goes free.

    The mechanic the whole trade uses, and the one the June meeting described in
    so many words - "the item carrying the lowest monetary value must be
    discounted by 100%". Written on *units* rather than lines, because a single
    line of three shirts is three qualifying pieces.
    """
    return _group_units(lines, base, config)[0]


def _shares_for(
    reward_type: str,
    rule: Rule,
    covered: Sequence[CartLine],
    base: Mapping[int, int],
    params: Mapping[str, Any],
    after_discount: bool = False,
) -> dict[int, int]:
    if reward_type == REWARD_PCT_OFF:
        percent = hundredths(params.get("percent", "0"))
        return {line.line_no: percent_of(base[line.line_no], percent) for line in covered}
    if reward_type == REWARD_AMT_OFF:
        amount = int(params.get("amount_paise") or 0)
        return _spread(
            amount,
            [(line.line_no, base[line.line_no]) for line in covered],
            earlier_first=after_discount,
        )
    if reward_type == REWARD_FIXED_PRICE:
        price = int(params.get("price_paise") or 0)
        return {
            line.line_no: max(base[line.line_no] // line.qty - price, 0) * line.qty
            for line in covered
        }
    if reward_type == REWARD_ITEM_FREE:
        if after_discount and allocation_of(rule) == ALLOCATION_BY_MRP:
            free, members = _group_units(covered, base, rule.trigger_config or {})
            return _spread_group_by_mrp(covered, base, free, members)
        return _free_units(covered, base, rule.trigger_config or {})
    return {}


def _propose(
    rule: Rule,
    lines: Sequence[CartLine],
    base: Mapping[int, int],
    day: date,
    after_discount: bool = False,
) -> _Proposal | None:
    """What this rule would do to these lines, given what is left of them.

    `base` is what each line still costs after earlier layers - the "already
    discounted price" D5 Q13 says an add-on works on. The *trigger*, though, is
    always measured on printed MRP, because that is the number on the placard and
    the number the customer counted (D5 Q13 again, the other half of it).
    """
    covered = [line for line in lines if covers(rule, line, day) and base.get(line.line_no, 0) > 0]
    if not covered:
        return None
    slab = _slab_for(rule, covered)
    if slab is None:  # the ladder's bottom step was never reached
        return None

    shares = _shares_for(
        rule.reward_type, rule, covered, base, _reward_params(rule, slab), after_discount
    )
    # No rule may give away more of a line than the line still costs.
    capped = {
        line_no: min(paise, base.get(line_no, 0)) for line_no, paise in shares.items() if paise > 0
    }
    return _Proposal(rule=rule, shares=capped) if capped else None


# --- gifts, which are earned rather than deducted --------------------------


def _split_gifts(rules: Sequence[Rule], cart: Cart) -> tuple[list[Rule], list[Entitlement]]:
    """Take the gift offers out of the discount competition (D5 Q11).

    A gift takes nothing off any line, so it cannot be ranked against a rule that
    does, and leaving it in the greedy loop would have it bidding nought for ever
    against offers that bid real money. It resolves once, here, on its own terms:
    the threshold is reached or it is not; the trolley is in stock or the counter
    has said it is not, in which case the offer's pre-decided fallback discount
    takes its place and competes normally from then on.
    """
    keep: list[Rule] = []
    earned: list[Entitlement] = []
    for rule in rules:
        if rule.reward_type != REWARD_GIFT:
            keep.append(rule)
            continue
        covered = [line for line in cart.lines if covers(rule, line, cart.day)]
        if not covered:
            continue
        slab = _slab_for(rule, covered)
        if slab is None:  # the spend threshold was not reached
            continue
        params = _reward_params(rule, slab)
        if rule.id not in cart.declined_gifts:
            earned.append(
                Entitlement(
                    offer_id=rule.id,
                    offer_name=rule.name,
                    barcode=str(params.get("gift_barcode") or ""),
                    token_price_paise=int(params.get("token_price_paise") or 0),
                )
            )
            continue
        fallback = params.get("fallback") or {}
        reward_type = str(fallback.get("reward_type") or "")
        if reward_type not in FALLBACK_REWARDS:
            # No fallback configured: the customer gets nothing, and that is a
            # gap in the rulebook for a human to close, not a crash here.
            continue
        keep.append(
            replace(
                rule,
                reward_type=reward_type,
                reward_config=dict(fallback.get("reward_config") or {}),
            )
        )
    return keep, earned


# --- the greedy loop -------------------------------------------------------


@dataclass
class _Awards:
    """What has been given away so far, and what it beat on the way."""

    discount: dict[int, int] = field(default_factory=dict)
    winner: dict[int, _Proposal] = field(default_factory=dict)
    stack: dict[int, list[tuple[_Proposal, int]]] = field(default_factory=dict)
    beat: dict[int, list[tuple[_Proposal, int]]] = field(default_factory=dict)

    def base_for(self, lines: Sequence[CartLine]) -> dict[int, int]:
        return {
            line.line_no: line.gross_paise - self.discount.get(line.line_no, 0) for line in lines
        }


def _run_layer(
    rules: Sequence[Rule],
    cart: Cart,
    awards: _Awards,
    *,
    exclusive: bool,
    after_discount: bool = False,
) -> None:
    """One layer, resolved to a fixed point.

    Every rule still standing bids on the lines nobody has claimed; the best bid
    is taken, its lines are claimed, and the survivors bid again on what is left.
    That second round is the reason this is a loop and not a sort: an offer that
    overlapped the winner on one line may still be the best thing available on
    the three lines the winner did not want.
    """
    claimed: set[int] = set()
    remaining = list(rules)
    first_round = True

    while remaining:
        open_lines = [line for line in cart.lines if line.line_no not in claimed]
        if not open_lines:
            break
        base = awards.base_for(open_lines)
        proposals = [
            proposal
            for proposal in (
                _propose(rule, open_lines, base, cart.day, after_discount) for rule in remaining
            )
            if proposal is not None
        ]
        if not proposals:
            break
        proposals.sort(key=lambda proposal: proposal.rank)
        best = proposals[0]

        # "What it beat" is the field of the first round: the candidates that
        # were on the table when the winner was picked. Later rounds are picking
        # over what is left, and calling those losers too would tell a store
        # manager the bill considered an offer it never could have applied.
        if first_round and exclusive:
            for other in proposals[1:]:
                for line_no, paise in sorted(other.shares.items()):
                    awards.beat.setdefault(line_no, []).append((other, paise))
            first_round = False

        for line_no, paise in best.shares.items():
            awards.discount[line_no] = awards.discount.get(line_no, 0) + paise
            if exclusive:
                awards.winner[line_no] = best
            else:
                awards.stack.setdefault(line_no, []).append((best, paise))
            claimed.add(line_no)
        remaining = [rule for rule in remaining if rule.id != best.rule.id]


def _evidence(line: CartLine, awards: _Awards) -> dict[str, Any]:
    """Why this line cost what it cost - the record B3 and the daily check read.

    Empty when nothing applied, which is the same `{}` the till writes when it
    has no rulebook at all: "no offer claimed" reads the same either way, and the
    accept pipeline does not have to tell the two apart.
    """
    saved = awards.discount.get(line.line_no, 0)
    if saved <= 0:
        return {}
    winner = awards.winner.get(line.line_no)
    return {
        "offer_id": winner.rule.id if winner else None,
        "offer_name": winner.rule.name if winner else "",
        "layer": winner.rule.layer if winner else "",
        "saved_paise": saved,
        "beat": [
            {"offer_id": p.rule.id, "offer_name": p.rule.name, "saved_paise": paise}
            for p, paise in awards.beat.get(line.line_no, [])
            if winner is None or p.rule.id != winner.rule.id
        ],
        "stack": [
            {
                "offer_id": p.rule.id,
                "offer_name": p.rule.name,
                "layer": p.rule.layer,
                "saved_paise": paise,
            }
            for p, paise in awards.stack.get(line.line_no, [])
        ],
    }


def resolve(cart: Cart, rules: Sequence[Rule], *, after_discount: bool = False) -> Resolution:
    """Price a cart against the rulebook. The one entry point.

    Rules arrive already narrowed to this store - the server does that once, and
    the till is only ever sent its own store's rules (D5 Q4, so that a new store
    is never auto-enrolled into a running offer). Their *dates*, though, are
    judged here against the cart's own day, so an offline counter starts and
    stops an offer on its own clock (grill Q3).

    ``after_discount`` is ticket 11 (GST after discount), only where the store's
    switch is on: a bill-level amount spreads its spare paisa to the earlier
    line (B9), a buy-X-get-Y spreads each group's price by MRP (unless the offer
    says ``free_piece``), and a bank offer that does not reduce value takes
    nothing off any line and is returned in ``bank_offers`` instead.
    """
    awards = _Awards()

    brand_rules = [rule for rule in rules if rule.layer == LAYER_BRAND]
    brand_rules, entitlements = _split_gifts(brand_rules, cart)
    _run_layer(brand_rules, cart, awards, exclusive=True, after_discount=after_discount)

    bank_offers: tuple[BankOffer, ...] = ()
    for layer in ADD_ON_LAYERS:
        layer_rules = [
            rule
            for rule in rules
            if rule.layer == layer and rule.combinable and rule.reward_type in ADD_ON_REWARDS
        ]
        if after_discount and layer == LAYER_BANK:
            bank_offers = _bank_layer_after_discount(layer_rules, cart, awards)
            continue
        _run_layer(layer_rules, cart, awards, exclusive=False, after_discount=after_discount)

    return Resolution(
        lines=tuple(
            LineOutcome(
                line_no=line.line_no,
                discount_paise=awards.discount.get(line.line_no, 0),
                offer_id=(
                    awards.winner[line.line_no].rule.id if line.line_no in awards.winner else None
                ),
                evidence=_evidence(line, awards),
            )
            for line in cart.lines
        ),
        entitlements=tuple(sorted(entitlements, key=lambda gift: gift.offer_id)),
        bank_offers=bank_offers,
        after_discount=after_discount,
    )


def _bank_layer_after_discount(
    rules: Sequence[Rule], cart: Cart, awards: _Awards
) -> tuple[BankOffer, ...]:
    """The bank layer with GST after discount (ticket 11, ST-POS-1).

    Resolved in one pass exactly as the bank layer always was - on what each
    line still costs after the brand and storewide offers, one bank offer a line
    - but on a copy. Then each winner is sorted by its own setting: one that
    reduces value becomes a line discount as before; every other one takes
    nothing off any line and becomes that offer's "bank offer" payment, so no
    taxable value moves.
    """
    if not rules:
        return ()
    shadow = _Awards(discount=dict(awards.discount))
    _run_layer(rules, cart, shadow, exclusive=False, after_discount=True)
    totals: dict[int, BankOffer] = {}
    for line_no, stacked in shadow.stack.items():
        for proposal, paise in stacked:
            rule = proposal.rule
            if reduces_value(rule):
                awards.discount[line_no] = awards.discount.get(line_no, 0) + paise
                awards.stack.setdefault(line_no, []).append((proposal, paise))
                continue
            seen = totals.get(rule.id)
            totals[rule.id] = BankOffer(
                offer_id=rule.id,
                offer_name=rule.name,
                amount_paise=(seen.amount_paise if seen else 0) + paise,
            )
    return tuple(totals[key] for key in sorted(totals))
