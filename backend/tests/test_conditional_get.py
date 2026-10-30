"""ETag / 304 on the back office's hottest reads (caching plan Phase 3).

For every endpoint in ``CONDITIONAL_PATHS``: a 200 carries a strong ETag, no
browser storage (``no-store, private``) and ``Vary`` on the cookie and the
unit/brand context; the same request with ``If-None-Match`` answers 304 with an
empty body; a data change moves the ETag; another person, another store context
or another tenant presenting the first person's ETag gets their own 200; and a
caller the endpoint refuses is still refused, never told "not modified".
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.http import HttpResponse, JsonResponse
from django.test import RequestFactory

from alerts.models import Alert, AlertKind, AlertStatus
from core.conditional_get import CONDITIONAL_PATHS, ConditionalApiGetMiddleware
from core.tenancy import tenant_context
from masters.models import Season
from stockledger.models import StockOnHand
from tests.test_session_bootstrap import _client_for
from tests.test_so03_denials import TenantWorld
from tests.test_so03_denials import worlds as worlds
from vendors.models import Vendor

PATHS = sorted(CONDITIONAL_PATHS)
OWNER = {"all_sites": True, "all_brands": True}


def _ok(client: Any, path: str, **headers: Any) -> Any:
    response = client.get(path, **headers)
    assert response.status_code == 200, (path, response.status_code, response.content[:200])
    return response


def _vary(response: Any) -> set[str]:
    return {part.strip().lower() for part in str(response.get("Vary", "")).split(",")}


# --- Data changes, one per endpoint ----------------------------------------


def _rename_site(world: TenantWorld) -> None:
    site = world.sites[0]
    site.name = f"{site.name} moved"
    site.save()


def _rename_brand(world: TenantWorld) -> None:
    brand = world.brands[0]
    brand.name = f"{brand.name} renamed"
    brand.save(update_fields=["name", "updated_at"])


def _new_season(world: TenantWorld) -> None:
    Season.objects.create(code=f"t{uuid.uuid4().hex[:8]}", name="Test season")


def _restate_gstin(world: TenantWorld) -> None:
    gstin = world.sites[0].gstin
    assert gstin is not None
    gstin.state_name = "Restated"
    gstin.save()


def _rename_entity(world: TenantWorld) -> None:
    gstin = world.sites[0].gstin
    assert gstin is not None
    entity = gstin.legal_entity
    entity.name = f"{entity.name} renamed"
    entity.save()


def _new_vendor(world: TenantWorld) -> None:
    Vendor.objects.create(
        tenant=world.tenant, code=f"v{uuid.uuid4().hex[:8]}", name="New vendor",
        pan="ABCDE1234F", payment_terms="30 days",
    )


def _new_alert(world: TenantWorld) -> None:
    Alert.objects.create(
        kind=AlertKind.COUNT_DUE, kind_label="Count due", title="Count the whole store",
        dedupe_key=uuid.uuid4().hex, store=world.sites[0], brand="", status=AlertStatus.OPEN,
    )


def _new_stock(world: TenantWorld) -> None:
    StockOnHand.objects.create(
        store=world.sites[0], sku_code=uuid.uuid4().hex, brand=world.brands[0].name,
        brand_ref=world.brands[0], net_qty=3, net_value_paise=72000,
    )


#: The approvals inbox is covered by the endpoint-free middleware test below
#: (a pending approval needs a full document and policy to exist).
CHANGES: dict[str, Any] = {
    "/api/masters/stores": _rename_site,
    "/api/masters/locations": _rename_site,
    "/api/masters/brands": _rename_brand,
    "/api/masters/seasons": _new_season,
    "/api/masters/gstins": _restate_gstin,
    "/api/masters/entities": _rename_entity,
    "/api/vendors": _new_vendor,
    "/api/auth/bootstrap": _rename_brand,
    "/api/alerts": _new_alert,
    "/api/stockledger/on-hand": _new_stock,
}


@pytest.mark.parametrize("path", PATHS)
def test_200_carries_an_etag_and_a_repeat_is_304(worlds: Any, settings: Any, path: str) -> None:
    world, _ = worlds
    client = _client_for(world, settings, "owner", **OWNER)
    first = _ok(client, path)
    etag = first["ETag"]
    assert etag.startswith('"') and etag.endswith('"') and not etag.startswith("W/")
    assert first["Cache-Control"] == "no-store, private"
    assert {"cookie", "x-kdps-unit", "x-kdps-brand"} <= _vary(first)
    # A repeat without a validator is the same body and the same ETag.
    assert _ok(client, path)["ETag"] == etag

    again = client.get(path, HTTP_IF_NONE_MATCH=etag)
    assert again.status_code == 304
    assert again.content == b""
    assert again["ETag"] == etag
    assert again["Cache-Control"] == "no-store, private"
    assert {"cookie", "x-kdps-unit", "x-kdps-brand"} <= _vary(again)
    # A stale validator gets the full answer.
    stale = client.get(path, HTTP_IF_NONE_MATCH='"0000"')
    assert stale.status_code == 200 and stale.content == first.content


@pytest.mark.parametrize("path", sorted(CHANGES))
def test_a_data_change_moves_the_etag(worlds: Any, settings: Any, path: str) -> None:
    world, _ = worlds
    client = _client_for(world, settings, "owner", **OWNER)
    before = _ok(client, path)
    with tenant_context(world.tenant.pk):
        CHANGES[path](world)
    after = client.get(path, HTTP_IF_NONE_MATCH=before["ETag"])
    assert after.status_code == 200, path
    assert after.content != before.content
    assert after["ETag"] != before["ETag"]


@pytest.mark.parametrize("path", PATHS)
def test_another_person_store_or_tenant_never_gets_a_304_on_someone_elses_etag(
    worlds: Any, settings: Any, path: str
) -> None:
    world, other = worlds
    owner = _client_for(world, settings, "owner", **OWNER)
    etag = _ok(owner, path)["ETag"]
    others = {
        "store person": (_client_for(world, settings, "store_person", sites=(world.sites[0],), all_brands=True), {}),
        "second owner": (_client_for(world, settings, "owner", **OWNER), {}),
        "other tenant": (_client_for(other, settings, "owner", **OWNER), {}),
        "same owner, one store": (owner, {"HTTP_X_KDPS_UNIT": str(world.sites[0].pk)}),
        "same owner, one brand": (owner, {"HTTP_X_KDPS_BRAND": str(world.brands[0].pk)}),
    }
    for label, (client, headers) in others.items():
        plain = client.get(path, **headers)
        crossed = client.get(path, HTTP_IF_NONE_MATCH=etag, **headers)
        assert crossed.status_code != 304, (label, path)
        assert crossed.status_code == plain.status_code, (label, path)
        # Their own answer, never the first person's.
        assert crossed.content == plain.content, (label, path)
        if crossed.status_code == 200:
            assert crossed["ETag"] != etag, (label, path)
            assert client.get(path, HTTP_IF_NONE_MATCH=crossed["ETag"], **headers).status_code == 304


@pytest.mark.parametrize("path", PATHS)
def test_a_refused_caller_is_still_refused_with_a_valid_etag(worlds: Any, settings: Any, path: str) -> None:
    from rest_framework.test import APIClient

    world, _ = worlds
    owner = _client_for(world, settings, "owner", **OWNER)
    etag = _ok(owner, path)["ETag"]
    signed_out = APIClient().get(path, HTTP_IF_NONE_MATCH=etag)
    assert signed_out.status_code in (401, 403), path
    assert "ETag" not in signed_out
    # A lapsed assignment: whatever the endpoint answers, never a 304 on the
    # owner's validator, and a refusal stays a refusal without an ETag.
    lapsed = _client_for(
        world, settings, "owner", **OWNER, starts=timedelta(days=-3), ends=timedelta(days=-1),
    )
    plain = lapsed.get(path)
    crossed = lapsed.get(path, HTTP_IF_NONE_MATCH=etag)
    assert crossed.status_code == plain.status_code != 304
    if crossed.status_code != 200:
        assert "ETag" not in crossed


# --- The middleware alone (every listed path, including the approvals inbox) --


def _run(path: str, body: Any, *, status: int = 200, **headers: Any) -> HttpResponse:
    request = RequestFactory().get(path, **headers)

    def view(_request: Any) -> HttpResponse:
        return JsonResponse(body, safe=False, status=status)

    response = ConditionalApiGetMiddleware(view)(request)
    assert isinstance(response, HttpResponse)
    return response


@pytest.mark.parametrize("path", PATHS)
def test_middleware_etag_follows_the_body(path: str) -> None:
    first = _run(path, [{"id": 1, "title": "Approve markdown"}])
    second = _run(path, [{"id": 1, "title": "Approve markdown"}, {"id": 2, "title": "New"}])
    assert first.status_code == second.status_code == 200
    assert first["ETag"] != second["ETag"]
    assert _run(path, [{"id": 1, "title": "Approve markdown"}], HTTP_IF_NONE_MATCH=first["ETag"]).status_code == 304
    assert _run(path, [], HTTP_IF_NONE_MATCH=first["ETag"]).status_code == 200


def test_middleware_leaves_other_paths_errors_and_writes_alone() -> None:
    other = _run("/api/approvals", [])
    assert "ETag" not in other
    refused = _run("/api/vendors", {"detail": "no"}, status=403)
    assert "ETag" not in refused and refused.status_code == 403
    request = RequestFactory().post("/api/vendors", data=json.dumps({}), content_type="application/json")
    created = ConditionalApiGetMiddleware(lambda _r: JsonResponse({"id": 1}, status=201))(request)
    assert "ETag" not in created


def test_a_compressing_proxys_encoding_mark_still_matches() -> None:
    first = _run("/api/masters/stores", [{"id": 1}])
    marked = first["ETag"][:-1] + '-gzip"'
    assert _run("/api/masters/stores", [{"id": 1}], HTTP_IF_NONE_MATCH=marked).status_code == 304
    assert _run("/api/masters/stores", [{"id": 2}], HTTP_IF_NONE_MATCH=marked).status_code == 200
