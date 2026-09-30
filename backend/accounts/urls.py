from __future__ import annotations

from django.urls import path

from django.urls import include

from accounts.views import (
    ChangePasswordView,
    CookieRefreshView,
    CsrfView,
    LoginView,
    LogoutView,
    MeView,
    StepUpView,
    TillPinView,
)
from accounts.registration_views import RegistrationConfirmView, RegistrationView

# Session lifecycle and one canonical People & Access administration surface.
urlpatterns = [
    path("registration", RegistrationView.as_view(), name="installation-registration"),
    path(
        "registration/confirm",
        RegistrationConfirmView.as_view(),
        name="installation-registration-confirm",
    ),
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
    path("admin/", include("accounts.unified_urls")),
]
