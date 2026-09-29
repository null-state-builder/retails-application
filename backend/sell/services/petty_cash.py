"""A store's petty cash (store operations ticket 42, ST-MNY-3).

Each store keeps a petty cash box at a set **float**, held by a named
**custodian** (overall PRD R-FIN-015: the petty-cash custodian boundary). Head
office (``money: manage``) sets both.

**Top-ups** put cash into the box, from the store's till or from head office.
Both sides are named: a till top-up is given by the login that took it out of
the drawer; a head office top-up names the person who brought it and their
voucher. The custodian receives it. A top-up may not take the box above its
float.

**Spends** take cash out of the box. Each records an expense head, the amount,
what it was for and a photo of the bill (R-FIN-014). A spend saved with no photo
is not refused - it opens an owned ``petty_cash_no_bill`` exception, which only
attaching the photo closes. A spend may not be more than the box holds, less
what is already waiting for approval.

**A spend over the limit** (``KDPS_PETTY_CASH_APPROVAL_ABOVE_PAISE``, Rs 2,000)
waits for the Owner in the approvals inbox (``approvals.Approval``, kind
``petty_cash``); the spine refuses the person who recorded it. Until approved
the cash has not left the box. An approval is its own audited command inside
the inbox decision; a refusal (switch off, box short) undoes the decision.

**The day-close count** (ticket 41). The count is of the store's cash: the drawer
and the petty cash box together (baseline B125). Money spent from the box comes
off the expected cash when it counts as spent (``counts_from``: when recorded,
or when the Owner approved it); head office top-ups are new cash and come on.
A till top-up moves cash inside the store, so it changes nothing (R-FIN-015:
internal custody transfers cancel within the consolidated boundary). Every
write here takes the store's ``cash-drawer`` lock, as a count does, so a count
never sees half a change.

**The bill photo** is kept in the write-once store (``core.offbox``) under a key
of its own; the row keeps the key and its SHA-256. The bytes are written before
the row, so a failure can leave an unlinked file but never a row that claims a
missing photo.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q, Sum
from django.utils import timezone

from accounts.goods_models import StaffAssignment
from accounts.models import User
from accounts.permissions import user_can
from accounts.role_lists import PETTY_CASH_APPROVER_ROLES
from accounts.sections import CAP_MANAGE, CAP_OPERATE
from alerts.goods_services import open_exception, resolve_exceptions
from approvals.models import Approval
from approvals.services import AlreadyPendingError, ApprovalError, request_approval
from core.canonical import sha256_hex
from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    execute_command,
)
from core.offbox import OffboxError, get_store
from core.refusals import Refusal
from core.tenancy import current_tenant_id, require_tenant_id
from masters.models import Store
from masters.scoping import actionable_store_ids
from masters.store_feature_registry import PETTY_CASH
from masters.store_features import is_feature_on, require_feature
from sell.petty_cash_models import PettyCashFloat, PettyCashSpend, PettyCashTopUp

FEATURE_KEY = PETTY_CASH

#: The approvals-spine family (``Approval.kind``) and how the inbox names it.
APPROVAL_KIND = "petty_cash"
APPROVAL_LABEL = "Petty cash"

EXCEPTION_KIND = "petty_cash_no_bill"

FLOAT_ACTION = "sell.petty_cash.float.set"
TOP_UP_ACTION = "sell.petty_cash.top_up"
SPEND_ACTION = "sell.petty_cash.spend"
BILL_ACTION = "sell.petty_cash.bill"
APPROVE_ACTION = "sell.petty_cash.approve"
REJECT_ACTION = "sell.petty_cash.reject"

#: The largest bill photo taken. A phone photo is a few megabytes.
MAX_BILL_BYTES = 10_000_000

#: A bill photo is a picture or a PDF, told by its first bytes, never its name.
_SIGNATURES: tuple[tuple[str, bytes], ...] = (
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("application/pdf", b"%PDF-"),
)

#: The head that needs a note saying what the money was for.
OTHER_HEAD = "Other"

_ALREADY = "PETTY_CASH_ALREADY_RECORDED"


def approval_limit_paise() -> int:
    """A spend above this waits for the Owner (PRD section 23: Rs 2,000)."""
    return int(getattr(settings, "KDPS_PETTY_CASH_APPROVAL_ABOVE_PAISE", 200_000))


def expense_heads() -> tuple[str, ...]:
    """The heads a spend may be booked under, until OQ-03 settles the catalogue."""
    return tuple(getattr(settings, "KDPS_PETTY_CASH_HEADS", (OTHER_HEAD,)))


def needs_approval(amount_paise: int) -> bool:
    return amount_paise > approval_limit_paise()


# --- who may do what ------------------------------------------------------------


def may_read(user: Any) -> bool:
    """The store (``money: operate``, "Expenses only (create)"), Owner and Accounts."""
    return user_can(user, "money", CAP_OPERATE)


def may_record(user: Any) -> bool:
    """Recording a spend or a top-up is creating an expense: ``money: operate``."""
    return may_read(user)


def may_set_float(user: Any) -> bool:
    """Head office sets the float and names the custodian: ``money: manage``."""
    return user_can(user, "money", CAP_MANAGE)


def store_for(user: Any, site_id: Any, *, minimum: str = CAP_OPERATE) -> Store:
    """The store ``user`` asked about, if they may act there; else "not found".

    With no store named, a person scoped to exactly one store gets that store.
    """
    ids = actionable_store_ids(user, section="money", minimum=minimum)
    if site_id in (None, ""):
        if ids is not None and len(ids) == 1:
            site_id = ids[0]
        else:
            raise Refusal("VALIDATION", "Choose a store.", status=400)
    try:
        pk = int(site_id)
    except (TypeError, ValueError):
        raise Refusal("VALIDATION", "Choose a store.", status=400) from None
    if ids is not None and pk not in ids:
        raise Refusal("NOT_FOUND", "That store was not found.", status=404)
    store = Store.objects.filter(tenant_id=require_tenant_id(), pk=pk, is_active=True).first()
    if store is None:
        raise Refusal("NOT_FOUND", "That store was not found.", status=404)
    return store


def custodian_choices(store: Store) -> list[User]:
    """The people who can hold this store's box: active, and placed at this store."""
    rows = (
        User.objects.filter(
            tenant_id=store.tenant_id, is_active=True,
            human__staff__assignments__site_id=store.pk,
        )
        .order_by("full_name", "username")
        .distinct()
    )
    return [user for user in rows if _placed_at(user, store)]


