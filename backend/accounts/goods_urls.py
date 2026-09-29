"""Goods-v1 admin routes (E061-E084, E214, E237), under ``/api/goods-v1/auth/``.

These answer the goods contract and nothing else. The legacy two-administrator
screens keep their own routes under ``/api/auth/``; the two families no longer
share a path, so neither has to guess which contract a caller wanted (GSA-T01).
Literal routes come before converter routes. Staff and audit events use UUIDs;
logins and roles keep their existing integer IDs.
"""

from __future__ import annotations

from django.urls import path

from accounts.goods_admin_views import (
    GoodsAccessMatrixView,
    GoodsActorPolicyListView,
    GoodsAdminMetaView,
    GoodsApprovalPolicyListView,
    GoodsPrivilegedChangeListView,
    GoodsPrivilegedChangeReviewView,
    GoodsRoleAccessView,
    GoodsRoleDetailView,
    GoodsRoleListCreateView,
    GoodsStaffAssignView,
    GoodsStaffDetailView,
    GoodsStaffListCreateView,
    GoodsStaffRetireView,
    GoodsUserDetailView,
    GoodsUserGrantsView,
    GoodsUserListCreateView,
    GoodsUserTillPinResetView,
    GoodsUserTillPinSetView,
)

# E222-E225 (design §6.1): the per-item actor and approval policy readers are
# owned by the masters slice, which drafts and approves configuration. They are
# wired in here because the route itself belongs to the admin surface.
from masters.goods_views import GoodsActorPolicyDetailView, GoodsApprovalPolicyDetailView

urlpatterns = [
    path("admin/meta", GoodsAdminMetaView.as_view(), name="goods-admin-meta"),
    # The access matrix (#173). The api-contract sketched these at
    # `/api/accounts/...`, a prefix this project does not mount - accounts has
    # always lived under `auth/`, and its admin surface under `auth/admin/`.
    path("admin/access-matrix", GoodsAccessMatrixView.as_view(), name="goods-access-matrix"),
    path("admin/staff", GoodsStaffListCreateView.as_view(), name="goods-staff-list"),
    path("admin/staff/<uuid:pk>/retire", GoodsStaffRetireView.as_view(), name="goods-staff-retire"),
    path("admin/staff/<uuid:pk>/assign", GoodsStaffAssignView.as_view(), name="goods-staff-assign"),
    path("admin/staff/<uuid:pk>", GoodsStaffDetailView.as_view(), name="goods-staff-detail"),
    path("admin/users", GoodsUserListCreateView.as_view(), name="goods-user-list"),
    path("admin/users/<int:pk>/grants", GoodsUserGrantsView.as_view(), name="goods-user-grants"),
    path(
        "admin/users/<int:pk>/till-pin/reset",
        GoodsUserTillPinResetView.as_view(),
        name="goods-user-till-pin-reset",
    ),
    path(
        "admin/users/<int:pk>/till-pin",
        GoodsUserTillPinSetView.as_view(),
        name="goods-user-till-pin-set",
    ),
    path("admin/users/<int:pk>", GoodsUserDetailView.as_view(), name="goods-user-detail"),
    path("admin/roles", GoodsRoleListCreateView.as_view(), name="goods-role-list"),
    # Keyed by role *code*, as the contract says: the grid knows codes, and a
    # code survives a reseed where a primary key does not. Declared above the
    # `<int:pk>` detail route only for readability; the converters keep them apart.
    path("admin/roles/<slug:code>/access", GoodsRoleAccessView.as_view(), name="goods-role-access"),
    path("admin/roles/<int:pk>", GoodsRoleDetailView.as_view(), name="goods-role-detail"),
    path(
        "admin/actor-policies", GoodsActorPolicyListView.as_view(), name="goods-actor-policy-list"
    ),
    path(
        "admin/actor-policies/<path:action>",
        GoodsActorPolicyDetailView.as_view(),
        name="goods-actor-policy-detail",
    ),
    path(
        "admin/approval-policies",
        GoodsApprovalPolicyListView.as_view(),
        name="goods-approval-policy-list",
    ),
    path(
        "admin/approval-policies/<str:kind>",
        GoodsApprovalPolicyDetailView.as_view(),
        name="goods-approval-policy-detail",
    ),
    path(
        "admin/privileged-changes",
        GoodsPrivilegedChangeListView.as_view(),
        name="goods-privileged-change-list",
    ),
    path(
        "admin/privileged-changes/<uuid:pk>/review",
        GoodsPrivilegedChangeReviewView.as_view(),
        name="goods-privileged-change-review",
    ),
]
