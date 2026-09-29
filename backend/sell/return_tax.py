"""Exchange and return tax: the rules, as plain arithmetic (store operations ticket 13).

Store operations PRD §6 ST-CMP-2 and the tax baselines (each one "baseline, CA
to confirm", gate: CA sign-off, §32). Pure - no ORM - so the golden cases in
``sell/return_tax_vectors/*.json`` run through exactly these functions, and the
till's twin (``till/returnTax.ts``) runs the very same files.

What a piece coming back is worth, and what it reverses:

* **The returned piece is reversed at the rate and value on its original bill**
  (frozen line evidence, never today's rule): what the customer paid for that
  quantity (``refund_share``, D2) with the tax inside it at the original line's
  rate. The new piece is taxed at today's rule and value, as any sale is.
* **After the credit-note deadline** - 30 November after the financial year of
  the original bill ends, or the date Accounts recorded the annual return as
  filed for that GSTIN and year if that is earlier (CGST Act s.34(2), B10) - the
  customer still gets the value back, but with **no tax reduction**: the leg
  carries no tax, and the bill is flagged.
* **A bill paid partly by a bank offer** (B60): the customer is credited only
  what they paid for the piece - its value less its share of the bank offer. The
  tax reversed is still on the full value, because the bank offer never lowered
  the taxable value (ticket 11). The bank's share is reversed against
  ``BANK_OFFER_RECEIVABLE`` and flagged for Accounts.
* **Returns stay within the GSTIN that issued the bill**, unless the version's
  ``cross_gstin_returns`` option says otherwise. A store with no GSTIN recorded
  cannot issue a credit note at all, so it is refused rather than guessed.

The options live on the tax settings version (``masters.tax_settings``): these
functions take them already read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from core.fiscal import financial_year, financial_year_months
from sell.pricing import split_inclusive
from sell.refund_math import refund_share

#: Refusal codes, shared with the till and the golden cases.
CROSS_GSTIN = "cross_gstin"
NO_GSTIN = "no_gstin"


@dataclass(frozen=True)
class ReturnTaxOptions:
    """The two choices of a tax version this ticket reads (defaults: the baselines)."""

    #: Returns across GSTINs: off (baseline, CA to confirm).
    cross_gstin_returns: bool = False
    #: The date Accounts recorded each annual return as filed:
    #: ``{gstin: {"26-27": date}}``. Empty means none recorded (B10).
    annual_return_filed: Mapping[str, Mapping[str, date]] = field(default_factory=dict)


def options_from_json(raw: Mapping[str, object] | None) -> ReturnTaxOptions:
    """A tax version's options (``SavedTaxVersion.options_json``) as this module
    reads them. Absent keys are the baselines; a date that does not read is left
    out - the save already refuses one (``masters.tax_settings.parse_options``),
    so only a hand-edited row could carry it."""
    raw = raw or {}
    filed: dict[str, dict[str, date]] = {}
    years_by_gstin = raw.get("annual_return_filed")
    if isinstance(years_by_gstin, Mapping):
        for gstin, years in years_by_gstin.items():
            if not isinstance(years, Mapping):
                continue
            for fy, value in years.items():
                try:
                    filed.setdefault(str(gstin), {})[str(fy)] = date.fromisoformat(str(value))
                except ValueError:
                    continue
    return ReturnTaxOptions(
        cross_gstin_returns=raw.get("cross_gstin_returns") is True, annual_return_filed=filed
    )


def credit_note_deadline(original_day: date, gstin: str, options: ReturnTaxOptions) -> date:
    """The last day a credit note may reduce tax for a bill of ``original_day`` (B10).

    30 November after the bill's financial year ends, or the filing date Accounts
    recorded for that GSTIN and year when it is earlier. Never the later of the two.
    """
    fy = financial_year(original_day)
    year_ends = financial_year_months(fy)[-1]  # 1 March of the closing year
    statutory = date(year_ends.year, 11, 30)
    filed = (options.annual_return_filed.get(gstin) or {}).get(fy)
    return min(statutory, filed) if filed is not None else statutory


def is_late(original_day: date, exchange_day: date, gstin: str, options: ReturnTaxOptions) -> bool:
    """Is a piece taken back on ``exchange_day`` past its credit-note deadline?"""
    return exchange_day > credit_note_deadline(original_day, gstin, options)


def gstin_refusal(original_gstin: str, billing_gstin: str, options: ReturnTaxOptions) -> str | None:
    """Why this store may not take back a bill of that GSTIN, as a code, or None.

    ``no_gstin``: one of the two stores has no GSTIN recorded, so no credit note
    can say whose tax it reduces. ``cross_gstin``: the bill is another GSTIN's and
    cross-GSTIN returns are off.
    """
    if not billing_gstin.strip() or not original_gstin.strip():
        return NO_GSTIN
    if original_gstin.strip() != billing_gstin.strip() and not options.cross_gstin_returns:
        return CROSS_GSTIN
    return None


def bank_shares(lines: Sequence[tuple[int, int]], bank_offer_paise: int) -> dict[int, int]:
    """Each sold line's share of a bill's bank offers, ``{line_no: paise}`` (B60).

    Spread by value, to the paisa, spare paisa by largest remainder with a tie to
    the earlier line (B9) - the same spread a bill-level amount takes. ``lines``
    is ``(line_no, net_paise)`` for the original bill's sold lines.
    """
    total = sum(net for _, net in lines)
    if bank_offer_paise <= 0 or total <= 0:
        return {line_no: 0 for line_no, _ in lines}
    shares = {line_no: bank_offer_paise * net // total for line_no, net in lines}
    spare = bank_offer_paise - sum(shares.values())
    by_remainder = sorted(lines, key=lambda row: (-((bank_offer_paise * row[1]) % total), row[0]))
    for line_no, _ in by_remainder[:spare]:
        shares[line_no] += 1
    return shares


@dataclass(frozen=True)
class OriginalLine:
    """One sold line of the original bill, as it stands now."""

    line_no: int
    qty: int
    net_paise: int
    gst_rate: Decimal
    returned_qty: int = 0
    returned_paise: int = 0
    #: The line's share of the bill's bank offers (``bank_shares``).
    bank_offer_paise: int = 0
    #: What earlier returns of this line already took off that share.
    returned_bank_paise: int = 0


@dataclass(frozen=True)
class Leg:
    """One piece (or pieces) of a line coming back."""

    original_line: int
    qty: int
    #: What the pieces are worth back: the value the credit note reverses.
    refund_paise: int
    gst_rate: Decimal
    #: The tax reversed: the original rate on that value, or nothing when late.
    gst_paise: int
    #: The bank offer's part of that value, which the customer never paid.
    bank_offer_paise: int
    late: bool

    @property
    def credit_paise(self) -> int:
        """What the customer is credited: the value less the bank's part."""
        return self.refund_paise - self.bank_offer_paise


