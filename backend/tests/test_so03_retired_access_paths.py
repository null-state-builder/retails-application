"""Retired SBU mutation and optional admin cannot bypass scoped assignments."""

from __future__ import annotations

import importlib
import uuid
from typing import Any, cast

import pytest
from django.test import override_settings
from django.urls import Resolver404, resolve


def test_sbu_retirement_direct_url_is_unmounted() -> None:
    path = f"/api/goods-v1/masters/stores/1/sbus/{uuid.uuid4()}/retire"
    with pytest.raises(Resolver404):
        resolve(path)


def test_django_admin_requires_explicit_debug_enablement(monkeypatch: pytest.MonkeyPatch) -> None:
    from config import urls

    monkeypatch.setenv("ENABLE_DJANGO_ADMIN", "1")
    with override_settings(DEBUG=False):
        importlib.reload(urls)
        assert not urls._ENABLE_ADMIN
        assert not any(str(cast(Any, pattern).pattern).startswith("admin/") for pattern in urls.urlpatterns)
    monkeypatch.delenv("ENABLE_DJANGO_ADMIN")
    importlib.reload(urls)


def test_pending_legacy_access_approval_cannot_apply_old_authority() -> None:
    from accounts.access_changes import AccessChangeError, apply_access_change
    from accounts.models import AccessChange
    from approvals.hooks import run_on_approved

    change = AccessChange(resource=AccessChange.Resource.USER, payload={"role_id": 1})
    with pytest.raises(AccessChangeError, match="legacy access change"):
        apply_access_change(change, actor=cast(Any, None))
    with pytest.raises(AccessChangeError, match="legacy access change"):
        run_on_approved(change, actor=cast(Any, None))
