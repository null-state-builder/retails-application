"""Read scope for outbound documents (issue #141) — one predicate per type.

Writing an outbound document has always been gated (`enforce_store_scope`);
reading one was not, so the Deoghar manager's Transfers screen listed a move
between two other stores and any voucher id opened. The gates live here, above
the views, for one reason: the top-bar search registry (`search.views`) narrows
exactly the same documents, and a predicate written twice is a predicate that
drifts. The `#101` rewrite found the booking predicate hand-copied three times.

Nothing here decides *policy* — that is `masters.scoping`, which resolves the
caller's stores, honours the top-bar switcher and fails a brand-scoped caller
closed. This module only says **where a document's store lives** when the answer
is not the plain `store_id` field.

The store-owned documents (return to brand, adjustment, write-off, V-flip, mark
damaged) need nothing here at all: they carry `store_id`, so their views call
`masters.scoping.scope_by_store` directly.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Model, Q, QuerySet

from masters.scoping import scope_by_store_predicate


def transfer_at_stores(store_ids: list[int]) -> Q:
    """A transfer belongs to **both ends of the move**.

    The sender is answerable for the pieces and the receiver is expecting them,
    so either end may read it — matching the top-bar search, and the reason this
    document cannot go through the plain `store_id` gate.
    """
    return Q(source_store_id__in=store_ids) | Q(destination_store_id__in=store_ids)


def scope_transfers[M: Model](qs: QuerySet[M], user: Any) -> QuerySet[M]:
    """Restrict a transfer queryset to the caller's own end of the move.

    Only the *shape* of the match is ours; the rule is `masters.scoping`'s, so a
    brand-scoped caller fails closed here for the same reason and by the same
    code as everywhere else — a transfer carries no brand, nothing can prove a
    row is theirs, and "stores are the wrong question" must never resolve to "so
    show every store" (ADR-0003). #110 replaces that interim with cross-by-brand.
    """
    scoped: QuerySet[M] = scope_by_store_predicate(qs, user, transfer_at_stores)
    return scoped


def stock_request_at_stores(store_ids: list[int]) -> Q:
    """A stock request belongs to **both ends of the ask** — the store that
    raised it and the store answering it — same shape as a transfer, and for
    the same reason (#74)."""
    return Q(requesting_store_id__in=store_ids) | Q(fulfilling_store_id__in=store_ids)


def scope_stock_requests[M: Model](qs: QuerySet[M], user: Any) -> QuerySet[M]:
    """`scope_transfers`'s rule, for a stock request's two ends."""
    scoped: QuerySet[M] = scope_by_store_predicate(qs, user, stock_request_at_stores)
    return scoped
