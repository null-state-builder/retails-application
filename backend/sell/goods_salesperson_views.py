"""The staff list per store, and the old salesperson rows Admin resolves (ticket 07).

Under ``/api/goods-v1/sell/``:

``GET  staff-list``                       who is assigned to a store, who is active,
                                          and who sells at the till (ST-HR-1)
``GET  salesperson-matches``              every old salesperson row, frozen, with the
                                          staff record it was matched to or the
                                          reason the rule found none (§29)
``POST salesperson-matches/<id>/resolve`` Admin picks the staff record for an
                                          unmatched row

**Staff list.** Readers hold ``hrms: view`` or higher, for the stores in their own
scope, and only where the store's ``staff-list`` switch is on (B24's rule for a
new screen). A store person sees their own store; another store's staff is not
found. Only what the list shows leaves: code, name, salesperson, active and the
dates of the time here - no phone or personal data (overall PRD §10.4).

**Matches.** Reading needs ``setup: view`` and shows the rows of stores in
scope. Resolving is Admin's alone: a role in
``accounts.role_lists.SALESPERSON_MATCH_RESOLVERS`` (``it_admin``) with the
row's store in scope. The staff record must have been placed at that store at
some time. A row already matched - by the rule or by Admin - is not changed
here: a past bill's seller is not re-pointed. Resolving is correcting an
existing record rather than new work, so it is never behind a switch. Each
resolve is one command whose audit record holds the values before and after,
under ``salesperson_match:<id>``.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_query,
    check_revision,
    parse_int_id,
    parse_meta,
    parse_uuid,
)
from accounts.permissions import user_can
from accounts.role_lists import SALESPERSON_MATCH_RESOLVERS
from accounts.sections import CAP_VIEW
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from masters.models import Store
from masters.scoping import actionable_store_ids, actionable_stores
from masters.store_feature_registry import STAFF_LIST
from masters.store_features import feature, switch_states
from sell.models import SalespersonMatch
from sell.services.salespeople import (
    ever_placed_at,
    people_placed_at,
    rule_outcomes,
    staff_list,
)
from sell.services.salesperson_move import RULE_ADMIN

RESOLVE_ACTION = "sell.salesperson_match.resolve"


# -- staff list ------------------------------------------------------------------------


class StaffListStoreSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class StaffListRowSerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    staff_code = serializers.CharField()
    display_name = serializers.CharField()
    salesperson = serializers.BooleanField()
    active = serializers.BooleanField()
    assigned_from = serializers.DateTimeField(allow_null=True)
    assigned_to = serializers.DateTimeField(allow_null=True)


class StaffListSerializer(serializers.Serializer[Any]):
    stores = StaffListStoreSerializer(many=True)
    site_id = serializers.IntegerField(allow_null=True)
    staff = StaffListRowSerializer(many=True)


def staff_list_stores(user: Any) -> list[Store]:
    """The selling stores in this person's scope where the staff list is switched on."""
    stores = list(actionable_stores(user).filter(store_type=Store.StoreType.STORE))
    states = switch_states(stores, [feature(STAFF_LIST)])
    return [store for store, state in zip(stores, states, strict=True) if state.enabled]


class GoodsStaffListView(GoodsAPIView):
    @extend_schema(responses=StaffListSerializer)
    def get(self, request: Request) -> Response:
        self.access(request)
        params = check_query(request, allowed=("site_id",))
        if not user_can(request.user, "hrms", CAP_VIEW):
            raise Refusal("ACTION_DENIED", "You do not have access to the staff list.")
        stores = staff_list_stores(request.user)
        chosen: Store | None = stores[0] if stores else None
        if params.get("site_id"):
            wanted = parse_int_id(params["site_id"], "site_id")
            chosen = next((store for store in stores if store.pk == wanted), None)
            if chosen is None:
                raise Refusal("NOT_FOUND", "That store's staff list is not available.", status=404)
        rows = staff_list(chosen) if chosen is not None else []
        body = {
            "stores": [{"id": s.pk, "code": s.code, "name": s.name} for s in stores],
            "site_id": chosen.pk if chosen is not None else None,
            "staff": [
                {
                    "id": str(row.staff_id),
                    "staff_code": row.staff_code,
                    "display_name": row.display_name,
                    "salesperson": row.salesperson,
                    "active": row.active,
                    "assigned_from": row.assigned_from,
                    "assigned_to": row.assigned_to,
                }
                for row in rows
            ],
        }
        return Response(StaffListSerializer(body).data)


