"""Saved size per brand (store operations ticket 18, ST-CUS-2).

Store operations PRD §8: the last size bought per brand and category is learned
from bills, and staff may correct it with the customer's agreement. It shows on
the till when the customer is added to a bill.

**Learning** (``learn_from_bill``): when a bill reaches head office at a store
with the ``saved-sizes`` switch on, each brand and category it sold teaches one
size (``learned_from``): the size sold in most pieces, a tie to the later line.
Only sold goods lines count - an exchange's returned leg and a service line
(an alteration charge) teach nothing, and neither does a line with no brand,
category or size. A pure return changes nothing: the customer may give a piece
back for any reason. Bills accepted before the switch was on are not read back.

**What stands** is the newest row by ``as_of``: the time the bill was made, or
the time of a correction. So an offline bill synced late never outranks a
newer bill, and a bill made before a correction never undoes it - while a bill
made after it is the last size bought. Every row is kept (``sell.SavedSize``).

**Whose.** A bill teaches the record that holds its number, and a record merged
into another teaches the kept one. A bill made before the number was the
record's (ticket 17) teaches whoever held the number then and has since moved
away from it, or nobody. Rows follow the record through a move to
a new number, and are read within the company of the store that recorded them.

**Correcting** (``correct``): at the till, online only, with the customer's
agreement ticked. Only a size already saved can be corrected, against the size
the till saw. Each correction and each learning is one audited command whose
record shows the sizes before and after and the number's last four digits.

Erasure and the retention clean-up (``customer_rights.remove_profile``) remove
the rows with the profile.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import Q, QuerySet

from core.commands import CommandResult, CommandRun, CommandSpec, LockRank, execute_command
from core.refusals import Refusal
from masters.models import Customer, CustomerNumber, Store
from masters.store_feature_registry import SAVED_SIZES
from masters.store_features import is_feature_on, require_feature
from sell.models import Sale, SaleLine, SavedSize
from sell.services.consent import masked
from sell.services.customer_numbers import not_the_holders
from sell.services.customers import normalise_mobile
from sell.services.split_shares import bill_principal

logger = logging.getLogger(__name__)

LEARN_ACTION = "sell.saved_size.learn"
CORRECT_ACTION = "sell.saved_size.correct"
SIZE_MAX = 24


def key(text: str) -> str:
    """Upper case, spaces collapsed: one brand or category however it is typed."""
    return " ".join(str(text or "").split()).upper()


def _spelled(text: str) -> str:
    return " ".join(str(text or "").split())


@dataclass(frozen=True)
class Learned:
    brand: str
    category: str
    size: str

    @property
    def brand_key(self) -> str:
        return key(self.brand)

    @property
    def category_key(self) -> str:
        return key(self.category)


# -- the rule --------------------------------------------------------------------------


def learned_from(lines: Iterable[Any]) -> list[Learned]:
    """The size each brand and category on a bill teaches.

    ``lines`` carry ``line_no``, ``brand``, ``item`` (the category), ``size``,
    ``qty``, ``direction`` and ``kind``. Only sold goods lines with a brand, a
    category and a size count. Within a brand and category the size sold in most
    pieces wins; a tie goes to the later line. Spelled as on its latest line."""
    tally: dict[tuple[str, str], dict[str, list[Any]]] = {}
    for line in sorted(lines, key=lambda row: int(row.line_no)):
        if line.direction != SaleLine.Direction.SALE or line.kind != SaleLine.Kind.GOODS:
            continue
        brand, category, size = _spelled(line.brand), _spelled(line.item), _spelled(line.size)
        if not (brand and category and size) or int(line.qty) <= 0:
            continue
        sizes = tally.setdefault((key(brand), key(category)), {})
        # [pieces, latest line, brand, category, size as spelled]
        entry = sizes.setdefault(key(size), [0, 0, brand, category, size])
        entry[0] += int(line.qty)
        entry[1:] = [int(line.line_no), brand, category, size]
    found = []
    for sizes in tally.values():
        pieces, _, brand, category, size = max(sizes.values(), key=lambda e: (e[0], e[1]))
        found.append(Learned(brand=brand, category=category, size=size))
    return found


# -- reading ---------------------------------------------------------------------------


def mobile_or_refuse(raw: str) -> str:
    """The bare 10-digit mobile, or ``VALIDATION``."""
    mobile = normalise_mobile(raw)
    if len(mobile) != 10:
        raise Refusal(
            "VALIDATION", "Saved sizes need the customer's 10-digit mobile number.", status=400
        )
    return mobile


def _kept(customer: Customer) -> Customer:
    """The record a merged one was kept as. A merge moves every record merged into
    the other one onto the kept one, so there is only ever one step."""
    if customer.merged_into_id is None:
        return customer
    return Customer.objects.get(pk=customer.merged_into_id)


def owner_of(mobile: str, made_at: datetime | None) -> Customer | None:
    """The open record a bill on this number, made then, belongs to - if any.

    The record holding the number, unless the bill was made before the number
    was theirs (ticket 17); then the one who held it then and has since moved
    away from it, if they are still held. ``made_at`` None asks who holds it now."""
    mobile = normalise_mobile(mobile)
    if len(mobile) != 10:
        return None
    customer = Customer.objects.filter(mobile=mobile).first()
    if customer is not None and not not_the_holders(customer, made_at):
        return _kept(customer)
    if made_at is None:
        return None
    moved = (
        CustomerNumber.objects.filter(
            mobile=mobile,
            reason=CustomerNumber.Reason.MOVED,
            customer__isnull=False,
            until__gte=made_at,
        )
        .filter(Q(since__isnull=True) | Q(since__lt=made_at))
        .select_related("customer")
        .order_by("until")
        .first()
    )
    return _kept(moved.customer) if moved is not None and moved.customer else None


def rows_of(customer: Customer, tenant_id: Any) -> QuerySet[SavedSize]:
    """Every row of this customer's, and of records merged into theirs, in this company."""
    return SavedSize.objects.filter(store__tenant_id=tenant_id).filter(
        Q(customer=customer) | Q(customer__merged_into=customer)
    )


