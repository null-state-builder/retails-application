"""The customer page and the customer's rights (store operations ticket 16, ST-CUS-1).

Store operations PRD §8: the customer record holds a name, an optional GSTIN,
the two consents (ST-CMP-6) and purchase history taken from bills. At the
customer's request staff can show what is held about them, correct it,
withdraw a consent, or erase it; each action is recorded. Erasure removes the
profile, sizes, consents and marketing history - never a tax invoice: the name,
GSTIN and address printed on a bill stay with that bill.

**Who finds whom.** A customer is found by a person when a bill, or a consent
answer, at a store they may read carries that customer's number - and only at
stores where the ``customer-rights`` switch is on. Their purchase history lists
those stores' bills only. A brand-scoped login reads no store, so finds nobody.
Reading needs ``sell: view``; acting on a request needs ``sell: operate`` at a
store the person may act at, with the switch on there.

**What erasure removes.** The customer row (the phone book the tills copy),
this company's consent answers for the number, their saved sizes (ticket 18)
and - once it exists - marketing history (ST-CUS-4). Bills are untouched. A row
in ``CustomerErasure`` says an erasure happened and when, so every till replaces
its copy of the customer list at its next sync (``sell.services.dataset``).

**Merge and a new number (ticket 17).** With the customer present, staff merge
another record for the same person into the one they keep, or move a record to
a new number. Both need the ``customer-merge-retention`` switch on at the store
as well. Bills are never rewritten: ``sell.services.customer_numbers`` says which
bills and answers are the person's, through every number they have had.

**What the audit log keeps.** Every write is one command with values before and
after, but the log outlives an erasure and is read by people with no need of a
customer's details, so a number shows its last four digits only, and a name or
GSTIN shows only its first characters (baseline, see the ticket 16 decisions).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from django.db.models import (
    Count,
    DateTimeField,
    Exists,
    F,
    Max,
    Min,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce, Greatest

from accounts.permissions import user_can
from accounts.sections import CAP_OPERATE, CAP_VIEW
from core.commands import CommandResult, CommandRun, LockRank, register_integrity_refusal
from core.documents import DocStatus
from core.refusals import Refusal, issue
from core.tenancy import current_tenant_id
from masters.consent_wording import current_wording
from masters.models import Customer, CustomerNumber, Store
from masters.scoping import actionable_store_ids, active_store_ids, is_brand_scoped, visible_store_ids
from masters.store_feature_registry import CUSTOMER_MERGE_RETENTION, CUSTOMER_RIGHTS
from masters.store_features import feature, is_feature_on, switch_states
from sell.gstin import describe as describe_gstin
from sell.gstin import normalise as normalise_gstin
from sell.models import ConsentAnswer, CustomerErasure, Sale, SavedSize
from sell.services.consent import consent_state, masked, newest
from sell.services.customer_numbers import Number, answers_q, bills_q, numbers_of
from sell.services.customers import normalise_mobile
from sell.services.saved_sizes import as_json as saved_size_json
from sell.services.saved_sizes import standing as saved_sizes

SHOW_ACTION = "sell.customer_rights.show"
CORRECT_ACTION = "sell.customer_rights.correct"
ERASE_ACTION = "sell.customer_rights.erase"
MERGE_ACTION = "sell.customer_rights.merge"
MOVE_ACTION = "sell.customer_rights.move"

#: How many customers a list or search answers with. A search narrows it.
LIST_LIMIT = 50
#: How many of a customer's bills the page lists, newest first.
BILL_LIMIT = 100
#: What the rights screen records a withdrawal's counter as.
RIGHTS_TILL = "Customers page"
NAME_MAX = 120

OFF_MESSAGE = (
    "Customer page and rights is switched off at your stores. "
    "Admin can switch it on in Setup, Feature Switches."
)
NUMBER_HELD_MESSAGE = (
    "That number already has a record. If it is the same person, merge the "
    "two records instead. Nothing was changed."
)
# Two moves onto one number at once: the second meets the unique number.
register_integrity_refusal(
    "masters_customer_mobile_key", "CUSTOMER_NUMBER_HELD", NUMBER_HELD_MESSAGE
)

MERGE_OFF_MESSAGE = (
    "Merging records and moving to a new number are switched off at this store. "
    "Admin can switch them on in Setup, Feature Switches."
)
#: The earliest moment, for "no earlier holder": a number with no move is theirs always.
_EPOCH = Value(datetime(1970, 1, 1, tzinfo=UTC), output_field=DateTimeField())
#: The latest moment, for "still theirs": a number nobody moved away from.
_FOREVER = Value(datetime(9999, 12, 31, tzinfo=UTC), output_field=DateTimeField())


# -- who may do what, where ------------------------------------------------------------


def _switched_on(stores: list[Store]) -> list[Store]:
    states = switch_states(stores, [feature(CUSTOMER_RIGHTS)])
    return [store for store, state in zip(stores, states, strict=True) if state.enabled]


def _company_stores(user: Any) -> QuerySet[Store]:
    """Stores of the person's own company: a customer's page never crosses one."""
    tenant_id = getattr(user, "tenant_id", None) or current_tenant_id()
    if tenant_id is None:
        return Store.objects.none()
    return Store.objects.filter(tenant_id=tenant_id).order_by("code")


def _scope_stores(user: Any) -> list[Store]:
    """Every store in the person's read scope (the switcher narrows it). None for
    a brand-scoped login: customers are found through stores."""
    if is_brand_scoped(user):
        return []
    ids = active_store_ids(user, section="sell", minimum="view")
    permitted = visible_store_ids(user, section="sell", minimum=CAP_VIEW)
    rows = _company_stores(user)
    if permitted is not None:
        rows = rows.filter(pk__in=permitted)
    if ids is not None:
        rows = rows.filter(pk__in=ids)
    return list(rows)


def reading_stores(user: Any) -> list[Store]:
    """The stores whose customers this person reads: in scope, switch on."""
    return _switched_on(_scope_stores(user))


def acting_stores(user: Any) -> list[Store]:
    """The stores this person may act at on a customer's request, switch on. Empty
    without ``sell: operate``."""
    if not may_act(user):
        return []
    ids = actionable_store_ids(user, section="sell", minimum=CAP_OPERATE)
    rows = _company_stores(user).filter(is_active=True)
    if ids is not None:
        rows = rows.filter(pk__in=ids)
    return _switched_on(list(rows))


def may_read(user: Any) -> bool:
    return user_can(user, "sell", CAP_VIEW)


def may_act(user: Any) -> bool:
    return user_can(user, "sell", CAP_OPERATE)


def require_reader(user: Any) -> list[Store]:
    """The reading stores, or the refusal: no ``sell``, no store, or switched off."""
    if not may_read(user):
        raise Refusal("ACTION_DENIED", "You do not have access to customers.")
    scope = _scope_stores(user)
    if not scope:
        raise Refusal("ACTION_DENIED", "Customers are found through stores, and you have none.")
    stores = _switched_on(scope)
    if not stores:
        raise Refusal("FEATURE_OFF", OFF_MESSAGE, status=403)
    return stores


def acting_store(user: Any, site_id: Any) -> Store:
    """The store a rights action is recorded at: one the person may act at, switch on."""
    if not may_act(user):
        raise Refusal(
            "ACTION_DENIED",
            "Only store staff can act on a customer's request. You can read this page.",
        )
    try:
        wanted = int(str(site_id))
    except (TypeError, ValueError):
        raise Refusal(
            "INVALID_REQUEST",
            "Say which store the customer is at.",
            issues=[issue("REQUIRED", "site_id is a store id", field="site_id")],
        ) from None
    store = next((s for s in acting_stores(user) if s.pk == wanted), None)
    if store is not None:
        return store
    ids = actionable_store_ids(user, section="sell", minimum=CAP_OPERATE)
    open_here = _company_stores(user).filter(pk=wanted, is_active=True).exists()
    if open_here and (ids is None or wanted in ids):
        raise Refusal("FEATURE_OFF", OFF_MESSAGE, status=403)
    raise Refusal("ACTION_DENIED", "You cannot act for that store.")


# -- finding customers -----------------------------------------------------------------


def _bills_at(store_ids: list[int]) -> QuerySet[Sale]:
    return Sale.objects.filter(store_id__in=store_ids).exclude(customer_mobile="")


def _answers_at(store_ids: list[int]) -> QuerySet[ConsentAnswer]:
    return ConsentAnswer.objects.filter(store_id__in=store_ids)


def visible_customers(stores: list[Store]) -> QuerySet[Customer]:
    """Open customers with a bill or a consent answer at one of these stores,
    through any of their numbers, annotated with how many bills there on their
    own number and the newest one's time (the list's order; ``bill_stats`` gives
    a customer with other numbers their whole count).

    A record closed by a merge is not listed: its history is the kept record's."""
    ids = [store.pk for store in stores]
    moved = CustomerNumber.objects.filter(reason=CustomerNumber.Reason.MOVED)
    # Their own number is theirs after any earlier holder moved away from it, and
    # after they moved onto it (``numbers_of`` says the same in Python).
    taken_over = (
        moved.filter(mobile=OuterRef("mobile"))
        .filter(Q(customer__isnull=True) | ~Q(customer=OuterRef("pk")))
        .order_by("-until")
        .values("until")[:1]
    )
    own_since = Greatest(
        Coalesce(Subquery(taken_over), _EPOCH), Coalesce(F("mobile_since"), _EPOCH)
    )
    bills = _bills_at(ids).filter(
        customer_mobile=OuterRef("mobile"), billed_at__gt=OuterRef("number_since")
    )
    answers = _answers_at(ids).filter(
        mobile=OuterRef("mobile"), answered_at__gt=OuterRef("number_since")
    )
    counted = (
        bills.order_by().values("customer_mobile").annotate(n=Count("id"), last=Max("billed_at"))
    )
    # Their other numbers: each in its own window, the same way.
    other_taken_over = (
        moved.filter(mobile=OuterRef("mobile"), until__lt=Coalesce(OuterRef("until"), _FOREVER))
        .filter(Q(customer__isnull=True) | ~Q(customer=OuterRef("customer")))
        .order_by("-until")
        .values("until")[:1]
    )
    others = (
        CustomerNumber.objects.filter(customer=OuterRef("pk"))
        .annotate(
            held_since=Greatest(
                Coalesce(Subquery(other_taken_over), _EPOCH), Coalesce(F("since"), _EPOCH)
            ),
            held_until=Coalesce(F("until"), _FOREVER),
        )
        .filter(
            Exists(
                _bills_at(ids).filter(
                    customer_mobile=OuterRef("mobile"),
                    billed_at__gt=OuterRef("held_since"),
                    billed_at__lte=OuterRef("held_until"),
                )
            )
        )
    )
    return (
        Customer.objects.filter(merged_into__isnull=True)
        .annotate(number_since=own_since)
        .filter(Q(Exists(bills)) | Q(Exists(answers)) | Q(Exists(others)))
        .annotate(
            bills_here=Subquery(counted.values("n")[:1]),
            last_bill_at=Subquery(counted.values("last")[:1]),
            has_other_numbers=Exists(CustomerNumber.objects.filter(customer=OuterRef("pk"))),
        )
    )