# -- salesperson matches -------------------------------------------------------------


def may_resolve(user: Any, store_id: int | None = None) -> bool:
    """Admin only, with the row's store in scope (or break-glass)."""
    if getattr(user, "is_superuser", False):
        return True
    code = getattr(getattr(user, "role", None), "code", "")
    if code not in SALESPERSON_MATCH_RESOLVERS:
        return False
    ids = actionable_store_ids(user)
    return ids is None or (store_id is not None and store_id in ids)


class MatchedStaffSerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    staff_code = serializers.CharField()
    display_name = serializers.CharField()


class MatchRowSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    store_id = serializers.IntegerField()
    store_code = serializers.CharField()
    store_name = serializers.CharField()
    code = serializers.CharField()
    name = serializers.CharField()
    was_active = serializers.BooleanField()
    lines = serializers.IntegerField(help_text="Past sale lines sold under this row.")
    staff = MatchedStaffSerializer(allow_null=True)
    rule = serializers.CharField(allow_blank=True)
    rule_label = serializers.CharField()
    reason = serializers.CharField(allow_blank=True)
    matched_at = serializers.DateTimeField(allow_null=True)
    revision = serializers.IntegerField()


class CandidateSerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    staff_code = serializers.CharField()
    display_name = serializers.CharField()
    active = serializers.BooleanField()


class StoreCandidatesSerializer(serializers.Serializer[Any]):
    store_id = serializers.IntegerField()
    staff = CandidateSerializer(many=True)


class MatchesSerializer(serializers.Serializer[Any]):
    rows = MatchRowSerializer(many=True)
    unmatched = serializers.IntegerField()
    candidates = StoreCandidatesSerializer(many=True)
    can_resolve = serializers.BooleanField()


class ResolveRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField()
    staff_id = serializers.UUIDField()


def _staff_json(staff: Any) -> dict[str, Any] | None:
    if staff is None:
        return None
    return {
        "id": str(staff.pk),
        "staff_code": staff.human.staff_code,
        "display_name": staff.human.display_name,
    }


def _row_json(row: SalespersonMatch, lines: int, reason: str = "") -> dict[str, Any]:
    return {
        "id": row.pk,
        "store_id": row.store_id,
        "store_code": row.store.code,
        "store_name": row.store.name,
        "code": row.code,
        "name": row.name,
        "was_active": row.was_active,
        "lines": lines,
        "staff": _staff_json(row.staff),
        "rule": row.rule,
        "rule_label": row.get_rule_display(),
        "reason": reason,
        "matched_at": row.matched_at,
        "revision": row.revision,
    }


