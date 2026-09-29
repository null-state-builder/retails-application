"""Read-only reporting over historical standalone returns.

The writer and API retired when return legs moved into ordinary Sale documents
in #273. This query remains because old Return rows remain part of store history.
"""

from __future__ import annotations

from typing import Any

from core.documents import DocStatus
from masters.models import Store
from sell.models import Return


def returns_by_employee(store: Store, day: Any) -> list[dict[str, Any]]:
    """Historical standalone returns taken by each cashier/manager pair."""
    rows = (
        Return.objects.filter(
            store=store,
            returned_at__date=day,
            docstatus=DocStatus.SUBMITTED,
        )
        .select_related("created_by", "override_by")
        .prefetch_related("lines")
    )
    counts: dict[tuple[str, str], dict[str, Any]] = {}
    for ret in rows:
        key = (
            getattr(ret.created_by, "username", "") or "",
            getattr(ret.override_by, "username", "") or "",
        )
        entry = counts.setdefault(
            key,
            {"taken_by": key[0], "approved_by": key[1], "returns": 0, "value_paise": 0},
        )
        entry["returns"] += 1
        entry["value_paise"] += sum(line.refund_paise for line in ret.lines.all())
    return sorted(counts.values(), key=lambda row: (-row["returns"], row["taken_by"]))
