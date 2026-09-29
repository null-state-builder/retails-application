"""Store task checklists (store operations ticket 49, ST-OPS-4). See
``storefront.checklists`` for the rules.

``GET  /api/store/checklists?store=``                    the store's lists today, and
                                                         what is still to do from the last days
``POST /api/store/checklists/ticks``                     tick one item, with an optional photo
``GET  /api/store/checklists/ticks/<id>/photo``          a tick's photo
``GET  /api/store/checklist-templates``                  Admin's templates
``POST /api/store/checklist-templates``                  set a template
``POST /api/store/checklist-templates/<id>``             change it
``POST /api/store/checklist-templates/<id>/stop``        stop it

The store's list answers to ``home: view`` and the store the Dashboard itself
would open (``storefront.dashboard.resolve_store``: the caller's scope, narrowed by
the top-bar switcher). Ticking needs ``sell: operate`` too - the store's staff -
and the switch on at that store. Templates are read with ``setup: view`` and set,
changed and stopped by Admin only; setting or changing one needs the switch on at
a store in Admin's scope. Stopping never does: it only corrects what is there.
Every write runs as one command, so its ``AuditEvent`` records who, when and the
values before and after.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require_section, user_can_at
from accounts.sections import CAP_MANAGE, CAP_VIEW
from core.refusals import Refusal, issue
from masters.models import Store
from masters.scoping import actionable_stores, active_store_ids
from masters.store_features import require_feature
from storefront import checklists as rules
from storefront.checklist_models import ChecklistEvery, ChecklistTemplate, ChecklistTick
from storefront.dashboard import resolve_store

STORE_PARAM = OpenApiParameter(
    "store",
    OpenApiTypes.STR,
    required=False,
    description="The store's code. May be left out by a login placed at one store.",
)


# -- serializers (the OpenAPI shape the PWA's client is generated from) -------------


class ChecklistTickReadSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    by = serializers.CharField()
    at = serializers.DateTimeField()
    late = serializers.BooleanField(help_text="Ticked after the list's time had passed.")
    has_photo = serializers.BooleanField()


class ChecklistItemStateSerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    text = serializers.CharField()
    opens = serializers.CharField(help_text='"", "count_schedule" or "cash_count".')
    tick = ChecklistTickReadSerializer(allow_null=True)
    missed = serializers.BooleanField(help_text="The list's time has passed and it is not ticked.")


class ChecklistListSerializer(serializers.Serializer[Any]):
    template_id = serializers.UUIDField()
    name = serializers.CharField()
    due_on = serializers.DateField()
    due_by = serializers.CharField(allow_null=True, help_text="HH:MM; null is the day's end.")
    passed = serializers.BooleanField(help_text="The list's time has passed.")
    items = ChecklistItemStateSerializer(many=True)


class ChecklistTodaySerializer(serializers.Serializer[Any]):
    store = serializers.CharField()
    store_name = serializers.CharField()
    today = serializers.DateField()
    on = serializers.BooleanField(help_text="The store-checklists switch at this store.")
    can_tick = serializers.BooleanField()
    missed_days = serializers.IntegerField(help_text="How many days back a missed item shows.")
    lists = ChecklistListSerializer(many=True, help_text="Every list due today.")
    missed = ChecklistListSerializer(
        many=True, help_text="Earlier lists whose time passed, with only the items still open."
    )


class ChecklistTickWriteSerializer(serializers.Serializer[Any]):
    """Sent as a multipart form, with the photo as ``photo`` when there is one."""

    client_id = serializers.UUIDField(help_text="The screen's own id for this tick.")
    store = serializers.CharField(required=False, allow_blank=True, default="")
    template_id = serializers.UUIDField()
    item_id = serializers.CharField(max_length=36)
    due_on = serializers.DateField()
    photo = serializers.FileField(
        required=False, allow_null=True, default=None, help_text="A JPEG or PNG photo."
    )


class ChecklistTickSavedSerializer(serializers.Serializer[Any]):
    template_id = serializers.UUIDField()
    item_id = serializers.CharField()
    due_on = serializers.DateField()
    tick = ChecklistTickReadSerializer()


class ChecklistItemSerializer(serializers.Serializer[Any]):
    id = serializers.CharField(
        required=False, allow_blank=True, help_text="Kept from the template; empty for a new one."
    )
    text = serializers.CharField(max_length=rules.MAX_ITEM_TEXT)
    opens = serializers.CharField(required=False, allow_blank=True, default="")


class ChecklistTemplateSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    every = serializers.ChoiceField(choices=ChecklistEvery.choices)
    weekday = serializers.IntegerField(allow_null=True, help_text="0 Monday to 6 Sunday.")
    day_of_month = serializers.IntegerField(allow_null=True)
    due_by = serializers.CharField(allow_null=True, help_text="HH:MM; null is the day's end.")
    items = ChecklistItemSerializer(many=True)
    active = serializers.BooleanField()
    revision = serializers.IntegerField()


class ChecklistOpensSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    name = serializers.CharField()


class ChecklistTemplatesPageSerializer(serializers.Serializer[Any]):
    can_edit = serializers.BooleanField()
    stores_on = serializers.ListField(
        child=serializers.CharField(), help_text="Stores in scope with the switch on."
    )
    opens = ChecklistOpensSerializer(many=True)
    templates = ChecklistTemplateSerializer(many=True)


class ChecklistTemplateWriteSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField(help_text="The screen's own id; a retry reuses it.")
    name = serializers.CharField(max_length=rules.MAX_NAME)
    every = serializers.ChoiceField(choices=ChecklistEvery.choices)
    weekday = serializers.IntegerField(required=False, allow_null=True)
    day_of_month = serializers.IntegerField(required=False, allow_null=True)
    due_by = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    items = ChecklistItemSerializer(many=True)


class ChecklistTemplateChangeSerializer(ChecklistTemplateWriteSerializer):
    expected_revision = serializers.IntegerField()


class ChecklistTemplateStopSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    expected_revision = serializers.IntegerField()


# -- the store's list ------------------------------------------------------------------


def _store(user: Any, code: Any) -> Store:
    pick = resolve_store(user, str(code or "").strip())
    if pick.store is None or not user_can_at(user, "home", CAP_VIEW, site_id=pick.store.pk):
        raise Refusal("SCOPE_DENIED", pick.refusal or "That store is outside your access.", status=403)
    return pick.store


class ChecklistTodayView(APIView):
    """`GET /api/store/checklists?store=` - the store's lists today."""

    permission_classes = [IsAuthenticated, require_section("home", CAP_VIEW)]

    @extend_schema(parameters=[STORE_PARAM], responses=ChecklistTodaySerializer)
    def get(self, request: Request) -> Response:
        store = _store(request.user, request.query_params.get("store"))
        today = rules.today_at(store)
        on = bool(rules.switched_on([store]))
        body = {
            "store": store.code,
            "store_name": store.name,
            "today": timezone.localdate(),
            "on": on,
            "can_tick": on and rules.may_tick(request.user, store.pk),
            "missed_days": rules.missed_days(),
            "lists": today.lists,
            "missed": today.missed,
        }
        return Response(ChecklistTodaySerializer(body).data)


