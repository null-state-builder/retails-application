"""The reversing half of the docstatus FSM (#220, ADR-0006 correct-by-reversal).

The kernel has always said a submitted document "cancels only by the kernel's
reversing transition". Until this module, only the *transition* existed: `cancel()`
walked the row to `cancelled` and nothing else moved, so a bill's legs stayed in
the general ledger, its pieces stayed off the shelf, and the credit note it handed
the customer stayed spendable for money the books now said was never given back.

Two things live here, and the split is the whole design.

**`reverse_value_legs`** is the value half, and it is genuinely generic: it reads
back what a document wrote to the general ledger and posts the mirror of it. It
needs to know nothing about what the document *was*, because a mirror of a
balanced voucher is balanced by construction, and the legs carry their own
dimensions.

**Everything else is not generic**, and pretending otherwise is how a cancel goes
wrong quietly. What a cancel owes depends on what the document posted: a bill owes
its stock back and its collections un-taken, a transfer owes a bucket it moved
between two stores, a PT owes a vendor bill it raised. So each document type
**declares** what cancelling it reverses, into the register below, and a type that
has not declared one **refuses to cancel** (`ReversalNotDeclared`) instead of
walking to `cancelled` over postings nobody reversed. Refusing is the conservative
answer on a money path: an un-cancellable document is a shell session that stops,
and a half-reversed one is a set of books that ties while saying something untrue.

Declaring is the whole mechanism, and it is the same shape as `core.floors`::

    # in the app's AppConfig.ready()
    declare_reversal("sell.sale", reverse_sell_document)

A register rather than a `reverse()` override on each model, for one reason that
matters more than the taste of it: the cascade a document needs lives in a service
that reads the models, so a model that named its own cascade would close a real
import cycle, and the deferred import that hides one is exactly what this codebase
refuses to keep as a fix. The register also makes the set readable in one place -
a contract test asserts every concrete document either declares a reversal or is
listed as one nobody has designed yet, so a new money document cannot ship with a
cancel that quietly does nothing, which is how #220 happened.

**Where the reversal legs are filed.** Under the *same* `doc_type`/`doc_number` as
the original, not under a fresh number. A reversal is not an event of its own - it
is that document being undone - and the alternative would need a number from some
series, which the one document that matters most here (the till-numbered sale)
cannot have: its series belongs to the till. Filed this way the Tally join key
still names the bill the money belongs to, the trial balance and every control
account tie, and `memo` says which legs are the undoing.

**Why reversing twice is impossible**, without a marker on the leg: the FSM. The
only caller is `Document.cancel()`, which runs once per document in its life -
`submitted` may only move to `cancelled`, and a cancelled row is frozen by a DB
trigger. So at the moment a reversal is written, no reversal of that document can
already exist.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.gl import GLEntry

#: What a declared reversal is: `(document, actor) -> whatever it wants to report`.
#: The return value travels back out through `cancel()`, so a caller that needs a
#: summary of what was undone gets one without a second query.
ReversalFn = Callable[[Any, Any], Any]

#: `app_label.modelname` (Django's `Meta.label_lower`) → what cancelling it undoes.
REGISTERED_REVERSALS: dict[str, ReversalFn] = {}


class ReversalNotDeclared(Exception):
    """A document class has not said what cancelling it should reverse.

    Raised in place of a silent no-op cancel. See the module docstring for why a
    refusal is the safe default on a money path.
    """


class ReversalRefused(Exception):
    """A cancel that a declared reversal will not perform.

    Not a programmer error - a condition of the data. The one in the system today
    is a credit note that has already been partly spent: the money left the
    counter with a customer, and withdrawing it is a ruling rather than a cascade.
    """


def declare_reversal(model_label: str, reverse: ReversalFn) -> None:
    """Register what cancelling one document type undoes.

    Called from an app's `ready()`, which is where a side effect that has to
    happen exactly once at start-up belongs. Re-declaring the same type is a
    programmer error rather than a silent overwrite: two cascades for one document
    means two answers to "what does this cancel do", and the loser would be
    whichever module imported second.
    """
    if model_label in REGISTERED_REVERSALS:  # pragma: no cover - programmer error
        raise ValueError(f"{model_label}: a reversal is already declared")
    REGISTERED_REVERSALS[model_label] = reverse


def run_declared_reversal(doc: Any, actor: Any = None) -> Any:
    """Undo `doc`'s postings by whatever its type declared, or refuse."""
    label = doc._meta.label_lower
    reverse = REGISTERED_REVERSALS.get(label)
    if reverse is None:
        raise ReversalNotDeclared(
            f"cancelling a {type(doc).__name__} has no reversal declared, so it would "
            f"leave its postings standing (#220); declare one for {label} before "
            "cancelling it"
        )
    return reverse(doc, actor)


def reverse_value_legs(doc: Any, actor: Any = None) -> list[GLEntry]:
    """Mirror every value leg this document posted, negated, in one voucher.

    Same accounts, same dimensions, same party, opposite sign - so the mirror is
    balanced exactly as far as the original was, and no arithmetic is redone. A
    document that posted no value (a credit note, whose liability is raised by the
    bill that issued it) reverses to nothing at all and says so with an empty list.

    Goes through `post_entries` like every other posting, which is what keeps the
    posting floor, the party requirement on a payable, and balanced-or-fail in
    front of a reversal too. That has a consequence worth knowing before using
    this from a shell: reversing a bill that accrued a brand liability restores
    that payable, and the floor will not let it post without a named head-office
    person - so `actor` is not optional there.
    """
    from core.posting import Leg, post_entries  # noqa: PLC0415 — see below

    originals = list(GLEntry.objects.filter(doc_type=doc.doc_type, doc_number=doc.doc_number))
    if not originals:
        return []
    legs = [
        Leg(
            account=leg.account,
            amount=-int(leg.amount or 0),
            store=leg.store,
            gstin=leg.gstin,
            brand=leg.brand,
            season=leg.season,
            party_type=leg.party_type,
            party_code=leg.party_code,
            against_voucher=leg.against_voucher,
            memo=f"Reversal of {doc.doc_number}",
        )
        for leg in originals
    ]
    return post_entries(doc, legs, posted_by=actor)


# The import above is deferred for a real cycle, not for convenience:
# `core.posting` imports `core.documents` (the posting floor asks whether a
# posting's document is a real kernel document before it will honour the
# store-native carve-out), and `core.documents` imports this module to run a
# reversal inside the cancel transition. One of the three edges has to be made
# late, and this is the cheapest: it runs once per document, at cancel.
