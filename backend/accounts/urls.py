from __future__ import annotations

from django.urls import path

from accounts.views import (
    AccessMatrixView,
    ActorPolicyDetailView,
    ActorPolicyListView,
    AdminMetaView,
    ApprovalPolicyDetailView,
    ApprovalPolicyListCreateView,
    ChangePasswordView,
    CookieRefreshView,
    CsrfView,
    LoginView,
    LogoutView,
    MeView,
    RoleAccessView,
    RoleDetailView,
    RoleListCreateView,
    StepUpView,
    TillPinView,
    UserDetailView,
    UserListCreateView,
)

# Legacy admin routes only. The goods-v1 admin surface answers at
# `/api/goods-v1/auth/admin/...` (`accounts/goods_urls.py`); these paths no
# longer inspect a request to decide which contract a caller meant (GSA-T01).
urlpatterns = [
    path("csrf", CsrfView.as_view(), name="csrf"),
    path("login", LoginView.as_view(), name="login"),
    path("refresh", CookieRefreshView.as_view(), name="token-refresh"),
    path("logout", LogoutView.as_view(), name="logout"),
    path("step-up", StepUpView.as_view(), name="step-up"),
    path("change-password", ChangePasswordView.as_view(), name="change-password"),
    path("me", MeView.as_view(), name="me"),
    # A manager's own counter PIN (#182). Under `me/` because that is exactly its
    # scope: this endpoint can only ever write the caller's own row.
    path("me/till-pin", TillPinView.as_view(), name="me-till-pin"),
    path("admin/meta", AdminMetaView.as_view(), name="rbac-admin-meta"),
    path("admin/access-matrix", AccessMatrixView.as_view(), name="access-matrix"),
    path("admin/users", UserListCreateView.as_view(), name="rbac-user-list"),
    path("admin/users/<int:pk>", UserDetailView.as_view(), name="rbac-user-detail"),
    path("admin/roles", RoleListCreateView.as_view(), name="rbac-role-list"),
    path("admin/roles/<slug:code>/access", RoleAccessView.as_view(), name="rbac-role-access"),
    path("admin/roles/<int:pk>", RoleDetailView.as_view(), name="rbac-role-detail"),
    path("admin/actor-policies", ActorPolicyListView.as_view(), name="actor-policy-list"),
    path(
        "admin/actor-policies/<path:action>",
        ActorPolicyDetailView.as_view(),
        name="actor-policy-detail",
    ),
    path(
        "admin/approval-policies",
        ApprovalPolicyListCreateView.as_view(),
        name="approval-policy-list",
    ),
    path(
        "admin/approval-policies/<str:kind>",
        ApprovalPolicyDetailView.as_view(),
        name="approval-policy-detail",
    ),
]