class ChecklistTickView(APIView):
    """`POST /api/store/checklists/ticks` - tick one item. 201 first, 200 a retry."""

    permission_classes = [IsAuthenticated, require_section("home", CAP_VIEW)]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(
        request={"multipart/form-data": ChecklistTickWriteSerializer},
        responses={200: ChecklistTickSavedSerializer, 201: ChecklistTickSavedSerializer},
    )
    def post(self, request: Request) -> Response:
        form = ChecklistTickWriteSerializer(data=request.data)
        if not form.is_valid():
            field, errors = next(iter(form.errors.items()))
            message = f"{field}: {errors[0]}" if isinstance(errors, list) else str(errors)
            raise Refusal(
                "INVALID_REQUEST", message, issues=[issue("INVALID", message, field=field)]
            )
        data = form.validated_data
        store = _store(request.user, data["store"])
        if not rules.may_tick(request.user, store.pk):
            raise Refusal("ACTION_DENIED", "Only the store's staff tick its checklist.", status=403)
        require_feature(store, rules.FEATURE_KEY)
        photo = rules.read_photo(data.get("photo"))
        done = rules.tick(
            store,
            request.user,
            client_id=data["client_id"],
            template_id=data["template_id"],
            item_id=data["item_id"],
            due_on=data["due_on"],
            photo=photo,
        )
        body = {
            "template_id": done.tick.template_id,
            "item_id": done.tick.item_id,
            "due_on": done.tick.due_on,
            "tick": rules.tick_json(done.tick),
        }
        return Response(
            ChecklistTickSavedSerializer(body).data,
            status=status.HTTP_201_CREATED if done.created else status.HTTP_200_OK,
        )


