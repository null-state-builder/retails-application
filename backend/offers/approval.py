"""Who countersigns an offer before it may price a bill (D11 §7, D5 Q9).

D5 Q9 asked for "a named approver before go-live" and shipped half of it: the
table refuses a live rule with nobody's name on it, but the name could be the
author's own. This is the other half, and it is deliberately **not** a new
mechanism — offers join the same approval spine six other families already use,
so maker ≠ checker, one decision, reason-on-reject, the re-ask and the inbox all
arrive already built, and an offer waits for a person in the same place as
everything else.

Two facts make an offer unusual in that spine, and both are stated here rather
than discovered later:

* **Value is not the question.** A write-off is banded by what it costs. An offer
  is a standing instruction whose cost is not knowable until the month is over,
  so ``value_paise`` stays 0 — which the policy already reads as "unknown, so
  escalate" (``ApprovalPolicy.approver_roles_for``). Every offer therefore needs
  a second person, whatever it turns out to be worth.
* **Approval publishes it.** The engine reads ``live``. An ``approved`` rule
  sitting behind its own start date would never price anything, because nothing
  promotes it on the morning it begins — so the decision goes straight to
  ``live`` and the **dates** gate the pricing. That is also what an offline till
  needs: tomorrow's offer has to be in today's dataset.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.contrib.contenttypes.models import ContentType

from approvals.hooks import register_on_approved
from approvals.models import Approval, ApprovalPolicy, ApprovalStatus
from approvals.services import ApprovalError, ApprovalRequired, request_approval
from offers.models import Offer

#: The approval family code. Kept next to the module that owns the document, the
#: way every other wired family does it.
KIND = "offer"
LABEL = "Offer"


def _policy() -> ApprovalPolicy:
    """Read the live row; a missing policy is a closed gate, never a fallback."""
    try:
        return ApprovalPolicy.objects.get(kind=KIND)
    except ApprovalPolicy.DoesNotExist as exc:
        raise ApprovalRequired(
            "No approval policy is configured for offers, so nothing can be published."
        ) from exc


def headline(offer: Offer) -> str:
    """What the approver reads in the inbox before opening anything.

    Brand, the offer's own name, how many shops it reaches and from when — the
    four things that decide whether this needs a second look, in one line.
    """
    # Guarded on the object, not `brand_id`: identical behaviour and identical
    # query count (a null FK answers `None` without touching the database, and
    # the set case already loaded the row to read `.name`), but it is the form
    # mypy can narrow. The column is a PROTECT FK, so a set id always resolves.
    brand = offer.brand.name if offer.brand else "Storewide"
    stores = len((offer.store_scope or {}).get("stores") or [])
    where = "1 store" if stores == 1 else f"{stores} stores"
    until = f" → {offer.ends_on:%d %b}" if offer.ends_on else " → until stopped"
    return f"{brand} · {offer.name} · {where} · {offer.starts_on:%d %b}{until}"


def ask_for_countersignature(offer: Offer, *, requested_by: Any) -> Approval:
    """Put a draft offer in the inbox for somebody other than its author."""
    policy = _policy()
    return request_approval(
        offer,
        kind=KIND,
        kind_label=LABEL,
        title=headline(offer),
        # The author is the maker even when somebody else sends it on — that is
        # what stops "ask again" from quietly moving the maker aside.
        made_by=offer.created_by or requested_by,
        requested_by=requested_by,
        approver_roles=policy.approver_roles_for(0),
        brand=offer.brand.name if offer.brand else "",
    )


def publish(offer: Offer, *, actor: Any) -> None:
    """The decision *is* the publication (see the module note)."""
    if offer.status == Offer.Status.ENDED:
        raise ApprovalError(
            "This offer was stopped while it waited for approval — write a new one instead."
        )
    if offer.status == Offer.Status.LIVE:
        return  # idempotent: a second decision cannot double-publish
    offer.approved_by = actor
    offer.status = Offer.Status.LIVE
    offer.save(update_fields=["approved_by", "status", "updated_at"])


def pending_offer_ids(offer_ids: Iterable[int]) -> set[int]:
    """Which of these offers are waiting for somebody — one query, never per row.

    The list screen has to tell "a draft nobody has sent" apart from "a draft
    waiting for the owner", and asking the inbox once for the whole page is the
    difference between one query and fifty.
    """
    ids = [int(offer_id) for offer_id in offer_ids]
    if not ids:
        return set()
    return set(
        Approval.objects.filter(
            kind=KIND,
            content_type=ContentType.objects.get_for_model(Offer),
            object_id__in=ids,
            status=ApprovalStatus.PENDING,
        ).values_list("object_id", flat=True)
    )


def register() -> None:
    """Wired at app-ready, so every route to a decision publishes the same way."""
    register_on_approved(Offer, publish)