class GoodsSalespersonMatchesView(GoodsAPIView):
    @extend_schema(responses=MatchesSerializer)
    def get(self, request: Request) -> Response:
        self.access(request)
        params = check_query(request, allowed=("state",))
        if not user_can(request.user, "setup", CAP_VIEW):
            raise Refusal("ACTION_DENIED", "You do not have access to Setup.")
        stores = list(actionable_stores(request.user))
        rows = SalespersonMatch.objects.select_related("store", "staff__human").filter(
            store__in=stores
        )
        if params.get("state") == "unmatched":
            rows = rows.filter(staff__isnull=True)
        rows = rows.annotate(line_count=Count("lines")).order_by(
            "staff_id", "store__code", "code", "id"
        )
        listed = list(rows)
        waiting = [row for row in listed if row.staff_id is None]
        reasons = rule_outcomes(waiting)
        store_ids = sorted({row.store_id for row in waiting})
        placed = people_placed_at(store_ids)
        now = timezone.now()
        body = {
            "rows": [
                _row_json(
                    row,
                    row.line_count,
                    reasons[row.pk].reason if row.staff_id is None else "",
                )
                for row in listed
            ],
            "unmatched": SalespersonMatch.objects.filter(
                store__in=stores, staff__isnull=True
            ).count(),
            "candidates": [
                {
                    "store_id": store_id,
                    "staff": [
                        {
                            "id": str(person.pk),
                            "staff_code": person.human.staff_code,
                            "display_name": person.human.display_name,
                            "active": person.human.active
                            and (person.retired_at is None or person.retired_at > now),
                        }
                        for person in placed.get(store_id, [])
                    ],
                }
                for store_id in store_ids
            ],
            "can_resolve": may_resolve(request.user)
            or any(may_resolve(request.user, sid) for sid in store_ids),
        }
        return Response(MatchesSerializer(body).data)


class GoodsSalespersonMatchResolveView(GoodsAPIView):
    @extend_schema(request=ResolveRequestSerializer, responses=MatchRowSerializer)
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        in_scope = actionable_stores(request.user)
        row = (
            SalespersonMatch.objects.select_related("store")
            .filter(pk=pk, store__in=in_scope)
            .first()
        )
        if row is None:
            raise Refusal("NOT_FOUND", "That old salesperson row does not exist.", status=404)
        if not may_resolve(request.user, row.store_id):
            raise Refusal("ACTION_DENIED", "Only Admin can resolve an old salesperson row.")
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"staff_id"}, required=["staff_id"])
        staff_id = parse_uuid(body["staff_id"], "staff_id")
        store = row.store

        def handler(run: CommandRun) -> CommandResult:
            locked = SalespersonMatch.objects.select_for_update().get(pk=row.pk)
            check_revision(meta.expected_revision, locked.revision)
            if locked.staff_id is not None:
                raise Refusal(
                    "ALREADY_MATCHED",
                    "This old salesperson row is already matched. A past bill's seller is "
                    "not re-pointed.",
                    status=409,
                )
            if staff_id not in ever_placed_at(store, {staff_id}):
                raise Refusal(
                    "INVALID_REQUEST",
                    f"That person has never been on {store.code}'s staff list. Pick someone "
                    "who worked there, or add them in Setup, People and access first.",
                )
            from accounts.goods_models import Staff

            staff = Staff.objects.select_related("human").get(pk=staff_id)
            run.audit_before = {
                "store": store.code,
                "code": locked.code,
                "name": locked.name,
                "staff_id": None,
                "rule": "",
                "revision": locked.revision,
            }
            locked.staff = staff
            locked.rule = RULE_ADMIN
            locked.matched_at = run.now
            locked.matched_by = request.user  # type: ignore[assignment] # a signed-in login
            locked.revision += 1
            locked.save(update_fields=["staff", "rule", "matched_at", "matched_by", "revision"])
            run.audit_after = {
                "store": store.code,
                "code": locked.code,
                "name": locked.name,
                "staff_id": str(staff.pk),
                "staff_code": staff.human.staff_code,
                "rule": RULE_ADMIN,
                "revision": locked.revision,
            }
            return CommandResult(
                resource_type="salesperson_match",
                resource_id=str(locked.pk),
                revision=locked.revision,
                status_code=200,
            )

        self.run_command(
            request,
            access=access,
            action=RESOLVE_ACTION,
            meta=meta,
            business_input={"id": row.pk, "staff_id": str(staff_id)},
            handler=handler,
            resource_ids=[str(row.pk)],
            subject_key=f"salesperson_match:{row.pk}",
            site_id=store.pk,
        )
        saved = (
            SalespersonMatch.objects.select_related("store", "staff__human")
            .annotate(line_count=Count("lines"))
            .get(pk=row.pk)
        )
        return Response(MatchRowSerializer(_row_json(saved, saved.line_count)).data)
