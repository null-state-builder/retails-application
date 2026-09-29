"""Exchange and return tax at the accept pipeline (store operations ticket 13).

The rules themselves are plain arithmetic in ``sell.return_tax``, shared with the
till through the golden cases. This module is what the server needs around them:
which options are in force, which stores share a GSTIN, what a piece coming back
should have carried, and the credit note an exchange issues.

Everything here applies only to a bill that says the till took its returns by
these rules (``Sale.return_tax``). A store with the switch off - and every real
store while the CA sign-off gate is open - sends no such bill, and its returns
and exchanges work exactly as before (B5).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.db import models, transaction
from django.utils import timezone

from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from core.documents import DocStatus, VoucherSeries
from core.fiscal import financial_year
from masters.document_series import (
    DocumentSeries,
    NumberRefused,
    alert_on_refusal,
    issue_number,
    new_format_applies,
)
from masters.models import Store
from masters.store_feature_registry import EXCHANGE_RETURN_TAX
from masters.store_features import is_feature_on, is_real_store
from masters.tax_settings import in_force, saved_versions
from sell.models import (
    CREDIT_NOTE_DOC_TYPE,
    ContinuityFlag,
    ExchangeCreditNote,
    Sale,
    SaleLine,
    SaleTender,
)
from sell.return_tax import (
    CROSS_GSTIN,
    Leg,
    OriginalLine,
    ReturnTaxOptions,
    bank_shares,
    credit_note_deadline,
    gstin_refusal,
    options_from_json,
    return_leg,
)

FEATURE_KEY = EXCHANGE_RETURN_TAX

#: The audit action every credit note an exchange issues is recorded under.
ISSUE_ACTION = "sell.exchange_credit_note.issue"

#: A refusal: ``(contract code, words for the counter)``.
Refused = tuple[str, str]


def return_tax_on(store: Store) -> bool:
    """Is the store's switch on (and not held by its gate)?"""
    return is_feature_on(store, FEATURE_KEY)


def held_by_gate(store: Store) -> bool:
    """Is the switch held off here by its gate (a real store, B1, B2)?"""
    return is_real_store(store) and not return_tax_on(store)


def options_at(store: Store, at: datetime) -> ReturnTaxOptions:
    """The ticket 13 options of the tax version in force at ``at``.

    Read from the newest saved version whatever the store's tax-settings switch:
    these are facts and choices, not rates, and a filing date Accounts recorded
    must never be missed because rates are still taken from the slab table. With
    no version saved, the baselines: no cross-GSTIN returns, no filing date.
    """
    version = in_force(saved_versions(store.tenant_id), timezone.localdate(at), at)
    return options_from_json(version.options_json() if version is not None else None)


def gstin_of(store: Store) -> str:
    """The store's GSTIN as recorded, or "" when none is."""
    registration = store.gstin if store.gstin_id else None
    return (registration.gstin if registration is not None else "").strip().upper()


def stores_under(gstin: str, tenant_id: Any) -> list[Store]:
    """The open stores registered under ``gstin`` - where a bill of it can go back."""
    if not gstin:
        return []
    return list(
        Store.objects.filter(
            tenant_id=tenant_id,
            gstin__gstin__iexact=gstin,
            is_active=True,
            store_type=Store.StoreType.STORE,
        ).order_by("code")
    )


def refusal_words(code: str, *, original_store: Store, billing_store: Store) -> str:
    """What the counter says when it will not take a bill back (ST-CMP-2)."""
    if code != CROSS_GSTIN:
        missing = billing_store if not gstin_of(billing_store) else original_store
        return (
            f"{missing.name} ({missing.code}) has no GSTIN recorded, so no credit note can "
            "be issued for this return. Head office must record the store's GSTIN first."
        )
    gstin = gstin_of(original_store)
    where = stores_under(gstin, original_store.tenant_id) or [original_store]
    names = ", ".join(f"{store.name} ({store.code})" for store in where[:6])
    more = f" and {len(where) - 6} more" if len(where) > 6 else ""
    return (
        f"This bill was issued by {original_store.name} ({original_store.code}) under GSTIN "
        f"{gstin}. A return is taken only within the GSTIN that issued the bill, so it can "
        f"be returned at {names}{more}."
    )