def search_customers(stores: list[Store], q: str) -> tuple[list[Customer], bool]:
    """Newest buyers first. ``q`` of digits searches every number the customer has
    had (so an old number finds them), words the name."""
    rows = visible_customers(stores)
    term = (q or "").strip()
    if term:
        digits = normalise_mobile(term) if any(ch.isdigit() for ch in term) else ""
        if digits and not any(ch.isalpha() for ch in term):
            had = CustomerNumber.objects.filter(customer=OuterRef("pk"), mobile__contains=digits)
            rows = rows.filter(Q(mobile__contains=digits) | Q(Exists(had)))
        else:
            rows = rows.filter(name__icontains=term)
    # A customer known only by a consent answer has no bill: listed last.
    newest_first = F("last_bill_at").desc(nulls_last=True)
    found = list(rows.order_by(newest_first, "name", "mobile")[: LIST_LIMIT + 1])
    return found[:LIST_LIMIT], len(found) > LIST_LIMIT


def bill_stats(stores: list[Store], customer: Customer) -> tuple[int, Any]:
    """How many bills at these stores, and the newest one's time, on every number."""
    if not getattr(customer, "has_other_numbers", True):
        return getattr(customer, "bills_here", None) or 0, getattr(customer, "last_bill_at", None)
    span = _bills_of(stores, customer).aggregate(n=Count("id"), last=Max("billed_at"))
    return span["n"], span["last"]


