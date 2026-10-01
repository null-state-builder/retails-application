"""The only unauthenticated tenant-creation seam; permanently closed on claim."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.authentication import enforce_write_protection
from accounts.goods_api import GoodsAPIView
from accounts.registration_models import InstallationRegistration
from accounts.registration_serializers import (
    ConfirmationInput,
    RegistrationEditInput,
    RegistrationInput,
)
from accounts.registration_services import (
    confirm_registration,
    public_state,
    stage_registration,
)
from core.refusals import Refusal
from masters.goods_models import SiteGuard
from masters.models import Store


class RegistrationState(serializers.Serializer[Any]):
    available = serializers.BooleanField()
    pending_confirmation = serializers.BooleanField()
    message = serializers.CharField()
    synthetic = serializers.BooleanField()
    options = serializers.JSONField()


class RegistrationResult(serializers.Serializer[Any]):
    state = serializers.ChoiceField(choices=["awaiting_confirmation", "registered"])
    summary = serializers.JSONField(required=False)
    summary_hash = serializers.CharField(required=False)
    revision = serializers.IntegerField(required=False)
    confirmed = serializers.DictField(child=serializers.BooleanField(), required=False)
    confirming_role = serializers.CharField(required=False)
    first_store_id = serializers.IntegerField(allow_null=True, required=False)
    login_url = serializers.CharField()
    setup_complete = serializers.BooleanField(required=False)


def private_response(data: dict[str, Any], *, status: int = 200) -> Response:
    response = Response(data, status=status)
    response["Cache-Control"] = "no-store, private"
    response["Pragma"] = "no-cache"
    return response


class RegistrationView(APIView):
    authentication_classes: list[Any] = []
    permission_classes = [AllowAny]
    goods_contract = True

    @extend_schema(responses={200: RegistrationState})
    def get(self, request: Request) -> Response:
        return private_response(public_state())

    @extend_schema(
        request=RegistrationInput,
        responses={202: RegistrationResult, 200: RegistrationResult},
    )
    def post(self, request: Request) -> Response:
        enforce_write_protection(request)
        parsed = RegistrationInput(data=request.data)
        parsed.is_valid(raise_exception=True)
        result = stage_registration(parsed.validated_data)
        return private_response(
            result, status=200 if result["state"] == "registered" else 202
        )

    @extend_schema(request=RegistrationEditInput, responses={202: RegistrationResult})
    def patch(self, request: Request) -> Response:
        enforce_write_protection(request)
        parsed = RegistrationEditInput(data=request.data)
        parsed.is_valid(raise_exception=True)
        result = stage_registration(parsed.validated_data, edit=True)
        return private_response(result, status=202)


class RegistrationConfirmView(APIView):
    authentication_classes: list[Any] = []
    permission_classes = [AllowAny]
    goods_contract = True

    @extend_schema(request=ConfirmationInput, responses={200: RegistrationResult})
    def post(self, request: Request) -> Response:
        """Without a hash, authenticate to inspect; with a hash, confirm that exact summary."""
        enforce_write_protection(request)
        parsed = ConfirmationInput(data=request.data)
        parsed.is_valid(raise_exception=True)
        return private_response(confirm_registration(parsed.validated_data))


class RegistrationSetupView(GoodsAPIView):
    @extend_schema(responses={200: RegistrationResult})
    def get(self, request: Request) -> Response:
        access = self.access(request)
        access.require("access.manage")
        row = InstallationRegistration.objects.filter(
            tenant_id=access.tenant_id, completed_at__isnull=False
        ).first()
        if row is None:
            raise Refusal("NOT_FOUND", "This company has no signup proposal.")
        guard = SiteGuard.objects.filter(
            tenant_id=access.tenant_id,
            site_id=row.first_store_id,
            sell_ready=True,
            selling_mode=SiteGuard.SellingMode.ONLINE_ALPHA,
        ).first()
        complete = False
        first_store_id = row.first_store_id
        if guard is not None and first_store_id is not None:
            from django.utils import timezone

            from masters.first_store_readiness import selling_checks

            site = Store.objects.get(tenant_id=access.tenant_id, pk=first_store_id)
            complete = all(check["passed"] for check in selling_checks(site, timezone.now()))
        result = {
            "state": "registered",
            "summary": row.summary,
            "summary_hash": row.summary_hash,
            "revision": row.revision,
            "first_store_id": row.first_store_id,
            "login_url": "/login",
            "setup_complete": complete,
        }
        access.revalidate_delivery()
        return private_response(result)
