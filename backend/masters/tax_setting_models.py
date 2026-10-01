"""One saved version of the tax settings (store operations PRD §6, ticket 03).

Every tax and document choice is a versioned setting (§6 principle 1). A save
never changes an old version: it adds a new row with the next number and the date
it applies from, and the table is append-only in the database (no UPDATE, no
DELETE for the application role), so "an old version is never changed" is a
property of the table rather than a promise of the code.

**Version 1 has no row.** It is the dated slab table (``masters.GstSlab``) that
every bill was taxed under before this ticket, read exactly as the till and the
accept pipeline always read it (baseline B5). A store whose tax-settings switch
is off stays on version 1 whatever is saved here; the rows below apply only where
the switch is on. The CA sign-off gate on that switch closed on 1 October 2026
(§32); it stays off until Admin turns it on for a store.

**Room left for later tickets** (do not build them here):

* ticket 12 (accessories, built): a rule's ``kind`` is ``price_line`` or
  ``flat_rate`` - the rate schedule's one rate per HSN (``masters.tax_settings``).
* tickets 11 and 13: choices that are not per-HSN rates (buy-2-get-1 allocation
  method, whether a bank offer reduces the taxable value, rounding, cross-GSTIN
  returns, the annual-return filing date per GSTIN per year) go in ``options``,
  one key each, so they are versioned and dated with everything else.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from core.goods_base import APPEND_ONLY, TenantOwned


class TaxSettingVersion(TenantOwned):
    PROTECTION: ClassVar[str] = APPEND_ONLY

    #: 2 and up. Version 1 is the slab table and has no row (see module docstring).
    version = models.PositiveIntegerField()
    #: The first business day (India) a bill may be taxed under this version.
    applies_from = models.DateField()
    #: Per-HSN rules: ``[{kind, hsn_prefix, name, threshold_paise, rate_below,
    #: rate_above}]`` (a ``flat_rate`` rule adds ``rate``). Rates are two-decimal
    #: strings, money is integer paise.
    rules = models.JSONField(default=list)
    #: The rate a line takes when no rule matches its HSN. The bill is flagged
    #: ``tax_rule_missing`` every time it is used; it is never silent.
    unmatched_rate = models.DecimalField(max_digits=5, decimal_places=2)
    #: Non-rate choices for later tickets (11, 13), one key each. Empty today.
    options = models.JSONField(default=dict, blank=True)
    #: Why this version was saved, in the saver's words (overall PRD §8.9).
    note = models.CharField(max_length=240)
    actor = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "version"], name="uq_taxsettingversion_number"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=2), name="ck_taxsettingversion_after_slab_table"
            ),
        ]

    def __str__(self) -> str:
        return f"Tax settings v{self.version} (from {self.applies_from})"