def find_customer(stores: list[Store], pk: int) -> Customer:
    """One open customer this person may see, or ``NOT_FOUND`` - never a hint that
    somebody exists at a store they cannot read, or a record closed by a merge."""
    customer = visible_customers(stores).filter(pk=pk).first()
    if customer is None:
        raise Refusal("NOT_FOUND", "No customer like that at your stores.", status=404)
    return customer


def _bills_of(stores: list[Store], customer: Customer) -> QuerySet[Sale]:
    """This customer's bills at these stores, on every number they have had."""
    return Sale.objects.filter(store_id__in=[store.pk for store in stores]).filter(
        bills_q(numbers_of(customer))
    )


# -- the page --------------------------------------------------------------------------


def purchase_history(stores: list[Store], customer: Customer) -> tuple[list[dict[str, Any]], int]:
    """This customer's bills at these stores on every number they have had,
    newest first, and how many in all."""
    rows = _bills_of(stores, customer)
    total = rows.count()
    bills = [
        {
            "id": sale.pk,
            "doc_number": sale.tax_invoice_number or sale.doc_number or "",
            "billed_at": sale.billed_at,
            "store_code": sale.store.code,
            "customer_name": sale.customer_name,
            "buyer_gstin": sale.buyer_gstin,
            "net_paise": sale.net_paise,
            "exchange": sale.exchange_of_id is not None,
            "cancelled": sale.docstatus == DocStatus.CANCELLED,
        }
        for sale in rows.select_related("store").order_by("-billed_at", "-id")[:BILL_LIMIT]
    ]
    return bills, total