def standing(customer: Customer, tenant_id: Any) -> list[SavedSize]:
    """What stands per brand and category: the newest row, by brand then category."""
    newest: dict[tuple[str, str], SavedSize] = {}
    rows = rows_of(customer, tenant_id).select_related("store", "sale")
    for row in rows.order_by("-as_of", "-recorded_at", "-id"):
        newest.setdefault((row.brand_key, row.category_key), row)
    return sorted(newest.values(), key=lambda r: (r.brand_key, r.category_key))


def as_json(row: SavedSize, readable: set[int]) -> dict[str, Any]:
    """One size as a reader sees it. The bill it came from is named only when it
    is at a store the reader may read; its bill list does not show the others."""
    shown = row.sale is not None and row.sale.store_id in readable
    return {
        "brand": row.brand,
        "category": row.category,
        "size": row.size,
        "how": row.source,
        "as_of": row.as_of,
        "store_code": row.store.code,
        "doc_number": row.sale.doc_number if shown and row.sale is not None else None,
    }


def for_mobile(store: Store, raw_mobile: str) -> dict[str, Any]:
    """What the till shows for the number on the bill: the sizes that stand."""
    mobile = mobile_or_refuse(raw_mobile)
    customer = owner_of(mobile, None)
    rows = standing(customer, store.tenant_id) if customer is not None else []
    return {
        "mobile": mobile,
        "held": customer is not None,
        "sizes": [as_json(row, {store.pk}) for row in rows],
    }


def _audit_sizes(rows: Iterable[SavedSize | None]) -> list[dict[str, str]]:
    return [
        {"brand": row.brand, "category": row.category, "size": row.size}
        for row in rows
        if row is not None
    ]