def _placed_at(user: Any, store: Store) -> bool:
    human_id = getattr(user, "human_id", None)
    if human_id is None or getattr(user, "tenant_id", None) != store.tenant_id:
        return False
    now = timezone.now()
    placed = StaffAssignment.objects.filter(
        tenant_id=store.tenant_id, staff__human_id=human_id, site_id=store.pk,
        effective_from__lte=now,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=now)).exists()
    if not placed:
        return False
    ids = actionable_store_ids(user, section="money", minimum=CAP_OPERATE)
    return ids is not None and store.pk in ids


# --- where the box stands -------------------------------------------------------


def _spent(store: Store, upto: datetime | None = None) -> int:
    rows = PettyCashSpend.objects.filter(store=store, counts_from__isnull=False)
    if upto is not None:
        rows = rows.filter(counts_from__lte=upto)
    return int(rows.aggregate(total=Sum("amount_paise"))["total"] or 0)


def _topped_up(store: Store) -> int:
    rows = PettyCashTopUp.objects.filter(store=store)
    return int(rows.aggregate(total=Sum("amount_paise"))["total"] or 0)


def _waiting(store: Store) -> int:
    rows = PettyCashSpend.objects.filter(store=store, status=PettyCashSpend.Status.WAITING)
    return int(rows.aggregate(total=Sum("amount_paise"))["total"] or 0)


def balance_paise(store: Store) -> int:
    """What the box holds: every top-up less every spend that has left it."""
    return _topped_up(store) - _spent(store)