def version_of(customer: Customer) -> dict[str, str]:
    """What a correction must have been typed against: the values it saw."""
    return {"name": customer.name, "gstin": customer.gstin}


def page(user: Any, stores: list[Store], customer: Customer) -> dict[str, Any]:
    bills, total = purchase_history(stores, customer)
    tenant_id = stores[0].tenant_id
    acting = acting_stores(user)
    numbers = numbers_of(customer)
    return {
        "id": customer.pk,
        "name": customer.name,
        "mobile": customer.mobile,
        "gstin": customer.gstin,
        "consent": _consent(tenant_id, numbers[0]),
        "bills": bills,
        "bills_total": total,
        "numbers": [
            {"mobile": n.mobile, "reason": n.reason, "until": n.until} for n in numbers[1:]
        ],
        "stores": [{"id": s.pk, "code": s.code, "name": s.name} for s in acting],
        "can_act": bool(acting),
        "can_merge_or_move": any(is_feature_on(s, CUSTOMER_MERGE_RETENTION) for s in acting),
    }


def _consent(tenant_id: Any, own: Number) -> dict[str, Any]:
    """Both answers standing for their own number. An answer from before the
    number was theirs (an earlier holder's) is not theirs: never asked."""
    state = consent_state(tenant_id, own.mobile)

    def theirs(standing: dict[str, Any] | None) -> dict[str, Any] | None:
        if standing is None or own.since is None:
            return standing
        answered = datetime.fromisoformat(str(standing["answered_at"]))
        return standing if answered > own.since else None

    return {"bill": theirs(state["bill"]), "offers": theirs(state["offers"])}


