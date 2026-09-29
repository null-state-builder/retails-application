"""The tax a bill line should carry, under the version the bill falls in (ticket 03).

One object answers it for the accept pipeline's advisory check and for the
nightly daily check:

    books = StoreTaxBooks(store)            # one read of the switch and versions
    books.at(sale.billed_at)                # the version in force for that bill
    books.recorded(version, sale.billed_at) # the version the bill says it used
    rulebook.line_tax(hsn, net_paise, qty)  # -> split, and whether a rule matched

A bill's tax arithmetic is judged by the version it recorded (when this server
knows it), so a switch flipped or a version saved afterwards never re-judges it.
Only the accept pipeline asks whether the recorded version was the one in force
(`tax_version_mismatch`), because the switch is read as it stands now.

**Version 1 is today's calculation, untouched** (baseline B5): the dated slab
from ``GstSlab`` and ``sell.pricing.split_line``. A store with the switch off is
always on version 1. Saved versions (2 and up) apply only where the switch is on,
and use the value-before-tax rule (``split_by_value_before_tax``).

Nothing here refuses a bill (acceptance item 6). A line whose HSN has no rule
takes the version's own "no rule" rate and the bill is flagged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from django.utils import timezone

from masters.models import Store
from masters.tax_settings import (
    DEFAULT_ROUND_TOTAL_PAISE,
    LEGACY_VERSION,
    SavedTaxVersion,
    TaxRule,
    in_force,
    saved_versions,
    store_uses_settings,
)
from sell.pricing import TaxSplit, split_by_value_before_tax, split_inclusive, split_line
from sell.services.resolve import slab_for


@dataclass(frozen=True)
class LineTax:
    split: TaxSplit
    #: The saved rule the line fell under; ``None`` on version 1 or with no rule.
    rule: TaxRule | None
    #: True when a saved version had no rule for this HSN (the bill is flagged).
    rule_missing: bool

    @property
    def rule_kind(self) -> str:
        """Which rule taxed the line, as the line records it (ticket 12)."""
        if self.rule is not None:
            return self.rule.kind
        return RULE_NONE if self.rule_missing else RULE_SLAB

    @property
    def rule_hsn_prefix(self) -> str:
        return self.rule.hsn_prefix if self.rule is not None else ""


#: What a bill line records as the rule that taxed it (ticket 12), beside the
#: version: a saved rule's own kind (``price_line``/``flat_rate``), or one of these.
RULE_SLAB = "slab"  # version 1, the tax slab table
RULE_NONE = "no_rule"  # a saved version with no rule for the HSN: the "no rule" rate, flagged
RULE_UNKNOWN = "unknown"  # the bill names a version this server does not hold


@dataclass(frozen=True)
class TaxRulebook:
    """One version, on one business day."""

    day: date
    saved: SavedTaxVersion | None

    @property
    def version(self) -> int:
        return self.saved.version if self.saved is not None else LEGACY_VERSION

    @property
    def round_total_paise(self) -> int:
        """What the invoice total is rounded to (ticket 11): version 1 and every
        version saved before the option existed round to the rupee."""
        return self.saved.round_total_paise if self.saved is not None else DEFAULT_ROUND_TOTAL_PAISE

    def rule_record(self, hsn: str) -> tuple[str, str]:
        """``(kind, hsn_prefix)`` of the rule that taxes this HSN, as a line records it.

        Only the HSN chooses the rule; the price only chooses the rate within it.
        """
        if self.saved is None:
            return RULE_SLAB, ""
        rule = self.saved.rule_for(hsn)
        return (rule.kind, rule.hsn_prefix) if rule is not None else (RULE_NONE, "")

    def line_tax(self, hsn: str, inclusive_paise: int, qty: int) -> LineTax:
        if self.saved is None:
            return LineTax(
                split=split_line(inclusive_paise, qty, slab_for(hsn, self.day)),
                rule=None,
                rule_missing=False,
            )
        rule = self.saved.rule_for(hsn)
        if rule is None:
            return LineTax(
                split=split_inclusive(inclusive_paise, self.saved.unmatched_rate),
                rule=None,
                rule_missing=True,
            )
        if rule.is_flat:
            # Ticket 12: the rate schedule - the scheduled rate whatever the price.
            return LineTax(
                split=split_inclusive(inclusive_paise, rule.rate_above),
                rule=rule,
                rule_missing=False,
            )
        return LineTax(
            split=split_by_value_before_tax(
                inclusive_paise,
                qty,
                threshold_paise=rule.threshold_paise,
                rate_below=rule.rate_below,
                rate_above=rule.rate_above,
            ),
            rule=rule,
            rule_missing=False,
        )


class StoreTaxBooks:
    """Everything one store's bills can be taxed under, read once."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.uses_settings = store_uses_settings(store)
        self.versions = saved_versions(store.tenant_id)

    def at(self, billed_at: datetime) -> TaxRulebook:
        """The version in force for a bill made at ``billed_at`` here."""
        day = timezone.localdate(billed_at)
        if not self.uses_settings:
            return TaxRulebook(day=day, saved=None)
        return TaxRulebook(day=day, saved=in_force(self.versions, day, billed_at))

    def recorded(self, version: int, billed_at: datetime) -> TaxRulebook | None:
        """The version a bill says it was taxed under, or None if unknown here.

        A bill's tax is judged by the version it recorded (§6 principle 2): a
        later switch flip or a newer version never re-judges it. Whether that
        was the right version is `tax_version_mismatch`'s question, not this.
        """
        day = timezone.localdate(billed_at)
        if version == LEGACY_VERSION:
            return TaxRulebook(day=day, saved=None)
        saved = next((v for v in self.versions if v.version == version), None)
        return TaxRulebook(day=day, saved=saved) if saved is not None else None