@dataclass(frozen=True)
class PettyPosition:
    """The store's box as the server sees it now."""

    store: Store
    float_row: PettyCashFloat | None
    balance_paise: int
    waiting_paise: int
    spends: list[PettyCashSpend]
    top_ups: list[PettyCashTopUp]
    switched_on: bool
    may_set_float: bool
    custodians: list[User] = field(default_factory=list)

    @property
    def approval_above_paise(self) -> int:
        return approval_limit_paise()

    @property
    def heads(self) -> list[str]:
        return list(expense_heads())

    @property
    def missing_bills(self) -> int:
        return sum(1 for spend in self.spends if not spend.has_bill)


def position(store: Store, user: Any, *, limit: int = 50) -> PettyPosition:
    """The store's float, custodian, balance and latest spends and top-ups."""
    setter = may_set_float(user)
    return PettyPosition(
        store=store,
        float_row=PettyCashFloat.objects.filter(store=store).select_related("custodian").first(),
        balance_paise=balance_paise(store),
        waiting_paise=_waiting(store),
        spends=list(
            PettyCashSpend.objects.filter(store=store).select_related(
                "custodian", "recorded_by", "decided_by"
            )[:limit]
        ),
        top_ups=list(
            PettyCashTopUp.objects.filter(store=store).select_related("custodian", "recorded_by")[
                :limit
            ]
        ),
        switched_on=is_feature_on(store, FEATURE_KEY),
        may_set_float=setter,
        custodians=custodian_choices(store) if setter else [],
    )


def for_cash_count(
    store: Store, since: datetime, upto: datetime, *, first: bool
) -> tuple[int, int]:
    """Petty cash spent, and head office top-ups, in a count's window (ticket 41).

    ``first``: the store's first count starts at midnight and takes what is at
    it; a later count takes what came strictly after the previous count.
    """
    after = Q(counts_from__gte=since) if first else Q(counts_from__gt=since)
    spent = PettyCashSpend.objects.filter(after, store=store, counts_from__lte=upto).aggregate(
        total=Sum("amount_paise")
    )["total"]
    after = Q(recorded_at__gte=since) if first else Q(recorded_at__gt=since)
    brought = PettyCashTopUp.objects.filter(
        after,
        store=store,
        source=PettyCashTopUp.Source.HEAD_OFFICE,
        recorded_at__lte=upto,
    ).aggregate(total=Sum("amount_paise"))["total"]
    return int(spent or 0), int(brought or 0)


# --- shared plumbing ------------------------------------------------------------


def _validation(message: str) -> Refusal:
    return Refusal("VALIDATION", message, status=400)


def _principal(store: Store, actor: Any) -> Principal:
    """The person, or - for a login that is not a person - the store's login."""
    human_id = getattr(actor, "human_id", None)
    tenant_id = getattr(actor, "tenant_id", None) or current_tenant_id() or store.tenant_id
    user_id = getattr(actor, "pk", None)
    if human_id is not None:
        return Principal(tenant_id=tenant_id, human_id=human_id, user_id=user_id)
    return Principal(tenant_id=tenant_id, service_code="store-login", user_id=user_id)


def _lock_box(run: CommandRun, store: Store) -> None:
    """The same key a day-close count takes, so a count sees all of a change or none."""
    run.advisory_lock(LockRank.DOCUMENT, [f"cash-drawer:{store.pk}"])


def _float_of(store: Store) -> PettyCashFloat:
    row = PettyCashFloat.objects.filter(store=store).select_related("custodian").first()
    if row is None:
        raise Refusal(
            "PETTY_CASH_NOT_SET",
            "Head office has not set this store's petty cash float and custodian yet.",
            status=409,
        )
    return row


def rupees(paise: int) -> str:
    """Rupees in Indian grouping (Rs 1,23,456.50), for refusals and the inbox."""
    rupee, rest = divmod(abs(paise), 100)
    digits = str(rupee)
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    grouped = ",".join(filter(None, [head, *groups, tail]))
    sign = "-" if paise < 0 else ""
    return f"{sign}Rs {grouped}" + (f".{rest:02d}" if rest else "")


