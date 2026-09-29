"""What a bill looks like on the wire, in and out.

The inbound serializers do **shape** only - is this integer paise, is that one of
the four tender modes, is there at least one line. Everything that needs to know
about the business (does this barcode resolve, does the till's number belong to
this store, is that credit note real) is the accept pipeline's, because those
questions have their own contract error codes and the till routes on the code.
So a serializer failure here is always `VALIDATION` / 400, and never anything else.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from approvals.names import display_name
from core.fiscal import financial_year_months
from sell.models import (
    ContinuityFlag,
    ExchangeCreditNote,
    HeldBill,
    IrnQueueItem,
    Sale,
    SaleLine,
    SaleLineShare,
    SaleTender,
)


class _CustomerWriteSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(max_length=120, allow_blank=True, required=False, default="")
    mobile = serializers.CharField(max_length=15, allow_blank=True, required=False, default="")
    gstin = serializers.CharField(max_length=15, allow_blank=True, required=False, default="")


class _ShareWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """One salesperson's share of a split line (ticket 08)."""

    salesperson = serializers.UUIDField(help_text="A staff record from the store's staff list.")
    percent = serializers.IntegerField(help_text="Whole percent, 1 to 99; the two add to 100.")


class _LineWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """One line, whether it is being sold or given back inside an exchange."""

    line_no = serializers.IntegerField(min_value=1)
    direction = serializers.ChoiceField(
        choices=SaleLine.Direction.values, required=False, default=SaleLine.Direction.SALE
    )
    #: Ticket 22: ``alteration`` for a paid alteration's own line. Absent is
    #: goods, and no default, so an older bill fingerprints exactly as it did.
    kind = serializers.ChoiceField(choices=SaleLine.Kind.values, required=False)
    barcode = serializers.CharField(max_length=64, allow_blank=True, required=False, default="")
    season = serializers.CharField(max_length=120, allow_blank=True, required=False, default="")
    qty = serializers.IntegerField(min_value=1)
    mrp_paise = serializers.IntegerField(min_value=0, required=False, default=0)
    disc_paise = serializers.IntegerField(min_value=0, required=False, default=0)
    #: A sale line names what the customer paid; a return leg names what is given
    #: back. Both land in `SaleLine.net_paise` - `direction` carries the sign.
    net_paise = serializers.IntegerField(min_value=0, required=False)
    refund_paise = serializers.IntegerField(min_value=0, required=False)
    gst_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=0, required=False, default=Decimal(0)
    )
    gst_paise = serializers.IntegerField(min_value=0, required=False, default=0)
    salesperson = serializers.UUIDField(
        required=False,
        allow_null=True,
        default=None,
        help_text="Who sold the line: a staff record from the store's staff list (ticket 07).",
    )
    salesman = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
        help_text="Only from a till that queued the bill before the staff list: the id of "
        "the old salesperson row. It lands on that row's frozen copy.",
    )
    #: Ticket 08: a sold line shared between two salespeople. The first share is
    #: the line's own `salesperson`. Absent on every unsplit line, and no
    #: default, so an older bill fingerprints exactly as it did.
    shares = serializers.ListField(
        child=_ShareWriteSerializer(),
        required=False,
        allow_null=True,
        help_text="Two shares by whole percent, adding to 100 (ticket 08).",
    )
    offer_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    offer_evidence = serializers.JSONField(required=False, default=dict)
    manual_desc = serializers.CharField(
        max_length=200, allow_blank=True, required=False, default=""
    )
    condition = serializers.ChoiceField(
        choices=SaleLine.Condition.values, required=False, allow_blank=True, default=""
    )
    reason = serializers.CharField(max_length=40, allow_blank=True, required=False, default="")
    original_line = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
        help_text="The line number on the original bill this return leg gives back.",
    )
    override_by = serializers.IntegerField(required=False, allow_null=True, default=None)
    #: Ticket 13 (B60), a return leg only: the part of its value the bank offer
    #: paid on the original bill. No default, so an older bill fingerprints as it did.
    bank_offer_paise = serializers.IntegerField(min_value=0, required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # A sale names `net_paise` and a return leg names `refund_paise`, but the
        # two are the same field on the row - what the line is worth, with
        # `direction` carrying the sign. Either spelling is taken here, because a
        # line arriving under `exchange` is only stamped as a return by the parent
        # serializer, which has not run yet.
        net = attrs.get("net_paise")
        refund = attrs.get("refund_paise")
        preferred = refund if attrs["direction"] == SaleLine.Direction.RETURN else net
        value = preferred if preferred is not None else (net if net is not None else refund)
        if value is None:
            raise serializers.ValidationError(
                f"line {attrs['line_no']}: needs net_paise (a sale) or refund_paise (a return)."
            )
        attrs["value_paise"] = value
        if not attrs["offer_evidence"]:
            attrs["offer_evidence"] = {}
        if not isinstance(attrs["offer_evidence"], dict):
            raise serializers.ValidationError(
                f"line {attrs['line_no']}: offer evidence must be an object."
            )
        return attrs


class _OriginalRefSerializer(serializers.Serializer[dict[str, Any]]):
    store = serializers.CharField(max_length=16, required=False, allow_blank=True, default="")
    fy = serializers.CharField(max_length=7)
    till_seq = serializers.IntegerField(min_value=1)


class _ExchangeWriteSerializer(serializers.Serializer[dict[str, Any]]):
    original = _OriginalRefSerializer()
    lines = serializers.ListField(child=_LineWriteSerializer(), allow_empty=False)


class _TenderWriteSerializer(serializers.Serializer[dict[str, Any]]):
    mode = serializers.ChoiceField(
        choices=(
            SaleTender.Mode.CASH,
            SaleTender.Mode.CARD,
            SaleTender.Mode.UPI,
            SaleTender.Mode.BANK_OFFER,
            SaleTender.Mode.ADVANCE,
            SaleTender.Mode.GIFT_VOUCHER,
        )
    )
    amount_paise = serializers.IntegerField(min_value=0)
    #: Ticket 19: the number of the gift voucher a ``gift_voucher`` tender spends.
    #: Forbidden on every other mode. No default, so a replay from an older till
    #: fingerprints as it did.
    gift_voucher = serializers.CharField(max_length=32, required=False)
    #: Ticket 19: the check code printed on that voucher's slip (B311). Only on a
    #: ``gift_voucher`` tender, and always on it.
    gift_voucher_code = serializers.CharField(max_length=16, required=False)
    #: Ticket 11: which bank offer a ``bank_offer`` tender is. Forbidden on every
    #: other mode. No default, so a replay from an older till fingerprints as it did.
    offer_id = serializers.IntegerField(required=False, min_value=1)
    #: How the money was proven - `confirmed` (the bank answered) or `manual`
    #: (the cashier vouched: QR soundbox, static QR, no internet). Required on a
    #: UPI tender, forbidden on every other mode.
    upi_state = serializers.ChoiceField(
        choices=SaleTender.UpiState.choices, allow_blank=True, required=False, default=""
    )
    #: The acquirer's transaction reference. Required when `upi_state` is
    #: `confirmed` - a manual entry has nothing trustworthy to record - and
    #: forbidden otherwise.
    upi_reference = serializers.CharField(
        max_length=64, allow_blank=True, required=False, default=""
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        is_upi = attrs["mode"] == SaleTender.Mode.UPI
        state = attrs.get("upi_state") or ""
        reference = attrs.get("upi_reference") or ""
        if is_upi and not state:
            raise serializers.ValidationError("A UPI tender needs upi_state (confirmed or manual).")
        if not is_upi and state:
            raise serializers.ValidationError("upi_state is only for a UPI tender.")
        if state == SaleTender.UpiState.CONFIRMED and not reference:
            raise serializers.ValidationError(
                "A confirmed UPI tender needs the acquirer's reference."
            )
        if state != SaleTender.UpiState.CONFIRMED and reference:
            raise serializers.ValidationError("upi_reference is only for a confirmed UPI tender.")
        if attrs.get("offer_id") is not None and attrs["mode"] != SaleTender.Mode.BANK_OFFER:
            raise serializers.ValidationError("offer_id is only for a bank offer tender.")
        if attrs["mode"] == SaleTender.Mode.BANK_OFFER and attrs.get("offer_id") is None:
            raise serializers.ValidationError("A bank offer tender names its offer (offer_id).")
        voucher = (attrs.get("gift_voucher") or "").strip()
        if attrs["mode"] == SaleTender.Mode.GIFT_VOUCHER and not voucher:
            raise serializers.ValidationError(
                "A gift voucher tender names its voucher (gift_voucher)."
            )
        if (
            attrs["mode"] == SaleTender.Mode.GIFT_VOUCHER
            and not (attrs.get("gift_voucher_code") or "").strip()
        ):
            raise serializers.ValidationError(
                "A gift voucher tender carries the code on its slip (gift_voucher_code)."
            )
        if attrs["mode"] != SaleTender.Mode.GIFT_VOUCHER and (
            "gift_voucher" in attrs or "gift_voucher_code" in attrs
        ):
            raise serializers.ValidationError("gift_voucher is only for a gift voucher tender.")
        return attrs


class _TotalsWriteSerializer(serializers.Serializer[dict[str, Any]]):
    gross_paise = serializers.IntegerField(min_value=0)
    discount_paise = serializers.IntegerField(min_value=0)
    #: Parsed as an integer here; the accept pipeline refuses a negative total as
    #: `EXCHANGE_SHORT` so the business refusal keeps its contract code.
    net_paise = serializers.IntegerField()
    gst_paise = serializers.IntegerField()
    #: Rounding to the nearest rupee can never move a bill by more than 50 paise,
    #: and the bound matters because this line is the one number on the bill that
    #: nothing else derives: it sits inside the net-vs-tenders check, so an
    #: unbounded `round_paise: -50000` would let a bill take ₹500 less than its
    #: lines say and still add up.
    round_paise = serializers.IntegerField(required=False, default=0, min_value=-50, max_value=50)


#: What a manager can be asked to authorise at a till, and the one word a bill
#: uses when they were asked both at once. A closed set, because the daily check
#: groups bills by this value: a spelling nothing recognises is an exception
#: nobody counts. The till builds the pair in this order (`till/cart.ts`).
#:
#: What is *stored* is derived from what the pipeline itself found
#: (`accept._authorised_kind`), never from what arrives here - this validation
#: only keeps the wire honest about what the till believes it is asking for.
OVERRIDE_KINDS = ("late_return",)


class SellPolicyWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """The two policy dials are sent together, as one HO decision."""

    manual_discount_cap_percent = serializers.CharField()
    manual_discount_on_offer_lines = serializers.BooleanField()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(self.initial_data.get("manual_discount_cap_percent"), str):
            raise serializers.ValidationError("Discount cap must be a two-decimal string.")
        cap = attrs["manual_discount_cap_percent"]
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,2})\.[0-9]{2}", cap):
            raise serializers.ValidationError("Discount cap must be a two-decimal string.")
        try:
            percent = Decimal(cap)
        except InvalidOperation as exc:  # pragma: no cover - regex excludes this shape
            raise serializers.ValidationError("Discount cap must be a decimal.") from exc
        if not Decimal("0.00") <= percent <= Decimal("100.00"):
            raise serializers.ValidationError("Discount cap must be between 0.00 and 100.00.")
        if not isinstance(self.initial_data.get("manual_discount_on_offer_lines"), bool):
            raise serializers.ValidationError("Manual-on-offer must be a boolean.")
        attrs["manual_discount_cap_percent"] = percent
        return attrs


