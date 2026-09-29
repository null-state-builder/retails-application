"""The in-page search contract (issue #102) — one parameter, one meaning.

The top bar searches everything (issue #86, the `search` app). This is the other
half: the list a person is already standing on narrows in place. Every list
endpoint that offers it does so the same way — ``?q=<term>`` — so a store person
learns one behaviour and every screen honours it.

The rules are deliberately small, because a search that behaves differently per
screen is worse than no search at all:

* **an empty or blank term is not a search.** The endpoint answers exactly as it
  does without the parameter — no special empty state, no reordering.
* **matching is plain, case-insensitive substring** over a stated list of fields.
  No ranking, no fuzziness: the person is narrowing a list they can already see,
  not asking a question.
* **the filter goes on last.** It narrows a queryset that is *already* scoped by
  the module's own gate (``masters.scoping``), so searching can never widen what
  the caller may see. This module cannot enforce that — it takes whatever
  queryset it is handed — which is why every call site puts it after the scope.

Living in the kernel rather than in one of the domain apps is the point: three
apps use it and none of them may import each other.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet

#: The one query-parameter name. Shared with the global-search endpoint on
#: purpose: `?q=` means "the thing I typed" everywhere in the system.
SEARCH_PARAM = "q"


def search_term(request: Any) -> str:
    """The term the caller typed, trimmed. Blank means "no search"."""
    return str(request.query_params.get(SEARCH_PARAM) or "").strip()


def text_filter[QS: QuerySet[Any]](qs: QS, term: str, fields: tuple[str, ...]) -> QS:
    """Narrow `qs` to rows where any of `fields` contains `term`, ignoring case.

    `fields` may span to-one relations (``vendor__name``). It must not span a
    to-many one: that would multiply rows, and a list screen counting its own
    rows would then report more than it shows.
    """
    if not term:
        return qs
    match = Q()
    for field in fields:
        match |= Q(**{f"{field}__icontains": term})
    return qs.filter(match)