def _execute(
    store: Store,
    actor: Any,
    action: str,
    row_id: uuid.UUID,
    subject: str,
    data: dict[str, Any],
    handler: Any,
) -> CommandResult:
    return execute_command(
        _principal(store, actor),
        CommandSpec(
            action=action,
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{action}:{row_id}"),
            business_input={
                key: (str(value) if isinstance(value, uuid.UUID | datetime) else value)
                for key, value in data.items()
            },
            site_id=store.pk,
            subject_key=subject,
        ),
        handler,
    )


@dataclass(frozen=True)
class Saved:
    """A row the request wrote, or found already written (a replay)."""

    row: Any
    created: bool


def _conflict() -> Refusal:
    return Refusal(
        "COMMAND_CONFLICT",
        "This was already saved with different figures. Read the page again.",
        status=409,
    )


def _replayed(existing: Any, store: Store, same: Any) -> Saved | None:
    """The row an earlier copy of this request saved, if it is the same request."""
    if existing is None:
        return None
    if existing.store_id != store.pk or not same(existing):
        raise _conflict()
    return Saved(row=existing, created=False)


# --- the float and its custodian ------------------------------------------------


def float_facts(row: PettyCashFloat | None) -> dict[str, Any]:
    if row is None:
        return {"set": False}
    return {
        "set": True,
        "float_paise": row.float_paise,
        "custodian_user_id": row.custodian_id,
        "revision": row.revision,
    }


def set_float(store: Store, actor: Any, data: dict[str, Any]) -> PettyCashFloat:
    """Head office sets the float and the custodian. Audited with before and after."""
    request_id: uuid.UUID = data["id"]
    amount = int(data.get("float_paise") or 0)
    custodian_id = data.get("custodian")
    expected = int(data.get("expected_revision") or 0)
    if amount <= 0:
        raise _validation("The float must be more than nought.")
    custodian = User.objects.filter(pk=custodian_id, is_active=True).first()
    if custodian is None or not _placed_at(custodian, store):
        raise _validation("The custodian must be a person working at this store.")
    subject = f"petty_cash_float:{store.pk}"

    def handler(run: CommandRun) -> CommandResult:
        _lock_box(run, store)
        row = PettyCashFloat.objects.select_for_update().filter(store=store).first()
        current = row.revision if row is not None else 0
        if current != expected:
            raise Refusal(
                "REVISION_SUPERSEDED",
                "Someone changed this float after you opened it. Read the page again.",
                status=409,
            )
        before = float_facts(row)
        if row is None:
            row = PettyCashFloat(store=store, revision=1)
        else:
            row.revision += 1
        row.float_paise = amount
        row.custodian = custodian
        row.set_by = actor
        row.set_at = run.now
        row.save()
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = before
        run.audit_after = float_facts(row)
        return CommandResult(resource_type="petty_cash_float", resource_id=str(store.pk))

    _execute(store, actor, FLOAT_ACTION, request_id, subject, data, handler)
    return PettyCashFloat.objects.select_related("custodian").get(store=store)


# --- top-ups --------------------------------------------------------------------


def top_up_facts(row: PettyCashTopUp) -> dict[str, Any]:
    return {
        "recorded": True,
        "origin": row.source,
        "amount_paise": row.amount_paise,
        "custodian_user_id": row.custodian_id,
        "recorded_by_user_id": row.recorded_by_id,
        "given_by_name": row.given_by_name,
        "reference": row.reference,
    }


def _check_top_up(source: str, amount: int, given_by: str, reference: str) -> None:
    if source not in PettyCashTopUp.Source.values:
        raise _validation("Say whether the cash came from the till or from head office.")
    if amount <= 0:
        raise _validation("The amount must be more than nought.")
    if source == PettyCashTopUp.Source.HEAD_OFFICE and not given_by:
        raise _validation("Name who brought the cash from head office.")
    if source == PettyCashTopUp.Source.HEAD_OFFICE and not reference:
        raise _validation("Type head office's voucher or receipt number.")