class _OverrideWriteSerializer(serializers.Serializer[dict[str, Any]]):
    user_id = serializers.IntegerField()
    kind = serializers.ChoiceField(
        choices=OVERRIDE_KINDS, allow_blank=True, required=False, default=""
    )
    #: When the manager's PIN was accepted at the counter. A separate moment from
    #: `billed_at` - a manager authorises an exception and the cashier goes on
    #: scanning - and the whole point of the evidence is the gap between the two.
    #: Optional, because a till that predates this field is still a till.
    at = serializers.DateTimeField(required=False, allow_null=True, default=None)


def _check_advance_tenders(attrs: dict[str, Any]) -> None:
    """Ticket 20: an advance tender is the advance of the reservation the bill
    names - or, ticket 21, of the special order it names. Never both."""
    advances = [t for t in attrs.get("tenders") or [] if t["mode"] == SaleTender.Mode.ADVANCE]
    if attrs.get("reservation") and attrs.get("special_order"):
        raise serializers.ValidationError(
            "A bill collects a reservation or a special order, not both."
        )
    if advances and not (attrs.get("reservation") or attrs.get("special_order")):
        raise serializers.ValidationError(
            "An advance tender is a reservation's or a special order's advance: the bill names it."
        )
    if len(advances) > 1:
        raise serializers.ValidationError("A bill uses its advance once.")