def refuse_elsewhere(store: Store, data: dict[str, Any]) -> Refused | None:
    """Should an exchange of this bill be refused for where its original is from?

    Checked before anything is written. ``exchange.original.store`` names the
    store that issued the original; absent, it is this store (every till so far).
    """
    ref = ((data.get("exchange") or {}).get("original") or {}).get("store") or ""
    original_store = store
    if ref and ref.strip().upper() != store.code.upper():
        found = (
            Store.objects.filter(tenant_id=store.tenant_id, code__iexact=ref.strip())
            .select_related("gstin")
            .first()
        )
        if found is None:
            return "VALIDATION", f"No store with code '{ref}'."
        original_store = found
    options = options_at(store, data["billed_at"])
    code = gstin_refusal(gstin_of(original_store), gstin_of(store), options)
    if code is not None:
        contract = "CROSS_GSTIN_RETURN" if code == CROSS_GSTIN else "NO_GSTIN"
        words = refusal_words(code, original_store=original_store, billing_store=store)
        return contract, words
    if original_store.pk != store.pk:
        # Within the GSTIN, but this counter only ever held its own store's
        # bills: the pieces, their cost and what came back are that store's.
        return (
            "ORIGINAL_ELSEWHERE",
            f"This bill was issued by {original_store.name} ({original_store.code}). This "
            f"counter takes back only {store.code}'s own bills; take it to "
            f"{original_store.name}.",
        )
    return None


def where_to_return(store: Store, doc: str, at: datetime) -> dict[str, Any]:
    """Can this store take back the bill a customer's copy names, and if not where?

    For the counter's "find the bill" door, asked only when the bill is not one
    of this store's own. It answers where the bill can go back and never what is
    on it: no lines, no customer, no money - a store person reads their own
    store's bills and nobody else's.
    """
    wanted = doc.strip()
    bill = (
        Sale.objects.filter(store__tenant_id=store.tenant_id, docstatus=DocStatus.SUBMITTED)
        .filter(models.Q(doc_number__iexact=wanted) | models.Q(tax_invoice_number__iexact=wanted))
        .select_related("store", "store__gstin")
        .first()
        if wanted
        else None
    )
    if bill is None:
        return {"found": False, "refusal": "", "message": ""}
    if bill.store_id == store.pk:
        return {"found": True, "refusal": "", "message": ""}
    refused = refuse_elsewhere(
        store,
        {"billed_at": at, "exchange": {"original": {"store": bill.store.code}}},
    )
    code, message = refused if refused is not None else ("", "")
    return {"found": True, "refusal": code, "message": message}


@dataclass(frozen=True)
class Judged:
    """What the server says a piece coming back should carry."""

    leg: Leg
    deadline: date


def returned_bank_so_far(original: SaleLine) -> int:
    """What earlier returns of this line already took off its bank-offer share."""
    return int(
        sum(
            SaleLine.objects.filter(original_line=original, direction=SaleLine.Direction.RETURN)
            .exclude(sale__docstatus=DocStatus.CANCELLED)
            .values_list("bank_offer_paise", flat=True)
        )
    )


def bank_offer_paise_of(sale: Sale) -> int:
    """What the bank offers paid of a bill (ticket 11's "bank offer" payments)."""
    return sum(
        int(tender.amount_paise)
        for tender in sale.tenders.all()
        if tender.mode == SaleTender.Mode.BANK_OFFER
    )


def judge(
    original: SaleLine,
    qty: int,
    *,
    returned: tuple[int, int, int],
    exchange_at: datetime,
    options: ReturnTaxOptions,
) -> Judged:
    """The leg the server works out for ``qty`` pieces of ``original``.

    ``returned`` is ``(qty, paise, bank paise)`` already given back off the line
    before this leg (``refunds.returned_so_far`` and ``returned_bank_so_far``).
    """
    bill = original.sale
    deadline = credit_note_deadline(
        timezone.localdate(bill.billed_at), gstin_of(bill.store), options
    )
    late = timezone.localdate(exchange_at) > deadline
    # Ticket 22: no bank offer reached an alteration charge, so none is spread on it
    # - the till spreads it over the goods lines only, the same way.
    sold = [
        line
        for line in bill.lines.all()
        if line.direction == SaleLine.Direction.SALE and not line.is_alteration
    ]
    shares = bank_shares(
        [(line.line_no, int(line.net_paise)) for line in sold], bank_offer_paise_of(bill)
    )
    leg = return_leg(
        OriginalLine(
            line_no=original.line_no,
            qty=original.qty,
            net_paise=int(original.net_paise),
            gst_rate=original.gst_rate,
            returned_qty=returned[0],
            returned_paise=returned[1],
            bank_offer_paise=shares.get(original.line_no, 0),
            returned_bank_paise=returned[2],
        ),
        qty,
        late=late,
    )
    return Judged(leg=leg, deadline=deadline)


# --- the credit note ------------------------------------------------------------