def record_top_up(store: Store, actor: Any, data: dict[str, Any]) -> Saved:
    """Cash into the box from the till or head office, both sides named, once."""
    top_up_id: uuid.UUID = data["id"]
    source = str(data.get("origin") or "")
    amount = int(data.get("amount_paise") or 0)
    given_by = str(data.get("given_by_name") or "").strip()[:120]
    reference = str(data.get("reference") or "").strip()[:64]
    if source == PettyCashTopUp.Source.TILL:
        given_by, reference = "", ""

    def same(row: PettyCashTopUp) -> bool:
        return (row.source, row.amount_paise, row.given_by_name, row.reference) == (
            source,
            amount,
            given_by,
            reference,
        )

    replayed = _replayed(PettyCashTopUp.objects.filter(pk=top_up_id).first(), store, same)
    if replayed is not None:
        return replayed
    _check_top_up(source, amount, given_by, reference)
    subject = f"petty_cash_top_up:{top_up_id}"
    wrote: list[bool] = []

    def handler(run: CommandRun) -> CommandResult:
        _lock_box(run, store)
        if PettyCashTopUp.objects.filter(pk=top_up_id).exists():
            raise Refusal(_ALREADY, "This top-up is already recorded.", status=409)
        box = _float_of(store)
        room = box.float_paise - balance_paise(store)
        if amount > room:
            raise Refusal(
                "PETTY_CASH_OVER_FLOAT",
                f"The float is {rupees(box.float_paise)}; the box can take "
                f"{rupees(max(room, 0))} more.",
                status=409,
            )
        before = {"recorded": False, "balance_paise": box.float_paise - room}
        row = PettyCashTopUp.objects.create(
            id=top_up_id,
            store=store,
            source=source,
            amount_paise=amount,
            custodian=box.custodian,
            recorded_by=actor,
            given_by_name=given_by,
            reference=reference,
            recorded_at=run.now,
        )
        wrote.append(True)
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = before
        run.audit_after = {**top_up_facts(row), "balance_paise": balance_paise(store)}
        return CommandResult(resource_type="petty_cash_top_up", resource_id=str(row.pk))

    try:
        _execute(store, actor, TOP_UP_ACTION, top_up_id, subject, data, handler)
    except Refusal as refusal:
        if refusal.code != _ALREADY:
            raise
    row = PettyCashTopUp.objects.get(pk=top_up_id)
    if not same(row):
        raise _conflict()
    return Saved(row=row, created=bool(wrote))


# --- the bill photo -------------------------------------------------------------


@dataclass(frozen=True)
class BillFile:
    """A bill photo as it arrived: bytes, name, and what its first bytes say it is."""

    data: bytes
    filename: str
    media_type: str

    @property
    def sha256(self) -> str:
        return sha256_hex(self.data)


def read_bill(upload: Any) -> BillFile | None:
    """The uploaded bill photo, checked; ``None`` when no file was sent."""
    if upload is None:
        return None
    size = getattr(upload, "size", None)
    if size is not None and size > MAX_BILL_BYTES:
        raise Refusal("FILE_TOO_LARGE", "A bill photo can be up to 10 MB.", status=413)
    data = upload.read(MAX_BILL_BYTES + 1)
    if len(data) > MAX_BILL_BYTES:
        raise Refusal("FILE_TOO_LARGE", "A bill photo can be up to 10 MB.", status=413)
    if not data:
        raise Refusal("FILE_INVALID", "The bill photo is empty.", status=422)
    media_type = next((kind for kind, head in _SIGNATURES if data.startswith(head)), None)
    if media_type is None:
        raise Refusal(
            "FILE_INVALID", "A bill photo must be a JPEG or PNG picture, or a PDF.", status=422
        )
    name = str(getattr(upload, "name", "") or "bill").replace("\\", "/").split("/")[-1]
    return BillFile(data=data, filename=name[:255] or "bill", media_type=media_type)


def _store_bill(store: Store, spend_key: uuid.UUID, bill: BillFile) -> str:
    """Write the photo to the write-once store before any row names it."""
    key = f"petty-cash/{store.pk}/{spend_key}/{bill.sha256}"
    try:
        stored = get_store().put(key, bill.data)
    except OffboxError as exc:
        raise Refusal(
            "EVIDENCE_UNAVAILABLE", "The bill photo could not be stored; nothing was saved."
        ) from exc
    if stored.sha256 != bill.sha256 or stored.size != len(bill.data):
        raise Refusal("EVIDENCE_UNAVAILABLE", "The stored bill photo could not be confirmed.")
    return stored.key


