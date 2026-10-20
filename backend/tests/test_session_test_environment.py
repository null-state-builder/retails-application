"""The session says whether this tenant is a test environment (baseline B1).

The screens and every printed document read this flag, so test paper from a
synthetic tenant is never taken for a real tax invoice.
"""

from __future__ import annotations

from typing import Any

import pytest

from accounts.session_payload import session_payload
from core.tenancy import tenant_context
from tests.test_personal_till_pin import session_for
from tests.test_so03_denials import _assign, _person, worlds as worlds


@pytest.mark.parametrize("synthetic", [True, False])
def test_session_reports_test_environment_from_tenant(worlds: Any, settings: Any, synthetic: bool) -> None:
    world, _ = worlds
    world.tenant.synthetic = synthetic
    world.tenant.save(update_fields=["synthetic"])
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "test-mark")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        client, session, _ = session_for(user, settings)
        assert session_payload(user, session)["test_environment"] is synthetic
        result = client.get("/api/auth/me")
        assert result.status_code == 200
        assert result.json()["test_environment"] is synthetic