def held(customer: Customer, tenant_id: Any, stores: list[Store]) -> dict[str, Any]:
    """Everything held about this customer, for them to see (the "show" right):
    on every number they have had."""
    numbers = numbers_of(customer)
    answers = (
        ConsentAnswer.objects.filter(store__tenant_id=tenant_id)
        .filter(answers_q(numbers))
        .select_related("store")
        .order_by("-answered_at", "-received_at")
    )
    span = _bills_of(stores, customer).aggregate(
        first=Min("billed_at"), last=Max("billed_at"), n=Count("id")
    )
    return {
        "profile": {
            "name": customer.name,
            "mobile": customer.mobile,
            "gstin": customer.gstin,
            "first_recorded": customer.created_at,
            "last_changed": customer.updated_at,
        },
        "numbers": [
            {"mobile": n.mobile, "reason": n.reason, "until": n.until} for n in numbers[1:]
        ],
        "consent_answers": [
            {
                "question": row.question,
                "given": row.given,
                "how": row.how,
                "under_18": row.under_18,
                "wording_version": row.wording_version,
                "answered_at": row.answered_at,
                "store_code": row.store.code,
                "till_number": row.till_number,
                "mobile": row.mobile,
            }
            for row in answers
        ],
        "bills": {"count": span["n"], "first_at": span["first"], "last_at": span["last"]},
        # Ticket 18: what stands per brand and category, in this company.
        "sizes": [
            saved_size_json(row, {s.pk for s in stores}) for row in saved_sizes(customer, tenant_id)
        ],
        # Not recorded anywhere yet (ST-CUS-4); said, not hidden.
        "marketing": [],
    }


# -- audit values ----------------------------------------------------------------------


def masked_text(value: str) -> str:
    """A name as the audit log shows it: each word's first character."""
    return " ".join(word[0] + "*" * (len(word) - 1) for word in (value or "").split())


def audit_profile(customer: Customer) -> dict[str, Any]:
    return {
        "mobile": masked(customer.mobile),
        "name": masked_text(customer.name),
        # The state code only: the rest of a GSTIN carries the holder's PAN.
        "gstin": customer.gstin[:2] + "*" * (len(customer.gstin) - 2) if customer.gstin else "",
    }


# -- the writes ------------------------------------------------------------------------


def show_handler(customer_id: int, store: Store) -> Any:
    """Record that what is held was shown to the customer. Nothing changes."""

    def handler(run: CommandRun) -> CommandResult:
        customer = Customer.objects.filter(pk=customer_id).first()
        if customer is None:
            raise Refusal("NOT_FOUND", "This customer is no longer held.", status=404)
        run.audit_before = None
        run.audit_after = {**audit_profile(customer), "shown_at_store": store.code}
        return CommandResult(resource_type="customer", resource_id=str(customer_id))

    return handler


@dataclass(frozen=True)
class Correction:
    name: str
    gstin: str
    was_name: str
    was_gstin: str


def parse_correction(body: dict[str, Any]) -> Correction:
    name = " ".join(str(body.get("name") or "").split())
    if len(name) > NAME_MAX:
        raise Refusal(
            "INVALID_REQUEST",
            f"A name is at most {NAME_MAX} characters.",
            issues=[issue("INVALID", f"name is at most {NAME_MAX} characters", field="name")],
        )
    gstin = normalise_gstin(str(body.get("gstin") or ""))
    problem = describe_gstin(gstin)
    if problem:
        raise Refusal(
            "INVALID_REQUEST",
            f"That GSTIN cannot be saved. {problem}",
            issues=[issue("INVALID", problem, field="gstin")],
        )
    return Correction(
        name=name,
        gstin=gstin,
        was_name=str(body.get("was_name") or ""),
        was_gstin=str(body.get("was_gstin") or ""),
    )