def _attach(row: PettyCashSpend, key: str, bill: BillFile, actor: Any, now: datetime) -> None:
    row.bill_key = key
    row.bill_sha256 = bill.sha256
    row.bill_media_type = bill.media_type
    row.bill_size = len(bill.data)
    row.bill_filename = bill.filename
    row.bill_attached_by = actor
    row.bill_attached_at = now


def bill_bytes(row: PettyCashSpend) -> bytes:
    """The stored photo, checked against the hash the row kept."""
    try:
        data = get_store().get(row.bill_key)
    except OffboxError as exc:
        raise Refusal("EVIDENCE_UNAVAILABLE", "The bill photo is not available now.") from exc
    if sha256_hex(data) != row.bill_sha256:
        raise Refusal("EVIDENCE_UNAVAILABLE", "The stored bill photo does not match its record.")
    return data


# --- spends ---------------------------------------------------------------------


def spend_facts(row: PettyCashSpend) -> dict[str, Any]:
    return {
        "recorded": True,
        "head": row.head,
        "amount_paise": row.amount_paise,
        "note": row.note,
        "status": row.status,
        "counts_from": row.counts_from.isoformat() if row.counts_from else None,
        "custodian_user_id": row.custodian_id,
        "recorded_by_user_id": row.recorded_by_id,
        "decided_by_user_id": row.decided_by_id,
        "reject_reason": row.reject_reason,
        "bill_sha256": row.bill_sha256 or None,
    }


def headline(row: PettyCashSpend) -> str:
    """What the Owner reads in the inbox before opening anything."""
    photo = "" if row.has_bill else " · no bill photo"
    return f"{row.store.code} · {row.head} · {rupees(row.amount_paise)}{photo}"


def _subject(row_or_id: Any) -> str:
    pk = getattr(row_or_id, "pk", row_or_id)
    return f"petty_cash_spend:{pk}"


def _check_spend(head: str, amount: int, note: str) -> None:
    if head not in expense_heads():
        raise _validation("Choose an expense head from the list.")
    if amount <= 0:
        raise _validation("The amount must be more than nought.")
    if head == OTHER_HEAD and not note:
        raise _validation("Say what the money was spent on.")


def _check_room(store: Store, amount: int) -> int:
    """What the box holds, if it can pay ``amount`` beside what already waits."""
    held = balance_paise(store)
    free = held - _waiting(store)
    if amount > free:
        raise Refusal(
            "PETTY_CASH_SHORT",
            f"The box holds {rupees(held)}"
            + (f", {rupees(held - free)} of it waiting for the Owner" if held != free else "")
            + ". Top it up first.",
            status=409,
        )
    return held


def _ask_owner(row: PettyCashSpend, actor: Any) -> Approval:
    """Put a spend over the limit in the approvals inbox, for the Owner."""
    try:
        return request_approval(
            row,
            kind=APPROVAL_KIND,
            kind_label=APPROVAL_LABEL,
            title=headline(row),
            made_by=actor,
            requested_by=actor,
            approver_roles=sorted(PETTY_CASH_APPROVER_ROLES),
            store=row.store,
            value_paise=row.amount_paise,
        )
    except AlreadyPendingError as exc:  # pragma: no cover - a new row
        raise Refusal("STATE_CONFLICT", str(exc)) from exc


def _owe_bill(run: CommandRun, row: PettyCashSpend) -> None:
    """A spend saved with no bill photo opens an owned exception (R-FIN-014)."""
    if row.has_bill:
        return
    open_exception(
        run,
        kind=EXCEPTION_KIND,
        site_id=row.store_id,
        subject_key=_subject(row),
        reason_code="NO_BILL_PHOTO",
        source_event_key=row.client_id,
        allowed_resolution_actions=[f"POST /api/sell/petty-cash/spends/{row.pk}/bill"],
    )


