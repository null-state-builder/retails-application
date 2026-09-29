"""GST after discount: one bill, priced the way the till prices it (ticket 11).

Store operations PRD §6 ST-CMP-1 and the tax baselines (each one "baseline, CA
to confirm", gate: CA sign-off, §32). Pure - no ORM - so the golden bills in
``sell/tax_vectors/*.json`` run through exactly this function, and the till's
twin (``priceCart`` in ``till/cart.ts``) runs the very same files.

Where a store's ``gst-after-discount`` switch is on, and a bill says it was
priced that way, the server works the bill out again with this and compares it
with what the till printed, to the paisa. A difference is a flag on the bill,
never a refusal (acceptance item 6).

The steps, in the order the till takes them:

1. The offers engine, resolved ``after_discount`` (``offers.resolution``): a
   bill-level amount spread by value, spare paisa by largest remainder with ties
   to the earlier line (B9); buy 2 get 1 spread by MRP; a bank offer that does
   not reduce value set aside as a payment.
2. Each line: MRP x qty, less the offer, less the cashier's own discount, is the
   price after discount. Its tax comes from its own per-piece price, by the rule
   of the bill's tax version (ticket 03), rounded to the paisa on the line.
3. The bill: the lines less anything given back, then the round-off line to the
   version's unit (the nearest rupee), half up. What the customer pays is that
   total less the bank offers - they are payments, not price cuts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from offers.resolution import BankOffer, Resolution
from sell.pricing import TaxSplit, round_off, split_inclusive

#: ``(hsn, net_paise, qty) -> split``: the bill's tax version, one line at a time.
LineTaxer = Callable[[str, int, int], TaxSplit]


@dataclass(frozen=True)
class BillLineIn:
    line_no: int
    hsn: str
    qty: int
    mrp_paise: int
    #: What the cashier keyed in, over the whole line - never the offer's part.
    manual_disc_paise: int = 0
    #: Ticket 22: a line taxed at its own rate whatever the version says (an
    #: alteration charge, SAC 9988). No offer reaches it.
    fixed_rate: Decimal | None = None


@dataclass(frozen=True)
class PricedLine:
    line_no: int
    #: The offers engine's part of the line's discount.
    offer_paise: int
    #: The whole discount the line carries: the offer's and the cashier's.
    disc_paise: int
    net_paise: int
    gst_rate: Decimal
    gst_paise: int


@dataclass(frozen=True)
class PricedBill:
    lines: tuple[PricedLine, ...]
    bank_offers: tuple[BankOffer, ...]
    subtotal_paise: int
    round_paise: int
    #: The invoice total: the lines less what came back, plus the round-off.
    net_paise: int
    gst_paise: int

    @property
    def bank_offer_paise(self) -> int:
        return sum(offer.amount_paise for offer in self.bank_offers)

    @property
    def payable_paise(self) -> int:
        """What the customer pays with money: the total less the bank offers."""
        return self.net_paise - self.bank_offer_paise


def price_bill(
    lines: Sequence[BillLineIn],
    resolution: Resolution,
    line_tax: LineTaxer,
    *,
    round_total_paise: int,
    returned_paise: int = 0,
    returned_gst_paise: int = 0,
) -> PricedBill:
    """The bill as the till must have printed it, given the engine's answer."""
    offers = resolution.by_line()
    priced: list[PricedLine] = []
    for line in lines:
        outcome = None if line.fixed_rate is not None else offers.get(line.line_no)
        offer = outcome.discount_paise if outcome else 0
        net = line.mrp_paise * line.qty - offer - line.manual_disc_paise
        if net > 0 and line.qty > 0:
            split = (
                split_inclusive(net, line.fixed_rate)
                if line.fixed_rate is not None
                else line_tax(line.hsn, net, line.qty)
            )
            rate, gst = split.rate, split.gst_paise
        else:
            rate, gst = Decimal("0.00"), 0
        priced.append(
            PricedLine(
                line_no=line.line_no,
                offer_paise=offer,
                disc_paise=offer + line.manual_disc_paise,
                net_paise=net,
                gst_rate=rate,
                gst_paise=gst,
            )
        )
    subtotal = sum(line.net_paise for line in priced) - returned_paise
    rounding = round_off(subtotal, round_total_paise)
    return PricedBill(
        lines=tuple(priced),
        bank_offers=resolution.bank_offers,
        subtotal_paise=subtotal,
        round_paise=rounding,
        net_paise=subtotal + rounding,
        gst_paise=sum(line.gst_paise for line in priced) - returned_gst_paise,
    )
