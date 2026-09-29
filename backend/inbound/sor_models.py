"""What SOR ageing needs to know about a delivery (store operations ticket 24, ST-BRD-5).

Both are facts about one arrival - the brand's shipment - written once each time
somebody records them and never edited. The newest row for an arrival is what
stands; an older one stays as the history of what was said before.

* ``BrandDispatchDate`` - the day the brand dispatched the goods, from its challan
  or invoice. Typed at receiving, or later for a delivery received without one.
  An SOR piece's 5 and 6 months count from it. No row: the date is unknown, and
  the piece is listed as such, never aged from a guess.
* ``SorBrandInvoice`` - the brand's invoice for what it had supplied on sale or
  return in that delivery. Once it is recorded the pieces are the brand's supply
  invoiced (CGST Act s.31(7)) and leave the SOR ageing alert.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import EvidenceRow


class BrandDispatchDate(EvidenceRow):
    arrival = models.ForeignKey(
        "inbound.Arrival", on_delete=models.PROTECT, related_name="dispatch_dates"
    )
    dispatch_date = models.DateField()
    #: Why an earlier date was changed; blank for the first one.
    reason = models.CharField(max_length=240, blank=True)

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["arrival", "recorded_at"])]


class SorBrandInvoice(EvidenceRow):
    arrival = models.ForeignKey(
        "inbound.Arrival", on_delete=models.PROTECT, related_name="sor_invoices"
    )
    invoice_number = models.CharField(max_length=80)
    invoice_date = models.DateField()
    #: Why an earlier invoice was replaced; blank for the first one.
    reason = models.CharField(max_length=240, blank=True)

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["arrival", "recorded_at"])]