# -- learning from a bill --------------------------------------------------------------


def learn_from_bill(sale: Sale) -> None:
    """Learn the sizes this bill teaches, once, and audit it. Never raises: the
    bill is already printed and in the customer's hand (Rule 5)."""
    try:
        _learn(sale)
    except Exception:
        logger.exception("saved sizes not learned from sale %s", sale.doc_number)


def _newer(learned: Learned, now: dict[tuple[str, str], SavedSize], made_at: datetime) -> bool:
    """Would this bill's size stand: nothing saved, or only older than the bill?"""
    current = now.get((learned.brand_key, learned.category_key))
    return current is None or current.as_of < made_at


def _standing_by_key(customer: Customer, tenant_id: Any) -> dict[tuple[str, str], SavedSize]:
    return {(r.brand_key, r.category_key): r for r in standing(customer, tenant_id)}


def _learn(sale: Sale) -> None:
    if not sale.customer_mobile:
        return
    store = Store.objects.get(pk=sale.store_id)
    if not is_feature_on(store, SAVED_SIZES):
        return
    learned = learned_from(sale.lines.all())
    if not learned:
        return
    customer = owner_of(sale.customer_mobile, sale.billed_at)
    if customer is None:
        return
    made_at = sale.billed_at
    subject = f"customer:{customer.pk}"

    def handler(run: CommandRun) -> CommandResult:
        # One customer's sizes change one bill or correction at a time.
        run.advisory_lock(LockRank.DOCUMENT, [f"saved-size:{customer.pk}"])
        now = _standing_by_key(customer, store.tenant_id)
        before, after = [], []
        for one in learned:
            if not _newer(one, now, made_at):
                continue  # a newer bill or a correction already stands
            current = now.get((one.brand_key, one.category_key))
            row = SavedSize.objects.create(
                id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"saved-size:{sale.idempotency_uuid}:{one.brand_key}:{one.category_key}",
                ),
                customer=customer,
                store=store,
                brand=one.brand,
                category=one.category,
                brand_key=one.brand_key,
                category_key=one.category_key,
                size=one.size,
                source=SavedSize.Source.BILL,
                as_of=made_at,
                sale=sale,
                staff_id=sale.created_by_id,
            )
            before.append(current)
            after.append(row)
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = {
            "mobile": masked(customer.mobile),
            "doc_number": sale.doc_number,
            "sizes": _audit_sizes(before),
        }
        run.audit_after = {
            "mobile": masked(customer.mobile),
            "doc_number": sale.doc_number,
            "sizes": _audit_sizes(after),
        }
        return CommandResult(resource_type="customer", resource_id=str(customer.pk))

    # Nothing newer to learn: no command, so no empty record in the log.
    now = _standing_by_key(customer, store.tenant_id)
    if not any(_newer(one, now, made_at) for one in learned):
        return
    execute_command(
        bill_principal(store, sale.created_by),
        CommandSpec(
            action=LEARN_ACTION,
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"saved-sizes:{sale.idempotency_uuid}"),
            business_input={"idempotency_uuid": str(sale.idempotency_uuid)},
            site_id=store.pk,
            subject_key=subject,
        ),
        handler,
    )


# -- correcting at the till ------------------------------------------------------------


@dataclass(frozen=True)
class Correction:
    id: uuid.UUID
    mobile: str
    brand: str
    category: str
    size: str
    was_size: str


def parse_correction(data: dict[str, Any]) -> Correction:
    if data.get("agreed") is not True:
        raise Refusal(
            "SIZE_NOT_AGREED",
            "Ask the customer first. A saved size is changed only with their agreement.",
            status=400,
        )
    mobile = mobile_or_refuse(str(data.get("mobile") or ""))
    size = _spelled(str(data.get("size") or ""))
    if not size or len(size) > SIZE_MAX:
        raise Refusal(
            "VALIDATION", f"Type the new size (at most {SIZE_MAX} characters).", status=400
        )
    return Correction(
        id=data["id"],
        mobile=mobile,
        brand=_spelled(str(data.get("brand") or "")),
        category=_spelled(str(data.get("category") or "")),
        size=size,
        was_size=_spelled(str(data.get("was_size") or "")),
    )