def _number(store: Store, day: date, sale: Sale) -> tuple[str | None, str, dict[str, Any] | None]:
    """The credit note's number: ``(number, series, problem)``.

    The new series ``XXX/CN/2627/n`` where ticket 04's new format applies, else
    today's credit-note numbering. A number that cannot be issued is never
    guessed: the note is written without one and the bill is flagged, because
    the exchange has already happened at the counter.
    """
    if new_format_applies(store, day):
        try:
            with alert_on_refusal(), transaction.atomic():
                issued = issue_number(
                    series=DocumentSeries.CREDIT_NOTE,
                    site=store,
                    on=day,
                    document_type="exchange_credit_note",
                    document_ref=str(sale.pk),
                )
        except NumberRefused as refused:
            return (
                None,
                ExchangeCreditNote.Series.NEW,
                {
                    "reason": refused.reason,
                    "message": refused.message,
                },
            )
        return issued.number, ExchangeCreditNote.Series.NEW, None
    try:
        with transaction.atomic():
            _, number = VoucherSeries.allocate(
                fy=financial_year(day), store_code=store.code, doc_type=CREDIT_NOTE_DOC_TYPE
            )
    except VoucherSeries.DoesNotExist:
        return (
            None,
            ExchangeCreditNote.Series.TODAY,
            {
                "reason": "no_series",
                "message": f"{store.code} has no credit-note series for {financial_year(day)}.",
            },
        )
    return number, ExchangeCreditNote.Series.TODAY, None


def issue_credit_note(
    sale: Sale,
    store: Store,
    legs: list[SaleLine],
    *,
    original_sale: Sale | None,
    deadline: date | None,
    late: bool,
    actor: Any,
) -> tuple[ExchangeCreditNote, list[tuple[str, dict[str, Any]]]]:
    """Issue the credit note for the pieces this bill took back, and audit it.

    Called inside the accept transaction, after the bill has its number. ``late``
    is what the bill did: its legs reduced no tax (past the deadline). Answers
    the note and the flags to raise on the bill.
    """
    day = timezone.localdate(sale.billed_at)
    value = sum(int(line.net_paise) for line in legs)
    gst = sum(int(line.gst_paise) for line in legs)
    bank = sum(int(line.bank_offer_paise) for line in legs)
    number, series, problem = _number(store, day, sale)
    note = ExchangeCreditNote.objects.create(
        sale=sale,
        store=store,
        original_sale=original_sale,
        gstin=gstin_of(store),
        number=number,
        series=series,
        issued_on=day,
        value_paise=value,
        gst_paise=gst,
        bank_offer_paise=bank,
        late=late and gst == 0,
        deadline=deadline,
    )
    _audit(note, sale, store, actor)
    flags: list[tuple[str, dict[str, Any]]] = []
    if problem is not None:
        flags.append((ContinuityFlag.Kind.CREDIT_NOTE_NUMBER, {"series": series, **problem}))
    return note, flags


def note_json(note: ExchangeCreditNote) -> dict[str, Any]:
    """The credit note as the bill's read shape and its audit record carry it."""
    return {
        "number": note.number or "",
        "series": note.series,
        "issued_on": note.issued_on.isoformat(),
        "gstin": note.gstin,
        "value_paise": int(note.value_paise),
        "taxable_paise": note.taxable_paise,
        "gst_paise": int(note.gst_paise),
        "bank_offer_paise": int(note.bank_offer_paise),
        "credit_paise": note.credit_paise,
        "late": note.late,
        "deadline": note.deadline.isoformat() if note.deadline else None,
        "original": note.original_sale.doc_number if note.original_sale else None,
        # Projected from its bill, never written on the note: a cancelled
        # exchange's credit note is cancelled with it (overall PRD §8.3). Its
        # number stays used.
        "status": "cancelled" if note.sale.docstatus == DocStatus.CANCELLED else "issued",
    }


def _audit(note: ExchangeCreditNote, sale: Sale, store: Store, actor: Any) -> None:
    """One audit record for the credit note: nothing before, the note after.

    The command id is derived from the bill's own key, so a replay is the same
    record. A login that is not a person is recorded as the till's sync, still
    naming the login (the same rule as a split sale's shares, ticket 08).
    """
    human_id = getattr(actor, "human_id", None)
    tenant_id = getattr(actor, "tenant_id", None)
    user_id = getattr(actor, "pk", None)
    principal = (
        Principal(tenant_id=tenant_id, human_id=human_id, user_id=user_id)
        if human_id is not None and tenant_id is not None
        else Principal(tenant_id=store.tenant_id, service_code="till-sync", user_id=user_id)
    )
    subject = f"exchange_credit_note:{note.pk}"

    def handler(run: CommandRun) -> CommandResult:
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = {"doc_number": sale.doc_number, "credit_note": None}
        run.audit_after = {"doc_number": sale.doc_number, "credit_note": note_json(note)}
        return CommandResult(
            resource_type="exchange_credit_note", resource_id=str(note.pk), status_code=201
        )

    execute_command(
        principal,
        CommandSpec(
            action=ISSUE_ACTION,
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"exchange-cn:{sale.idempotency_uuid}"),
            business_input={"idempotency_uuid": str(sale.idempotency_uuid)},
            site_id=store.pk,
            subject_key=subject,
        ),
        handler,
    )