def correct_handler(customer_id: int, store: Store, change: Correction) -> Any:
    """Correct the name and GSTIN held. The number is changed by ticket 17's move."""

    def handler(run: CommandRun) -> CommandResult:
        rows = run.lock(LockRank.DOCUMENT, Customer.objects.filter(pk=customer_id))
        if not rows:
            raise Refusal("NOT_FOUND", "This customer is no longer held.", status=404)
        customer = rows[0]
        if (customer.name, customer.gstin) != (change.was_name, change.was_gstin):
            raise Refusal(
                "REVISION_SUPERSEDED",
                "Someone changed this customer after you opened the page. "
                "Reload and check what is held now.",
            )
        if (customer.name, customer.gstin) == (change.name, change.gstin):
            raise Refusal("INVALID_REQUEST", "Nothing was changed.")
        run.audit_before = audit_profile(customer)
        changed = [
            field
            for field, value in (("name", change.name), ("gstin", change.gstin))
            if getattr(customer, field) != value
        ]
        customer.name, customer.gstin = change.name, change.gstin
        # A bill made before now never writes its older values back (upsert).
        customer.corrected_at = run.now
        # `updated_at` moves: every till learns the correction at its next sync.
        customer.save(update_fields=[*changed, "corrected_at", "updated_at"])
        run.audit_after = {**audit_profile(customer), "changed": changed}
        return CommandResult(resource_type="customer", resource_id=str(customer_id))

    return handler


def withdrawal(
    customer: Customer, question: str, answer_id: uuid.UUID, now: Any, tenant_id: Any
) -> dict[str, Any]:
    """The consent answer a withdrawal on the rights screen records: a "no" at the
    counter, through ticket 15's own path, so it is audited as every answer is."""
    if question not in ConsentAnswer.Question.values:
        raise Refusal(
            "INVALID_REQUEST",
            "Say which consent to withdraw: the bill or offers.",
            issues=[issue("INVALID", "question is bill or offers", field="question")],
        )
    return {
        "id": answer_id,
        "mobile": customer.mobile,
        "question": question,
        "given": False,
        "how": ConsentAnswer.How.COUNTER,
        "wording_version": current_wording(tenant_id).version,
        "answered_at": now,
        "till_number": RIGHTS_TILL,
    }


def remove_profile(run: CommandRun, customer: Customer, store: Store) -> dict[str, Any]:
    """Remove what is held about a customer, on every number they have had: the
    record, any record merged into it, their other numbers and this company's
    consent answers. Bills are never touched. Shared by erasure (ticket 16) and
    the retention clean-up (ticket 17). Returns counts for the audit record."""
    numbers = numbers_of(customer)
    # The lock a consent answer takes, so none lands between count and delete.
    run.advisory_lock(LockRank.DOCUMENT, [f"consent:{n.mobile}" for n in numbers])
    answers = ConsentAnswer.objects.filter(store__tenant_id=store.tenant_id).filter(
        answers_q(numbers)
    )
    # Every bill is kept, not only those at the stores a person reads.
    bills_kept = Sale.objects.filter(store__tenant_id=store.tenant_id).filter(bills_q(numbers))
    records = 1 + Customer.objects.filter(merged_into=customer).count()
    # Ticket 18: every saved size of theirs, in every company - the record the
    # rows belong to is one row for all of them (B96).
    sizes = SavedSize.objects.filter(Q(customer=customer) | Q(customer__merged_into=customer))
    counts = {
        "saved_sizes": sizes.count(),
        "consent_answers": answers.count(),
        "bills_kept": bills_kept.count(),
        "records_removed": records,
    }
    removed, _ = answers.delete()
    sizes.delete()
    # A number they moved away from stays as a bare handover, joined to nobody, so
    # its next holder never sees their bills or answers from before (B117).
    CustomerNumber.objects.filter(customer=customer, reason=CustomerNumber.Reason.MOVED).update(
        customer=None
    )
    # The records merged into it and its merged numbers go with it (cascade).
    customer.delete()
    # Every till replaces its customer list at its next sync.
    CustomerErasure.objects.create(id=uuid.uuid4(), store=store, erased_at=run.now)
    return {**counts, "consent_answers_removed": removed}