def _check_gift_voucher_tenders(attrs: dict[str, Any]) -> None:
    """Ticket 19: a voucher is spent once per bill, by a tender of its own."""
    numbers = [
        str(t["gift_voucher"]).strip().upper()
        for t in attrs.get("tenders") or []
        if t["mode"] == SaleTender.Mode.GIFT_VOUCHER
    ]
    if len(numbers) != len(set(numbers)):
        raise serializers.ValidationError("A bill spends each gift voucher once.")


def _check_no_alteration_back(exchange_lines: list[dict[str, Any]]) -> None:
    """Ticket 22: an alteration charge is a service, never a piece given back."""
    for line in exchange_lines:
        if line.get("kind") == SaleLine.Kind.ALTERATION:
            raise serializers.ValidationError(
                f"line {line['line_no']}: an alteration charge is not a piece to take back."
            )


class SaleWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """One bill, as the till's queue replays it."""

    idempotency_uuid = serializers.UUIDField()
    store = serializers.CharField(max_length=16)
    fy = serializers.CharField(max_length=7)
    till_seq = serializers.IntegerField(min_value=1)
    origin = serializers.ChoiceField(
        choices=Sale.Origin.values, required=False, default=Sale.Origin.OFFLINE
    )
    #: `{StoreCode}-{CounterID}-{Seq}` off the device that printed it (R-POS-005,
    #: OPS-09). Sent by a registered till and checked against the counter the
    #: store actually has; absent from a legacy till, which has no counter id.
    till_number = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    billed_at = serializers.DateTimeField()
    customer = _CustomerWriteSerializer(required=False)
    lines = serializers.ListField(child=_LineWriteSerializer(), required=False, default=list)
    exchange = _ExchangeWriteSerializer(required=False, allow_null=True)
    tenders = serializers.ListField(child=_TenderWriteSerializer(), required=False, default=list)
    #: What the customer physically handed over in notes. Presentation only - it
    #: is not a tender and never joins the net-vs-tenders sum - but the bill
    #: carries it so a reprint can show the same change line the counter's own
    #: copy showed. Absent or null is the blank box: exactly the cash tender.
    #: What it may *not* be is a figure the bill cannot support, and the accept
    #: pipeline refuses those (`_check_cash_received`).
    cash_received_paise = serializers.IntegerField(
        required=False, allow_null=True, default=None, min_value=0
    )
    totals = _TotalsWriteSerializer()
    b2b_tax_kind = serializers.ChoiceField(
        choices=Sale.B2bTaxKind.values, required=False, allow_blank=True, default=""
    )
    override = _OverrideWriteSerializer(required=False, allow_null=True)
    #: The tax settings version the till taxed this bill under (ticket 03). Absent
    #: from a till that predates versions, which taxed by the slab table:
    #: version 1. No default, so a replay from such a till fingerprints as it did.
    tax_setting_version = serializers.IntegerField(
        required=False, min_value=1, max_value=2_147_483_647
    )
    #: The tax invoice number in the new series, ``XXX/26-27/n``, from the number
    #: block the counter holds (ticket 04). Absent before the new format starts
    #: and at a store where it is off. No default, so a replay from an older till
    #: fingerprints as it did.
    tax_invoice_number = serializers.CharField(max_length=40, required=False, allow_blank=True)
    #: Ticket 11: the till priced this bill with GST after discount. Absent is
    #: no - every till before the ticket, and every store with the switch off.
    #: No default, so a replay from an older till fingerprints as it did.
    gst_after_discount = serializers.BooleanField(required=False)
    #: Ticket 13: the till took the pieces coming back by the exchange and
    #: return tax rules (credit note, deadline, bank part). Absent is no. No
    #: default, so a replay from an older till fingerprints as it did.
    return_tax = serializers.BooleanField(required=False)
    #: Ticket 20: the customer reservation this bill collects. Its pieces must be
    #: on the bill, and an ``advance`` tender is that reservation's advance. No
    #: default, so a replay from an older till fingerprints as it did.
    reservation = serializers.UUIDField(required=False)
    #: Ticket 21: the special order this bill collects. The piece that arrived
    #: must be on the bill, and an ``advance`` tender is the order's advance. No
    #: default, for the same reason.
    special_order = serializers.UUIDField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        _check_advance_tenders(attrs)
        _check_gift_voucher_tenders(attrs)
        exchange = attrs.get("exchange") or None
        exchange_lines = list(exchange["lines"]) if exchange else []
        for line in exchange_lines:
            # Anything under `exchange` is a return leg by construction; the till
            # need not say so twice and cannot say otherwise.
            line["direction"] = SaleLine.Direction.RETURN
        _check_no_alteration_back(exchange_lines)
        lines = list(attrs.get("lines") or []) + exchange_lines
        if not lines:
            raise serializers.ValidationError("A bill needs at least one line.")
        seen: set[int] = set()
        for line in lines:
            if line["line_no"] in seen:
                raise serializers.ValidationError(f"line {line['line_no']} appears twice.")
            seen.add(line["line_no"])
        attrs["all_lines"] = lines
        # Ticket 13 (B60): a bank offer's part rides only on a piece coming back,
        # on a bill taken by the return tax rules, and is never more than the leg.
        for line in lines:
            bank = line.get("bank_offer_paise") or 0
            if not bank:
                continue
            if line["direction"] != SaleLine.Direction.RETURN or not attrs.get("return_tax"):
                raise serializers.ValidationError(
                    f"line {line['line_no']}: a bank offer part rides only on a piece coming "
                    "back, on a bill taken by the return tax rules."
                )
            if bank > line["value_paise"]:
                raise serializers.ValidationError(
                    f"line {line['line_no']}: the bank offer part is more than the piece is worth."
                )
        # A tender of nothing is not a tender.
        tenders = [t for t in attrs.get("tenders") or [] if t["amount_paise"] > 0]
        # Ticket 11: only a till pricing with GST after discount takes a bank
        # offer as payment, and it always says so on the bill. Any other bill
        # carrying one is not a bill a till made, so nothing is written.
        if not attrs.get("gst_after_discount") and any(
            t["mode"] == SaleTender.Mode.BANK_OFFER for t in tenders
        ):
            raise serializers.ValidationError(
                "A bank offer is a payment only on a bill priced with GST after discount."
            )
        attrs["tenders"] = tenders
        return attrs


