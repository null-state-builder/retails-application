"""Which tenant this unit of work belongs to (design §4.2).

One deployment serves exactly one tenant, but every goods-v1 table still carries
``tenant_id`` and forced row-level security. The database reads the tenant from
the ``app.tenant_id`` setting on the connection, so the application must set it
from a *trusted* source - the deployment binding - never from anything a client
sent. This module holds that value for the current context and mirrors it onto
the connection.

HTTP-free: the request middleware that calls ``tenant_context`` lives in
``masters``, which owns the ``Tenant`` table.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import connection

TENANT_SETTING = "app.tenant_id"
ACTOR_SETTING = "app.actor_id"

_current_tenant: ContextVar[uuid.UUID | None] = ContextVar("kdps_tenant", default=None)


class TenantNotBound(RuntimeError):
    """A goods-v1 operation ran with no trusted tenant in context."""


def current_tenant_id() -> uuid.UUID | None:
    return _current_tenant.get()


def require_tenant_id() -> uuid.UUID:
    tenant_id = _current_tenant.get()
    if tenant_id is None:
        raise TenantNotBound("no tenant is bound to this unit of work")
    return tenant_id


def apply_to_connection(tenant_id: uuid.UUID | None, *, local: bool = False) -> None:
    """Mirror ``tenant_id`` onto the database connection's RLS setting."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config(%s, %s, %s)",
            [TENANT_SETTING, str(tenant_id) if tenant_id else "", local],
        )


@contextmanager
def tenant_context(tenant_id: uuid.UUID | None) -> Iterator[None]:
    """Bind ``tenant_id`` for the duration of the block, restoring the previous one."""
    previous = _current_tenant.get()
    token = _current_tenant.set(tenant_id)
    apply_to_connection(tenant_id)
    try:
        yield
    finally:
        _current_tenant.reset(token)
        try:
            apply_to_connection(previous)
        except Exception:  # pragma: no cover - a broken connection is closed by Django
            pass