def record_spend(store: Store, actor: Any, data: dict[str, Any], bill: BillFile | None) -> Saved:
    """Cash out of the box, once, audited. Over the limit it waits for the Owner;
    with no bill photo it opens an owned exception."""
    client_id: uuid.UUID = data["id"]
    head = str(data.get("head") or "").strip()
    amount = int(data.get("amount_paise") or 0)
    note = str(data.get("note") or "").strip()[:200]

    def same(row: PettyCashSpend) -> bool:
        return (row.head, row.amount_paise, row.note) == (head, amount, note)

    replayed = _replayed(PettyCashSpend.objects.filter(client_id=client_id).first(), store, same)
    if replayed is not None:
        return replayed
    _check_spend(head, amount, note)
    key = _store_bill(store, client_id, bill) if bill is not None else ""
    waits = needs_approval(amount)
    holder: dict[str, PettyCashSpend] = {}

    def handler(run: CommandRun) -> CommandResult:
        _lock_box(run, store)
        if PettyCashSpend.objects.filter(client_id=client_id).exists():
            raise Refusal(_ALREADY, "This spend is already recorded.", status=409)
        box = _float_of(store)
        held = _check_room(store, amount)
        row = PettyCashSpend(
            client_id=client_id,
            store=store,
            head=head,
            amount_paise=amount,
            note=note,
            status=PettyCashSpend.Status.WAITING if waits else PettyCashSpend.Status.SPENT,
            counts_from=None if waits else run.now,
            custodian=box.custodian,
            recorded_by=actor,
            recorded_at=run.now,
        )
        if bill is not None:
            _attach(row, key, bill, actor, run.now)
        row.save()
        holder["row"] = row
        after: dict[str, Any] = spend_facts(row)
        if waits:
            after["approval_id"] = _ask_owner(row, actor).pk
        _owe_bill(run, row)
        run.audit_subject_key = _subject(row)
        run.audit_site_id = store.pk
        run.audit_before = {"recorded": False, "balance_paise": held}
        run.audit_after = {**after, "balance_paise": balance_paise(store)}
        return CommandResult(resource_type="petty_cash_spend", resource_id=str(row.pk))

    stored = {key_: value for key_, value in data.items()}
    stored["bill_sha256"] = bill.sha256 if bill is not None else None
    try:
        _execute(
            store, actor, SPEND_ACTION, client_id, f"petty_cash_spend:{client_id}", stored, handler
        )
    except Refusal as refusal:
        if refusal.code != _ALREADY:
            raise
    row = PettyCashSpend.objects.get(client_id=client_id)
    if not same(row):
        raise _conflict()
    return Saved(row=row, created="row" in holder)


def attach_bill(spend_id: int, store: Store, actor: Any, bill: BillFile) -> Saved:
    """The bill photo of a spend saved without one. Closes its exception.

    A photo is written once: the same photo again answers with the spend, a
    different one is refused.
    """
    row = PettyCashSpend.objects.filter(pk=spend_id, store=store).first()
    if row is None:
        raise Refusal("NOT_FOUND", "That spend was not found.", status=404)
    if row.has_bill:
        if row.bill_sha256 == bill.sha256:
            return Saved(row=row, created=False)
        raise Refusal("BILL_ALREADY_ATTACHED", "This spend already has its bill photo.", status=409)
    key = _store_bill(store, row.client_id, bill)
    subject = _subject(row)

    def handler(run: CommandRun) -> CommandResult:
        _lock_box(run, store)
        locked = PettyCashSpend.objects.select_for_update().get(pk=row.pk)
        if locked.has_bill:
            raise Refusal(
                "BILL_ALREADY_ATTACHED", "This spend already has its bill photo.", status=409
            )
        before = spend_facts(locked)
        _attach(locked, key, bill, actor, run.now)
        locked.save(
            update_fields=[
                "bill_key",
                "bill_sha256",
                "bill_media_type",
                "bill_size",
                "bill_filename",
                "bill_attached_by",
                "bill_attached_at",
            ]
        )
        resolve_exceptions(
            run, kind=EXCEPTION_KIND, subject_key=subject, reason_code="BILL_ATTACHED"
        )
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = before
        run.audit_after = spend_facts(locked)
        return CommandResult(resource_type="petty_cash_spend", resource_id=str(locked.pk))

    try:
        _execute(
            store,
            actor,
            BILL_ACTION,
            uuid.uuid5(row.client_id, bill.sha256),
            subject,
            {"spend_id": row.pk, "bill_sha256": bill.sha256},
            handler,
        )
    except Refusal as refusal:
        if refusal.code != "BILL_ALREADY_ATTACHED":
            raise
        again = PettyCashSpend.objects.get(pk=row.pk)
        if again.bill_sha256 != bill.sha256:
            raise
        return Saved(row=again, created=False)
    return Saved(row=PettyCashSpend.objects.get(pk=row.pk), created=True)