def erase_handler(customer_id: int, store: Store, last4: str) -> Any:
    """Erase what is held about a customer. Bills are never touched."""

    def handler(run: CommandRun) -> CommandResult:
        rows = run.lock(LockRank.DOCUMENT, Customer.objects.filter(pk=customer_id))
        if not rows or rows[0].merged_into_id is not None:
            raise Refusal("NOT_FOUND", "This customer is no longer held.", status=404)
        customer = rows[0]
        if customer.mobile[-4:] != last4:
            raise Refusal(
                "INVALID_REQUEST",
                "The last four digits do not match this customer's number. Nothing was erased.",
                issues=[issue("INVALID", "mobile_last4 does not match", field="mobile_last4")],
            )
        profile = audit_profile(customer)
        counts = remove_profile(run, customer, store)
        run.audit_before = {
            **profile,
            "consent_answers": counts["consent_answers"],
            "saved_sizes": counts["saved_sizes"],
        }
        run.audit_after = {
            "mobile": profile["mobile"],
            "profile": "erased",
            "consent_answers": 0,
            "saved_sizes": 0,
            "consent_answers_removed": counts["consent_answers_removed"],
            "bills_kept": counts["bills_kept"],
            "records_removed": counts["records_removed"],
        }
        return CommandResult(resource_type="customer", resource_id=str(customer_id))

    return handler


# -- merge and a new number (ticket 17) ------------------------------------------------


def merge_store(user: Any, site_id: Any) -> Store:
    """The store a merge or move is recorded at: one the person may act at on a
    customer's request, with ticket 17's switch on there too."""
    store = acting_store(user, site_id)
    if not is_feature_on(store, CUSTOMER_MERGE_RETENTION):
        raise Refusal("FEATURE_OFF", MERGE_OFF_MESSAGE, status=403)
    return store


def require_present(body: dict[str, Any]) -> None:
    if body.get("customer_present") is not True:
        raise Refusal(
            "INVALID_REQUEST",
            "Only with the customer present. Confirm they are here and asked for this.",
            issues=[issue("REQUIRED", "customer_present must be true", field="customer_present")],
        )


def merge_handler(keep_id: int, other_id: int, store: Store, last4: str) -> Any:
    """Merge another record for the same person into the one staff keep, with the
    customer present. The other record is closed, never deleted: its number and
    everything on it become the kept record's history. Bills are never touched."""

    def handler(run: CommandRun) -> CommandResult:
        rows = {
            row.pk: row
            for row in run.lock(
                LockRank.DOCUMENT, Customer.objects.filter(pk__in=[keep_id, other_id])
            )
        }
        keep, other = rows.get(keep_id), rows.get(other_id)
        if keep is None or other is None or keep.merged_into_id or other.merged_into_id:
            raise Refusal("NOT_FOUND", "One of these customers is no longer held.", status=404)
        if other.mobile[-4:] != last4:
            raise Refusal(
                "INVALID_REQUEST",
                "The last four digits do not match the other record's number. Nothing was merged.",
                issues=[issue("INVALID", "other_last4 does not match", field="other_last4")],
            )
        run.audit_before = {"kept": audit_profile(keep), "merged": audit_profile(other)}
        # What was merged into the other record, and its other numbers, follow it.
        Customer.objects.filter(merged_into=other).update(merged_into=keep)
        CustomerNumber.objects.filter(customer=other).update(customer=keep)
        CustomerNumber.objects.create(
            customer=keep,
            mobile=other.mobile,
            reason=CustomerNumber.Reason.MERGED,
            since=other.mobile_since,
            recorded_at=run.now,
        )
        # Closed as it was: its name and number stay as the customer gave them.
        other.merged_into, other.merged_at = keep, run.now
        other.save(update_fields=["merged_into", "merged_at"])
        run.audit_after = {
            "kept": audit_profile(keep),
            "merged": {"mobile": masked(other.mobile), "closed": True},
            "numbers": [masked(n.mobile) for n in numbers_of(keep)[1:]],
            "customer_present": True,
        }
        return CommandResult(resource_type="customer", resource_id=str(keep_id))

    return handler


