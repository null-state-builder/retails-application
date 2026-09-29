"""`/api/sell/alterations` - alteration job cards (store operations ticket 22, ST-ORD-3).

Online only: every call is a request to head office, and nothing is queued on a
device. Anyone who works the counter (``sell: operate``) at the store, and only
at a store they may act at (``actionable_stores``). Making a job card needs the
store's ``alterations`` switch on; reading one and moving it on never do
(ticket 01's rule: refuse only new work). The rules are
``sell.services.alterations``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.refusals import Refusal, first_message, refusal_body
from masters.models import Store
from masters.scoping import actionable_stores
from masters.store_feature_registry import ALTERATIONS
from masters.store_features import is_feature_on
from sell.alteration_models import AlterationJob, BilledRetainedCustody
from sell.models import SaleLine
from sell.permissions import CanRunTill
from sell.services import alterations as rules
from sell.services.customers import normalise_mobile

# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


class AlterationWriteSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField(help_text="The screen's own id; a retry reuses it.")
    store = serializers.CharField(max_length=16)
    bill = serializers.CharField(max_length=128, help_text="The garment's bill number.")
    line_no = serializers.IntegerField(min_value=1, help_text="The garment's line on it.")
    qty = serializers.IntegerField(min_value=1, max_value=99, required=False, default=1)
    charge_bill = serializers.CharField(
        max_length=128,
        required=False,
        allow_blank=True,
        default="",
        help_text="The bill the paid alteration's charge is on; blank is the garment's bill.",
    )
    charge_line_no = serializers.IntegerField(
        min_value=1,
        required=False,
        allow_null=True,
        default=None,
        help_text="The paid alteration's own line; none for a free alteration.",
    )
    customer_name = serializers.CharField(max_length=120)
    customer_mobile = serializers.CharField(max_length=20)
    work = serializers.CharField(max_length=500)
    measurements = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default=""
    )
    tailor = serializers.ChoiceField(choices=AlterationJob.Tailor.choices)
    tailor_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    promised_on = serializers.DateField()


class AlterationStepWriteSerializer(serializers.Serializer[dict[str, Any]]):
    store = serializers.CharField(max_length=16)
    command_id = serializers.UUIDField(
        required=False,
        help_text="The screen's own id for this attempt; a retry of the same tap reuses it.",
    )


class AlterationCancelWriteSerializer(AlterationStepWriteSerializer):
    reason = serializers.CharField(max_length=240)


class AlterationGarmentSerializer(serializers.Serializer[dict[str, Any]]):
    barcode = serializers.CharField()
    design = serializers.CharField(allow_blank=True)
    brand = serializers.CharField(allow_blank=True)
    item = serializers.CharField(allow_blank=True)
    size = serializers.CharField(allow_blank=True)
    color = serializers.CharField(allow_blank=True)


class CustodySerializer(serializers.Serializer[dict[str, Any]]):
    location = serializers.ChoiceField(choices=BilledRetainedCustody.Location.choices)
    expected_by = serializers.DateField()
    opened_at = serializers.DateTimeField()
    closed_at = serializers.DateTimeField(allow_null=True)
    outcome = serializers.CharField(allow_blank=True)


class AlterationSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField()
    ref = serializers.CharField()
    store = serializers.CharField()
    status = serializers.ChoiceField(choices=AlterationJob.Status.choices)
    bill = serializers.CharField()
    line_no = serializers.IntegerField()
    garment = AlterationGarmentSerializer()
    qty = serializers.IntegerField()
    charge_paise = serializers.IntegerField()
    charge_bill = serializers.CharField(allow_null=True)
    charge_line_no = serializers.IntegerField(allow_null=True)
    customer_name = serializers.CharField()
    customer_mobile = serializers.CharField()
    work = serializers.CharField()
    measurements = serializers.CharField(allow_blank=True)
    tailor = serializers.ChoiceField(choices=AlterationJob.Tailor.choices)
    tailor_name = serializers.CharField(allow_blank=True)
    promised_on = serializers.DateField()
    days_left = serializers.IntegerField()
    ready_at = serializers.DateTimeField(allow_null=True)
    customer_told_at = serializers.DateTimeField(allow_null=True)
    closed_at = serializers.DateTimeField(allow_null=True)
    cancel_reason = serializers.CharField(allow_blank=True)
    custody = CustodySerializer()


class AlterationListSerializer(serializers.Serializer[dict[str, Any]]):
    store = serializers.CharField()
    switched_on = serializers.BooleanField(
        help_text="Whether a new job card can be made at this store now."
    )
    jobs = AlterationSerializer(many=True)


class AlterationBillGarmentSerializer(serializers.Serializer[dict[str, Any]]):
    line_no = serializers.IntegerField()
    qty = serializers.IntegerField()
    free_qty = serializers.IntegerField(
        help_text="Pieces not given back and not on another job card."
    )
    garment = AlterationGarmentSerializer()


class AlterationBillChargeSerializer(serializers.Serializer[dict[str, Any]]):
    line_no = serializers.IntegerField()
    charge_paise = serializers.IntegerField()


class AlterationBillSerializer(serializers.Serializer[dict[str, Any]]):
    """A bill as the job card form offers it."""

    bill = serializers.CharField()
    printed = serializers.CharField(help_text="The number on the customer's copy.")
    billed_on = serializers.DateField()
    customer_name = serializers.CharField(allow_blank=True)
    customer_mobile = serializers.CharField(allow_blank=True)
    garments = AlterationBillGarmentSerializer(many=True)
    charges = AlterationBillChargeSerializer(many=True)


def _garment(line: SaleLine) -> dict[str, Any]:
    return {
        "barcode": line.barcode,
        "design": line.design,
        "brand": line.brand,
        "item": line.item,
        "size": line.size,
        "color": line.color,
    }


def job_body(job: AlterationJob, today: date) -> dict[str, Any]:
    custody = job.custody
    charge = job.charge_line
    return {
        "id": job.pk,
        "ref": job.ref,
        "store": job.store.code,
        "status": job.status,
        "bill": job.garment_line.sale.doc_number,
        "line_no": job.garment_line.line_no,
        "garment": _garment(job.garment_line),
        "qty": job.qty,
        "charge_paise": int(job.charge_paise),
        "charge_bill": charge.sale.doc_number if charge is not None else None,
        "charge_line_no": charge.line_no if charge is not None else None,
        "customer_name": job.customer_name,
        "customer_mobile": job.customer_mobile,
        "work": job.work,
        "measurements": job.measurements,
        "tailor": job.tailor,
        "tailor_name": job.tailor_name,
        "promised_on": job.promised_on,
        "days_left": (job.promised_on - today).days,
        "ready_at": job.ready_at,
        "customer_told_at": job.customer_told_at,
        "closed_at": job.closed_at,
        "cancel_reason": job.cancel_reason,
        "custody": {
            "location": custody.location,
            "expected_by": custody.expected_by,
            "opened_at": custody.opened_at,
            "closed_at": custody.closed_at,
            "outcome": custody.outcome,
        },
    }


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def _store(user: Any, code: str) -> Store:
    """The store named, if this person may act there; otherwise refused."""
    code = (code or "").strip().upper()
    if not code:
        raise Refusal("VALIDATION", "Name the store.", status=400)
    store: Store | None = actionable_stores(user, section="sell", minimum="operate").filter(code=code).first()
    if store is None:
        raise Refusal("SCOPE_DENIED", f"You cannot work at {code}.", status=403)
    return store


def _rows(store: Store) -> QuerySet[AlterationJob]:
    return rules.rows(store).select_related("store")


def _one(store: Store, pk: Any) -> Response:
    job = _rows(store).filter(pk=pk).first()
    if job is None:
        raise Refusal("NOT_FOUND", "That job card was not found at this store.", status=404)
    return Response(AlterationSerializer(job_body(job, timezone.localdate())).data)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class AlterationListView(APIView):
    """`GET` - this store's job cards: open ones first, then those ended in the
    last 30 days. `q` finds a job card, bill number or mobile. `POST` - make one
    (the switch must be on)."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        operation_id="sell_alterations_list",
        parameters=[
            OpenApiParameter("store", str, required=True),
            OpenApiParameter("q", str, required=False),
        ],
        responses=AlterationListSerializer,
    )
    def get(self, request: Request) -> Response:
        store = _store(request.user, request.query_params.get("store", ""))
        today = timezone.localdate()
        rows = _rows(store)
        q = (request.query_params.get("q") or "").strip()
        if q:
            mobile = normalise_mobile(q)
            match = (
                Q(ref__iexact=q)
                | Q(garment_line__sale__doc_number__iexact=q)
                | Q(garment_line__sale__tax_invoice_number__iexact=q)
                | Q(garment_line__sale__till_number__iexact=q)
            )
            if len(mobile) == 10:
                match |= Q(customer_mobile=mobile)
            rows = rows.filter(match)
        else:
            since = timezone.now() - timedelta(days=30)
            rows = rows.filter(Q(status__in=rules.OPEN) | Q(closed_at__gte=since))
        ordered = sorted(
            rows,
            key=lambda job: (not job.is_open, job.promised_on, job.ref),
        )
        body = {
            "store": store.code,
            "switched_on": is_feature_on(store, ALTERATIONS),
            "jobs": [job_body(job, today) for job in ordered],
        }
        return Response(AlterationListSerializer(body).data)

    @extend_schema(
        request=AlterationWriteSerializer,
        responses={200: AlterationSerializer, 201: AlterationSerializer},
    )
    def post(self, request: Request) -> Response:
        form = AlterationWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        data = dict(form.validated_data)
        store = _store(request.user, data["store"])
        made = rules.create(store, request.user, data)
        job = _rows(store).get(pk=made.job.pk)
        return Response(
            AlterationSerializer(job_body(job, timezone.localdate())).data,
            status=status.HTTP_201_CREATED if made.created else status.HTTP_200_OK,
        )


