"""The Customers menu: list, customer page and rights (store operations ticket 16).

Under ``/api/goods-v1/sell/``:

``GET  customers``                  customers at the person's stores, newest buyer
                                    first; ``?q=`` searches the number or the name
``GET  customers/<id>``             one customer: name, number, GSTIN, both consents
                                    and the bills they bought on at those stores
``POST customers/<id>/held``        show the customer everything held about them
``POST customers/<id>/correct``     correct the name or GSTIN held
``POST customers/<id>/withdraw``    withdraw "send my bill" or "send me offers"
``POST customers/<id>/erase``       erase what is held (bills stay)
``POST customers/<id>/merge``       merge another record for the same person into
                                    this one, with the customer present (ticket 17)
``POST customers/<id>/move``        move this record to the customer's new number,
                                    with the customer present (ticket 17)

Reading needs ``sell: view``; each POST needs ``sell: operate`` and names the
store the customer is at (``site_id``), one the person may act at. Everything
needs the ``customer-rights`` switch on; a merge or a move needs the
``customer-merge-retention`` switch on at that store too. Each POST is one command, audited with
values before and after (masked, see ``sell.services.customer_rights``); a
withdrawal is ticket 15's consent answer, audited as every answer is.

Online only: nothing here queues on a device. The screen says so when offline.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, parse_meta
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from masters.models import Customer, Store
from sell.models import ConsentAnswer
from sell.serializers import SavedSizeRowSerializer
from sell.services.consent import record_answer
from sell.services.customer_rights import (
    CORRECT_ACTION,
    ERASE_ACTION,
    MERGE_ACTION,
    MOVE_ACTION,
    SHOW_ACTION,
    acting_store,
    acting_stores,
    bill_stats,
    correct_handler,
    erase_handler,
    find_customer,
    held,
    merge_handler,
    merge_store,
    move_handler,
    page,
    parse_correction,
    parse_move,
    require_present,
    require_reader,
    search_customers,
    show_handler,
    withdrawal,
)

# -- shapes ----------------------------------------------------------------------------


class CustomerStoreSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class CustomerRowSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.CharField(allow_blank=True)
    mobile = serializers.CharField()
    gstin = serializers.CharField(allow_blank=True)
    bills = serializers.IntegerField(help_text="Bills at the stores you read.")
    last_bill_at = serializers.DateTimeField(allow_null=True)


class CustomerListSerializer(serializers.Serializer[Any]):
    customers = CustomerRowSerializer(many=True)
    truncated = serializers.BooleanField()
    stores = CustomerStoreSerializer(many=True, help_text="Where you may act, switch on.")
    can_act = serializers.BooleanField()


class GoodsConsentStandingSerializer(serializers.Serializer[Any]):
    given = serializers.BooleanField()
    how = serializers.CharField()
    under_18 = serializers.BooleanField(allow_null=True)
    wording_version = serializers.IntegerField()
    answered_at = serializers.CharField()


class CustomerConsentSerializer(serializers.Serializer[Any]):
    bill = GoodsConsentStandingSerializer(allow_null=True)
    offers = GoodsConsentStandingSerializer(allow_null=True)


class CustomerBillSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    doc_number = serializers.CharField(allow_blank=True)
    billed_at = serializers.DateTimeField()
    store_code = serializers.CharField()
    customer_name = serializers.CharField(allow_blank=True)
    buyer_gstin = serializers.CharField(allow_blank=True)
    net_paise = serializers.IntegerField()
    exchange = serializers.BooleanField()
    cancelled = serializers.BooleanField()


class CustomerNumberSerializer(serializers.Serializer[Any]):
    mobile = serializers.CharField()
    reason = serializers.ChoiceField(choices=["merged", "moved"])
    until = serializers.DateTimeField(
        allow_null=True, help_text="A moved number was theirs until then; merged: still theirs."
    )


class CustomerPageSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.CharField(allow_blank=True)
    mobile = serializers.CharField()
    gstin = serializers.CharField(allow_blank=True)
    consent = CustomerConsentSerializer()
    bills = CustomerBillSerializer(many=True)
    bills_total = serializers.IntegerField()
    numbers = CustomerNumberSerializer(
        many=True, help_text="Other numbers whose bills are theirs (ticket 17)."
    )
    stores = CustomerStoreSerializer(many=True)
    can_act = serializers.BooleanField()
    can_merge_or_move = serializers.BooleanField(
        help_text="Merge and new number are on at a store where you may act."
    )


class HeldProfileSerializer(serializers.Serializer[Any]):
    name = serializers.CharField(allow_blank=True)
    mobile = serializers.CharField()
    gstin = serializers.CharField(allow_blank=True)
    first_recorded = serializers.DateTimeField()
    last_changed = serializers.DateTimeField()


class HeldAnswerSerializer(serializers.Serializer[Any]):
    question = serializers.CharField()
    given = serializers.BooleanField()
    how = serializers.CharField()
    under_18 = serializers.BooleanField(allow_null=True)
    wording_version = serializers.IntegerField()
    answered_at = serializers.DateTimeField()
    store_code = serializers.CharField()
    till_number = serializers.CharField(allow_blank=True)
    mobile = serializers.CharField(help_text="The number it was given for.")


class HeldBillsSerializer(serializers.Serializer[Any]):
    count = serializers.IntegerField()
    first_at = serializers.DateTimeField(allow_null=True)
    last_at = serializers.DateTimeField(allow_null=True)


class CustomerHeldSerializer(serializers.Serializer[Any]):
    profile = HeldProfileSerializer()
    numbers = CustomerNumberSerializer(many=True)
    consent_answers = HeldAnswerSerializer(many=True)
    bills = HeldBillsSerializer()
    sizes = SavedSizeRowSerializer(
        many=True, help_text="Ticket 18: the size standing per brand and category."
    )
    marketing = serializers.ListField(child=serializers.DictField(), help_text="Not recorded yet.")


class CustomerErasedSerializer(serializers.Serializer[Any]):
    erased = serializers.BooleanField()


class _Meta(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    site_id = serializers.IntegerField(help_text="The store the customer is at.")


class CustomerHeldRequestSerializer(_Meta):
    pass


class CustomerCorrectRequestSerializer(_Meta):
    name = serializers.CharField(allow_blank=True)
    gstin = serializers.CharField(allow_blank=True)
    was_name = serializers.CharField(
        allow_blank=True, help_text="The name the form was built from."
    )
    was_gstin = serializers.CharField(allow_blank=True, help_text="The GSTIN it was built from.")


class CustomerWithdrawRequestSerializer(_Meta):
    question = serializers.ChoiceField(choices=["bill", "offers"])


class CustomerEraseRequestSerializer(_Meta):
    mobile_last4 = serializers.CharField(help_text="The number's last four digits, typed again.")


class CustomerMergeRequestSerializer(_Meta):
    other_id = serializers.IntegerField(help_text="The record merged in and closed.")
    other_last4 = serializers.CharField(help_text="Its number's last four digits, typed again.")
    customer_present = serializers.BooleanField(help_text="Must be true.")


class CustomerMoveRequestSerializer(_Meta):
    new_mobile = serializers.CharField(help_text="The customer's new mobile number.")
    was_mobile = serializers.CharField(help_text="The number the form was built from.")
    customer_present = serializers.BooleanField(help_text="Must be true.")


# -- reads -----------------------------------------------------------------------------


class GoodsCustomerListView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_sell_customers_list",
        parameters=[OpenApiParameter("q", str, description="Number or name contains")],
        responses=CustomerListSerializer,
    )
    def get(self, request: Request) -> Response:
        self.access(request)
        params = check_query(request, allowed=("q",))
        stores = require_reader(request.user)
        rows, truncated = search_customers(stores, params.get("q", ""))
        acting = acting_stores(request.user)
        body = {
            "customers": [
                {
                    "id": row.pk,
                    "name": row.name,
                    "mobile": row.mobile,
                    "gstin": row.gstin,
                    "bills": bills,
                    "last_bill_at": last,
                }
                for row in rows
                for bills, last in [bill_stats(stores, row)]
            ],
            "truncated": truncated,
            "stores": [{"id": s.pk, "code": s.code, "name": s.name} for s in acting],
            "can_act": bool(acting),
        }
        return Response(CustomerListSerializer(body).data)


class GoodsCustomerDetailView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_sell_customers_detail",
        responses=CustomerPageSerializer,
    )
    def get(self, request: Request, pk: int) -> Response:
        self.access(request)
        check_query(request, allowed=())
        stores = require_reader(request.user)
        customer = find_customer(stores, pk)
        return Response(CustomerPageSerializer(page(request.user, stores, customer)).data)


# -- the rights ------------------------------------------------------------------------


class _RightsView(GoodsAPIView):
    """One rights action: who, where, which customer, then one command."""

    fields: frozenset[str] = frozenset()

    def _prepare(
        self, request: Request, pk: int
    ) -> tuple[Any, Any, dict[str, Any], list[Store], Store, Customer | None]:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, {"site_id", *self.fields}, required=["site_id"])
        stores = require_reader(request.user)
        store = acting_store(request.user, body["site_id"])
        try:
            customer: Customer | None = find_customer(stores, pk)
        except Refusal:
            # Gone already - erased, perhaps by this very command before the
            # connection dropped. The command below answers a replay with what it
            # did then, and refuses anything else as not found.
            customer = None
        return access, meta, body, stores, store, customer


def _gone(run: CommandRun) -> CommandResult:
    raise Refusal("NOT_FOUND", "No customer like that at your stores.", status=404)


class GoodsCustomerHeldView(_RightsView):
    @extend_schema(request=CustomerHeldRequestSerializer, responses=CustomerHeldSerializer)
    def post(self, request: Request, pk: int) -> Response:
        access, meta, _, stores, store, customer = self._prepare(request, pk)
        if customer is None:
            raise Refusal("NOT_FOUND", "No customer like that at your stores.", status=404)
        self.run_command(
            request,
            access=access,
            action=SHOW_ACTION,
            meta=meta,
            business_input={"customer": pk, "site": store.pk},
            handler=show_handler(pk, store),
            resource_ids=[str(pk)],
            subject_key=f"customer:{pk}",
            site_id=store.pk,
        )
        return Response(CustomerHeldSerializer(held(customer, store.tenant_id, stores)).data)


class GoodsCustomerCorrectView(_RightsView):
    fields = frozenset({"name", "gstin", "was_name", "was_gstin"})

    @extend_schema(request=CustomerCorrectRequestSerializer, responses=CustomerPageSerializer)
    def post(self, request: Request, pk: int) -> Response:
        access, meta, body, stores, store, customer = self._prepare(request, pk)
        if customer is None:
            raise Refusal("NOT_FOUND", "No customer like that at your stores.", status=404)
        change = parse_correction(body)
        self.run_command(
            request,
            access=access,
            action=CORRECT_ACTION,
            meta=meta,
            business_input={
                "customer": pk,
                "site": store.pk,
                "name": change.name,
                "gstin": change.gstin,
                "was_name": change.was_name,
                "was_gstin": change.was_gstin,
            },
            handler=correct_handler(pk, store, change),
            resource_ids=[str(pk)],
            subject_key=f"customer:{pk}",
            site_id=store.pk,
        )
        fresh = find_customer(stores, pk)
        return Response(CustomerPageSerializer(page(request.user, stores, fresh)).data)


class GoodsCustomerWithdrawView(_RightsView):
    fields = frozenset({"question"})

    @extend_schema(request=CustomerWithdrawRequestSerializer, responses=CustomerPageSerializer)
    def post(self, request: Request, pk: int) -> Response:
        _, meta, body, stores, store, customer = self._prepare(request, pk)
        if customer is None:
            raise Refusal("NOT_FOUND", "No customer like that at your stores.", status=404)
        # A replay keeps the time the answer was first recorded, so it matches.
        first = ConsentAnswer.objects.filter(pk=meta.command_id).first()
        data = withdrawal(
            customer,
            str(body.get("question") or ""),
            meta.command_id,
            first.answered_at if first is not None else timezone.now(),
            store.tenant_id,
        )
        # The answer's id is the command's: pressing again after a dropped
        # connection replays the same answer rather than recording a second.
        self.goods_command_id = meta.command_id
        record_answer(store, request.user, data)
        return Response(CustomerPageSerializer(page(request.user, stores, customer)).data)


class GoodsCustomerEraseView(_RightsView):
    fields = frozenset({"mobile_last4"})

    @extend_schema(request=CustomerEraseRequestSerializer, responses=CustomerErasedSerializer)
    def post(self, request: Request, pk: int) -> Response:
        access, meta, body, _, store, customer = self._prepare(request, pk)
        last4 = str(body.get("mobile_last4") or "").strip()
        if len(last4) != 4 or not last4.isdigit():
            raise Refusal(
                "INVALID_REQUEST",
                "Type the last four digits of the customer's number to confirm.",
            )
        self.run_command(
            request,
            access=access,
            action=ERASE_ACTION,
            meta=meta,
            business_input={"customer": pk, "site": store.pk, "mobile_last4": last4},
            handler=_gone if customer is None else erase_handler(pk, store, last4),
            resource_ids=[str(pk)],
            subject_key=f"customer:{pk}",
            site_id=store.pk,
        )
        return Response(CustomerErasedSerializer({"erased": True}).data)


class GoodsCustomerMergeView(_RightsView):
    fields = frozenset({"other_id", "other_last4", "customer_present"})

    @extend_schema(request=CustomerMergeRequestSerializer, responses=CustomerPageSerializer)
    def post(self, request: Request, pk: int) -> Response:
        access, meta, body, stores, _, customer = self._prepare(request, pk)
        store = merge_store(request.user, body["site_id"])
        require_present(body)
        try:
            other_id = int(str(body.get("other_id")))
        except (TypeError, ValueError):
            raise Refusal("INVALID_REQUEST", "Pick the other record to merge in.") from None
        if other_id == pk:
            raise Refusal("INVALID_REQUEST", "A record cannot be merged into itself.")
        last4 = str(body.get("other_last4") or "").strip()
        if len(last4) != 4 or not last4.isdigit():
            raise Refusal(
                "INVALID_REQUEST",
                "Type the last four digits of the other record's number to confirm.",
            )
        try:
            find_customer(stores, other_id)
            found = customer is not None
        except Refusal:
            # Closed already - perhaps by this very command before the connection
            # dropped. The command answers a replay with what it did then.
            found = False
        self.run_command(
            request,
            access=access,
            action=MERGE_ACTION,
            meta=meta,
            business_input={
                "customer": pk,
                "other": other_id,
                "site": store.pk,
                "other_last4": last4,
                "customer_present": True,
            },
            handler=merge_handler(pk, other_id, store, last4) if found else _gone,
            resource_ids=[str(pk), str(other_id)],
            subject_key=f"customer:{pk}",
            site_id=store.pk,
        )
        return Response(
            CustomerPageSerializer(page(request.user, stores, find_customer(stores, pk))).data
        )


class GoodsCustomerMoveView(_RightsView):
    fields = frozenset({"new_mobile", "was_mobile", "customer_present"})

    @extend_schema(request=CustomerMoveRequestSerializer, responses=CustomerPageSerializer)
    def post(self, request: Request, pk: int) -> Response:
        access, meta, body, stores, _, customer = self._prepare(request, pk)
        store = merge_store(request.user, body["site_id"])
        require_present(body)
        move = parse_move(body)
        self.run_command(
            request,
            access=access,
            action=MOVE_ACTION,
            meta=meta,
            business_input={
                "customer": pk,
                "site": store.pk,
                "new_mobile": move.new,
                "was_mobile": move.was,
                "customer_present": True,
            },
            handler=_gone if customer is None else move_handler(pk, store, move),
            resource_ids=[str(pk)],
            subject_key=f"customer:{pk}",
            site_id=store.pk,
        )
        return Response(
            CustomerPageSerializer(page(request.user, stores, find_customer(stores, pk))).data
        )
