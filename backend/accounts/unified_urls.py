"""One supported People & Access API namespace."""

from __future__ import annotations

from django.urls import path

from accounts.goods_admin_views import (
    GoodsAdminMetaView,
    GoodsPrivilegedChangeListView,
    GoodsPrivilegedChangeReviewView,
    GoodsRoleDetailView,
    GoodsRoleListCreateView,
    GoodsStaffAssignView,
    GoodsStaffDetailView,
    GoodsStaffListCreateView,
    GoodsStaffRetireView,
    GoodsUserDetailView,
    GoodsUserListCreateView,
    GoodsUserTillPinResetView,
    GoodsUserTillPinSetView,
)
from accounts.unified_admin import RolePolicyView, UserAssignmentsView, WorkflowPolicyView
from accounts.reconciliation_views import AssignmentReconciliationView
from accounts.registration_views import RegistrationSetupView

urlpatterns = [
    path("registration", RegistrationSetupView.as_view(), name="company-registration-setup"),
    path("reconciliation", AssignmentReconciliationView.as_view(), name="access-reconciliation"),
    path("meta", GoodsAdminMetaView.as_view(), name="access-admin-meta"),
    path("staff", GoodsStaffListCreateView.as_view(), name="access-staff-list"),
    path("staff/<uuid:pk>/retire", GoodsStaffRetireView.as_view(), name="access-staff-retire"),
    path("staff/<uuid:pk>/assign", GoodsStaffAssignView.as_view(), name="access-staff-assign"),
    path("staff/<uuid:pk>", GoodsStaffDetailView.as_view(), name="access-staff-detail"),
    path("users", GoodsUserListCreateView.as_view(), name="access-user-list"),
    path("users/<int:pk>/assignments", UserAssignmentsView.as_view(), name="access-user-assignments"),
    path("users/<int:pk>/till-pin/reset", GoodsUserTillPinResetView.as_view(), name="access-user-pin-reset"),
    path("users/<int:pk>/till-pin", GoodsUserTillPinSetView.as_view(), name="access-user-pin"),
    path("users/<int:pk>", GoodsUserDetailView.as_view(), name="access-user-detail"),
    path("roles", GoodsRoleListCreateView.as_view(), name="access-role-list"),
    path("roles/<slug:code>/policy", RolePolicyView.as_view(), name="access-role-policy"),
    path("workflow-policy", WorkflowPolicyView.as_view(), name="access-workflow-policy"),
    path("roles/<int:pk>", GoodsRoleDetailView.as_view(), name="access-role-detail"),
    path("privileged-changes", GoodsPrivilegedChangeListView.as_view(), name="access-review-list"),
    path("privileged-changes/<uuid:pk>/review", GoodsPrivilegedChangeReviewView.as_view(), name="access-review"),
]