class AlterationBillView(APIView):
    """`GET` - a bill of this store as the job card form offers it: each garment
    line with the pieces still free to alter, and alteration charges not yet on
    a job card."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        parameters=[
            OpenApiParameter("store", str, required=True),
            OpenApiParameter("number", str, required=True),
        ],
        responses=AlterationBillSerializer,
    )
    def get(self, request: Request) -> Response:
        store = _store(request.user, request.query_params.get("store", ""))
        number = (request.query_params.get("number") or "").strip()
        sale = rules.find_bill(store, number)
        if sale is None:
            raise Refusal(
                "BILL_NOT_FOUND",
                f"No bill {number or '(blank)'} stands at {store.code}.",
                status=404,
            )
        found = rules.bill_lines(sale)
        body = {
            "bill": sale.doc_number,
            "printed": sale.tax_invoice_number or sale.till_number or sale.doc_number,
            "billed_on": timezone.localdate(sale.billed_at),
            "customer_name": sale.customer_name,
            "customer_mobile": sale.customer_mobile,
            "garments": [
                {
                    "line_no": line.line_no,
                    "qty": line.qty,
                    "free_qty": free,
                    "garment": _garment(line),
                }
                for line, free in found.garments
            ],
            "charges": [
                {"line_no": line.line_no, "charge_paise": int(line.net_paise)}
                for line in found.charges
            ],
        }
        return Response(AlterationBillSerializer(body).data)


class AlterationDetailView(APIView):
    """`GET` - one job card."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        operation_id="sell_alterations_detail",
        parameters=[OpenApiParameter("store", str, required=True)],
        responses=AlterationSerializer,
    )
    def get(self, request: Request, pk: str) -> Response:
        store = _store(request.user, request.query_params.get("store", ""))
        return _one(store, pk)


