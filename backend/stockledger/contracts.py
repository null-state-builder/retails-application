"""The contract fence between legacy stock and goods-v1 sites.

The check lives below routes and workflow services: every append to the legacy
stock ledger reaches it, including direct ORM calls from commands, imports,
signals and repair tools.  Historical reversal services enter the explicit
context manager; ordinary legacy mutations have no flag or setting to send.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from core.refusals import Refusal

_reversal_reason: ContextVar[str | None] = ContextVar("legacy_reversal_reason", default=None)


def _site_ids(sites: int | Any | Iterable[int | Any]) -> set[int]:
    values = (
        sites if isinstance(sites, Iterable) and not isinstance(sites, (str, bytes)) else [sites]
    )
    ids: set[int] = set()
    for value in values:
        site_id = value if isinstance(value, int) else getattr(value, "pk", None)
        if site_id is not None:
            ids.add(int(site_id))
    return ids


def goods_v1_site_ids(sites: int | Any | Iterable[int | Any]) -> set[int]:
    from masters.goods_models import SiteGuard

    ids = _site_ids(sites)
    if not ids:
        return set()
    return set(
        SiteGuard.objects.filter(
            site_id__in=ids, stock_contract=SiteGuard.StockContract.GOODS_V1
        ).values_list("site_id", flat=True)
    )


def require_legacy_stock_writer(sites: int | Any | Iterable[int | Any], *, operation: str) -> None:
    """Refuse a legacy mutation at any goods-v1 site before its first stock row."""
    fenced = goods_v1_site_ids(sites)
    if fenced and _reversal_reason.get() is None:
        raise Refusal(
            "CONTRACT_DISABLED",
            f"{operation} uses the legacy stock contract and is disabled for this goods-v1 site.",
        )


@contextmanager
def authorised_legacy_reversal(reason: str) -> Iterator[None]:
    """Allow only an existing reversal service to mirror historical legacy facts."""
    if not reason.strip():
        raise ValueError("an authorised legacy reversal names its reason")
    token = _reversal_reason.set(reason)
    try:
        yield
    finally:
        _reversal_reason.reset(token)
