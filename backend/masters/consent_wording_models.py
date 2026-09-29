"""One saved version of the customer consent wording (store operations ticket 15).

The customer answers three questions on the customer display (§6 ST-CMP-6):
send my bill, whether they are under 18, and send me offers. Each answer
records the wording version it was given under, so what a customer agreed to
can always be read back word for word.

Admin changes the wording by saving a new version; an old version is never
changed (append-only table, as ``TaxSettingVersion``).

**Version 1 has no row.** It is the first wording, baseline B13, written in
``masters.consent_wording.VERSION_ONE``. Saved versions start at 2.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from core.goods_base import APPEND_ONLY, TenantOwned


class ConsentWording(TenantOwned):
    PROTECTION: ClassVar[str] = APPEND_ONLY

    #: 2 and up. Version 1 is the built-in first wording (see module docstring).
    version = models.PositiveIntegerField()
    #: The "send my bill" question.
    bill_text = models.CharField(max_length=200)
    #: The under-18 question asked before offers.
    age_text = models.CharField(max_length=200)
    #: The "send me offers" question.
    offers_text = models.CharField(max_length=200)
    #: Why this version was saved, in the saver's words.
    note = models.CharField(max_length=240)
    actor = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "version"], name="uq_consentwording_number"),
            models.CheckConstraint(
                condition=models.Q(version__gte=2), name="ck_consentwording_after_first"
            ),
        ]

    def __str__(self) -> str:
        return f"Consent wording v{self.version}"
