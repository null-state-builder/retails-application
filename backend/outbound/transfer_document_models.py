"""The document one shipment travels with (store operations PRD ST-TRF-2; ticket 36).

A dispatch between two sites under the same GSTIN carries a delivery challan in
the sending site's ``XXX/DC/2627/n`` series. A dispatch between two GSTINs is a
supply between distinct persons and carries a tax invoice from the sending GSTIN
to the receiving one, in the sending site's ``XXX/26-27/n`` series. Which one is
worked out from the two registrations themselves, never from a state name
(overall PRD R-FIN-017).

Written once, inside the dispatch that it goes with, and never changed: the
table is append-only in the database (no UPDATE, no DELETE for the application
role), so a corrected document is a new document, never an edit of this one.
Everything it prints - names, GSTINs, lines, values, tax - is frozen here, so a
later rename, rate change or cost correction never rewrites a paper that left
with the goods.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from core.goods_base import APPEND_ONLY, TenantOwned


class TransferDocumentKind(models.TextChoices):
    DELIVERY_CHALLAN = "delivery_challan", "Delivery challan"
    TAX_INVOICE = "tax_invoice", "Tax invoice"


class TransferTaxKind(models.TextChoices):
    #: A delivery challan: no supply, no tax.
    NONE = "none", "No tax"
    #: Two GSTINs in one state.
    CGST_SGST = "cgst_sgst", "CGST and SGST"
    #: Two GSTINs in two states.
    IGST = "igst", "IGST"


class TransferDocument(TenantOwned):
    PROTECTION: ClassVar[str] = APPEND_ONLY

    dispatch = models.OneToOneField(
        "outbound.TransferDispatch", on_delete=models.PROTECT, related_name="document"
    )
    kind = models.CharField(max_length=16, choices=TransferDocumentKind.choices)
    #: ``XXX/DC/2627/n`` or ``XXX/26-27/n``, as ``masters.document_series`` issued it.
    number = models.CharField(max_length=16)
    #: The dispatch's business day (India): the document's date.
    issued_on = models.DateField()
    source_site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    destination_site = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="+"
    )
    #: Both ends as they stood when the goods left: ``{code, name, gstin,
    #: state_code, legal_name}``.
    parties = models.JSONField()
    tax_kind = models.CharField(max_length=10, choices=TransferTaxKind.choices)
    #: The tax-settings version the rates came from (1 is the slab table).
    tax_version = models.PositiveIntegerField(null=True, blank=True)
    #: ``[{sku_id, description, hsn, qty, unit_cost_paise, taxable_paise,
    #: rate, cgst_paise, sgst_paise, igst_paise}]``, money as integer paise.
    #: A delivery challan's value is ``None`` where the cost is not known.
    lines = models.JSONField()
    taxable_paise = models.BigIntegerField(null=True, blank=True)
    tax_paise = models.BigIntegerField(default=0)
    total_paise = models.BigIntegerField(null=True, blank=True)
    issued_by = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="uq_transferdocument_number"),
        ]
