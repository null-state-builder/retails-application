"""The single balanced-or-fail posting engine (ADR-0006).

`post_entries(doc, legs)` is the *sole* writer of the value general ledger. It
enforces the two laws every poster must obey:

1. **Balanced** — Σ(leg.amount) MUST equal 0 in paise, with ≥ 2 legs (a debit and
   a credit). An unbalanced set raises and writes nothing.
2. **All-or-none** — every leg commits in one `transaction.atomic()` or none does.

Dimensions are snapshotted onto each leg at write time; liability *timing* lives on
the calling document (which legs it hands us), never in this engine.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from django.core.exceptions import PermissionDenied
from django.db import transaction

from core.documents import Document
from core.floors import REGISTERED_FLOOR_EXCEPTIONS, FloorException
from core.gl import GLAccount, GLEntry
from core.operational import OperationalBatch, OperationalBatchError, validate_structure


class PostingError(Exception):
    """Base for posting-engine violations."""


class UnbalancedError(PostingError):
    """The legs of a posting do not sum to zero in paise (or there are fewer than two)."""


class PostingFloorError(PostingError, PermissionDenied):
    """A non-configurable people rule refused an otherwise valid posting."""


@dataclass(frozen=True)
class Leg:
    """One side of a balanced posting. `amount` is signed paise: debit > 0, credit < 0.

    Per-leg dimensions override the document defaults (a leg may carry its own
    store/gstin/brand/season/party — e.g. a payable leg with no store)."""

    account: str
    amount: int
    store: Any = None
    gstin: Any = None
    brand: str = ""
    season: str = ""
    party_type: str = ""
    party_code: str = ""
    against_voucher: str = ""
    memo: str = ""


def dr(account: str, paise: int, **dims: Any) -> Leg:
    """A debit leg (+paise)."""
    return Leg(account=account, amount=abs(int(paise)), **dims)


def cr(account: str, paise: int, **dims: Any) -> Leg:
    """A credit leg (−paise)."""
    return Leg(account=account, amount=-abs(int(paise)), **dims)


@dataclass(frozen=True)
class PostingRef:
    """A lightweight source-document reference for callers that are not (yet) a
    `core.documents.Document` (e.g. a PT file before Phase D reparenting). Anything
    exposing `doc_type` + `doc_number` (+ optional `store`/`gstin`/`posted_by`) works."""

    doc_type: str
    doc_number: str
    store: Any = None
    gstin: Any = None
    posted_by: Any = None


#: The goods-v1 operational inventory ledger (design §7.1). Domain callers choose it
#: in code; no HTTP input can select it.
OPERATIONAL_LEDGER = "operational_inventory"


class OperationalAdapter(Protocol):
    def write(self, run: Any, doc: Any, batch: OperationalBatch) -> Any: ...


_OPERATIONAL_ADAPTER: OperationalAdapter | None = None


def register_operational_adapter(adapter: OperationalAdapter) -> None:
    """Registered once at start-up by the stock ledger; code, not configuration."""
    global _OPERATIONAL_ADAPTER
    _OPERATIONAL_ADAPTER = adapter


@transaction.atomic
def post_entries(
    doc: Any,
    legs: Sequence[Leg] = (),
    *,
    posted_by: Any = None,
    ledger: str = "gl",
    batch: OperationalBatch | None = None,
    run: Any = None,
) -> list[GLEntry]:
    """Write a balanced set of GL legs for `doc`, or fail without writing anything.

    With ``ledger="operational_inventory"`` the same entry point takes a paired
    ``OperationalBatch`` instead of GL legs and hands it, structurally validated,
    to the registered stock-ledger adapter inside the caller's command. That
    branch writes no GL entry, payable or cash row, and returns an empty list.

    `doc` is the source document/reference (exposes `doc_type` + a minted
    `doc_number`; optionally `store`/`gstin`/`posted_by`). Raises `UnbalancedError`
    if the legs do not sum to zero or there are fewer than two; `PostingError` if the
    document has no number or a leg amount is not integer paise.

    Reads as the three things it is: refuse anything that is not a balanced
    posting, resolve what the legs inherit from the document, then write. The
    refusals are grouped in `_refuse_unpostable` on purpose — they are the
    invariants of the value ledger, and they are worth reading in one place.
    """
    if ledger == OPERATIONAL_LEDGER:
        if batch is None or legs:
            raise PostingError("an operational posting takes one batch and no GL legs")
        if _OPERATIONAL_ADAPTER is None:
            raise PostingError("no operational inventory adapter is registered")
        if doc is None or getattr(doc, "pk", None) is None:
            raise PostingError("an operational posting needs its persisted official source version")
        if getattr(getattr(doc, "_meta", None), "label_lower", "") != "core.officialversion":
            raise PostingError("an operational posting needs an OfficialVersion source")
        if batch.version_id != doc.pk:
            raise PostingError("the operational batch belongs to a different source version")
        try:
            validate_structure(batch)
        except OperationalBatchError as exc:
            raise PostingError(str(exc)) from exc
        _OPERATIONAL_ADAPTER.write(run, doc, batch)
        return []
    if ledger != "gl":
        raise PostingError(f"unknown ledger {ledger!r}")
    legs = list(legs)
    _refuse_unpostable(doc, legs)
    doc_type, doc_number, default_store, default_gstin, actor = _posting_context(doc, posted_by)
    _refuse_actor_outside_floor(doc, actor, legs, doc_type)

    rows = [
        GLEntry(
            account=leg.account,
            amount=leg.amount,
            doc_type=doc_type,
            doc_number=doc_number,
            against_voucher=leg.against_voucher,
            party_type=leg.party_type,
            party_code=leg.party_code,
            store=leg.store if leg.store is not None else default_store,
            gstin=leg.gstin if leg.gstin is not None else default_gstin,
            brand=leg.brand,
            season=leg.season,
            line_no=i,
            memo=leg.memo,
            posted_by=actor,
        )
        for i, leg in enumerate(legs, start=1)
    ]
    GLEntry.objects.bulk_create(rows)
    return rows


def _refuse_actor_outside_floor(doc: Any, actor: Any, legs: list[Leg], doc_type: str) -> None:
    """A store may move pieces, but it never writes rupees into the books.

    This sits in the sole GL writer, below every API and business module, so a
    Setup grant, shell call or future endpoint cannot route around it.

    The one carve-out — the store-native sale — is declared in `core.floors` with
    its reason, and is narrow by construction: it names its document types and
    the exact accounts they may touch, so a leg outside the allow-list drops back
    onto the floor even on a document the exception covers.
    """
    if getattr(actor, "scope_type", "") == "store" and not _store_actor_within_exception(
        doc, legs, doc_type
    ):
        raise PostingFloorError(
            "A store-scoped user cannot post value to the books; "
            "a named head-office person must perform this action."
        )
    raises_brand_liability = any(leg.account == GLAccount.VENDOR_PAYABLE for leg in legs)
    actor_name = getattr(actor, "full_name", "") or getattr(actor, "username", "") if actor else ""
    if doc_type in {"PT", "VFL"}:
        assert_pt_or_vflip_actor(actor)
    # Reads as "head office", and for every path but one it is: a store actor has
    # already been refused above. The exception is the sale's own SOR accrual,
    # where the store *is* allowed to raise a payable — machine-computed from the
    # vendor's settlement rate — and the carve-out has separately required that
    # leg to name the vendor it is owed to. What this rule adds on top, for every
    # actor alike, is that nobody anonymous owes a brand money.
    if raises_brand_liability and (not getattr(actor, "pk", None) or not actor_name):
        raise PostingFloorError(
            "Money owed to a brand cannot post without a named head-office person."
        )


def _store_actor_within_exception(doc: Any, legs: list[Leg], doc_type: str) -> bool:
    """Is this posting inside a declared carve-out from the store floor?

    Three things must all hold, and the order is the order of trust:

    1. `doc` is a real `Document`. `doc_type` is a free string on a `PostingRef`,
       so without this any caller could label a posting `"SAL"` and inherit the
       shop floor's permission to write value.
    2. Some registered exception covers that document type.
    3. Every leg sits inside that exception's account allow-list — one stray
       account and the whole posting is back on the floor — and any leg on a
       party-required account names the vendor it is owed to.
    """
    if not isinstance(doc, Document):
        return False
    covering = [e for e in REGISTERED_FLOOR_EXCEPTIONS.values() if e.covers(doc_type)]
    return bool(covering) and any(
        all(_leg_within(leg, exception) for leg in legs) for exception in covering
    )


def _leg_within(leg: Leg, exception: FloorException) -> bool:
    if leg.account not in exception.accounts:
        return False
    if leg.account in exception.party_required_accounts:
        return leg.party_type == "vendor" and bool(leg.party_code)
    return True


def assert_pt_or_vflip_actor(actor: Any) -> None:
    """Floor shared by PT value posting and V-flip stock + value posting."""
    actor_name = getattr(actor, "full_name", "") or getattr(actor, "username", "") if actor else ""
    if (
        getattr(actor, "scope_type", "") == "store"
        or not getattr(actor, "pk", None)
        or not actor_name
        or not getattr(actor, "may_post_pt_or_vflip_floor", False)
    ):
        raise PostingFloorError(
            "Only a named Accounts or Owner head-office person may post PT inwarding "
            "or V-flip value."
        )


def _refuse_unpostable(doc: Any, legs: list[Leg]) -> None:
    """Every reason a posting is refused, in one place. Raises, or returns None."""
    if len(legs) < 2:
        raise UnbalancedError("a posting needs at least two legs (a debit and a credit)")
    for leg in legs:
        if isinstance(leg.amount, bool) or not isinstance(leg.amount, int):
            raise PostingError(
                f"leg amount must be integer paise, got {type(leg.amount).__name__} "
                f"for account {leg.account!r}"
            )
    total = sum(leg.amount for leg in legs)
    if total != 0:
        raise UnbalancedError(f"legs do not balance: Σ = {total} paise (must be 0)")
    if not getattr(doc, "doc_type", None) or not getattr(doc, "doc_number", None):
        raise PostingError("post_entries requires a doc with a doc_type and a minted doc_number")


def _posting_context(doc: Any, posted_by: Any) -> tuple[str, str, Any, Any, Any]:
    """What every leg of this voucher inherits from its document.

    An unauthenticated actor is recorded as nobody rather than as itself: a leg's
    `posted_by` is evidence, and `AnonymousUser` is not a person.
    """
    actor = posted_by if posted_by is not None else getattr(doc, "posted_by", None)
    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None
    return (
        doc.doc_type,
        doc.doc_number,
        getattr(doc, "store", None),
        getattr(doc, "gstin", None),
        actor,
    )
