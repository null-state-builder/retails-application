"""Deterministic PT valuation for the pinned fashion profile (design §3.4, Phase 1 §7.2).

Pure Python: no Django, no HTTP, no floats. Money is integer paise, percentages
are ``Decimal`` with two places, and every rounding is half-up. Expected numbers
for tests come from ``docs/features/goods-to-store-acceptance/calculation-vectors.md``,
which were worked out by hand - never from this code.

Three directions and nothing else:

* base-to-ticket: ``P RATE = round_paise(BASIC × (1 + t))``,
  ``MRP = round_rupee(P RATE × (1 + g) ÷ (1 − m))``, slab chosen by candidate MRP;
* ticket-to-purchase: ``P RATE = round_paise(MRP ÷ (1 + g) × (1 − m))``, ``BASIC = P RATE``;
* both-supplied: ``P RATE = round_paise(BASIC × (1 + t))``, MRP kept.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

BASE_TO_TICKET = "base_to_ticket"
TICKET_TO_PURCHASE = "ticket_to_purchase"
BOTH_SUPPLIED = "both_supplied"
DIRECTIONS = (BASE_TO_TICKET, TICKET_TO_PURCHASE, BOTH_SUPPLIED)

MAX_PAISE = 99_999_999_999
MAX_PERCENT = Decimal("99.99")
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Slab:
    lower_paise: int
    upper_paise: int | None
    lower_inclusive: bool
    upper_inclusive: bool
    input_pct: Decimal
    output_pct: Decimal

    def contains(self, mrp_paise: int) -> bool:
        above = (
            mrp_paise >= self.lower_paise if self.lower_inclusive else mrp_paise > self.lower_paise
        )
        if self.upper_paise is None:
            return above
        below = (
            mrp_paise <= self.upper_paise if self.upper_inclusive else mrp_paise < self.upper_paise
        )
        return above and below


@dataclass(frozen=True)
class Rates:
    transport_pct: Decimal
    pricing_margin_pct: Decimal


@dataclass(frozen=True)
class RowInput:
    direction: str
    basic_paise: int | None = None
    mrp_paise: int | None = None
    check_p_rate_paise: int | None = None
    check_mrp_paise: int | None = None
    tolerance_minor_units: int = 0


@dataclass(frozen=True)
class CalcIssue:
    code: str
    field: str
    message: str


@dataclass(frozen=True)
class Calculated:
    direction: str
    p_rate_paise: int | None
    mrp_paise: int | None
    basic_paise: int | None
    input_tax_pct: Decimal | None
    output_tax_pct: Decimal | None
    margin_pct: Decimal | None
    transport_pct: Decimal
    pricing_margin_pct: Decimal
    slab_index: int | None
    issues: tuple[CalcIssue, ...] = field(default_factory=tuple)

    @property
    def valid(self) -> bool:
        return not self.issues


def round_half_up(value: Decimal, places: str = "1") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def fraction(pct: Decimal) -> Decimal:
    return pct / HUNDRED


def _percent_issue(name: str, pct: Decimal) -> CalcIssue | None:
    if pct < 0 or pct > MAX_PERCENT or pct != pct.quantize(Decimal("0.01")):
        return CalcIssue("PERCENT_OUT_OF_RANGE", name, f"{name} must be between 0.00 and 99.99")
    return None


def _money_issue(name: str, paise: int | None) -> CalcIssue | None:
    if paise is None:
        return None
    if paise > MAX_PAISE:
        return CalcIssue("MONEY_OUT_OF_RANGE", name, f"{name} is above ₹999,999,999.99")
    if paise < 0:
        return CalcIssue("MONEY_OUT_OF_RANGE", name, f"{name} cannot be negative")
    return None


def cost_from_basic(basic_paise: int, transport_pct: Decimal) -> int:
    return int(round_half_up(Decimal(basic_paise) * (1 + fraction(transport_pct))))


def mrp_from_cost(cost_paise: int, gst_pct: Decimal, margin_pct: Decimal) -> int:
    rupees = Decimal(cost_paise) * (1 + fraction(gst_pct)) / (1 - fraction(margin_pct)) / HUNDRED
    return int(round_half_up(rupees)) * 100


def cost_from_mrp(mrp_paise: int, gst_pct: Decimal, margin_pct: Decimal) -> int:
    return int(
        round_half_up(Decimal(mrp_paise) / (1 + fraction(gst_pct)) * (1 - fraction(margin_pct)))
    )


def displayed_margin(mrp_paise: int, cost_paise: int) -> Decimal:
    return round_half_up(Decimal(mrp_paise - cost_paise) / Decimal(mrp_paise) * HUNDRED, "0.01")


def slab_for(mrp_paise: int, slabs: Sequence[Slab]) -> int | None:
    matches = [index for index, slab in enumerate(slabs) if slab.contains(mrp_paise)]
    return matches[0] if len(matches) == 1 else None


def calculate(row: RowInput, rates: Rates, slabs: Sequence[Slab]) -> Calculated:
    issues: list[CalcIssue] = []
    for name, pct in (
        ("transport_pct", rates.transport_pct),
        ("pricing_margin_pct", rates.pricing_margin_pct),
    ):
        problem = _percent_issue(name, pct)
        if problem:
            issues.append(problem)
    for name, paise in (("basic_paise", row.basic_paise), ("mrp_paise", row.mrp_paise)):
        problem = _money_issue(name, paise)
        if problem:
            issues.append(problem)
    if row.direction not in DIRECTIONS:
        issues.append(CalcIssue("DIRECTION_INVALID", "direction", "unknown formula direction"))

    def done(
        cost: int | None,
        mrp: int | None,
        basic: int | None,
        slab_index: int | None,
    ) -> Calculated:
        slab = slabs[slab_index] if slab_index is not None else None
        final = list(issues)
        if cost is not None:
            problem = _money_issue("p_rate_paise", cost)
            if problem:
                final.append(problem)
            elif cost == 0:
                final.append(CalcIssue("COST_ZERO", "p_rate_paise", "P RATE cannot be zero"))
        if mrp is not None:
            problem = _money_issue("mrp_paise", mrp)
            if problem:
                final.append(problem)
            elif mrp == 0:
                final.append(CalcIssue("MRP_ZERO", "mrp_paise", "MRP cannot be zero"))
        if cost is not None and mrp is not None and cost > mrp:
            final.append(CalcIssue("COST_ABOVE_MRP", "p_rate_paise", "P RATE is above MRP"))
        tolerance = max(0, row.tolerance_minor_units)
        if (
            row.check_p_rate_paise is not None
            and cost is not None
            and abs(row.check_p_rate_paise - cost) > tolerance
        ):
            final.append(
                CalcIssue(
                    "DERIVED_MISMATCH",
                    "p_rate_paise",
                    "the supplied P RATE differs from the calculation",
                )
            )
        if (
            row.direction == BASE_TO_TICKET
            and row.check_mrp_paise is not None
            and mrp is not None
            and abs(row.check_mrp_paise - mrp) > tolerance
        ):
            final.append(
                CalcIssue(
                    "DERIVED_MISMATCH", "mrp_paise", "the supplied MRP differs from the calculation"
                )
            )
        margin = (
            displayed_margin(mrp, cost)
            if cost is not None and mrp and not any(i.code in {"MONEY_OUT_OF_RANGE"} for i in final)
            else None
        )
        return Calculated(
            direction=row.direction,
            p_rate_paise=cost,
            mrp_paise=mrp,
            basic_paise=basic,
            input_tax_pct=slab.input_pct if slab else None,
            output_tax_pct=slab.output_pct if slab else None,
            margin_pct=margin,
            transport_pct=rates.transport_pct,
            pricing_margin_pct=rates.pricing_margin_pct,
            slab_index=slab_index,
            issues=tuple(final),
        )

    if issues:
        return done(None, row.mrp_paise, row.basic_paise, None)

    if row.direction == BASE_TO_TICKET:
        if row.basic_paise is None:
            issues.append(CalcIssue("REQUIRED", "basic_paise", "BASIC is required"))
            return done(None, None, None, None)
        cost = cost_from_basic(row.basic_paise, rates.transport_pct)
        if cost > MAX_PAISE:
            return done(cost, None, row.basic_paise, None)
        candidates = [
            (mrp_from_cost(cost, slab.output_pct, rates.pricing_margin_pct), index)
            for index, slab in enumerate(slabs)
        ]
        qualifying = [(mrp, index) for mrp, index in candidates if slabs[index].contains(mrp)]
        if not qualifying:
            issues.append(
                CalcIssue("MRP_SLAB_GAP", "mrp_paise", "no tax slab contains the calculated MRP")
            )
            return done(cost, None, row.basic_paise, None)
        mrp, index = min(qualifying)
        return done(cost, mrp, row.basic_paise, index)

    if row.mrp_paise is None:
        issues.append(CalcIssue("REQUIRED", "mrp_paise", "MRP is required"))
        return done(None, None, row.basic_paise, None)
    supplied_slab = slab_for(row.mrp_paise, slabs)
    if supplied_slab is None:
        issues.append(
            CalcIssue("TAX_SLAB_MISSING", "hsn", "no single tax slab applies to this MRP")
        )
        return done(None, row.mrp_paise, row.basic_paise, None)
    if row.direction == TICKET_TO_PURCHASE:
        cost = cost_from_mrp(
            row.mrp_paise, slabs[supplied_slab].output_pct, rates.pricing_margin_pct
        )
        return done(cost, row.mrp_paise, cost, supplied_slab)
    if row.basic_paise is None:
        issues.append(CalcIssue("REQUIRED", "basic_paise", "BASIC is required"))
        return done(None, row.mrp_paise, None, supplied_slab)
    cost = cost_from_basic(row.basic_paise, rates.transport_pct)
    return done(cost, row.mrp_paise, row.basic_paise, supplied_slab)