class AlterationStepView(APIView):
    """`POST` - move a job card on one step: to the tailor, ready, customer told,
    handed over. Wired once per step (``step`` below)."""

    permission_classes = [IsAuthenticated, CanRunTill]
    step = ""

    STEPS = {
        "send": rules.send_to_tailor,
        "ready": rules.mark_ready,
        "told": rules.customer_told,
        "hand-over": rules.hand_over,
    }

    @extend_schema(request=AlterationStepWriteSerializer, responses=AlterationSerializer)
    def post(self, request: Request, pk: str) -> Response:
        form = AlterationStepWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        store = _store(request.user, form.validated_data["store"])
        self.STEPS[self.step](
            store, request.user, pk, command_id=form.validated_data.get("command_id")
        )
        return _one(store, pk)


class AlterationCancelView(APIView):
    """`POST` - cancel an open job card; the garment goes back as it is."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(request=AlterationCancelWriteSerializer, responses=AlterationSerializer)
    def post(self, request: Request, pk: str) -> Response:
        form = AlterationCancelWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        store = _store(request.user, form.validated_data["store"])
        rules.cancel(
            store,
            request.user,
            pk,
            reason=form.validated_data["reason"],
            command_id=form.validated_data.get("command_id"),
        )
        return _one(store, pk)