class ChecklistTickPhotoView(APIView):
    """`GET` a tick's photo, for anyone who may open that store's list."""

    permission_classes = [IsAuthenticated, require_section("home", CAP_VIEW)]

    @extend_schema(
        responses={
            (200, "*/*"): {
                "type": "string",
                "format": "binary",
                "description": "The photo as it was taken.",
            }
        }
    )
    def get(self, request: Request, pk: uuid.UUID) -> HttpResponse:
        row = ChecklistTick.objects.select_related("store").filter(pk=pk).first()
        ids = active_store_ids(request.user, section="home", minimum="view")
        if row is None or (ids is not None and row.store_id not in ids) or not row.has_photo:
            raise Refusal("NOT_FOUND", "That photo was not found.", status=404)
        # The same gate as the list itself, so the photo never opens where it does not.
        _store(request.user, row.store.code)
        response = HttpResponse(rules.photo_bytes(row), content_type=row.photo_media_type)
        response["Content-Disposition"] = f'inline; filename="checklist-{row.pk}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response


# -- Admin's templates -------------------------------------------------------------------


def _stores_on(user: Any, *, minimum: str = CAP_VIEW) -> list[Store]:
    return rules.switched_on(list(actionable_stores(user, section="setup", minimum=minimum)))


def _require_editor(user: Any) -> None:
    if not rules.may_edit_templates(user):
        raise Refusal("ACTION_DENIED", "Only Admin sets the store checklists.", status=403)


def _require_on_somewhere(user: Any) -> None:
    if not _stores_on(user, minimum=CAP_MANAGE):
        raise Refusal(
            "FEATURE_OFF",
            "Store task checklists are not switched on at any store you work at.",
            status=403,
        )


def _template(pk: uuid.UUID) -> ChecklistTemplate:
    row = ChecklistTemplate.objects.filter(pk=pk).first()
    if row is None:
        raise Refusal("NOT_FOUND", "That checklist was not found.", status=404)
    return row


def _body(request: Request) -> dict[str, Any]:
    if not isinstance(request.data, dict):
        raise Refusal("INVALID_REQUEST", "The request body must be a JSON object.")
    return dict(request.data)


class ChecklistTemplatesView(APIView):
    permission_classes = [IsAuthenticated, require_section("setup", CAP_VIEW)]

    @extend_schema(
        operation_id="store_checklist_templates_list",
        responses=ChecklistTemplatesPageSerializer,
    )
    def get(self, request: Request) -> Response:
        if not rules.may_read_templates(request.user):
            raise Refusal("ACTION_DENIED", "You cannot read store checklist templates.", status=403)
        on = _stores_on(request.user)
        body = {
            "can_edit": rules.may_edit_templates(request.user) and bool(on),
            "stores_on": [store.code for store in on],
            "opens": [{"key": key, "name": name} for key, name in rules.OPENS.items()],
            "templates": [rules.template_json(row) for row in rules.live_templates()],
        }
        return Response(ChecklistTemplatesPageSerializer(body).data)

    @extend_schema(
        operation_id="store_checklist_templates_create",
        request=ChecklistTemplateWriteSerializer, responses={201: ChecklistTemplateSerializer}
    )
    def post(self, request: Request) -> Response:
        _require_editor(request.user)
        _require_on_somewhere(request.user)
        body = _body(request)
        data = rules.clean_template(body)
        row = rules.set_template(request.user, body.get("command_id"), data)
        return Response(
            ChecklistTemplateSerializer(rules.template_json(row)).data,
            status=status.HTTP_201_CREATED,
        )


class ChecklistTemplateChangeView(APIView):
    permission_classes = [IsAuthenticated, require_section("setup", CAP_VIEW)]

    @extend_schema(
        operation_id="store_checklist_templates_change",
        request=ChecklistTemplateChangeSerializer,
        responses=ChecklistTemplateSerializer,
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        _require_editor(request.user)
        _require_on_somewhere(request.user)
        body = _body(request)
        template = _template(pk)
        data = rules.clean_template(body, kept=template)
        row = rules.change_template(
            request.user, template, body.get("command_id"), body.get("expected_revision"), data
        )
        return Response(ChecklistTemplateSerializer(rules.template_json(row)).data)


class ChecklistTemplateStopView(APIView):
    permission_classes = [IsAuthenticated, require_section("setup", CAP_VIEW)]

    @extend_schema(request=ChecklistTemplateStopSerializer, responses=ChecklistTemplateSerializer)
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        _require_editor(request.user)
        body = _body(request)
        row = rules.stop_template(
            request.user, _template(pk), body.get("command_id"), body.get("expected_revision")
        )
        return Response(ChecklistTemplateSerializer(rules.template_json(row)).data)