# --- the Owner's decision, from the approvals inbox -----------------------------


def approval_of(row: PettyCashSpend) -> Approval | None:
    return (
        Approval.objects.filter(
            kind=APPROVAL_KIND,
            content_type=ContentType.objects.get_for_model(PettyCashSpend),
            object_id=row.pk,
        )
        .order_by("-created_at", "-id")
        .first()
    )


def _decide(row: PettyCashSpend, actor: Any, *, approved: bool, reason: str) -> None:
    """The Owner's decision as its own audited command, inside the inbox decision.

    The approvals spine has already checked the role and that the Owner is not
    the person who recorded it. An approval also needs the switch on and the box
    to still hold the money; a refusal undoes the whole decision.
    """
    human_id = getattr(actor, "human_id", None)
    if human_id is None:
        raise ApprovalError("A petty cash spend is approved by a named person.")
    store = row.store
    subject = _subject(row)

    def handler(run: CommandRun) -> CommandResult:
        _lock_box(run, store)
        locked = PettyCashSpend.objects.select_for_update().get(pk=row.pk)
        if locked.status != PettyCashSpend.Status.WAITING:
            raise Refusal("STATE_CONFLICT", f"This spend is {locked.get_status_display().lower()}.")
        before = spend_facts(locked)
        if approved:
            require_feature(store, FEATURE_KEY)
            held = balance_paise(store)
            if locked.amount_paise > held:
                raise Refusal(
                    "PETTY_CASH_SHORT",
                    f"The box holds {rupees(held)} now, less than this spend. The store must "
                    "top it up before it is approved.",
                    status=409,
                )
            locked.status = PettyCashSpend.Status.APPROVED
            locked.counts_from = run.now
        else:
            locked.status = PettyCashSpend.Status.REJECTED
            locked.reject_reason = reason[:2000]
            # The cash never left the box, so no bill is owed.
            resolve_exceptions(
                run, kind=EXCEPTION_KIND, subject_key=subject, reason_code="SPEND_REJECTED"
            )
        locked.decided_by = actor
        locked.decided_at = run.now
        locked.save(
            update_fields=["status", "counts_from", "reject_reason", "decided_by", "decided_at"]
        )
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = before
        run.audit_after = spend_facts(locked)
        return CommandResult(resource_type="petty_cash_spend", resource_id=str(locked.pk))

    tenant_id = current_tenant_id() or store.tenant_id
    try:
        execute_command(
            Principal(tenant_id=tenant_id, human_id=human_id, user_id=getattr(actor, "pk", None)),
            CommandSpec(
                action=APPROVE_ACTION if approved else REJECT_ACTION,
                command_id=uuid.uuid4(),
                business_input={"spend_id": row.pk, "approved": approved, "reason": reason},
                subject_key=subject,
                site_id=store.pk,
            ),
            handler,
        )
    except Refusal as refusal:
        raise ApprovalError(refusal.message) from refusal


def on_approved(row: PettyCashSpend, *, actor: Any) -> None:
    _decide(row, actor, approved=True, reason="")


def on_rejected(row: PettyCashSpend, *, actor: Any, reason: str) -> None:
    _decide(row, actor, approved=False, reason=reason)


def register_approval_hooks() -> None:
    from approvals.hooks import register_on_approved, register_on_rejected

    register_on_approved(PettyCashSpend, on_approved)
    register_on_rejected(PettyCashSpend, on_rejected)
