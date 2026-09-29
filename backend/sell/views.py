"""The three sale endpoints - take a bill, find a bill, read a bill - and a mirror.

**No endpoint in this module can change a posted sale** (A7), and that is not an
omission to be corrected later: a bill is a printed fact in a customer's hand, and
the only honest correction is the kernel's reversing transition. A PUT onto a sale
here would be a way to make the paper and the books disagree.

The one PUT that does exist is not about a sale at all. A held bill is a cart the
counter parked (#185, grill Q13) - no document, no number, no stock, no money -
and the till pushes its whole list so the Dashboard can count them. It sits here
because it is the same till, the same store and the same gate; what makes the
rule above hold is that there is nothing on the other end of it to overwrite.

Everything money-shaped lives in `sell.services.accept`; these views translate
between HTTP and it, and answer refusals in the till's own vocabulary - a
sentence for the person, a code for the queue.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import Prefetch, Q, QuerySet
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from approvals.names import display_name
from accounts.principal import resolve_access
from core.dates import parse_day
from core.refusals import Refusal, first_message, refusal_body
from masters.models import Store
from masters.scoping import scope_by_entitlement, scope_by_store
from masters.store_feature_registry import (
    CUSTOMER_CONSENT,
    CUSTOMER_DISPLAY,
    EXCHANGE_RETURN_TAX,
    SAVED_SIZES,
)
from masters.store_features import require_feature
from sell.models import ContinuityFlag, HeldBill, IrnQueueItem, Sale, SaleLine, SellPolicy
from sell.permissions import (
    CanHandOverTill,
    CanReadOrBill,
    CanReadOrManagePolicy,
    CanReadSales,
    CanRunTill,
    CanWorkIrnQueue,
    CanWorkStoreFlags,
)
from sell.schema_serializers import (
    DatasetReadSerializer,
    HeldBillsCountReadSerializer,
    IrnQueueReadSerializer,
    RegisterHandoverReadSerializer,
    RegisterReadSerializer,
    SellPolicyReadSerializer,
    StoreFlagsReadSerializer,
    TillAllocationReleasedReadSerializer,
    TillRegisteredReadSerializer,
    TillResumedReadSerializer,
    TillStateReadSerializer,
)
from sell.serializers import (
    ConsentAnswerWriteSerializer,
    ConsentRecordedSerializer,
    ConsentStateSerializer,
    ContinuityFlagRowSerializer,
    ContinuityFlagWriteSerializer,
    CustomerDisplayPermitSerializer,
    HeldBillsWriteSerializer,
    IrnQueueRowSerializer,
    IrnQueueWriteSerializer,
    RegisterHandoverWriteSerializer,
    ReturnWhereSerializer,
    SaleAcceptedSerializer,
    SaleReadSerializer,
    SaleRowSerializer,
    SaleWriteSerializer,
    SavedSizeCorrectionWriteSerializer,
    SavedSizesSerializer,
    SellPolicyWriteSerializer,
    TillAllocationReleaseSerializer,
    TillNumberBlocksWriteSerializer,
    TillNumberingSerializer,
    TillRegisterWriteSerializer,
    TillResumeSerializer,
)
from sell.services.accept import AcceptError, accept_sale
from sell.services.consent import consent_state, mobile_or_refuse, record_answer
from sell.services.dataset import TillScopeError, build_dataset, resolve_till_store
from sell.services.exchange_tax import where_to_return
from sell.services.invoice_numbers import till_numbering
from sell.services.refunds import with_returned
from sell.services.register import record_handover, register_state
from sell.services.saved_sizes import correct as correct_saved_size
from sell.services.saved_sizes import for_mobile as saved_sizes_for
from sell.services.till_authority import (
    TillError,
    active_till,
    issue_allocation,
    live_pause,
    register_till,
    release_allocation,
    renew_authority,
    resume_till,
    till_state,
)

#: A search is for finding one customer's bill, not for exporting the day.
SEARCH_LIMIT = 50

#: The Bills screen's own query parameters (OPS-08). Named as a set because the
#: presence of any one of them is what tells `GET /sell/sales` which of its two
#: questions it is being asked.
BILL_LIST_KEYS = ("store", "from", "to", "q")

#: How many of the *settled* rows the queue sends back. The work is the pending
#: list, which is never truncated - what is bounded is the history behind it, and
#: a clerk looking for a bill they raised in March searches for it by number
#: rather than scrolling. When the cap bites, the response says `truncated` and
#: the screen says so out loud.
SETTLED_LIMIT = 200


class SellPolicyView(APIView):
    """The chain-wide discount dials, read whole and written whole (#271)."""

    permission_classes = [IsAuthenticated, CanReadOrManagePolicy]

    @extend_schema(responses=SellPolicyReadSerializer)
    def get(self, request: Request) -> Response:
        return Response(SellPolicy.current().as_till_policy())

    @extend_schema(request=SellPolicyWriteSerializer, responses=SellPolicyReadSerializer)
    def put(self, request: Request) -> Response:
        form = SellPolicyWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        policy = SellPolicy.current()
        policy.manual_discount_cap_percent = form.validated_data["manual_discount_cap_percent"]
        policy.manual_discount_on_offer_lines = form.validated_data[
            "manual_discount_on_offer_lines"
        ]
        policy.save(update_fields=["manual_discount_cap_percent", "manual_discount_on_offer_lines"])
        return Response(policy.as_till_policy())


def _sales(user: Any) -> QuerySet[Sale]:
    """Bills at the caller's own stores, in the read shape.

    The only way this module reaches `Sale` on a read path, so a screen cannot
    forget the scope: a store person sees their own counter's bills and nobody
    else's.
    """
    rows: QuerySet[Sale] = scope_by_store(
        # `irn_queue_item` is joined rather than left to the serializer: the read
        # shape names it (#187), and a reverse one-to-one nobody joined is a
        # query per bill on a list of fifty.
        Sale.objects.select_related(
            "store", "created_by", "irn_queue_item", "exchange_of"
        ).prefetch_related(
            # Annotated rather than left to the serializer: what is still
            # returnable is what the counter's return mode is *for*, and two
            # queries a line on a bill of twenty is a screen that pauses while
            # somebody waits at the counter (#184).
            Prefetch(
                "lines",
                queryset=with_returned(SaleLine.objects.all()),
            ),
            "lines__shares",
            "tenders__credit_note",
            "flags",
            "credit_notes_issued",
        ),
        user,
        "store_id",
     section="sell", minimum="view")
    return rows


def till_store(request: Request) -> tuple[Store | None, Response]:
    """The one store this caller is a counter for, or the refusal to send back.

    Shared by every endpoint that speaks to or about a counter - the dataset, the
    register, the held-bill mirror and the handover - because it is one rule with
    one sentence, "a till is a store login by construction", and a second copy of
    it is a second place for the wording, or the status, to drift.

    `TILL_SCOPE` is the one refusal here that carries a code, and the till needs
    it to: it means "this login will never be a till, a human must fix the
    account", which is not something to retry.
    """
    try:
        return resolve_till_store(request.user), Response(status=200)
    except TillScopeError as exc:
        return None, Response(refusal_body("TILL_SCOPE", str(exc)), status=403)


def _can_accept_bill(access: Any, store_code: str) -> bool:
    try:
        store = resolve_till_store(access.user)
    except TillScopeError:
        return False
    return store.code.casefold() == store_code.strip().casefold() and access.covers_all_actions(
        {"section.sell.operate"}, [(store.pk, None)], {"customer"}, roles={"store_person"},
    )


class SaleListCreateView(APIView):
    """`POST` - the till syncing a bill. `GET` - customer search / reprint (E1, E2).

    The POST is idempotent: the till replays from a durable queue, so the same
    `idempotency_uuid` answers **200 with the same bill and no second write**,
    while a first arrival answers 201. The till tells the two apart on the status
    code and stops replaying either way.
    """

    permission_classes = [IsAuthenticated, CanReadOrBill]

    @extend_schema(
        request=SaleWriteSerializer,
        responses={200: SaleAcceptedSerializer, 201: SaleAcceptedSerializer},
    )
    def post(self, request: Request) -> Response:
        form = SaleWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        try:
            access = resolve_access(request)
            with access.guard_legacy_write(lambda current: _can_accept_bill(current, form.validated_data["store"])):
                result = accept_sale(dict(form.validated_data), request.user)
        except AcceptError as exc:
            return Response(refusal_body(exc.code, exc.message), status=exc.status)
        body = {
            "doc_number": result.sale.doc_number,
            "tax_invoice_number": result.sale.tax_invoice_number,
            "id": result.sale.id,
            "flags": result.flags,
        }
        return Response(
            body,
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )

    @extend_schema(
        parameters=[
            OpenApiParameter("mobile", str, description="Customer mobile contains"),
            OpenApiParameter("name", str, description="Customer name contains"),
            OpenApiParameter("doc", str, description="Bill number or document number contains"),
            OpenApiParameter(
                "recent",
                int,
                description="Return the N (1-10) most-recent store-scoped bills, newest-first."
                " Mutually exclusive with mobile/name/doc.",
            ),
            OpenApiParameter("store", str, description="Store code, within what the caller reads"),
            OpenApiParameter("from", str, description="Billed on or after this day (YYYY-MM-DD)"),
            OpenApiParameter("to", str, description="Billed on or before this day (YYYY-MM-DD)"),
            OpenApiParameter(
                "q",
                str,
                description="Customer name, mobile or bill number contains - the Bills search box",
            ),
        ],
        responses=SaleRowSerializer(many=True),
    )
    def get(self, request: Request) -> Response:
        mobile = (request.query_params.get("mobile") or "").strip()
        name = (request.query_params.get("name") or "").strip()
        doc = (request.query_params.get("doc") or "").strip()
        recent_raw = (request.query_params.get("recent") or "").strip()
        listing = {key: (request.query_params.get(key) or "").strip() for key in BILL_LIST_KEYS}
        if any(listing.values()):
            if mobile or name or doc or recent_raw:
                return Response(
                    refusal_body(
                        "VALIDATION",
                        "The Bills list and the customer search are separate queries.",
                    ),
                    status=400,
                )
            return self._bills(request, listing)

        if recent_raw:
            return self._recent(request, recent_raw, searching=bool(mobile or name or doc))
        return self._search(request, mobile=mobile, name=name, doc=doc)

    def _recent(self, request: Request, recent_raw: str, *, searching: bool) -> Response:
        """`?recent=N` - the counter's own last few bills, for the return doors."""
        # ?recent is mutually exclusive with search filters.
        if searching:
            return Response(
                refusal_body(
                    "VALIDATION",
                    "?recent cannot be combined with mobile, name, or doc search filters.",
                ),
                status=400,
            )
        try:
            n = int(recent_raw)
            if n < 1 or n > 10:
                raise ValueError
        except ValueError:
            return Response(
                refusal_body("VALIDATION", "?recent must be a whole number between 1 and 10."),
                status=400,
            )
        rows = _sales(request.user).order_by("-billed_at")[:n]
        return Response(SaleRowSerializer(rows, many=True).data)

    def _search(self, request: Request, *, mobile: str, name: str, doc: str) -> Response:
        """The customer-search doors (E1): who bought it, or what it was numbered."""
        if not (mobile or name or doc):
            return Response(
                refusal_body(
                    "VALIDATION", "Search by mobile number, customer name or bill number."
                ),
                status=400,
            )
        rows = _sales(request.user)
        if mobile:
            rows = rows.filter(customer_mobile__icontains=mobile)
        if name:
            rows = rows.filter(customer_name__icontains=name)
        if doc:
            # A person reads "74" off the slip as often as the whole key, so both
            # find the bill. The sequence arm is only added when the term really
            # is a number, rather than smuggled in as a value no row can hold.
            match = Q(doc_number__icontains=doc) | Q(tax_invoice_number__icontains=doc)
            if doc.isdigit():
                match |= Q(till_seq=int(doc))
            rows = rows.filter(match)
        return Response(SaleRowSerializer(rows[:SEARCH_LIMIT], many=True).data)

    def _bills(self, request: Request, listing: dict[str, str]) -> Response:
        """`GET ?store=&from=&to=&q=` - the Bills screen's day (OPS-08).

        A different question from the customer search above, and so a different
        branch: that one asks "where is this person's bill", this one asks "what
        did this counter do today". It is bounded by day rather than by a term,
        it sorts newest-first, and it is happy to answer nothing.

        The scope is `_sales`, unchanged: `store` narrows *within* what the
        caller may already read, so naming somebody else's store returns an
        empty list rather than their bills.

        The day bounds are read in the store's own timezone, because a cashier
        asking for "today" means the day they are standing in rather than a UTC
        window that ends at half past five in the evening.
        """
        rows = _sales(request.user)
        if listing["store"]:
            rows = rows.filter(store__code__iexact=listing["store"])
        for key, lookup in (("from", "billed_at__date__gte"), ("to", "billed_at__date__lte")):
            if not listing[key]:
                continue
            day = parse_day(listing[key])
            if day is None:
                return Response(
                    refusal_body("VALIDATION", f"`{key}` must be a date, as 2026-09-22."),
                    status=400,
                )
            rows = rows.filter(**{lookup: day})
        term = listing["q"]
        if term:
            # One box, three ways a person names a bill: who bought it, the
            # number on the slip, or the sequence they read off the end of it.
            match = (
                Q(customer_name__icontains=term)
                | Q(customer_mobile__icontains=term)
                | Q(doc_number__icontains=term)
                | Q(tax_invoice_number__icontains=term)
            )
            if term.isdigit():
                match |= Q(till_seq=int(term))
            rows = rows.filter(match)
        return Response(
            SaleRowSerializer(rows.order_by("-billed_at")[:SEARCH_LIMIT], many=True).data
        )


class DatasetView(APIView):
    """`GET /api/sell/dataset` - everything the counter has to know offline.

    Gated at `sell: operate`, not `view`: this is not a report about selling, it is
    the working copy a till bills from, and it carries the store's manager
    override PIN hashes. Somebody who may read yesterday's bills has no use for it.

    See `sell.services.dataset` for what is in it and why the cursor laps
    backwards.

    `TILL_SCOPE` is the one refusal that carries a code, and the till needs it to:
    it means "this login will never be a till, a human must fix the account", which
    is not something to retry. The capability refusal above it answers with DRF's
    own `{"detail": ...}` at 403 - the same as every sibling `require_section` gate,
    and the same as `/api/stock/availability` recorded when it shipped. Unifying the
    two body shapes is one change across every gate in the project, not this
    endpoint's to make alone.
    """

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        parameters=[OpenApiParameter(name="since", type=str, required=False)],
        responses=DatasetReadSerializer,
    )
    def get(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        payload = build_dataset(store, request.query_params.get("since") or "")
        payload = _with_till_allocation(store, payload)
        # Building a large dataset can outlast a session or assignment. No
        # buffered stock, customer or manager-PIN data may leave after revocation.
        access = resolve_access(request)
        if not access.refresh():
            raise Refusal("AUTH_REQUIRED", "Your access changed. Sign in again.")
        try:
            current = resolve_till_store(request.user)
        except TillScopeError as exc:
            return Response(refusal_body("TILL_SCOPE", str(exc)), status=403)
        if current.pk != store.pk:
            return Response(refusal_body("TILL_SCOPE", "This login changed stores."), status=403)
        if not access.covers_all_actions(
            {"section.sell.operate"}, [(store.pk, None)], {"customer"}, roles={"store_person"},
        ):
            raise Refusal("FIELD_DENIED", "This dataset requires customer access at the counter.", status=403)
        return Response(payload)


class CustomerDisplayView(APIView):
    """`GET /api/sell/customer-display?store=<code>` - may this window show that
    store's customer display (ticket 09, ST-POS-6)?

    Asked once, by the display window as it opens. The window then follows the
    bill through the browser (a channel between it and the till on the same
    device), so it keeps working offline and never asks the server again.

    The same rung and the same one-store rule as the dataset: only a till login
    for exactly the store the window names, and only while the store has the
    switch on. A read: it writes nothing and leaves no audit record.
    """

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        parameters=[OpenApiParameter("store", str, required=True)],
        responses=CustomerDisplayPermitSerializer,
    )
    def get(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        if request.query_params.get("store", "") != store.code:
            raise Refusal(
                "ACTION_DENIED",
                "This login is not the till for that store, so it cannot open its "
                "customer display. Open it from the till at the store.",
            )
        require_feature(store, CUSTOMER_DISPLAY)
        return Response(CustomerDisplayPermitSerializer({"store_code": store.code}).data)


class ConsentView(APIView):
    """`/api/sell/consents` - customer consent at the counter (ticket 15, ST-CMP-6).

    `GET ?mobile=` - what stands for that number: the newest answer to "send my
    bill" and to "send me offers", or null for never asked (off). The counter
    shows it beside the customer so staff know what a withdrawal would change.

    `POST` - one answer, from the till's own queue, so an answer given offline
    arrives later with the time it was given. A replay of the same answer
    answers 200 and writes nothing; a first arrival answers 201 and is audited.
    See `sell.services.consent` for the rules.

    A till login for its one store, like the dataset. Reading needs the switch
    on; a no or a withdrawal is always taken, and a yes needs the switch on.
    """

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        parameters=[OpenApiParameter("mobile", str, required=True)],
        responses=ConsentStateSerializer,
    )
    def get(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        require_feature(store, CUSTOMER_CONSENT)
        mobile = mobile_or_refuse(request.query_params.get("mobile", ""))
        return Response(ConsentStateSerializer(consent_state(store.tenant_id, mobile)).data)

    @extend_schema(
        request=ConsentAnswerWriteSerializer,
        responses={200: ConsentRecordedSerializer, 201: ConsentRecordedSerializer},
    )
    def post(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        form = ConsentAnswerWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        recorded = record_answer(store, request.user, dict(form.validated_data))
        body = {
            "id": recorded.answer.pk,
            "state": consent_state(store.tenant_id, recorded.answer.mobile),
        }
        return Response(
            ConsentRecordedSerializer(body).data,
            status=status.HTTP_201_CREATED if recorded.created else status.HTTP_200_OK,
        )


class SavedSizeView(APIView):
    """`/api/sell/saved-sizes` - the customer's saved size per brand (ticket 18, ST-CUS-2).

    `GET ?mobile=` - the sizes that stand for the number on the bill: the last
    size bought per brand and category, learned from bills, or as staff last
    corrected it. The till shows them when the customer is added to a bill.

    `POST` - correct one saved size, with the customer's agreement (`agreed`),
    against the size the till showed (`was_size`). A replay of the same id
    answers 200 and writes nothing; a first arrival answers 201 and is audited.
    See `sell.services.saved_sizes` for the rules.

    A till login for its one store, like the dataset. Both need the switch on.
    Online only: nothing is queued on the till.
    """

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        parameters=[OpenApiParameter("mobile", str, required=True)],
        responses=SavedSizesSerializer,
    )
    def get(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        require_feature(store, SAVED_SIZES)
        body = saved_sizes_for(store, request.query_params.get("mobile", ""))
        return Response(SavedSizesSerializer(body).data)

    @extend_schema(
        request=SavedSizeCorrectionWriteSerializer,
        responses={200: SavedSizesSerializer, 201: SavedSizesSerializer},
    )
    def post(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        require_feature(store, SAVED_SIZES)
        form = SavedSizeCorrectionWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        corrected = correct_saved_size(store, request.user, dict(form.validated_data))
        body = saved_sizes_for(store, corrected.customer.mobile)
        return Response(
            SavedSizesSerializer(body).data,
            status=status.HTTP_201_CREATED if corrected.created else status.HTTP_200_OK,
        )


class ReturnWhereView(APIView):
    """`GET /api/sell/return-where?doc=<number>` - where can this bill go back?
    (store operations ticket 13, ST-CMP-2)

    The counter asks when a customer's copy names a bill that is not one of its
    store's own. A bill another GSTIN issued is refused with words saying where
    it can be returned (baseline, CA to confirm). The answer names stores only,
    never what is on the bill. A till login at a store with the return tax
    switch on; a read, so no audit record.
    """

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        parameters=[OpenApiParameter("doc", str, required=True)],
        responses=ReturnWhereSerializer,
    )
    def get(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        require_feature(store, EXCHANGE_RETURN_TAX)
        answer = where_to_return(store, request.query_params.get("doc", ""), timezone.now())
        return Response(ReturnWhereSerializer(answer).data)


def _with_till_allocation(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    """Protect what this answer hands the counter, and tell it what it is holding.

    A sync is the moment the till takes a snapshot away with it (PRD §10.2), so it
    is the moment the protection starts - not a separate call somebody could
    forget to make, and not a flag on the request the device could decline to set.

    Only for a goods-v1 store with a registered counter. A legacy store's shelf is
    the legacy ledger's, which this ticket does not touch; a store with no
    registered till has no device to protect anything for.
    """
    version = payload.get("working_set_version")
    till = active_till(store)
    if till is None or version is None:
        payload["till"] = till_state(store, till).as_payload()
        return payload
    if live_pause(till) is not None:
        # The counter is in its transfer pause (change PRD §10.2). Its stock was
        # let go on purpose, and a sync taking it back is what made whoever
        # approves the transfer race a five-minute timer. The till cannot bill
        # until it resumes and takes a fresh dataset, and that sync protects it.
        payload["till"] = till_state(store, till).as_payload()
        return payload
    issue_allocation(
        till,
        int(version),
        {
            "stock_rows": len(payload.get("stock") or []),
            "stock_qty": sum(int(row.get("qty") or 0) for row in payload.get("stock") or []),
            "bills": len(payload.get("bills") or []),
        },
    )
    payload["till"] = till_state(store, till).as_payload()
    return payload


class TillView(APIView):
    """`GET /api/sell/till` - which counter this store is, and how long it may bill.

    The same rung and the same one-store rule as the dataset: a till asking about
    itself. What it answers with is the device's identity, its 24-hour window and
    the working-set version its shelf is on - the three facts a counter compares
    against what it is holding before it trusts itself to bill.
    """

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(responses=TillStateReadSerializer)
    def get(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        return Response(till_state(store).as_payload())


class TillRegisterView(APIView):
    """`POST /api/sell/till/register` - this device is the store's counter now.

    A rung above the till itself (`sell: approve`), for the reason the handover
    sits there: registering a counter decides which machine owns a numbered money
    series, and the cashier whose machine just died is the person who fetches a
    manager rather than the person who decides.

    A store that already has a counter is refused with `TILL_TAKEN` unless the
    caller says out loud that it is *replacing* it and why. Replacement retires
    the old device, writes the handover row the store then works its drawer of
    receipts down from, and issues a fresh counter id.
    """

    permission_classes = [IsAuthenticated, CanHandOverTill]

    @extend_schema(request=TillRegisterWriteSerializer, responses={201: TillRegisteredReadSerializer})
    def post(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        form = TillRegisterWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        try:
            till, token = register_till(
                store,
                request.user,
                replace=bool(form.validated_data["replace"]),
                reason=form.validated_data["reason"],
            )
        except TillError as exc:
            return Response(refusal_body(exc.code, exc.message), status=exc.status)
        body = till_state(store, till).as_payload()
        # The only time the token is ever sent. It identifies the device on the
        # shop floor; it is not a credential, and the session is still what
        # authenticates every call the device makes.
        body["device_token"] = token
        return Response(body, status=status.HTTP_201_CREATED)


class TillRenewView(APIView):
    """`POST /api/sell/till/renew` - another 24 hours of offline billing.

    The till's own call, on the till's own rung, and it works only because it is
    *online*: the window is evidence that this device was talking to us within the
    day, and a device that could mint one would be evidence of nothing. Nothing
    else moves - the series, the snapshot and the queue are all untouched.
    """

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(request=None, responses=TillStateReadSerializer)
    def post(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        try:
            return Response(renew_authority(store).as_payload())
        except TillError as exc:
            return Response(refusal_body(exc.code, exc.message), status=exc.status)


class TillNumberBlocksView(APIView):
    """`POST /api/sell/till/number-blocks` - invoice numbers to bill with offline.

    Store operations ticket 04 (ST-CMP-5). The counter says which of its blocks
    it still holds and how far into each it is; head office tops it up to at least
    one block's worth for this month and the next, once the new number format has
    started (or is about to) at a store where it is switched on. A POST because it
    may issue numbers. The till's own call, on the till's own rung; it works only
    online, like the renewal it rides beside.
    """

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(request=TillNumberBlocksWriteSerializer, responses=TillNumberingSerializer)
    def post(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        form = TillNumberBlocksWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        try:
            held = {
                int(key): int(value)
                for key, value in (form.validated_data.get("held") or {}).items()
            }
        except ValueError:
            return Response(
                refusal_body("VALIDATION", "held names blocks by their id."), status=400
            )
        answer = till_numbering(store, active_till(store), held)
        return Response(TillNumberingSerializer(answer.as_payload()).data)


class TillAllocationReleaseView(APIView):
    """`POST /api/sell/till/allocations/{version}/release` - let the stock go (§10.2).

    The store's own person, at `sell: operate` - the rung the counter runs at -
    with a written reason (Anand, 25 September 2026). It was `sell: approve`,
    which no store seat holds, and every sync takes a hold that stops any transfer
    out of the store being approved, so a store that had ever billed could never
    send stock back. What is being said is still "this counter has reconciled and
    its stock is free again": the release refuses while a numbered bill has not
    arrived. Expiry never does this on its own - see `sell.services.till_authority`.
    """

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        request=TillAllocationReleaseSerializer,
        responses=TillAllocationReleasedReadSerializer,
    )
    def post(self, request: Request, version: int) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        form = TillAllocationReleaseSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        try:
            allocation, pause = release_allocation(
                store,
                int(version),
                request.user,
                form.validated_data["reason"],
                form.validated_data["fy"],
                form.validated_data["next_seq"],
            )
        except TillError as exc:
            return Response(refusal_body(exc.code, exc.message), status=exc.status)
        return Response(
            {
                "version": allocation.version,
                "released_at": allocation.released_at.isoformat()
                if allocation.released_at
                else None,
                "reason": allocation.release_reason,
                "paused": True,
                "pause_fy": pause.fy,
                "pause_next_seq": pause.next_seq,
            }
        )


class TillResumeView(APIView):
    """`POST /api/sell/till/resume` - end the counter's transfer pause (§10.2).

    The store's own person, at the rung the counter runs at (Anand, 25 September
    2026). Ending the pause here is only the server's half: the till stays shut
    until it has taken a fresh dataset, which is also the sync that protects its
    new shelf. Resuming while a transfer out of the store still waits is allowed;
    the fresh hold then stops that approval until the store pauses again.
    """

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(request=TillResumeSerializer, responses=TillResumedReadSerializer)
    def post(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        form = TillResumeSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        try:
            ended = resume_till(
                store,
                request.user,
                form.validated_data.get("fy"),
                form.validated_data.get("next_seq"),
            )
        except TillError as exc:
            return Response(refusal_body(exc.code, exc.message), status=exc.status)
        return Response(
            {
                "paused": False,
                "resumed_at": ended.resumed_at.isoformat()
                if ended is not None and ended.resumed_at
                else None,
            }
        )


class RegisterView(APIView):
    """`GET /api/sell/register` - the till's boot and recovery state (#180).

    The one call a counter makes before it trusts its own bill counter. Gated the
    same as the dataset, and for the same reason: it describes one counter's
    numbering, which is only ever of use to that counter.

    Read-only. The deliberate handover that moves a series onto a new machine is
    the sibling POST, and it belongs to a manager rather than to a till (#189).

    See `sell.services.register` for what each field answers.
    """

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(responses=RegisterReadSerializer)
    def get(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        return Response(register_state(store).as_payload())


class RegisterHandoverView(APIView):
    """`POST /api/sell/register/handover` - this store bills from a new machine now.

    The sibling of the GET, and everything it does differently follows from the
    same sentence: boot reconciliation is a till talking to itself every morning,
    while a handover is a person deciding that the machine holding this store's
    bill counter is not coming back.

    So it sits a rung higher (`sell: approve`), it will not run without a written
    reason, and it leaves a row behind. What it answers with is the number the new
    machine resumes at and the bills the old one never sent - each of which is a
    printed receipt somebody has to key back in under its original number.

    **It writes nothing to the counter.** The till numbers bills and the server
    accepts them; a handover that also moved `VoucherSeries.next_seq` would be the
    server forming an opinion about a number nobody has printed.
    """

    permission_classes = [IsAuthenticated, CanHandOverTill]

    @extend_schema(
        request=RegisterHandoverWriteSerializer,
        responses=RegisterHandoverReadSerializer,
    )
    def post(self, request: Request) -> Response:
        # The same one-store rule, and the same refusal word, as the two till
        # endpoints: a handover is about one counter's numbering, so a login that
        # can see three shops has no counter to hand over. `TILL_SCOPE` rather
        # than the contract's sketched `SCOPE_DENIED` for the reason the dataset
        # and register endpoints record - it means "this login will never be a
        # till", which is a thing to fix in the account, not to retry.
        store, refusal = till_store(request)
        if store is None:
            return refusal
        form = RegisterHandoverWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        result = record_handover(store, request.user, form.validated_data["reason"])
        return Response(result.as_payload())


class HeldBillsView(APIView):
    """`PUT /api/sell/held-bills` - the counter's parked carts, as a whole list.

    Replace-all, and that is the design rather than an economy. The till is
    authoritative (grill Q13): a hold lives in IndexedDB, is resumed at the
    counter, and may be parked and picked up half a dozen times while the line to
    head office is down. There is no per-hold delete to replay, so the honest
    mirror is "here is everything I have now" - anything the store no longer holds
    is gone by not being mentioned.

    Gated at `sell: operate` and to one store, exactly as the dataset is: this is
    the counter talking about its own counter. Somebody who may read yesterday's
    bills has nothing to park.

    The list is keyed by **store**, which is the same one-till-per-store invariant
    the sale series rests on (`uq_sale_store_fy_seq`). Two counters at one shop
    would each replace the other's mirror here; that is not a new assumption, and
    it moves when the register handover (#189) gives a till an identity.
    """

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(request=HeldBillsWriteSerializer, responses=HeldBillsCountReadSerializer)
    def put(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        form = HeldBillsWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        rows: list[dict[str, Any]] = list(form.validated_data["held"])

        with transaction.atomic():
            # **The store is on both halves, and on the upsert's lookup rather
            # than only in its defaults.** The delete reaching past this counter
            # is the obvious hazard - one store's empty push clearing another's
            # Dashboard row - but the upsert is the quieter one: matching a hold
            # by its uuid alone would find another store's row and move it here,
            # silently, taking that store's count down with it.
            HeldBill.objects.filter(store=store).exclude(
                held_uuid__in=[row["held_uuid"] for row in rows]
            ).delete()
            for row in rows:
                # Updated rather than replaced so a hold keeps its `created_at`:
                # "parked since 11am" is the fact the day-close prompt is about,
                # and a row reborn on every push would always read as new.
                HeldBill.objects.update_or_create(
                    store=store,
                    held_uuid=row["held_uuid"],
                    defaults={
                        "label": row["label"],
                        "held_at": row["held_at"],
                        "expires_policy": row["expires_policy"],
                        "payload": row["payload"],
                    },
                )
        return Response({"count": len(rows)})


def _irn_queue() -> QuerySet[IrnQueueItem]:
    """Every queue row, joined for the read shape. Ungated - use one of the two
    below, never this."""
    return IrnQueueItem.objects.select_related("sale", "sale__store", "handled_by")


def _irn_rows(user: Any) -> QuerySet[IrnQueueItem]:
    """The queue as this caller *reads* it, in deadline order.

    `scope_by_store` on the bill's own store: the rows are a store's bills, so
    the list obeys the top-bar unit switcher exactly as every other document list
    does. An accountant scoped to the whole network sees the whole network until
    they pick a shop, and then they see that shop - which is the same sentence
    the Vendor Ledger and the sales list already hold to.

    Ordering is `IrnQueueItem.Meta` - due date, then id. It is the model's, not
    this function's, because "the oldest deadline first" is what the queue *is*.
    """
    rows: QuerySet[IrnQueueItem] = scope_by_store(_irn_queue(), user, "sale__store_id", section="sell", minimum="view")
    return rows


def _irn_row_to_act_on(user: Any) -> QuerySet[IrnQueueItem]:
    """The queue as this caller may *write* it - the entitlement boundary.

    Deliberately not `_irn_rows`. ADR-0003 is explicit that what you are looking
    at must never decide what you may do, and the reading gate obeys the top-bar
    switcher: an accountant who narrowed the list to one shop to work through it
    would find every other shop's row answering "no such bill", which is the
    switcher silently stripping a right an administrator granted.
    """
    rows: QuerySet[IrnQueueItem] = scope_by_entitlement(_irn_queue(), user, "sale__store_id", section="sell", minimum="operate")
    return rows


class IrnQueueView(APIView):
    """`GET /api/sell/irn-queue` - the B2B bills head office still owes an IRN.

    Above the e-invoice threshold every GSTIN-bearing counter sale must carry an
    IRN within thirty days or it is invalid and the customer loses their input
    credit (grill Q8). The store cannot raise one and is never asked to: the
    deadline rides as data into this list, oldest first, and the people who file
    the returns work it.

    Read-only about the *bill*. Nothing here can touch a posted sale (A7); the
    sibling PUT writes the portal's answer onto the queue row beside it, which is
    a fact about a filing rather than a fact about a sale.
    """

    permission_classes = [IsAuthenticated, CanWorkIrnQueue]

    @extend_schema(
        parameters=[OpenApiParameter(name="status", type=str, required=False)],
        responses=IrnQueueReadSerializer,
    )
    def get(self, request: Request) -> Response:
        today = timezone.localdate()
        rows = _irn_rows(request.user)
        wanted = (request.query_params.get("status") or IrnQueueItem.Status.PENDING).strip()
        if wanted != "all":
            if wanted not in IrnQueueItem.Status.values:
                return Response(
                    refusal_body("VALIDATION", f"'{wanted}' is not a queue status."), status=400
                )
            rows = rows.filter(status=wanted)
        # **Pending rows are never cut, whichever filter is on.** They are the
        # work: a bill dropped off the bottom of this list is a tax invoice
        # nobody raises, and a customer who loses their credit. What is bounded
        # is the settled tail behind them, which is history, and history is what
        # gets long - so the cap is applied to the settled rows alone, and never
        # to a mixed list by counting from the top of it.
        settled = list(rows.exclude(status=IrnQueueItem.Status.PENDING)[: SETTLED_LIMIT + 1])
        truncated = len(settled) > SETTLED_LIMIT
        listed = sorted(
            list(rows.filter(status=IrnQueueItem.Status.PENDING)) + settled[:SETTLED_LIMIT],
            key=lambda row: (row.due_on, row.id),
        )
        pending = _irn_rows(request.user).filter(status=IrnQueueItem.Status.PENDING)
        return Response(
            {
                "today": today,
                "rows": IrnQueueRowSerializer(listed, many=True, context={"today": today}).data,
                # Said out loud rather than silently: a list that stopped at 200
                # and did not mention it reads as "that is all of them".
                "truncated": truncated,
                # Both counts are over the *pending* rows whatever is being
                # listed: they are the header a clerk reads to know whether
                # anything is on fire, and a filter is not supposed to move it.
                "pending_count": pending.count(),
                "overdue_count": pending.filter(due_on__lt=today).count(),
            }
        )


class IrnQueueItemView(APIView):
    """`PUT /api/sell/irn-queue/{id}` - what the portal answered.

    One way only. A row goes to `generated` with its reference or to `failed`,
    and a row already generated is refused: an IRN is the invoice's identity at
    the GSTN, and a second one silently replacing the first would leave two
    documents in the world claiming to be this bill.
    """

    permission_classes = [IsAuthenticated, CanWorkIrnQueue]

    @extend_schema(request=IrnQueueWriteSerializer, responses=IrnQueueRowSerializer)
    def put(self, request: Request, pk: int) -> Response:
        form = IrnQueueWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        # Out of scope answers 404 rather than 403, exactly as the sale detail
        # does: a 403 would confirm a bill this person may not see exists.
        rows = _irn_row_to_act_on(request.user).filter(pk=pk)
        if not rows.exists():
            return Response(
                refusal_body("NOT_FOUND", f"No queued bill #{pk} at your stores."), status=404
            )
        # **The guard is the UPDATE's own WHERE clause, not a read followed by a
        # write.** Reading the status into Python and then saving is check-then
        # -act: two clerks working the same row - or one clerk on two tabs -
        # would both see `pending`, both pass, and the second reference would
        # replace the first silently. An IRN is the invoice's identity at the
        # GSTN, so that is two documents in the world claiming to be this bill,
        # and the 409 that exists to prevent exactly this would never fire.
        # One statement, and the database decides who won.
        written = rows.exclude(status=IrnQueueItem.Status.GENERATED).update(
            status=form.validated_data["status"],
            irn=form.validated_data["irn"],
            handled_by=request.user,
            handled_at=timezone.now(),
            updated_at=timezone.now(),
        )
        row = _irn_row_to_act_on(request.user).get(pk=pk)
        if not written:
            return Response(
                refusal_body(
                    "IRN_ALREADY_RECORDED",
                    f"{row.sale.doc_number} already carries IRN {row.irn}.",
                ),
                status=409,
            )
        return Response(IrnQueueRowSerializer(row, context={"today": timezone.localdate()}).data)


class SaleDetailView(APIView):
    """`GET /api/sell/sales/{doc_number}` - one bill, read-only, for reprint.

    Out of scope answers 404 rather than 403, the same as every other document
    detail here: a 403 would confirm the bill exists.
    """

    permission_classes = [IsAuthenticated, CanReadSales]

    @extend_schema(responses=SaleReadSerializer)
    def get(self, request: Request, doc_number: str) -> Response:
        # By its doc number, or by its number in the new invoice series (ticket 04),
        # which is what a customer's copy says once the new format has started.
        sale = (
            _sales(request.user)
            .filter(Q(doc_number=doc_number) | Q(tax_invoice_number=doc_number))
            .first()
        )
        if sale is None:
            return Response(
                refusal_body("NOT_FOUND", f"No bill '{doc_number}' at your stores."), status=404
            )
        return Response(SaleReadSerializer(sale).data)


def _flags(user: Any) -> QuerySet[ContinuityFlag]:
    """This store's exception rows as the caller *reads* them.

    `scope_by_store` on the flag's own store, so the top-bar unit switcher
    narrows the list exactly as every other document list does. Head office
    scoped to the network sees the network until it picks a shop.
    """
    rows: QuerySet[ContinuityFlag] = scope_by_store(
        ContinuityFlag.objects.select_related("store", "sale", "resolved_by"), user, "store_id"
    , section="sell", minimum="view")
    return rows


def _flag_to_act_on(user: Any) -> QuerySet[ContinuityFlag]:
    """The rows the caller may *clear* - the entitlement boundary.

    Deliberately not `_flags`. ADR-0003: what you are looking at must never decide
    what you may do, and the reading gate obeys the switcher - so a manager who
    narrowed the list to one shop would find every other shop's row answering
    "no such flag", which is the switcher silently stripping a right an
    administrator granted.
    """
    rows: QuerySet[ContinuityFlag] = scope_by_entitlement(
        ContinuityFlag.objects.select_related("store", "sale"), user, "store_id"
    , section="sell", minimum="operate")
    return rows


class StoreFlagsView(APIView):
    """`GET /api/sell/flags` - what the counter's day left open (#188).

    The list side of the exception queue the accept pipeline and the nightly
    check write to. It is a *list*, not a report: every row is something somebody
    is expected to look at and then say something about, which is what the
    sibling PUT is for.

    Gated on `money`, not `sell` - see `sell.permissions.CanWorkStoreFlags`.
    """

    permission_classes = [IsAuthenticated, CanWorkStoreFlags]

    @extend_schema(
        parameters=[
            OpenApiParameter(name="status", type=str, required=False),
            OpenApiParameter(name="date", type=str, required=False),
        ],
        responses=StoreFlagsReadSerializer,
    )
    def get(self, request: Request) -> Response:
        rows = _flags(request.user)
        wanted = (request.query_params.get("status") or ContinuityFlag.Status.OPEN).strip()
        if wanted != "all":
            if wanted not in ContinuityFlag.Status.values:
                return Response(
                    refusal_body("VALIDATION", f"'{wanted}' is not a flag status."), status=400
                )
            rows = rows.filter(status=wanted)
        asked = (request.query_params.get("date") or "").strip()
        if asked:
            day = parse_day(asked)
            if day is None:
                return Response(
                    refusal_body("VALIDATION", f"'{asked}' is not a date (use 2026-07-31)."),
                    status=400,
                )
            rows = ContinuityFlag.for_day(rows, day)
        # **Open rows are never cut, whichever filter is on** - the same rule the
        # IRN queue holds to, for the same reason: they are the work, and a row
        # dropped off the bottom is an exception nobody ever answers. What is
        # bounded is the settled tail behind them, which is history.
        settled = list(
            rows.exclude(status=ContinuityFlag.Status.OPEN).order_by("-created_at")[
                : SETTLED_LIMIT + 1
            ]
        )
        truncated = len(settled) > SETTLED_LIMIT
        listed = sorted(
            list(rows.filter(status=ContinuityFlag.Status.OPEN)) + settled[:SETTLED_LIMIT],
            key=lambda row: row.created_at,
            reverse=True,
        )
        return Response(
            {
                "rows": ContinuityFlagRowSerializer(listed, many=True).data,
                "truncated": truncated,
                # Over the *open* rows whatever is being listed: it is the header
                # somebody reads to know whether anything is waiting, and a filter
                # is not supposed to move it.
                "open_count": _flags(request.user)
                .filter(status=ContinuityFlag.Status.OPEN)
                .count(),
            }
        )


class StoreFlagView(APIView):
    """`PUT /api/sell/flags/{id}` - somebody looked at this one.

    Two answers, and no way back to `open`. **Resolved** is "dealt with";
    **ignored** is "looked at, needs nothing" - and the second takes a note,
    because the first is usually evidenced by the thing itself having changed and
    the second is evidenced by nothing unless a person says why.

    It cannot touch the bill (A7). Clearing an exception is a statement about
    somebody's attention, not a correction of a document - a bill that really is
    wrong is corrected by the kernel's reversing transition and by nothing here.
    """

    permission_classes = [IsAuthenticated, CanWorkStoreFlags]

    @extend_schema(request=ContinuityFlagWriteSerializer, responses=ContinuityFlagRowSerializer)
    def put(self, request: Request, pk: int) -> Response:
        form = ContinuityFlagWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        # Out of scope answers 404 rather than 403, exactly as the IRN row and the
        # sale detail do: a 403 would confirm a bill this person may not see.
        rows = _flag_to_act_on(request.user).filter(pk=pk)
        if not rows.exists():
            return Response(
                refusal_body("NOT_FOUND", f"No exception #{pk} at your stores."), status=404
            )
        # The guard is the UPDATE's own WHERE clause rather than a read then a
        # write: two people clearing one row - or one person on two tabs - would
        # both see `open`, both pass, and the second answer would silently replace
        # the first, taking the note and the name with it.
        written = rows.filter(status=ContinuityFlag.Status.OPEN).update(
            status=form.validated_data["status"],
            cleared_note=form.validated_data["note"],
            resolved_by=request.user,
            resolved_at=timezone.now(),
            updated_at=timezone.now(),
        )
        row = _flag_to_act_on(request.user).get(pk=pk)
        if not written:
            return Response(
                refusal_body(
                    "FLAG_ALREADY_CLEARED",
                    f"{row.get_kind_display()} was already {row.get_status_display().lower()}"
                    f"{f' by {display_name(row.resolved_by)}' if row.resolved_by_id else ''}.",
                ),
                status=409,
            )
        return Response(ContinuityFlagRowSerializer(row).data)