@dataclass(frozen=True)
class Corrected:
    customer: Customer
    created: bool


def _conflict() -> Refusal:
    return Refusal(
        "SIZE_CONFLICT", "This correction id is already used for a different change.", status=409
    )


def _replay(change: Correction, store: Store) -> Corrected | None:
    row = SavedSize.objects.filter(pk=change.id).select_related("customer").first()
    if row is None:
        return None
    same = (
        row.source == SavedSize.Source.STAFF
        and row.store_id == store.pk
        and (row.brand_key, row.category_key, row.size)
        == (key(change.brand), key(change.category), change.size)
    )
    if not same:
        raise _conflict()
    return Corrected(customer=row.customer, created=False)


def correct(store: Store, actor: Any, data: dict[str, Any]) -> Corrected:
    """Correct one saved size at the customer's request, once, and audit it."""
    require_feature(store, SAVED_SIZES)
    change = parse_correction(data)
    replayed = _replay(change, store)
    if replayed is not None:
        return replayed
    customer = owner_of(change.mobile, None)
    if customer is None:
        raise Refusal("NOT_FOUND", "No customer holds this number yet.", status=404)
    wanted = (key(change.brand), key(change.category))
    subject = f"customer:{customer.pk}"

    def handler(run: CommandRun) -> CommandResult:
        run.advisory_lock(LockRank.DOCUMENT, [f"saved-size:{customer.pk}"])
        current = next(
            (
                row
                for row in standing(customer, store.tenant_id)
                if (row.brand_key, row.category_key) == wanted
            ),
            None,
        )
        if current is None:
            raise Refusal(
                "SIZE_NOT_SAVED",
                "No size is saved for that brand and category, so there is nothing to correct.",
                status=404,
            )
        if key(current.size) != key(change.was_size):
            raise Refusal(
                "REVISION_SUPERSEDED",
                f"The saved size changed to {current.size} since you opened it. "
                "Check it with the customer again.",
                status=409,
            )
        if key(current.size) == key(change.size):
            raise Refusal("INVALID_REQUEST", "That is already the saved size.", status=400)
        row = SavedSize.objects.create(
            id=change.id,
            customer=customer,
            store=store,
            brand=current.brand,
            category=current.category,
            brand_key=current.brand_key,
            category_key=current.category_key,
            size=change.size,
            source=SavedSize.Source.STAFF,
            as_of=run.now,
            staff=actor,
        )
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        facts = {
            "mobile": masked(customer.mobile),
            "brand": current.brand,
            "category": current.category,
        }
        run.audit_before = {**facts, "size": current.size, "source": current.source}
        run.audit_after = {**facts, "size": row.size, "source": row.source, "agreed": True}
        return CommandResult(
            resource_type="customer", resource_id=str(customer.pk), status_code=201
        )

    try:
        result = execute_command(
            bill_principal(store, actor),
            CommandSpec(
                action=CORRECT_ACTION,
                command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"saved-size-correct:{change.id}"),
                business_input={
                    "id": str(change.id),
                    "store": store.code,
                    "mobile": change.mobile,
                    "brand": change.brand,
                    "category": change.category,
                    "size": change.size,
                    "was_size": change.was_size,
                },
                site_id=store.pk,
                subject_key=subject,
            ),
            handler,
        )
    except Refusal as refusal:
        if refusal.code != "COMMAND_CONFLICT":
            raise
        raise _conflict() from None
    # A copy of the same correction that got there first is answered as a replay.
    return Corrected(customer=customer, created=not result.replayed)
