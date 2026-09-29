"""The register of cross-scope reads — every endpoint that deliberately shows a
person rows outside their own scope, and why (#175).

`masters.scoping` is fail-closed on purpose: a store sees its store, a brand
manager sees their brands, and an unrecognised scope sees nothing. That rule is
the permission boundary, so a view that steps outside it is not a coding style
choice — it is a decision about who may see whose business, and it belongs in
writing next to the rule it suspends rather than as an un-gated queryset in a
view module for the next reader to notice.

Declaring one is the whole mechanism::

    SOMETHING = declare_scope_exception(
        "app.read",
        withholds=("cost", "value"),
        reason="...why the boundary cannot simply apply here...",
    )

`withholds` is what keeps the exception *narrow*, and it is the half that does
real work: a cross-scope read is safe only because of what it does not carry, so
the field-name fragments the response may never contain are declared here and
asserted against the live payload by the endpoint's own test. Widening the
exception and widening the payload are then the same conversation.

The reason is mandatory and non-empty by construction, and a contract test reads
``REGISTERED_SCOPE_EXCEPTIONS`` and asserts the whole set — so reaching past the
scope gate is a decision somebody made on purpose, never a diff nobody noticed.
Same pattern, same intent as ``accounts.role_lists`` and ``core.floors``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScopeException:
    """One documented read that reaches past `masters.scoping`, and its limits."""

    #: ``app.read`` — the module and the read, e.g. ``stockledger.cross_store_availability``.
    name: str
    #: Lower-case fragments no field name in this endpoint's response may contain.
    #: The written half of "quantities only, never cost".
    withholds: frozenset[str]
    reason: str

    def leaked_fields(self, payload: Any) -> list[str]:
        """Field names in `payload` this exception promised never to carry.

        Walks the whole response, not the top level: the shape is nested, and a
        cost field added three levels down is exactly the one nobody would spot
        in review. An empty list is the guarantee holding.
        """
        return sorted({key for key in _field_names(payload) if self._withheld(key)})

    def _withheld(self, key: str) -> bool:
        lowered = key.lower()
        return any(fragment in lowered for fragment in self.withholds)


def _field_names(payload: Any) -> set[str]:
    """Every dict key anywhere in a JSON-shaped value."""
    if isinstance(payload, dict):
        names: set[str] = set(payload)
        for value in payload.values():
            names |= _field_names(value)
        return names
    if isinstance(payload, (list, tuple)):
        names = set()
        for item in payload:
            names |= _field_names(item)
        return names
    return set()


#: Declaration order, which is also the order the contract test reports them in.
REGISTERED_SCOPE_EXCEPTIONS: dict[str, ScopeException] = {}


def declare_scope_exception(
    name: str, *, withholds: tuple[str, ...], reason: str
) -> ScopeException:
    """Register a read that reaches past the scope gate, and hand back the entry."""
    if not reason.strip():  # pragma: no cover - programmer error
        raise ValueError(f"{name}: a scope exception needs a reason")
    if not withholds:  # pragma: no cover - programmer error
        raise ValueError(f"{name}: a scope exception must say what it withholds")
    if name in REGISTERED_SCOPE_EXCEPTIONS:  # pragma: no cover - programmer error
        raise ValueError(f"{name}: already declared")
    entry = ScopeException(
        name=name,
        withholds=frozenset(fragment.lower() for fragment in withholds),
        reason=reason,
    )
    REGISTERED_SCOPE_EXCEPTIONS[name] = entry
    return entry


CROSS_STORE_AVAILABILITY = declare_scope_exception(
    "stockledger.cross_store_availability",
    withholds=("cost", "value", "price", "mrp", "paise", "rupees", "margin", "amount"),
    reason=(
        "A customer at the counter asks for a shirt in L and the store does not "
        "have it. Answering 'we don't stock it' when a sister store has three is "
        "the loss this system exists to stop, so the availability search reads "
        "every store's on-hand rather than the caller's own — the D10 ruling of "
        "30 July, and the whole point of the screen. It is safe to widen only "
        "because of what it withholds: quantities, sizes and store codes, never "
        "cost, landed value, MRP or margin. Another store's money stays another "
        "store's business, and this read cannot become a way to see it. Only the "
        "*store* axis is suspended - a brand-scoped caller is still narrowed to "
        "the brands they are entitled to, because the customer-at-the-counter "
        "argument is a store's and says nothing about letting one brand's "
        "representative read another brand's network position. It is read-only "
        "and places no hold on the piece; asking for it is a stock request that "
        "walks its own approval route."
    ),
)