def return_leg(line: OriginalLine, qty: int, *, late: bool) -> Leg:
    """The leg for ``qty`` pieces of ``line``: value, tax reversed, bank part."""
    refund = refund_share(
        paid_paise=line.net_paise,
        line_qty=line.qty,
        returning=qty,
        returned_qty=line.returned_qty,
        returned_paise=line.returned_paise,
    )
    bank = refund_share(
        paid_paise=line.bank_offer_paise,
        line_qty=line.qty,
        returning=qty,
        returned_qty=line.returned_qty,
        returned_paise=line.returned_bank_paise,
    )
    return Leg(
        original_line=line.line_no,
        qty=qty,
        refund_paise=refund,
        gst_rate=line.gst_rate,
        gst_paise=0 if late else split_inclusive(refund, line.gst_rate).gst_paise,
        bank_offer_paise=bank,
        late=late,
    )


@dataclass(frozen=True)
class CreditNoteTotals:
    """The credit note an exchange issues: what it reverses."""

    value_paise: int
    gst_paise: int
    bank_offer_paise: int
    late: bool

    @property
    def taxable_paise(self) -> int:
        return self.value_paise - self.gst_paise

    @property
    def credit_paise(self) -> int:
        return self.value_paise - self.bank_offer_paise


def credit_note_totals(legs: Sequence[Leg]) -> CreditNoteTotals:
    return CreditNoteTotals(
        value_paise=sum(leg.refund_paise for leg in legs),
        gst_paise=sum(leg.gst_paise for leg in legs),
        bank_offer_paise=sum(leg.bank_offer_paise for leg in legs),
        late=any(leg.late for leg in legs),
    )
