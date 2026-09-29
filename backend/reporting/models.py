"""The reporting store: copies of billing facts, kept apart from billing (R-AN-003).

Reports never read the billing tables while somebody waits for a page. A job
(``reporting.sales_facts.refresh``, on the worker's clock and from
``manage.py refresh_sales_report``) copies what each accepted bill says into the
tables below, and every report reads only these. The copy is derived and may be
rebuilt at any time from the bills (overall PRD §8.1): nothing here is a source
of truth, and nothing here is ever written back.

Every row carries the business day in India time and the store, which is the
one index every standard report filters on.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from core.money import MoneyField


class SalesLineFact(models.Model):
    """One bill line - or one salesperson's share of a split line - as a report reads it.

    Signs are applied: a returned piece is negative pieces and negative value, so
    a plain sum nets returns out. A split line (ticket 08) is two rows, one per
    person, whose pieces and money add back to the line exactly; an unsplit line
    is one row with ``share_percent`` 100.

    ``pieces_x100`` is pieces times the person's percentage, so a 60/40 split of
    one piece is 60 and 40 and the two still add to one whole piece (100).
    ``cost_paise`` is None where the line had no cost yet (sold before its goods
    were priced): unknown, never nought.
    """

    class Source(models.TextChoices):
        SALE = "sale", "A bill (sold lines and exchange legs)"
        OLD_RETURN = "old_return", "An old standalone return"

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    day = models.DateField(help_text="The business day, India time.")
    source = models.CharField(max_length=12, choices=Source.choices)
    doc_id = models.BigIntegerField(help_text="The bill (or old return) this row came from.")
    line_id = models.BigIntegerField()
    share_position = models.PositiveSmallIntegerField(default=0)
    share_percent = models.PositiveSmallIntegerField(default=100)
    #: The bill this row counts towards, or None: only a sold piece makes a bill.
    bill_id = models.BigIntegerField(null=True, blank=True)
    brand = models.CharField(max_length=120, blank=True, default="")
    # Stable scope identity; historical brand text is display evidence only.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    category = models.CharField(max_length=120, blank=True, default="")
    #: The season as the bill line names it (code or name), "" where it names none.
    #: The inventory report (ticket 43) reads a brand's terms by brand and season.
    season = models.CharField(max_length=120, blank=True, default="")
    #: ``staff:<id>``, ``old:<id>`` (a frozen old salesperson row) or "" (nobody named).
    salesperson_key = models.CharField(max_length=64, blank=True, default="")
    salesperson_name = models.CharField(max_length=160, blank=True, default="")
    salesperson_code = models.CharField(max_length=40, blank=True, default="")
    pieces_x100 = models.BigIntegerField()
    gross_paise = MoneyField(default=0)
    disc_paise = MoneyField(default=0)
    value_paise = MoneyField(default=0)
    gst_paise = MoneyField(default=0)
    cost_paise = MoneyField(null=True, blank=True)

    class Meta:
        db_table = "report_sales_line_fact"
        ordering = ["store_id", "day", "id"]
        indexes = [
            models.Index(fields=["store", "day"], name="rpt_saleline_store_day_idx"),
            models.Index(fields=["source", "doc_id"], name="rpt_saleline_doc_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.source}:{self.doc_id}/{self.line_id}#{self.share_position}"


class SalesTenderFact(models.Model):
    """Money taken on a bill by one way of paying, or given back on an old return.

    ``amount_paise`` is negative only for an old standalone return's refund.
    """

    GIVEN_BACK = "given_back"

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    day = models.DateField()
    source = models.CharField(max_length=12, choices=SalesLineFact.Source.choices)
    doc_id = models.BigIntegerField()
    bill_id = models.BigIntegerField(null=True, blank=True)
    mode = models.CharField(max_length=12)
    amount_paise = MoneyField()

    class Meta:
        db_table = "report_sales_tender_fact"
        ordering = ["store_id", "day", "id"]
        indexes = [
            models.Index(fields=["store", "day"], name="rpt_tender_store_day_idx"),
            models.Index(fields=["source", "doc_id"], name="rpt_tender_doc_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.source}:{self.doc_id} {self.mode} {self.amount_paise}"


class ReportRefresh(models.Model):
    """How fresh one reporting copy is: the as-of time every report states.

    ``as_of`` is when the last successful refresh started: every bill the server
    had accepted by then is in. ``failed_at``/``failure`` say when a later run
    failed and why, so a report can say its copy is older than it should be.
    """

    key = models.CharField(max_length=40, primary_key=True)
    as_of = models.DateTimeField(null=True, blank=True)
    took_ms = models.IntegerField(default=0)
    documents = models.IntegerField(default=0, help_text="Documents the last run re-read.")
    failed_at = models.DateTimeField(null=True, blank=True)
    failure = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        db_table = "report_refresh"

    def __str__(self) -> str:
        return f"{self.key} as of {self.as_of}"


class OfferSimLineFact(models.Model):
    """One sold bill line, as the offer engine needs to see it (ticket 30, ST-OFR-3).

    The sales copy above holds money by salesperson; an offer is judged on the
    piece - its brand, category, style, size, colour, barcode, season, MRP and
    whether it may be discounted at all - so this copy keeps exactly that. Only
    sold lines of accepted, numbered, uncancelled bills: a simulation asks what
    an offer would have given on the pieces people bought.

    ``brand_key`` is the brand reduced the way the engine compares brands (upper
    case, letters and digits only), so a brand offer reads only its own lines.
    ``no_discount`` is the piece's flag as it stands at the copy. ``cost_paise``
    is None where the line had no cost yet: unknown, never nought.
    """

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    day = models.DateField(help_text="The business day, India time.")
    bill_id = models.BigIntegerField()
    line_id = models.BigIntegerField()
    line_no = models.IntegerField()
    brand = models.CharField(max_length=120, blank=True, default="")
    # Stable scope identity; historical brand text is display evidence only.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    brand_key = models.CharField(max_length=120, blank=True, default="")
    item = models.CharField(max_length=120, blank=True, default="")
    design = models.CharField(max_length=120, blank=True, default="")
    size = models.CharField(max_length=24, blank=True, default="")
    color = models.CharField(max_length=60, blank=True, default="")
    barcode = models.CharField(max_length=64, blank=True, default="")
    season = models.CharField(max_length=120, blank=True, default="")
    qty = models.IntegerField()
    mrp_paise = MoneyField(default=0)
    disc_paise = MoneyField(default=0)
    net_paise = MoneyField(default=0)
    gst_paise = MoneyField(default=0)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    cost_paise = MoneyField(null=True, blank=True)
    no_discount = models.BooleanField(default=False)
    #: Ticket 31 (return on each offer): the offer the bill says won this line,
    #: and each offer's part of the line's discount as ``{offer id: paise}``
    #: (the winner and every add-on stacked on it; the part no offer gave is not
    #: in it). ``offer_parts_unread`` marks a line whose offer record could not
    #: be read against its discount: its whole discount is then the winner's.
    offer_id = models.BigIntegerField(null=True, blank=True)
    offer_parts = models.JSONField(default=dict, blank=True)
    offer_parts_unread = models.BooleanField(default=False)

    class Meta:
        db_table = "report_offer_sim_line_fact"
        ordering = ["store_id", "day", "bill_id", "line_no"]
        indexes = [
            models.Index(fields=["store", "day"], name="rpt_offersim_store_day_idx"),
            models.Index(fields=["brand_key", "day"], name="rpt_offersim_brand_day_idx"),
            models.Index(fields=["bill_id"], name="rpt_offersim_bill_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.bill_id}/{self.line_no}"


class OfferSimulation(models.Model):
    """One run of a draft offer against past bills - an estimate, kept as run.

    ``result`` holds each period's figures **per store**, so a reader is shown
    only the stores in their own scope (``reporting.offer_simulation.read``).
    ``offer_updated_at`` is the offer as it stood when this ran: a later change
    to the offer makes this estimate stale, and the page says so.
    """

    offer = models.ForeignKey("offers.Offer", on_delete=models.CASCADE, related_name="+")
    offer_updated_at = models.DateTimeField()
    run_at = models.DateTimeField(auto_now_add=True)
    run_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    #: The bills copy's as-of time: every bill accepted by then is in.
    as_of = models.DateTimeField(null=True, blank=True)
    formula_version = models.CharField(max_length=40)
    took_ms = models.IntegerField(default=0)
    result = models.JSONField(default=dict)

    class Meta:
        db_table = "report_offer_simulation"
        ordering = ["-run_at", "-id"]
        indexes = [models.Index(fields=["offer", "-run_at"], name="rpt_offersim_offer_idx")]

    def __str__(self) -> str:
        return f"offer {self.offer_id} simulated {self.run_at}"


# --- GST reports (store operations ticket 47, ST-RPT-5) ------------------------------
#
# Copied by ``reporting.gst_facts`` on the worker's clock, like the sales copy.
# Every money figure is a positive magnitude; ``kind`` says whether a row is an
# invoice or a credit note. The GSTIN is the store's registration when the row
# was copied, since a bill does not record one (baseline B73).


class GstSupply(models.TextChoices):
    B2B = "b2b", "B2B (buyer GSTIN on the bill)"
    B2C = "b2c", "B2C"


class GstSplit(models.TextChoices):
    INTRA = "intra", "CGST + SGST (same state)"
    INTER = "inter", "IGST (another state)"


class GstLineFact(models.Model):
    """One taxed line: a piece sold on an invoice, or given back on a credit note.

    ``late`` marks a piece given back after the credit-note deadline: its value
    was given with no tax reduction (ticket 13), so the outward summary leaves
    it out. A return taken before ticket 13's credit notes (netted on the bill)
    is a credit line like any other.
    """

    class Kind(models.TextChoices):
        INVOICE = "invoice", "Sold on an invoice"
        CREDIT = "credit", "Given back (credit note)"

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    gstin = models.CharField(max_length=15, blank=True, default="")
    day = models.DateField(help_text="The document's business day, India time.")
    source = models.CharField(max_length=12, choices=SalesLineFact.Source.choices)
    doc_id = models.BigIntegerField(help_text="The bill (or old return) this row came from.")
    line_id = models.BigIntegerField()
    kind = models.CharField(max_length=8, choices=Kind.choices)
    supply = models.CharField(max_length=3, choices=GstSupply.choices)
    split = models.CharField(max_length=5, choices=GstSplit.choices)
    hsn = models.CharField(max_length=24, blank=True, default="")
    rate = models.DecimalField(max_digits=5, decimal_places=2)
    pieces = models.IntegerField()
    taxable_paise = MoneyField(default=0)
    igst_paise = MoneyField(default=0)
    cgst_paise = MoneyField(default=0)
    sgst_paise = MoneyField(default=0)
    tax_paise = MoneyField(default=0)
    value_paise = MoneyField(default=0)
    late = models.BooleanField(default=False)

    class Meta:
        db_table = "report_gst_line_fact"
        ordering = ["store_id", "day", "id"]
        indexes = [
            models.Index(fields=["store", "day"], name="rpt_gstline_store_day_idx"),
            models.Index(fields=["source", "doc_id"], name="rpt_gstline_doc_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.source}:{self.doc_id}/{self.line_id}"


class GstDocumentFact(models.Model):
    """One tax document with its values: a B2B invoice, or a credit note.

    ``source`` says where it came from: ``sale`` (a B2B bill), ``exchange_cn``
    (ticket 13's credit note beside a new invoice), ``netted`` (pieces given
    back on a bill that issued no credit note, before ticket 13's switch) or
    ``old_return`` (a standalone return from before #273). ``doc_id`` is the
    bill's id for the first three and the return's for the last.

    IRN: ``irn_status`` is the IRN queue's word for a B2B invoice, ``untracked``
    for a credit note against a B2B bill (no IRN queue for credit notes yet,
    ST-CMP-4) and blank where no IRN applies.
    """

    class Kind(models.TextChoices):
        INVOICE = "invoice", "Tax invoice"
        CREDIT_NOTE = "credit_note", "Credit note"

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    gstin = models.CharField(max_length=15, blank=True, default="")
    day = models.DateField()
    source = models.CharField(max_length=12)
    doc_id = models.BigIntegerField()
    kind = models.CharField(max_length=12, choices=Kind.choices)
    number = models.CharField(max_length=128, blank=True, default="")
    original_number = models.CharField(max_length=128, blank=True, default="")
    supply = models.CharField(max_length=3, choices=GstSupply.choices)
    split = models.CharField(max_length=5, choices=GstSplit.choices)
    buyer_gstin = models.CharField(max_length=15, blank=True, default="")
    pieces = models.IntegerField(default=0)
    taxable_paise = MoneyField(default=0)
    igst_paise = MoneyField(default=0)
    cgst_paise = MoneyField(default=0)
    sgst_paise = MoneyField(default=0)
    tax_paise = MoneyField(default=0)
    value_paise = MoneyField(default=0)
    late = models.BooleanField(default=False)
    irn_status = models.CharField(max_length=10, blank=True, default="")
    irn = models.CharField(max_length=64, blank=True, default="")
    irn_due_on = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "report_gst_document_fact"
        ordering = ["store_id", "day", "id"]
        indexes = [
            models.Index(fields=["store", "day"], name="rpt_gstdoc_store_day_idx"),
            models.Index(fields=["source", "doc_id"], name="rpt_gstdoc_doc_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.number or self.source + ':' + str(self.doc_id)}"


class GstNumberFact(models.Model):
    """One number of a document series, used or cancelled, for GSTR-1 Table 13.

    ``origin`` and ``origin_ref`` name what the row was copied from, so a
    refresh replaces exactly it: ``number`` (the new-format register,
    ``IssuedDocumentNumber``), ``sale`` (a bill's number in today's format),
    ``cn`` (an exchange credit note in today's format), ``store_credit`` (a
    store-credit note) or ``return`` (an old standalone return).

    ``period`` is the first day of the month the number is reported in.
    ``unused_offline`` marks a number of an offline till's block cancelled
    unused at month end. ``store`` is empty for head office's own numbers
    (debit notes carry head office's prefix for a GSTIN).
    """

    class Status(models.TextChoices):
        ISSUED = "issued", "Issued"
        CANCELLED = "cancelled", "Cancelled"

    store = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.DO_NOTHING, related_name="+"
    )
    gstin = models.CharField(max_length=15, blank=True, default="")
    nature = models.CharField(max_length=20)
    #: The number with its running part as ``n``, e.g. ``DEA/26-27/n``.
    series = models.CharField(max_length=128)
    n = models.BigIntegerField()
    number = models.CharField(max_length=128)
    status = models.CharField(max_length=10, choices=Status.choices)
    period = models.DateField()
    unused_offline = models.BooleanField(default=False)
    origin = models.CharField(max_length=12)
    origin_ref = models.CharField(max_length=64)

    class Meta:
        db_table = "report_gst_number_fact"
        ordering = ["gstin", "nature", "series", "n"]
        indexes = [
            models.Index(fields=["period", "store"], name="rpt_gstno_period_store_idx"),
            models.Index(fields=["origin", "origin_ref"], name="rpt_gstno_origin_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.number} ({self.status})"


class GstOpenBlockFact(models.Model):
    """An offline till's block of invoice numbers that month end has not closed.

    Until it closes, the block's unused numbers are not yet recorded as
    cancelled, so Table 13 for that month is not final. Rebuilt whole on every
    refresh (there are only a few open blocks at any time).
    """

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    gstin = models.CharField(max_length=15, blank=True, default="")
    month = models.DateField()
    first_number = models.CharField(max_length=16)
    last_number = models.CharField(max_length=16)

    class Meta:
        db_table = "report_gst_open_block_fact"
        ordering = ["month", "store_id", "id"]

    def __str__(self) -> str:
        return f"{self.first_number}-{self.last_number} ({self.month:%Y-%m})"


class GiftItcFact(models.Model):
    """One gift piece tag (ticket 14) as the Gift Stock report reads it.

    Copied from ``sell.GiftPiece`` with the bill's number and the line's
    description, leaving out tags on a cancelled bill. ``cost_paise`` and
    ``itc_paise`` are None where unknown (``missing`` says why) - never nought.
    ``returned`` counts pieces given back since against this line; the figures
    stay as they are (baseline B98).
    """

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    gstin = models.CharField(max_length=15, blank=True, default="")
    day = models.DateField(help_text="The day the piece left stock, India time.")
    tag_id = models.BigIntegerField(unique=True)
    doc_id = models.BigIntegerField(help_text="The bill the piece left on.")
    line_id = models.BigIntegerField()
    doc_number = models.CharField(max_length=128, blank=True, default="")
    barcode = models.CharField(max_length=64, blank=True, default="")
    brand = models.CharField(max_length=120, blank=True, default="")
    # Stable scope identity; historical brand text is display evidence only.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    item = models.CharField(max_length=120, blank=True, default="")
    hsn = models.CharField(max_length=24, blank=True, default="")
    pieces = models.IntegerField()
    source = models.CharField(max_length=12)
    offer_name = models.CharField(max_length=200, blank=True, default="")
    #: The input tax rates of the pieces' receipt layers, as written ("5", "5, 12").
    input_tax_pct = models.CharField(max_length=40, blank=True, default="")
    cost_paise = MoneyField(null=True, blank=True)
    itc_paise = MoneyField(null=True, blank=True)
    missing = models.CharField(max_length=12, blank=True, default="")
    returned = models.IntegerField(default=0)

    class Meta:
        db_table = "report_gift_itc_fact"
        ordering = ["store_id", "day", "id"]
        indexes = [models.Index(fields=["store", "day"], name="rpt_giftitc_store_day_idx")]

    def __str__(self) -> str:
        return f"gift {self.doc_number or self.doc_id}/{self.line_id}"


class DiscountFundingFact(models.Model):
    """One funded part of a bill line's discount, as the funding report reads it (ticket 25).

    Copied from ``sell_sale_line_funding`` by ``reporting.funding_facts``. Signs
    are applied: a part given back is negative, so a plain sum nets returns out.
    ``brand_paise`` and ``kdps_paise`` are None where the split is unknown, with
    the reason kept; ``discount_paise`` is always the part's whole discount.
    """

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    day = models.DateField(help_text="The bill's business day, India time.")
    bill_id = models.BigIntegerField()
    line_id = models.BigIntegerField()
    part = models.PositiveSmallIntegerField()
    #: 1 for a sold line, -1 for a piece given back.
    sign = models.SmallIntegerField()
    #: The brand as the bill line names it, and the brand it was matched to (null
    #: where the line's brand is not one brand in the list). Claims read the id.
    brand = models.CharField(max_length=120, blank=True, default="")
    brand_ref_id = models.BigIntegerField(null=True, blank=True)
    offer_id = models.BigIntegerField(null=True, blank=True)
    offer_name = models.CharField(max_length=160, blank=True, default="")
    funder = models.CharField(max_length=8, blank=True, default="")
    discount_paise = MoneyField(default=0)
    brand_paise = MoneyField(null=True, blank=True)
    kdps_paise = MoneyField(null=True, blank=True)
    unknown_reason = models.CharField(max_length=16, blank=True, default="")

    class Meta:
        db_table = "report_discount_funding_fact"
        ordering = ["store_id", "day", "id"]
        indexes = [
            models.Index(fields=["store", "day"], name="rpt_funding_store_day_idx"),
            models.Index(fields=["bill_id"], name="rpt_funding_bill_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.bill_id}/{self.line_id}#{self.part}"


class ShrinkageLineFact(models.Model):
    """One line of an approved loss typed as shrinkage (ticket 44, ST-INV-4).

    Two sources, both goods-v1 documents the Owner approved and that still stand
    (not reversed): a **shrinkage adjustment** (recorded pieces lost, usually
    found short at a count) and a **write-off** whose reason is one of the
    shrinkage reasons (``KDPS_SHRINKAGE_WRITEOFF_REASONS``). An adjust-down (a
    counting or recording error) is not shrinkage and is never copied.

    ``cost_paise`` is the pieces' own recorded layer cost, as the document
    valued them; None where any piece had no recorded cost (unknown, never
    nought). ``brand`` and ``category`` are named as the till names them on a
    bill, so they line up with the sales copy. Rebuilt whole on every refresh:
    there are few such documents, and a reversal simply drops out.
    """

    class Source(models.TextChoices):
        SHRINKAGE = "shrinkage", "A shrinkage adjustment"
        WRITEOFF = "writeoff", "A write-off typed as shrinkage"

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    day = models.DateField(help_text="The day the loss was approved, India time.")
    source = models.CharField(max_length=12, choices=Source.choices)
    document_id = models.UUIDField()
    number = models.CharField(max_length=128, blank=True, default="")
    line_key = models.UUIDField()
    reason_code = models.CharField(max_length=60, blank=True, default="")
    sku_id = models.UUIDField(null=True, blank=True)
    brand = models.CharField(max_length=120, blank=True, default="")
    # Stable scope identity; historical brand text is display evidence only.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    category = models.CharField(max_length=120, blank=True, default="")
    pieces = models.IntegerField()
    cost_paise = MoneyField(null=True, blank=True)

    class Meta:
        db_table = "report_shrinkage_line_fact"
        ordering = ["store_id", "day", "id"]
        indexes = [models.Index(fields=["store", "day"], name="rpt_shrink_store_day_idx")]

    def __str__(self) -> str:
        return f"{self.source}:{self.number or self.document_id}/{self.line_key}"


class MarginShareFact(models.Model):
    """One sold or given-back line's split between the brand and KDPS (ticket 27).

    Copied from ``sell_sale_line_margin_share`` by ``reporting.margin_facts``.
    Signs are applied: a piece given back is negative, so a plain sum nets returns
    out. ``kdps_paise`` and ``brand_paise`` are None where the line is not split,
    with the reason kept; ``value_paise`` is always the line's value without GST.
    """

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    day = models.DateField(help_text="The bill's business day, India time.")
    bill_id = models.BigIntegerField()
    doc_number = models.CharField(max_length=128, blank=True, default="")
    line_id = models.BigIntegerField()
    #: 1 for a sold line, -1 for a piece given back.
    sign = models.SmallIntegerField()
    #: The brand as the bill line names it, and the brand list's row where known.
    brand = models.CharField(max_length=120, blank=True, default="")
    brand_ref_id = models.BigIntegerField(null=True, blank=True)
    season = models.CharField(max_length=120, blank=True, default="")
    barcode = models.CharField(max_length=64, blank=True, default="")
    #: Pieces, signed.
    qty = models.IntegerField()
    model = models.CharField(max_length=12, blank=True, default="")
    margin_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    value_paise = MoneyField(default=0)
    kdps_paise = MoneyField(null=True, blank=True)
    brand_paise = MoneyField(null=True, blank=True)
    unknown_reason = models.CharField(max_length=16, blank=True, default="")

    class Meta:
        db_table = "report_margin_share_fact"
        ordering = ["store_id", "day", "id"]
        indexes = [
            models.Index(fields=["store", "day"], name="rpt_margin_store_day_idx"),
            models.Index(fields=["bill_id"], name="rpt_margin_bill_idx"),
            models.Index(fields=["brand_ref_id", "day"], name="rpt_margin_brand_day_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.bill_id}/{self.line_id}"


# --- Inventory report (store operations ticket 43, ST-RPT-2) --------------------------
#
# Copied by ``reporting.inventory_facts`` on the worker's clock. Goods-v1 stores
# only: a store still on the legacy stock contract keeps no origins or
# acceptance, so it has a snapshot row that says so and no stock rows.


class InventorySnapshot(models.Model):
    """One store's stock as it stood at the end of one day (the last refresh that day).

    Written for every selling store on every day the copy is refreshed, so a day
    with nothing on hand is still a day with a known stock of nought. A day with
    no row was never snapshotted: its stock is unknown, never guessed.
    """

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    day = models.DateField(help_text="The business day, India time.")
    taken_at = models.DateTimeField()
    #: False for a legacy store: it keeps no origins, so nothing is counted.
    goods_records = models.BooleanField(default=True)
    #: The in-season days with no sale that made a piece dead stock, or None
    #: when the ageing policy has none (then nothing is counted as dead).
    idle_days = models.IntegerField(null=True, blank=True)
    #: Ticket 29: this run also kept the stock by barcode (``InventoryItemFact``).
    #: False on snapshots taken before, whose stock by barcode is not known.
    items_kept = models.BooleanField(default=False)

    class Meta:
        db_table = "report_inventory_snapshot"
        ordering = ["store_id", "day"]
        constraints = [
            models.UniqueConstraint(fields=["store", "day"], name="uq_rpt_invsnap_store_day"),
        ]

    def __str__(self) -> str:
        return f"{self.store_id} on {self.day}"


class InventoryStockFact(models.Model):
    """Good, accepted pieces of one brand, category and season at a store at a day's end.

    The pieces ticket 33 ages (``stockledger.goods_ageing.store_ageing``).
    ``cost_paise`` is their recorded layer cost (every origin has one). A piece
    is **dead** when it is in season and its item has had no sale here for the
    ageing policy's days (ticket 33's rule, the same the Stock Ageing page flags).
    """

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    day = models.DateField()
    brand = models.CharField(max_length=120, blank=True, default="")
    # Stable scope identity; historical brand text is display evidence only.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    category = models.CharField(max_length=120, blank=True, default="")
    season_id = models.BigIntegerField(null=True, blank=True)
    season = models.CharField(max_length=120, blank=True, default="")
    pieces = models.IntegerField()
    cost_paise = MoneyField(default=0)
    dead_pieces = models.IntegerField(default=0)
    dead_cost_paise = MoneyField(default=0)
    #: Ticket 45 (brand performance, stock age): pieces of a season that has
    #: ended (ticket 33 counts them aged), and each piece's days at this store
    #: added up, so pieces / piece_days is their average age here. None on a
    #: snapshot taken before ticket 45: unknown, never nought.
    ended_pieces = models.IntegerField(null=True, blank=True)
    piece_days = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "report_inventory_stock_fact"
        ordering = ["store_id", "day", "id"]
        indexes = [models.Index(fields=["store", "day"], name="rpt_invstock_store_day_idx")]

    def __str__(self) -> str:
        return f"{self.store_id} {self.day} {self.brand}/{self.category}/{self.season}"


class InventoryReceiptFact(models.Model):
    """Pieces of one item and season put away good at a store (ticket 43's "received").

    From an acceptance event (append-only) of a receipt PT, an opening PT or a
    transfer. A customer return put back and goods coming back from a vendor
    return are not received. ``event_id`` with ``origin_id`` names the row, so
    a refresh that re-reads an event replaces exactly it.
    """

    class Source(models.TextChoices):
        RECEIPT = "receipt", "A receipt from a vendor"
        OPENING = "opening", "Opening stock"
        TRANSFER = "transfer", "A transfer in"

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    day = models.DateField(help_text="The day the pieces were put away, India time.")
    event_id = models.UUIDField()
    origin_id = models.UUIDField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices)
    sku_id = models.UUIDField(null=True, blank=True)
    brand = models.CharField(max_length=120, blank=True, default="")
    # Stable scope identity; historical brand text is display evidence only.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    category = models.CharField(max_length=120, blank=True, default="")
    season_id = models.BigIntegerField(null=True, blank=True)
    season = models.CharField(max_length=120, blank=True, default="")
    pieces = models.IntegerField()

    class Meta:
        db_table = "report_inventory_receipt_fact"
        ordering = ["store_id", "day", "id"]
        indexes = [
            models.Index(fields=["store", "day"], name="rpt_invrcpt_store_day_idx"),
            models.Index(fields=["event_id"], name="rpt_invrcpt_event_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.source} {self.event_id}"


class ExceptionFact(models.Model):
    """One counter exception, as the exceptions report reads it (ticket 48, ST-RPT-6).

    Kinds: a cancelled bill, a line priced by hand at the counter, a line with a
    manual discount, a manager's PIN use,
    a cash variance, missing bill numbers, a bill with a return without a bill,
    a bill over the no-bill return cap, a bill that reached head office late,
    and a store's checklist items not ticked by their time (ticket 49).

    ``staff_key`` is the login the exception is about (``user:<id>``) - the
    cashier who billed, the manager whose PIN approved, the person who counted -
    or "" for one that is about the store (missing bill numbers). ``staff_name``
    is that person's name when the copy was made. ``events`` is how many it
    counts (a hole of five numbers is 5); ``value_paise`` the money it is about,
    as a positive amount, None where there is none. ``bill_id`` is the bill a
    row came from (None for a cash count or missing numbers), so a changed bill's
    rows are replaced on the next refresh.
    """

    class Kind(models.TextChoices):
        CANCELLED_BILL = "cancelled_bill", "Cancelled bill"
        PRICE_TYPED = "price_typed", "Price typed at the counter"
        MANUAL_DISCOUNT = "manual_discount", "Manual discount"
        PIN_USE = "pin_use", "Manager PIN use"
        CASH_VARIANCE = "cash_variance", "Cash variance"
        NUMBER_HOLE = "number_hole", "Bill-number hole"
        NO_BILL_RETURN = "no_bill_return", "Return without a bill"
        NO_BILL_CAP = "no_bill_cap", "Over the no-bill return cap"
        LATE_SYNC = "late_sync", "Late sync"
        CHECKLIST_MISSED = "checklist_missed", "Checklist items missed"

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    day = models.DateField(help_text="The day the exception is about, India time.")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    bill_id = models.BigIntegerField(null=True, blank=True)
    staff_key = models.CharField(max_length=64, blank=True, default="")
    staff_name = models.CharField(max_length=160, blank=True, default="")
    events = models.IntegerField(default=1)
    value_paise = MoneyField(null=True, blank=True)
    #: The bill number, count or flag the row came from.
    reference = models.CharField(max_length=128, blank=True, default="")

    class Meta:
        db_table = "report_exception_fact"
        ordering = ["store_id", "day", "id"]
        indexes = [
            models.Index(fields=["store", "day"], name="rpt_exception_store_day_idx"),
            models.Index(fields=["bill_id"], name="rpt_exception_bill_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.reference}"


# --- Brand reports (store operations ticket 29, ST-BRD-2) ------------------------------
#
# Each brand's Sale and SOH reports, made in the brand's own layout. Two copies
# feed them (bill lines by brand, and each store's stock by barcode, both kept
# apart from billing and receiving); the layouts and the files the monthly run
# made are the only records the feature itself writes.


class BrandSaleLineFact(models.Model):
    """One goods line of an accepted bill, as a brand's Sale report lists it.

    Signed: a piece sold is positive, a piece given back (an exchange leg, or an
    old standalone return) is negative, at what the customer paid for it. Only
    numbered, uncancelled documents; an alteration charge is not a brand's line.
    ``brand_key`` is the bill's brand reduced as the offer engine reduces it
    (upper case, letters and digits), so one brand's report finds its own lines
    however the tag spelt the brand.
    """

    class Source(models.TextChoices):
        SALE = "sale", "Bill"
        OLD_RETURN = "old_return", "Old return document"

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    day = models.DateField(help_text="The business day, India time.")
    source = models.CharField(max_length=12, choices=Source.choices)
    doc_id = models.BigIntegerField()
    doc_number = models.CharField(max_length=128, blank=True, default="")
    line_id = models.BigIntegerField()
    line_no = models.IntegerField(default=0)
    brand = models.CharField(max_length=120, blank=True, default="")
    # Stable scope identity; historical brand text is display evidence only.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    brand_key = models.CharField(max_length=120, blank=True, default="")
    item = models.CharField(max_length=120, blank=True, default="")
    design = models.CharField(max_length=120, blank=True, default="")
    size = models.CharField(max_length=24, blank=True, default="")
    color = models.CharField(max_length=60, blank=True, default="")
    barcode = models.CharField(max_length=64, blank=True, default="")
    season = models.CharField(max_length=120, blank=True, default="")
    qty = models.IntegerField(help_text="Pieces; negative when given back.")
    #: None for an old return whose original bill line is not held: unknown, never nought.
    mrp_paise = MoneyField(null=True, blank=True, help_text="MRP of one piece.")
    gross_paise = MoneyField(default=0, help_text="MRP times pieces, signed.")
    disc_paise = MoneyField(default=0)
    value_paise = MoneyField(default=0, help_text="What the customer paid, GST inside.")
    gst_paise = MoneyField(default=0)

    class Meta:
        db_table = "report_brand_sale_line_fact"
        ordering = ["store_id", "day", "doc_id", "line_no", "id"]
        indexes = [
            models.Index(
                fields=["store", "brand_key", "day"], name="rpt_brandsale_store_brand_idx"
            ),
            models.Index(fields=["source", "doc_id"], name="rpt_brandsale_doc_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.doc_number}/{self.line_no}"


class InventoryItemFact(models.Model):
    """Good, accepted pieces of one barcode (at one MRP) standing at a store at a day's end.

    Written by the inventory copy beside its brand totals (``InventoryStockFact``),
    from the same pieces. Only the last snapshot of each month is kept per store:
    a later day of the same month replaces an earlier one, so a past month keeps
    its closing stock and the running month its newest.
    """

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    day = models.DateField()
    brand = models.CharField(max_length=120, blank=True, default="")
    # Stable scope identity; historical brand text is display evidence only.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    brand_key = models.CharField(max_length=120, blank=True, default="")
    item = models.CharField(max_length=120, blank=True, default="")
    design = models.CharField(max_length=120, blank=True, default="")
    size = models.CharField(max_length=24, blank=True, default="")
    color = models.CharField(max_length=60, blank=True, default="")
    barcode = models.CharField(max_length=64, blank=True, default="")
    season = models.CharField(max_length=120, blank=True, default="")
    #: The MRP its origin carries; None where the origin has none (never nought).
    mrp_paise = MoneyField(null=True, blank=True)
    pieces = models.IntegerField()

    class Meta:
        db_table = "report_inventory_item_fact"
        ordering = ["store_id", "day", "brand_key", "design", "size", "barcode"]
        indexes = [
            models.Index(fields=["store", "day", "brand_key"], name="rpt_invitem_store_day_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.store_id} {self.barcode} x{self.pieces} on {self.day}"


class BrandReportKind(models.TextChoices):
    SALE = "sale", "Sale"
    SOH = "soh", "SOH"


class BrandReportLayout(models.Model):
    """One brand's saved layout for one report: columns, order, headers, formats, file type.

    A row with no brand is the company's standard layout, used by every brand
    with no layout of its own; each company (tenant) has its own. The first two
    are the KDPS Sale and SOH sheets. A company with no standard row yet uses the
    built-in KDPS sheets unsaved (``brand_layouts.standard``); saving one makes it.
    ``layout`` is checked by ``reporting.brand_layouts.clean`` before it is saved;
    ``revision`` goes up by one on every save, so a stale screen is refused.
    """

    tenant = models.ForeignKey("masters.Tenant", on_delete=models.PROTECT, related_name="+")
    brand = models.ForeignKey(
        "masters.Brand", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    kind = models.CharField(max_length=4, choices=BrandReportKind.choices)
    name = models.CharField(max_length=120)
    layout = models.JSONField()
    revision = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "report_brand_layout"
        ordering = ["tenant_id", "kind", "brand_id"]
        constraints = [
            models.UniqueConstraint(fields=["brand", "kind"], name="uq_rpt_brandlayout_brand_kind"),
            models.UniqueConstraint(
                fields=["tenant", "kind"],
                condition=models.Q(brand__isnull=True),
                name="uq_rpt_brandlayout_standard_kind",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} r{self.revision}"


class BrandReportRun(models.Model):
    """The month's brand reports made for one store: once, by the monthly run."""

    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    month = models.DateField(help_text="The month, as its first day.")
    made_at = models.DateTimeField()
    took_ms = models.IntegerField(default=0)
    files = models.IntegerField(default=0)

    class Meta:
        db_table = "report_brand_report_run"
        ordering = ["-month", "store_id"]
        constraints = [
            models.UniqueConstraint(fields=["store", "month"], name="uq_rpt_brandrun_store_month"),
        ]

    def __str__(self) -> str:
        return f"{self.store_id} {self.month:%Y-%m}"


class BrandReportFile(models.Model):
    """One report file the monthly run made, kept exactly as made (never changed).

    ``about`` holds what the file states - basis, as-of, missing data - and the
    layout it was made in, so the file can be explained after the layout changes.
    """

    run = models.ForeignKey(BrandReportRun, on_delete=models.PROTECT, related_name="made")
    store = models.ForeignKey("masters.Store", on_delete=models.DO_NOTHING, related_name="+")
    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=4, choices=BrandReportKind.choices)
    month = models.DateField()
    file_name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=120)
    content = models.BinaryField()
    size = models.IntegerField(default=0)
    rows = models.IntegerField(default=0)
    #: None when the file used the built-in KDPS sheet no company had saved yet.
    layout_id = models.BigIntegerField(null=True, blank=True)
    layout_revision = models.PositiveIntegerField()
    as_of = models.DateTimeField(null=True, blank=True)
    about = models.JSONField(default=dict)
    made_at = models.DateTimeField()

    class Meta:
        db_table = "report_brand_report_file"
        ordering = ["-month", "store_id", "brand_id", "kind"]
        constraints = [
            models.UniqueConstraint(
                fields=["store", "brand", "kind", "month"], name="uq_rpt_brandfile_one_a_month"
            ),
        ]

    def __str__(self) -> str:
        return self.file_name