@dataclass(frozen=True)
class Move:
    new: str
    was: str


def parse_move(body: dict[str, Any]) -> Move:
    new = normalise_mobile(str(body.get("new_mobile") or ""))
    if len(new) != 10:
        raise Refusal(
            "INVALID_REQUEST",
            "Type the new number: a ten-digit mobile.",
            issues=[issue("INVALID", "new_mobile is a 10-digit mobile", field="new_mobile")],
        )
    was = str(body.get("was_mobile") or "")
    if new == was:
        raise Refusal("INVALID_REQUEST", "That is the number held now. Nothing was changed.")
    return Move(new=new, was=was)


def move_handler(customer_id: int, store: Store, move: Move) -> Any:
    """Move a record to the customer's new number, with the customer present.

    The old number is kept as history up to now: bills on it stay theirs, and a
    bill made before now that syncs later brings no record back. From now on the
    old number is somebody else's, so its standing yeses are withdrawn; the new
    number starts with no consent, for the customer to answer on the display."""

    def handler(run: CommandRun) -> CommandResult:
        rows = run.lock(LockRank.DOCUMENT, Customer.objects.filter(pk=customer_id))
        if not rows or rows[0].merged_into_id is not None:
            raise Refusal("NOT_FOUND", "This customer is no longer held.", status=404)
        customer = rows[0]
        if customer.mobile != move.was:
            raise Refusal(
                "REVISION_SUPERSEDED",
                "Someone changed this customer's number after you opened the page. "
                "Reload and check the number held now.",
            )
        holder = Customer.objects.filter(mobile=move.new).first()
        if holder is not None:
            raise Refusal(
                "CUSTOMER_NUMBER_HELD",
                NUMBER_HELD_MESSAGE
                if holder.merged_into_id is None
                else "That number is already one of another customer's numbers "
                "(merged into their record). Nothing was changed.",
                status=409,
            )
        old = customer.mobile
        run.advisory_lock(LockRank.DOCUMENT, [f"consent:{old}", f"consent:{move.new}"])
        withdrawn = 0
        # The old number is somebody else's from now; a yes still standing on the
        # new number was somebody else's too. Neither carries to the customer.
        for mobile, question in (
            (number, question)
            for number in (old, move.new)
            for question in ConsentAnswer.Question.values
        ):
            standing = newest(store.tenant_id, mobile, question)
            if standing is None or not standing.given:
                continue
            ConsentAnswer.objects.create(
                id=uuid.uuid5(run.key_id, f"{mobile}:{question}"),
                store=store,
                mobile=mobile,
                question=question,
                given=False,
                how=ConsentAnswer.How.COUNTER,
                wording_version=current_wording(store.tenant_id).version,
                answered_at=run.now,
                till_number=RIGHTS_TILL,
                staff_id=run.principal.user_id,
            )
            withdrawn += 1
        CustomerNumber.objects.create(
            customer=customer,
            mobile=old,
            reason=CustomerNumber.Reason.MOVED,
            since=customer.mobile_since,
            until=run.now,
            recorded_at=run.now,
        )
        # Bills and answers on the new number from before now are not theirs.
        customer.mobile, customer.mobile_since = move.new, run.now
        # `updated_at` moves: every till learns the new number at its next sync.
        customer.save(update_fields=["mobile", "mobile_since", "updated_at"])
        # And forgets the old one: the list is sent whole (as after an erasure).
        CustomerErasure.objects.create(id=uuid.uuid4(), store=store, erased_at=run.now)
        run.audit_before = {"mobile": masked(old)}
        run.audit_after = {
            "mobile": masked(move.new),
            "old_number": masked(old),
            "consents_withdrawn": withdrawn,
            "customer_present": True,
        }
        return CommandResult(resource_type="customer", resource_id=str(customer_id))

    return handler
