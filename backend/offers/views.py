"""The rulebook API: head office writes rules, everybody else reads them.

Three endpoints and a hard asymmetry between them. Reading is wide - a store
manager checks what is running this morning, and the till pulls the same rows
inside its dataset. Writing is head office's alone, and writing over a **live**
rule is not possible at all: the till has already cached it and bills have
already printed under it, so a change ends the running rule and starts a new one
beside it (`PUT` below), which is the same snapshot discipline every posted
document in this system obeys.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require_section
from accounts.sections import CAP_OPERATE, CAP_VIEW
from approvals.services import AlreadyPendingError, ApprovalError
from core.refusals import first_message, refusal_body
from masters.models import Store
from masters.scoping import active_brand_names, active_store_ids, is_brand_scoped
from offers import tax_settings as offer_tax_settings
from offers.approval import ask_for_countersignature, pending_offer_ids
from offers.models import Offer, OfferQuerySet
from offers.serializers import OfferReadSerializer, OfferWriteSerializer

#: `view` reads the rulebook, `operate` writes one. It used to say `manage`,
#: which exactly one persona holds - IT Admin - so in a system whose access table
#: hands offers to the Owner, the Brand Manager and HO Ops, the only seat that
#: could write a rule was IT, and every rule in the database had been seeded by
#: code rather than authored by a person (D11 §6, F1). Authoring is `operate`;
#: countersigning is `approve` and happens in the approvals inbox; `manage` keeps
#: what it should always have kept, which is configuration.
CanReadOrAuthor = require_section("offers_price", CAP_VIEW, write_minimum=CAP_OPERATE)

#: Statuses whose content a bill may already have been priced under, and which
#: are therefore not editable at all. `ended` is in here with `live` because the
#: accept pipeline consults ended rules for bills printed inside their dates.
FROZEN_STATUSES = frozenset({Offer.Status.LIVE, Offer.Status.ENDED})


def visible_offers(user: Any, *, include_ended: bool = False) -> OfferQuerySet:
    """The rules this caller may read, narrowed the way their screen is.

    Two axes, and they are asked in the caller's own terms. A store person sees
    rules that name a store they are working in **through the unit switcher**,
    because `store_scope` is a list of codes rather than a foreign key and the
    switcher is what decides which store the screen is about (the #171 lesson:
    one scope model per screen, never two). A brand-scoped person sees their own
    brands' rules, plus the storewide ones - a storewide discount lands on their
    brand's pieces, so it is not somebody else's data being leaked to them.
    """
    # The list is what is running and what is coming; a store has no use for a
    # history of stopped promotions. One rule named directly is a different
    # question - head office has to be able to read, and refuse to change, an
    # offer whose bills are still syncing.
    rows = OfferQuerySet(Offer).select_related("brand", "approved_by")
    if not include_ended:
        rows = rows.exclude(status=Offer.Status.ENDED)
    if is_brand_scoped(user):
        names = active_brand_names(user)
        if names is not None:
            rows = rows.filter(Q(brand__name__in=names) | Q(brand__isnull=True))
        return rows

    store_ids = active_store_ids(user)
    if store_ids is None:  # head office: the network's rulebook
        return rows
    # Upper-cased, because `store_scope.stores` is normalised on the way in and
    # `Store.code` is a slug with no normalisation of its own. Without it a store
    # whose code is not already upper case would 404 on its own rule.
    codes = [
        code.upper()
        for code in Store.objects.filter(id__in=store_ids).values_list("code", flat=True)
    ]
    if not codes:
        return rows.none()
    reach = Q()
    for code in codes:
        reach |= Q(store_scope__stores__contains=[code])
    return rows.filter(reach)


def _bad_request(errors: Any) -> Response:
    return Response(refusal_body("VALIDATION", first_message(errors)), status=400)


class OfferListCreateView(APIView):
    """`GET` - what is running (and what is coming). `POST` - author a draft."""

    permission_classes = [IsAuthenticated, CanReadOrAuthor]

    def get(self, request: Request) -> Response:
        rows = visible_offers(request.user)
        if request.query_params.get("live") == "true":
            rows = rows.live_on(timezone.localdate())
        brand = (request.query_params.get("brand") or "").strip()
        if brand:
            rows = rows.filter(Q(brand__code__iexact=brand) | Q(brand__name__iexact=brand))
        store = (request.query_params.get("store") or "").strip().upper()
        if store:
            # Narrows *within* what the caller may already see - it never reaches
            # past the switcher (the amended dashboard rule, contract §Step 1).
            rows = rows.filter(store_scope__stores__contains=[store])
        page = list(rows.order_by("priority", "id"))
        body = OfferReadSerializer(page, many=True).data
        # "A draft nobody has sent" and "a draft the Owner is holding" are
        # different states to the person who wrote it, and one query for the whole
        # page answers it (D11 §7).
        waiting = pending_offer_ids(offer.id for offer in page)
        for item in body:
            item["awaiting_approval"] = item["id"] in waiting
        return Response(body)

    @transaction.atomic  # the offer and its tax-settings audit record, together
    def post(self, request: Request) -> Response:
        serializer = OfferWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return _bad_request(serializer.errors)
        offer = serializer.save(created_by=request.user, status=Offer.Status.DRAFT)
        offer_tax_settings.audit(offer, {}, request.user)  # ticket 11
        # Stated even though it is always false here, so the author screen reads
        # one shape whether it just saved a rule or fetched one.
        body = OfferReadSerializer(offer).data
        body["awaiting_approval"] = False
        return Response(body, status=201)


class OfferDetailView(APIView):
    """`GET` one rule; `PUT` to change it - or, if it is live, to replace it."""

    permission_classes = [IsAuthenticated, CanReadOrAuthor]

    def get(self, request: Request, pk: int) -> Response:
        offer = visible_offers(request.user, include_ended=True).filter(pk=pk).first()
        if offer is None:
            return Response(refusal_body("NOT_FOUND", f"No offer {pk}."), status=404)
        body = OfferReadSerializer(offer).data
        body["awaiting_approval"] = bool(pending_offer_ids([offer.id]))
        return Response(body)

    @transaction.atomic
    def put(self, request: Request, pk: int) -> Response:
        # Scoped exactly as the GET is. A mutating path that read `Offer.objects`
        # while its sibling read `visible_offers` would let anybody holding the write
        # rung re-price another store's or another brand's rule, and would answer
        # 200 where the GET answers 404 - the read-scope-fails-open class this
        # codebase has been bitten by before.
        # `of=("self",)` because `visible_offers` joins the nullable brand and approver,
        # and Postgres will not lock the nullable side of an outer join. The row
        # being locked is this rule's, which is the only one being written.
        offer = (
            visible_offers(request.user, include_ended=True)
            .select_for_update(of=("self",))
            .filter(pk=pk)
            .first()
        )
        if offer is None:
            return Response(refusal_body("NOT_FOUND", f"No offer {pk}."), status=404)

        if offer.status in FROZEN_STATUSES:
            return self._frozen(offer, request)
        if str(request.data.get("status") or "") == Offer.Status.APPROVED:
            # The half of D5 Q9 that was deferred, now decided (D11 §7). Approving
            # by writing "approved" onto your own draft is not a gate, so this path
            # is closed: an offer is cleared by a second person, from the inbox.
            return Response(
                refusal_body(
                    "VALIDATION",
                    "An offer is not approved by whoever wrote it - send it for approval "
                    "and let a second person clear it.",
                ),
                status=400,
            )
        serializer = OfferWriteSerializer(offer, data=request.data, partial=True)
        if not serializer.is_valid():
            return _bad_request(serializer.errors)
        before = offer_tax_settings.settings_of(offer)
        saved = serializer.save()
        offer_tax_settings.audit(saved, before, request.user)  # ticket 11
        return Response(OfferReadSerializer(saved).data)

    def _frozen(self, offer: Offer, request: Request) -> Response:
        """A rule the counter has already priced under: stop it, or replace it.

        Nothing else. `live` is obvious - the till has it cached and bills have
        printed under it. `ended` matters just as much and is easier to miss: the
        accept pipeline consults ended rules for any bill printed inside their
        dates (`sell.services.recompute`), so moving an ended rule's percentage or
        back-dating its `ends_on` silently rewrites what the server believes every
        un-synced offline bill was owed - and a bill that was inside its cap
        becomes a bill over it, refused, with the store's queue stopped behind it.

        So exactly two moves are legal here, and neither carries any other field:

          · `{"status": "ended"}` on a live rule stops it (see `_stop`);
          · anything else is authored as a **new** rule (see `_end_and_replace`),
            which is the documents-snapshot discipline every posted document in
            this system obeys.
        """
        wants = str(request.data.get("status") or "")
        stopping_only = set(request.data.keys()) <= {"status"} and wants == Offer.Status.ENDED
        if stopping_only:
            if offer.status == Offer.Status.ENDED:
                return Response(OfferReadSerializer(offer).data)
            return self._stop(offer)
        if offer.status == Offer.Status.ENDED:
            return Response(
                refusal_body(
                    "VALIDATION",
                    "This offer has already ended. Bills were priced under it, so it "
                    "cannot be changed - write a new offer instead.",
                ),
                status=400,
            )
        serializer = OfferWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return _bad_request(serializer.errors)
        return self._end_and_replace(offer, serializer, request)

    def _stop(self, offer: Offer) -> Response:
        """Stop a running rule at the end of today, and never any other day.

        Two boundaries, and both matter for the same reason - the accept pipeline
        prices a bill against the rules that were running on the day it printed.

        It does not move **backwards**. Fifty tills are holding this rule offline
        and will keep applying it until their own clocks pass its end date;
        back-dating would not stop one of them, it would only make every bill they
        printed today disagree with the server. The placard is still in the
        window, too.

        And it does not move **forwards**. A rule whose end date has already gone
        by is stopped where it stopped: pushing `ends_on` out to today would widen
        the window a bill can be re-priced in, and every un-synced bill from the
        days in between would suddenly be explained by a rule that was not
        running when it was rung up.
        """
        today = timezone.localdate()
        stops_at = today if offer.ends_on is None else min(offer.ends_on, today)
        offer.status = Offer.Status.ENDED
        offer.ends_on = max(stops_at, offer.starts_on)
        offer.save(update_fields=["status", "ends_on", "updated_at"])
        return Response(OfferReadSerializer(offer).data)

    def _end_and_replace(
        self, offer: Offer, serializer: OfferWriteSerializer, request: Request
    ) -> Response:
        """Stop the running rule, and author its successor as a fresh draft.

        The successor starts as a **draft**, not live, and that is not an
        oversight: D5 Q9's gate is a named approver before an offer reaches a shop
        floor, and a change big enough to need a new rule is exactly the change
        that gate exists for. The cost is that a live promotion stops while its
        replacement is approved, which is head office's to sequence - by dating
        the successor's `starts_on`.
        """
        self._stop(offer)
        successor = serializer.save(
            created_by=request.user,
            status=Offer.Status.DRAFT,
            replaces=offer,
        )
        offer_tax_settings.audit(successor, offer_tax_settings.settings_of(offer), request.user)
        body = OfferReadSerializer(successor).data
        body["replaced_offer_id"] = offer.id
        return Response(body, status=201)


class OfferApprovalRequestView(APIView):
    """Send a draft offer for countersignature (D11 §7).

    The author's last act on their own rule. What comes back is the offer as it
    now stands, so the screen can say "waiting for the Owner" without asking a
    second question. The decision publishes it - see `offers.approval`.

    This replaces `_approval_stamp`, which recorded whoever wrote "approved" onto
    a draft as its approver - the half of D5 Q9 that was deferred, and a gate that
    the author could clear alone.
    """

    permission_classes = [IsAuthenticated, CanReadOrAuthor]

    def post(self, request: Request, pk: int) -> Response:
        offer = visible_offers(request.user).filter(pk=pk).first()
        if offer is None:
            return Response(refusal_body("NOT_FOUND", f"No offer {pk}."), status=404)
        if offer.status != Offer.Status.DRAFT:
            return Response(
                refusal_body(
                    "VALIDATION",
                    "Only a draft is sent for approval; this one is "
                    f"{offer.get_status_display().lower()}.",
                ),
                status=400,
            )
        try:
            approval = ask_for_countersignature(offer, requested_by=request.user)
        except AlreadyPendingError as exc:
            return Response(refusal_body("CONFLICT", str(exc)), status=409)
        except ApprovalError as exc:
            return Response(refusal_body("VALIDATION", str(exc)), status=400)
        body = OfferReadSerializer(offer).data
        body["awaiting_approval"] = True
        body["approval"] = {
            "id": approval.id,
            "title": approval.title,
            "approver_roles": list(approval.approver_roles),
        }
        return Response(body, status=201)
