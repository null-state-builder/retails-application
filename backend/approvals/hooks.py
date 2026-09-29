"""What a document is allowed to do when its own approval clears.

Almost every wired family posts *after* approval, by its maker, and registers
nothing here. Damage flags (#138) cannot: the maker is a store person whom the
ruling bars from moving stock, so if the confirmation did not post, nobody left
in the flow could.

The owning module registers a callback for its own model in ``AppConfig.ready``;
``approvals`` calls it and never learns what it is approving (ADR-0002 — a
module's database is private, and the import-linter contract keeps it that way).
The callback runs inside the decision's transaction, so the movement and the
decision commit together, or roll back together.
"""

from __future__ import annotations

from typing import Any, Protocol


class OnApproved(Protocol):
    """What a registered callback is handed: the document, and who decided."""

    # `subject` is positional-only (the `/`), because `run_on_approved` below
    # passes it positionally and every registered callback names it after its
    # own document - `offer`, `booking`, `mark`, `change`. Without the `/` the
    # protocol promises a *keyword* named `subject` that no implementation has,
    # so each registration failed to type-check and was being silenced with its
    # own `# type: ignore[arg-type]`. The name is ours to choose here, not a
    # promise to callers.
    def __call__(self, subject: Any, /, *, actor: Any) -> None: ...


class OnRejected(Protocol):
    """What a registered rejection callback is handed: the document, who, and why."""

    def __call__(self, subject: Any, /, *, actor: Any, reason: str) -> None: ...


_ON_APPROVED: dict[type, OnApproved] = {}
_ON_REJECTED: dict[type, OnRejected] = {}


def register_on_approved(model: type, callback: OnApproved) -> None:
    """Say that ``model``'s approval is what posts it.

    Registered once at app-ready, so every route to a decision — API, shell,
    management command — goes through the same callback.
    """
    _ON_APPROVED[model] = callback


def run_on_approved(subject: Any, *, actor: Any) -> None:
    """Hand an approved document its own decision, if it asked for it.

    Unregistered types — the ordinary approve-then-post families — pass through
    untouched. A callback that refuses (the flagged piece was sold while it
    waited) raises ``ApprovalError``, which the decide endpoint answers 400 to.
    """
    callback = _ON_APPROVED.get(type(subject))
    if callback is not None:
        callback(subject, actor=actor)


def register_on_rejected(model: type, callback: OnRejected) -> None:
    """Say that ``model`` records its own rejections (store operations ticket 38).

    Most families need nothing: a rejected approval is the whole record. A family
    that keeps its own audit trail of each decision registers here; the callback
    runs inside the decision's transaction, like ``register_on_approved``.
    """
    _ON_REJECTED[model] = callback


def run_on_rejected(subject: Any, *, actor: Any, reason: str) -> None:
    callback = _ON_REJECTED.get(type(subject))
    if callback is not None:
        callback(subject, actor=actor, reason=reason)