class _HeldBillWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """One parked cart, as the till mirrors it up (contract, step 3).

    `payload` is checked for being an object and for nothing else. It is the
    counter's cart, the counter reprices it on retrieval, and a server that
    validated its shape would be promising to understand a structure it has no
    business reading (see `HeldBill`).
    """

    held_uuid = serializers.UUIDField()
    # `label` is also the name of DRF's own form-label attribute on `Field`, so the
    # declaration reads as an override of it. It is not one: `SerializerMetaclass`
    # lifts every declared field off the class, and the wire contract calls this
    # field `label`, so the clash is in the stubs' view of the class body only.
    label = serializers.CharField(  # type: ignore[assignment]
        max_length=120, allow_blank=True, required=False, default="", trim_whitespace=False
    )
    held_at = serializers.DateTimeField()
    expires_policy = serializers.ChoiceField(
        choices=HeldBill.ExpiresPolicy.values,
        required=False,
        default=HeldBill.ExpiresPolicy.TODAY,
    )
    payload = serializers.DictField(required=False, default=dict)


class HeldBillsWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """The counter's whole list, which is the only thing it ever sends.

    `held` is required rather than defaulted to empty: "I have nothing parked" is
    a real and destructive statement - it clears the store's Dashboard row - and a
    body that forgot to say it should not be able to make it by accident.
    """

    held = serializers.ListField(child=_HeldBillWriteSerializer(), allow_empty=True)

    def validate_held(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        keys = [row["held_uuid"] for row in rows]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError("The same hold appears twice in one push.")
        return rows


class RegisterHandoverWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """The one thing a handover asks for: why (#189).

    Required, non-blank, and that is the whole of the validation. The reason is
    the only part of the row a person writes, and it is what makes a handful of
    unexplained holes in a store's bill series into "the counter machine died on
    Tuesday" - so a handover with an empty one would leave exactly the audit
    trail the row exists to prevent.
    """

    reason = serializers.CharField(max_length=240)

    def validate_reason(self, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise serializers.ValidationError(
                "Say why the counter is moving to a different machine."
            )
        return reason


class TillRegisterWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """Registering the device this store bills from (OPS-09, PRD §10.1).

    `replace` is deliberately an explicit flag with a reason behind it rather than
    something the server infers from "there is already one". A store whose counter
    machine has died is replacing a device; a second machine quietly registering
    beside the first is the failure `TILL_TAKEN` exists to catch, and the two look
    identical from here unless somebody says which it is.
    """

    replace = serializers.BooleanField(required=False, default=False)
    reason = serializers.CharField(max_length=240, required=False, allow_blank=True, default="")


def _counter_year(value: str) -> None:
    """A financial year label exactly as the till writes one (`26-27`)."""
    try:
        financial_year_months(value)
    except ValueError as exc:
        raise serializers.ValidationError(str(exc)) from exc


class TillAllocationReleaseSerializer(serializers.Serializer[dict[str, Any]]):
    """Letting a till's protected quantities go (PRD §10.2). Always with a reason."""

    reason = serializers.CharField(max_length=240)
    #: The financial year the counter is counting in - its own, not head
    #: office's, because the two straddle 1 April (change PRD §10.2).
    fy = serializers.CharField(max_length=5, validators=[_counter_year])
    #: The number the counter would bill next in `fy`. Every number below it
    #: must have arrived before the stock is let go.
    next_seq = serializers.IntegerField(min_value=1)

    def validate_reason(self, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise serializers.ValidationError(
                "Say why the counter's protected stock is being released."
            )
        return reason


class TillResumeSerializer(serializers.Serializer[dict[str, Any]]):
    """Ending the counter's transfer pause (change PRD §10.2)."""

    #: Where the counter would bill next as it resumes: its year and number,
    #: both or neither. Optional - a counter that billed nothing while paused is
    #: still where it paused.
    fy = serializers.CharField(max_length=5, required=False, validators=[_counter_year])
    next_seq = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if ("fy" in attrs) != ("next_seq" in attrs):
            raise serializers.ValidationError(
                "Name the counter's year and its next bill number together."
            )
        return attrs


# --- read shapes -------------------------------------------------


class SaleAcceptedSerializer(serializers.Serializer[dict[str, Any]]):
    """The replay-safe acknowledgement returned when the queue lands a bill."""

    doc_number = serializers.CharField()
    #: The number in the new invoice series the server wrote on the bill, or null.
    tax_invoice_number = serializers.CharField(allow_null=True, required=False)
    id = serializers.IntegerField()
    flags = serializers.ListField(child=serializers.CharField())


class SaleLineShareReadSerializer(serializers.ModelSerializer[SaleLineShare]):
    """One person's share of a split line, as the bill was made (ticket 08)."""

    salesperson_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = SaleLineShare
        fields = [
            "position",
            "salesperson_id",
            "salesperson_code",
            "salesperson_name",
            "percent",
            "value_paise",
        ]


class SaleLineReadSerializer(serializers.ModelSerializer[SaleLine]):
    #: The seller's code and name exactly as the bill was made (ticket 07).
    salesman_code = serializers.CharField(source="salesperson_code", read_only=True)
    salesman_name = serializers.CharField(source="salesperson_name", read_only=True)
    #: How much of this line has already been given back, either as a return leg
    #: on a later bill or on a historical standalone return. Annotated by
    #: `refunds.with_returned` on the queryset, so it is one query for the bill
    #: rather than four per line; `0` where nothing annotated it, which is honest
    #: for a caller that did not ask.
    returned_qty = serializers.IntegerField(read_only=True, default=0)
    #: And what those pieces were worth back. The counter needs both to price an
    #: exchange leg offline: the last piece of a line settles the remainder, so a
    #: till that knew only the count would get the second partial return wrong -
    #: and would find out after the receipt had printed. See `refunds`.
    returned_paise = serializers.IntegerField(read_only=True, default=0)
    #: Ticket 13 (B60): what earlier returns took off the line's bank-offer share,
    #: so a counter can work out the next piece's part offline.
    returned_bank_paise = serializers.IntegerField(read_only=True, default=0)
    #: Ticket 08: each salesperson's share where the line is split; empty otherwise.
    shares = SaleLineShareReadSerializer(many=True, read_only=True)

    class Meta:
        model = SaleLine
        fields = [
            "line_no",
            "direction",
            # Ticket 22: goods, or an alteration charge (never taken back).
            "kind",
            "barcode",
            "season",
            "design",
            "color",
            "size",
            "brand",
            "item",
            "hsn",
            "qty",
            "mrp_paise",
            "disc_paise",
            "net_paise",
            "gst_rate",
            "gst_paise",
            # Ticket 12: the version and rule this line was taxed under.
            "tax_setting_version",
            "tax_rule_kind",
            "tax_rule_hsn_prefix",
            "salesman_code",
            "salesman_name",
            "shares",
            "offer_evidence",
            "manual_desc",
            "sold_before_inward",
            "costing_status",
            "return_reason",
            "condition",
            "returned_qty",
            "returned_paise",
            # Ticket 13 (B60): a return leg's bank-offer part, and what earlier
            # returns took off a sold line's share.
            "bank_offer_paise",
            "returned_bank_paise",
        ]


class SaleTenderReadSerializer(serializers.ModelSerializer[SaleTender]):
    credit_note_number = serializers.CharField(
        source="credit_note.doc_number", read_only=True, default=""
    )
    #: Ticket 19: the gift voucher a `gift_voucher` tender spent, else blank.
    gift_voucher_number = serializers.CharField(
        source="gift_voucher.number", read_only=True, default=""
    )

    class Meta:
        model = SaleTender
        #: `upi_state` rides on the read shape because manual and
        #: provider-confirmed must stay distinguishable everywhere a bill is
        #: looked at, not only where it was rung up (PRD §9.1, R-POS-007). Blank
        #: on cash and card, which are always the cashier's own word.
        fields = [
            "mode",
            "amount_paise",
            "upi_state",
            "upi_reference",
            "credit_note_number",
            # Ticket 11: the bank offer a `bank_offer` tender is, else null.
            "offer",
            "gift_voucher_number",
        ]


class FlagReadSerializer(serializers.ModelSerializer[ContinuityFlag]):
    class Meta:
        model = ContinuityFlag
        fields = ["kind", "status", "details", "created_at"]


class ExchangeCreditNoteReadSerializer(serializers.Serializer[dict[str, Any]]):
    """The credit note an exchange issued (ticket 13), as a bill's read shape carries it."""

    number = serializers.CharField(help_text="Blank only when it could not be numbered.")
    series = serializers.CharField(help_text="CN: the new series; CRN: today's numbering.")
    issued_on = serializers.DateField()
    gstin = serializers.CharField()
    value_paise = serializers.IntegerField()
    taxable_paise = serializers.IntegerField()
    gst_paise = serializers.IntegerField()
    bank_offer_paise = serializers.IntegerField()
    credit_paise = serializers.IntegerField()
    late = serializers.BooleanField(
        help_text="After the credit-note deadline: the value was given, no tax reduced."
    )
    deadline = serializers.DateField(allow_null=True)
    original = serializers.CharField(allow_null=True)
    status = serializers.ChoiceField(
        choices=["issued", "cancelled"],
        help_text="Cancelled with its bill; the number stays used.",
    )


class SaleReadSerializer(serializers.ModelSerializer[Sale]):
    """The whole bill, read-only. There is no write counterpart, by design (A7)."""

    store_code = serializers.CharField(source="store.code", read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)
    #: The registration the reprint has to carry. A tax invoice without a GSTIN on
    #: it is not one, and a reprint reached from customer search has no till
    #: behind it to borrow the number from - the dataset is a *counter's* copy,
    #: and whoever is looking up an old bill may not be standing at one.
    store_gstin = serializers.CharField(source="store.gstin.gstin", read_only=True, default="")
    #: The e-invoice reference, once head office has raised one (#187). Blank on
    #: every B2C bill and on a B2B bill still in the queue - and the reprint
    #: prints no IRN line for that blank, as the original did when it came off
    #: the counter's printer (ST-CMP-4: nothing promises an IRN to follow).
    #:
    #: A method field rather than `source="irn_queue_item.irn"`, which looks like
    #: it would do: DRF catches the `RelatedObjectDoesNotExist` a B2C bill raises
    #: and answers `None` *before* it ever reaches `default`, so the API would
    #: send `null` where the contract and the TypeScript type both say a string.
    irn = serializers.SerializerMethodField()
    #: The bill this one gave pieces back against, when it carries an exchange
    #: (#184). Null on an ordinary sale. A method field for the reason `irn` is
    #: one: DRF answers `None` for a traversal through a null FK *before* it ever
    #: reaches `default`, so a `source=` would send `null` where the shape says a
    #: string. A reprint needs it because a returned line on the paper has to say
    #: which bill it came back against - otherwise it reads as a piece sold at a
    #: negative price.
    exchange_of = serializers.SerializerMethodField()
    lines = SaleLineReadSerializer(many=True, read_only=True)
    tenders = SaleTenderReadSerializer(many=True, read_only=True)
    flags = FlagReadSerializer(many=True, read_only=True)
    credit_notes_issued = serializers.SerializerMethodField()
    #: Ticket 13: the credit note this exchange issued beside its new invoice,
    #: or null (every ordinary sale, and every exchange at a store with the
    #: return tax switch off).
    credit_note = serializers.SerializerMethodField()
    billed_by = serializers.CharField(source="created_by.username", read_only=True, default="")
    authorised_by = serializers.CharField(source="override_by.username", read_only=True, default="")

    class Meta:
        model = Sale
        fields = [
            "id",
            "doc_number",
            "docstatus",
            "store_code",
            "store_name",
            "store_gstin",
            "fy",
            "till_seq",
            "origin",
            "billed_at",
            "customer_name",
            "customer_mobile",
            "buyer_gstin",
            "b2b_tax_kind",
            "tax_setting_version",
            "gst_after_discount",
            "return_tax",
            "tax_invoice_number",
            "irn",
            "exchange_of",
            "gross_paise",
            "discount_paise",
            "net_paise",
            "gst_paise",
            "round_paise",
            "cash_received_paise",
            "billed_by",
            "authorised_by",
            "override_kind",
            "override_at",
            "lines",
            "tenders",
            "flags",
            "credit_notes_issued",
            "credit_note",
        ]

    def get_irn(self, obj: Sale) -> str:
        queued = getattr(obj, "irn_queue_item", None)
        return queued.irn if queued else ""

    def get_exchange_of(self, obj: Sale) -> dict[str, Any] | None:
        original = obj.exchange_of
        if original is None:
            return None
        return {
            "doc_number": original.doc_number or "",
            "fy": original.fy,
            "till_seq": original.till_seq,
        }

    @extend_schema_field(ExchangeCreditNoteReadSerializer(allow_null=True))
    def get_credit_note(self, obj: Sale) -> dict[str, Any] | None:
        from sell.services.exchange_tax import note_json

        note = (
            ExchangeCreditNote.objects.filter(sale=obj)
            .select_related("original_sale", "sale")
            .first()
        )
        return note_json(note) if note is not None else None

    def get_credit_notes_issued(self, obj: Sale) -> list[dict[str, Any]]:
        return [
            {"doc_number": note.doc_number, "value_paise": note.value_paise}
            for note in obj.credit_notes_issued.all()
        ]


class SaleRowSerializer(serializers.ModelSerializer[Sale]):
    """One row of the customer-search / reprint list, and of Bills (OPS-08)."""

    store_code = serializers.CharField(source="store.code", read_only=True)
    #: How the bill was paid, for the Bills row's tender summary. The whole rows
    #: rather than a rendered string: the screen says "Cash + UPI" and the paper
    #: says something else again, and one of them changing must not change both.
    tenders = SaleTenderReadSerializer(many=True, read_only=True)
    lines_summary = serializers.SerializerMethodField()
    #: Total pieces sold (SALE direction only), for the recent-bill pick list.
    pieces = serializers.SerializerMethodField()
    #: Unique salespeople on the bill, for the recent-bill pick list.
    salespeople = serializers.SerializerMethodField()

    class Meta:
        model = Sale
        fields = [
            "id",
            "doc_number",
            "tax_invoice_number",
            "store_code",
            "billed_at",
            "customer_name",
            "customer_mobile",
            "net_paise",
            "fy",
            "till_seq",
            "tenders",
            "lines_summary",
            "pieces",
            "salespeople",
        ]

    def get_lines_summary(self, obj: Sale) -> str:
        lines = list(obj.lines.all())
        pieces = sum(line.qty for line in lines if line.direction == SaleLine.Direction.SALE)
        brands = sorted({line.brand for line in lines if line.brand})
        shown = ", ".join(brands[:2])
        if len(brands) > 2:
            shown = f"{shown} +{len(brands) - 2}"
        piece_word = "piece" if pieces == 1 else "pieces"
        return f"{pieces} {piece_word} · {shown}" if shown else f"{pieces} {piece_word}"

    def get_pieces(self, obj: Sale) -> int:
        return sum(
            line.qty for line in obj.lines.all() if line.direction == SaleLine.Direction.SALE
        )

    def get_salespeople(self, obj: Sale) -> list[str]:
        # The name frozen on each line as the bill was made (ticket 07).
        seen: set[str] = set()
        names: list[str] = []
        for line in obj.lines.all():
            name = line.salesperson_name
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names


class IrnQueueRowSerializer(serializers.ModelSerializer[IrnQueueItem]):
    """One B2B bill on head office's clock (#187, grill Q8).

    Everything a clerk needs to raise the invoice on the government portal
    without opening the bill: whose registration it is, what it was worth, which
    split it carries, and how long is left. `days_left` is annotated by the view
    from one `today` rather than computed per row - thirty rows must not disagree
    about what day it is because the clock ticked over mid-response.
    """

    doc_number = serializers.CharField(source="sale.doc_number", read_only=True)
    store_code = serializers.CharField(source="sale.store.code", read_only=True)
    store_name = serializers.CharField(source="sale.store.name", read_only=True)
    billed_at = serializers.DateTimeField(source="sale.billed_at", read_only=True)
    buyer_gstin = serializers.CharField(source="sale.buyer_gstin", read_only=True)
    customer_name = serializers.CharField(source="sale.customer_name", read_only=True)
    b2b_tax_kind = serializers.CharField(source="sale.b2b_tax_kind", read_only=True)
    net_paise = serializers.IntegerField(source="sale.net_paise", read_only=True)
    gst_paise = serializers.IntegerField(source="sale.gst_paise", read_only=True)
    #: Who worked the row, as a person reads a person - `approvals.names` is the
    #: one spelling of that in the project, and it falls back to the username on
    #: an account nobody has given a full name.
    handled_by_name = serializers.SerializerMethodField()
    days_left = serializers.SerializerMethodField()

    class Meta:
        model = IrnQueueItem
        fields = [
            "id",
            "doc_number",
            "store_code",
            "store_name",
            "billed_at",
            "buyer_gstin",
            "customer_name",
            "b2b_tax_kind",
            "net_paise",
            "gst_paise",
            "due_on",
            "days_left",
            "status",
            "irn",
            "handled_by_name",
            "handled_at",
        ]

    def get_handled_by_name(self, obj: IrnQueueItem) -> str:
        return display_name(obj.handled_by)

    def get_days_left(self, obj: IrnQueueItem) -> int:
        """Days to the deadline; negative once it has gone by.

        `today` comes in on the serializer's context because it is the view's
        single reading of the clock (Rule 11 - the deadline is data, and so is the
        day it is measured against).
        """
        today: date = self.context["today"]
        return (obj.due_on - today).days


class IrnQueueWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """What head office writes back after a run on the portal.

    Only the two terminal answers: a row goes to `generated` with the reference
    the portal gave, or to `failed` so the clerk can see what still has to be
    chased. It never goes back to `pending` - "we tried and it did not work" is a
    fact worth keeping, and a row that could be reset would lose it.
    """

    status = serializers.ChoiceField(
        choices=[IrnQueueItem.Status.GENERATED, IrnQueueItem.Status.FAILED]
    )
    irn = serializers.CharField(max_length=64, allow_blank=True, required=False, default="")

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # A generated row with no reference on it is the one shape that would
        # make the queue lie: it would leave the list, and the bill would still
        # have no IRN on it anywhere.
        if attrs["status"] == IrnQueueItem.Status.GENERATED and not attrs["irn"].strip():
            raise serializers.ValidationError("Give the IRN the portal returned.")
        attrs["irn"] = attrs["irn"].strip()
        return attrs


class ContinuityFlagRowSerializer(serializers.ModelSerializer[ContinuityFlag]):
    """One exception on a store's list (#188).

    Wider than `FlagReadSerializer`, which rides inside a bill and can leave out
    everything the bill already says. This one is read on its own screen, so it
    carries the bill it is about - or says there is none, which is a fact rather
    than a gap: a hole is a bill that never arrived, and a seller's return count
    is a pattern across bills.
    """

    #: The kind said the way a person says it - `ContinuityFlag.Kind`'s own label,
    #: so the wording lives once, on the model, beside the reason it exists.
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    store_code = serializers.CharField(source="store.code", read_only=True)
    #: Empty on a flag about no particular bill. The screen links on this, so a
    #: `null` would need every caller to remember which of the two it had.
    doc_number = serializers.SerializerMethodField()
    billed_at = serializers.DateTimeField(source="sale.billed_at", read_only=True, default=None)
    resolved_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ContinuityFlag
        fields = [
            "id",
            "kind",
            "kind_label",
            "status",
            "store_code",
            "doc_number",
            "billed_at",
            "details",
            "created_at",
            "cleared_note",
            "resolved_by_name",
            "resolved_at",
        ]

    def get_doc_number(self, obj: ContinuityFlag) -> str:
        # `sale_id` is what says whether there is a bill at all; that it also
        # settles `obj.sale` being non-null is the step mypy cannot take by
        # itself, so the fetch is bound to a name it can narrow.
        sale = obj.sale if obj.sale_id else None
        return (sale.doc_number or "") if sale is not None else ""

    def get_resolved_by_name(self, obj: ContinuityFlag) -> str:
        return display_name(obj.resolved_by)


class ContinuityFlagWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """Clearing one exception, which is a statement about a person's attention.

    Two answers and no way back to `open`. **Resolved** means the thing was dealt
    with; **ignored** means somebody looked and decided it needs nothing - which
    is a different sentence, worth keeping apart, and the one the nightly check
    reads to know not to raise it again.

    A `note` is required on `ignored` and optional on `resolved`, and the
    asymmetry is the point: "I dealt with it" is usually evidenced by the thing
    itself having changed, while "this one is fine" is evidenced by nothing at
    all unless the person says why.
    """

    status = serializers.ChoiceField(
        choices=[ContinuityFlag.Status.RESOLVED, ContinuityFlag.Status.IGNORED]
    )
    note = serializers.CharField(max_length=240, allow_blank=True, required=False, default="")

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs["note"] = attrs["note"].strip()
        if attrs["status"] == ContinuityFlag.Status.IGNORED and not attrs["note"]:
            raise serializers.ValidationError("Say why this one needs nothing doing.")
        return attrs


class TillNumberBlocksWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """What the counter holds of its invoice number blocks (ticket 04).

    ``held`` maps each block id the counter still holds to the next number it
    would take from it. A block it does not name is one it no longer holds.
    """

    held = serializers.DictField(child=serializers.IntegerField(min_value=1), required=False)


class TillNumberBlockSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    prefix = serializers.CharField()
    fy = serializers.CharField()
    month = serializers.CharField(help_text="YYYY-MM")
    first = serializers.IntegerField()
    last = serializers.IntegerField()


class TillNumberingSerializer(serializers.Serializer[dict[str, Any]]):
    on = serializers.BooleanField()
    new_format_from = serializers.DateField()
    prefix = serializers.CharField(allow_blank=True)
    block_size = serializers.IntegerField()
    blocks = TillNumberBlockSerializer(many=True)
    problem = serializers.CharField(allow_blank=True)


class CustomerDisplayPermitSerializer(serializers.Serializer[dict[str, Any]]):
    """The customer display's permit (ticket 09): the store it may show, nothing else.

    The bill itself never comes from the server; the till beside the display
    sends it through the browser.
    """

    store_code = serializers.CharField()


class ReturnWhereSerializer(serializers.Serializer[dict[str, Any]]):
    found = serializers.BooleanField(help_text="A bill with that number exists in the company.")
    refusal = serializers.CharField(
        allow_blank=True,
        help_text="Why this store cannot take it back: CROSS_GSTIN_RETURN, NO_GSTIN or "
        "ORIGINAL_ELSEWHERE; blank when it can, or when there is no such bill.",
    )
    message = serializers.CharField(allow_blank=True, help_text="Where it can be returned.")


class ConsentAnswerWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """One consent answer from the till (ticket 15, ST-CMP-6)."""

    id = serializers.UUIDField(help_text="The till's own id for this answer; a replay reuses it.")
    mobile = serializers.CharField(max_length=20)
    question = serializers.ChoiceField(choices=["bill", "offers"])
    given = serializers.BooleanField(help_text="Yes, or no (refused or withdrawn).")
    how = serializers.ChoiceField(
        choices=["display", "counter"],
        help_text="display: the customer tapped it; counter: staff recorded a withdrawal.",
    )
    under_18 = serializers.BooleanField(
        required=False, allow_null=True, default=None, help_text="Offers only."
    )
    wording_version = serializers.IntegerField(min_value=1)
    answered_at = serializers.DateTimeField(help_text="When the customer answered, till clock.")
    till_number = serializers.CharField(required=False, allow_blank=True, default="")


class ConsentStandingSerializer(serializers.Serializer[dict[str, Any]]):
    given = serializers.BooleanField()
    how = serializers.CharField()
    under_18 = serializers.BooleanField(allow_null=True)
    wording_version = serializers.IntegerField()
    answered_at = serializers.DateTimeField()


class ConsentStateSerializer(serializers.Serializer[dict[str, Any]]):
    """What stands for a number: the newest answer to each question, or null
    (never asked, which means off)."""

    mobile = serializers.CharField()
    bill = ConsentStandingSerializer(allow_null=True)
    offers = ConsentStandingSerializer(allow_null=True)


class ConsentRecordedSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField()
    state = ConsentStateSerializer()


# --- ticket 18: saved size per brand ---------------------------------------------


class SavedSizeCorrectionWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """One correction of a saved size at the till (ticket 18, ST-CUS-2)."""

    id = serializers.UUIDField(
        help_text="The till's own id for this correction; a retry reuses it."
    )
    mobile = serializers.CharField(max_length=20)
    brand = serializers.CharField(max_length=120)
    category = serializers.CharField(max_length=120)
    size = serializers.CharField(max_length=40, allow_blank=True, trim_whitespace=False)
    was_size = serializers.CharField(
        max_length=40, allow_blank=True, help_text="The size the till showed; refused if changed."
    )
    agreed = serializers.BooleanField(help_text="The customer agreed to the change.")


class SavedSizeRowSerializer(serializers.Serializer[dict[str, Any]]):
    brand = serializers.CharField()
    category = serializers.CharField()
    size = serializers.CharField()
    how = serializers.ChoiceField(
        choices=["bill", "staff"], help_text="bill: learned from a bill; staff: corrected."
    )
    as_of = serializers.DateTimeField(help_text="When the bill was made, or the correction made.")
    store_code = serializers.CharField()
    doc_number = serializers.CharField(allow_null=True, help_text="The bill it was learned from.")


class SavedSizesSerializer(serializers.Serializer[dict[str, Any]]):
    """The sizes that stand for a number: the newest per brand and category."""

    mobile = serializers.CharField()
    held = serializers.BooleanField(help_text="Whether a customer record holds this number.")
    sizes = SavedSizeRowSerializer(many=True)


# --- ticket 41: the day-close cash count ------------------------------------------


class CashCountWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """One day-close count from the till (ticket 41, ST-MNY-2)."""

    id = serializers.UUIDField(help_text="The till's own id for this count; a retry reuses it.")
    notes = serializers.DictField(
        child=serializers.IntegerField(min_value=0, max_value=100_000),
        help_text='Pieces of each note by face value in rupees: {"500": 3, "200": 0, ...}.',
    )
    coins_paise = serializers.IntegerField(default=0)
    opening_paise = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
        help_text="The opening float, typed only on the store's first count.",
    )
    expected_paise = serializers.IntegerField(
        help_text="The expected cash the screen showed; refused if it has changed since."
    )
    approved_by = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
        help_text="The manager who typed their own PIN, when the count differs.",
    )
    manager_pin = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=12,
        write_only=True,
        help_text="That manager's own PIN, checked again here; never stored.",
    )
    till_number = serializers.CharField(required=False, allow_blank=True, default="")


class CashCountReadSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    business_day = serializers.DateField()
    counted_at = serializers.DateTimeField()
    window_from = serializers.DateTimeField()
    opening_paise = serializers.IntegerField()
    opening_declared = serializers.BooleanField()
    cash_sales_paise = serializers.IntegerField()
    cash_refunds_paise = serializers.IntegerField()
    movements_paise = serializers.IntegerField()
    petty_cash_paise = serializers.IntegerField()
    petty_top_ups_paise = serializers.IntegerField()
    expected_paise = serializers.IntegerField()
    notes = serializers.DictField(child=serializers.IntegerField())
    coins_paise = serializers.IntegerField()
    counted_paise = serializers.IntegerField()
    variance_paise = serializers.IntegerField()
    counted_by = serializers.IntegerField(source="counted_by_id")
    counted_by_name = serializers.SerializerMethodField()
    approved_by = serializers.IntegerField(source="approved_by_id", allow_null=True)
    approved_by_name = serializers.SerializerMethodField()
    approved_at = serializers.DateTimeField(allow_null=True)

    def get_counted_by_name(self, row: Any) -> str:
        return display_name(row.counted_by)

    def get_approved_by_name(self, row: Any) -> str:
        return display_name(row.approved_by) if row.approved_by_id else ""


class CashMovementWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """Cash taken out of the drawer: a bank deposit or a handover (ticket 41)."""

    id = serializers.UUIDField(help_text="The till's own id for this movement; a retry reuses it.")
    kind = serializers.CharField(help_text="deposit or handover.")
    amount_paise = serializers.IntegerField()
    received_by = serializers.CharField(
        allow_blank=True, max_length=120, help_text="The bank, or the person who took the cash."
    )
    reference = serializers.CharField(
        allow_blank=True, max_length=64, help_text="The deposit slip or the signed receipt number."
    )
    till_number = serializers.CharField(required=False, allow_blank=True, default="")


class CashMovementReadSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    kind = serializers.CharField()
    amount_paise = serializers.IntegerField()
    given_by = serializers.IntegerField(source="given_by_id")
    given_by_name = serializers.SerializerMethodField()
    received_by = serializers.CharField()
    reference = serializers.CharField()
    recorded_at = serializers.DateTimeField()

    def get_given_by_name(self, row: Any) -> str:
        return display_name(row.given_by)


class CashPreviousSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    business_day = serializers.DateField()
    counted_paise = serializers.IntegerField()


class CashTendersSerializer(serializers.Serializer[dict[str, int]]):
    cash = serializers.IntegerField()
    card = serializers.IntegerField()
    upi = serializers.IntegerField()
    credit_note = serializers.IntegerField()


class CashPositionSerializer(serializers.Serializer[Any]):
    """The Z-report: the drawer since the previous count, and today's count if saved."""

    store = serializers.CharField(source="store.code")
    switched_on = serializers.BooleanField(
        help_text="Off: saved counts can be read, nothing new can be counted or recorded."
    )
    business_day = serializers.DateField()
    window_from = serializers.DateTimeField()
    window_to = serializers.DateTimeField()
    denominations = serializers.SerializerMethodField()
    opening_declared = serializers.BooleanField(source="first")
    opening_paise = serializers.IntegerField(allow_null=True)
    previous = CashPreviousSerializer(allow_null=True)
    cash_sales_paise = serializers.IntegerField()
    cash_refunds_paise = serializers.IntegerField()
    movements_paise = serializers.IntegerField()
    movements = CashMovementReadSerializer(many=True)
    petty_cash_paise = serializers.IntegerField(help_text="Petty cash spent (ticket 42).")
    petty_top_ups_paise = serializers.IntegerField(
        help_text="Petty cash brought from head office (ticket 42): new cash in the store."
    )
    expected_paise = serializers.IntegerField(allow_null=True)
    tenders = CashTendersSerializer()
    bills = serializers.IntegerField()
    counted = CashCountReadSerializer(allow_null=True)
    recent = serializers.SerializerMethodField()

    def get_denominations(self, _: Any) -> list[int]:
        from sell.services.cash_count import DENOMINATIONS

        return list(DENOMINATIONS)

    @extend_schema_field(CashCountReadSerializer(many=True))
    def get_recent(self, position: Any) -> list[dict[str, Any]]:
        from sell.services.cash_count import recent_counts

        return list(CashCountReadSerializer(recent_counts(position.store), many=True).data)
