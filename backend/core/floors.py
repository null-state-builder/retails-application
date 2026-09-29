"""The register of posting-floor exceptions — every carve-out from "a store never
writes rupees into the books", and why.

The floor itself (`core.posting._refuse_actor_outside_floor`) is constitution: a
store-scoped actor cannot post value. It sits in the sole GL writer precisely so
no Setup grant, shell call or future endpoint can route around it.

Selling breaks that shape once, and only once. A bill is a store-native money
document whose every rupee is machine-computed — scan, dated GST slab, the
rulebook, the settlement rate — so there is no store *discretion* in it to
protect against. Refusing the till would not make the books safer; it would
mean no store could sell.

So the carve-out is declared here rather than hidden as an `if doc_type == …` in
the engine, and it is declared *narrowly*: a named exception names the document
types it covers and the exact accounts those documents may touch. A leg outside
the allow-list falls straight back to the floor. Declaring one is the whole
mechanism::

    SOMETHING = declare_floor_exception(
        "core.something_floor",
        doc_types=("SAL",),
        accounts=(GLAccount.CASH, …),
        reason="...why the floor cannot simply apply here...",
    )

The reason is mandatory and non-empty by construction, and a contract test reads
``REGISTERED_FLOOR_EXCEPTIONS`` and asserts the whole set — so widening the floor
is a decision somebody has to make on purpose, never a diff nobody noticed.
Same pattern, same intent as ``accounts.role_lists``.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.gl import GLAccount


@dataclass(frozen=True)
class FloorException:
    """One documented carve-out from the store-cannot-post-value floor."""

    #: ``app.name`` — the module and the carve-out, e.g. ``core.store_native_sale_floor``.
    name: str
    #: Document types this exception covers; anything else is untouched by it.
    doc_types: frozenset[str]
    #: The only accounts those documents may post to under it.
    accounts: frozenset[str]
    #: Accounts that, under this exception, must additionally name their party —
    #: a liability leg with no counterparty is not machine-computed, it is a guess.
    party_required_accounts: frozenset[str]
    reason: str

    def covers(self, doc_type: str) -> bool:
        return doc_type in self.doc_types


#: Declaration order, which is also the order the contract test reports them in.
REGISTERED_FLOOR_EXCEPTIONS: dict[str, FloorException] = {}


def declare_floor_exception(
    name: str,
    *,
    doc_types: tuple[str, ...],
    accounts: tuple[str, ...],
    reason: str,
    party_required_accounts: tuple[str, ...] = (),
) -> FloorException:
    """Register a posting-floor carve-out and hand back the entry to check against."""
    if not reason.strip():  # pragma: no cover - programmer error
        raise ValueError(f"{name}: a floor exception needs a reason")
    if not doc_types or not accounts:  # pragma: no cover - programmer error
        raise ValueError(f"{name}: a floor exception must name its doc types and accounts")
    if name in REGISTERED_FLOOR_EXCEPTIONS:  # pragma: no cover - programmer error
        raise ValueError(f"{name}: already declared")
    entry = FloorException(
        name=name,
        doc_types=frozenset(doc_types),
        accounts=frozenset(accounts),
        party_required_accounts=frozenset(party_required_accounts),
        reason=reason,
    )
    REGISTERED_FLOOR_EXCEPTIONS[name] = entry
    return entry


STORE_NATIVE_SALE = declare_floor_exception(
    "core.store_native_sale_floor",
    doc_types=("SAL", "SRT"),
    accounts=(
        GLAccount.CASH,
        GLAccount.CARD_CLEARING,
        GLAccount.UPI_CLEARING,
        GLAccount.CREDIT_NOTE_LIABILITY,
        # Store operations ticket 11: a bank instant discount taken as a
        # payment, machine-computed by the rulebook like every other rupee here.
        GLAccount.BANK_OFFER_RECEIVABLE,
        # Store operations ticket 20: a reservation's advance used as a tender on
        # its pickup bill - the liability the receipt voucher raised, given up.
        GLAccount.CUSTOMER_ADVANCE,
        # Store operations ticket 19: a gift voucher spent as a tender - the
        # liability its sale raised, given up by what the bill used.
        GLAccount.GIFT_VOUCHER_LIABILITY,
        GLAccount.SALES_REVENUE,
        # Store operations ticket 22: a paid alteration's own line, at the
        # charge the counter typed and the fixed rate for SAC 9988.
        GLAccount.ALTERATION_REVENUE,
        GLAccount.OUTPUT_GST,
        GLAccount.COGS,
        GLAccount.INVENTORY,
        GLAccount.SOR_STOCK,
        GLAccount.SOR_CONTRA,
        GLAccount.ROUND_OFF,
        GLAccount.VENDOR_PAYABLE,
    ),
    party_required_accounts=(GLAccount.VENDOR_PAYABLE,),
    reason=(
        "The sale (SAL) and the plain return (SRT) are the two money documents a "
        "store originates by itself, and the counter cannot wait for head office: "
        "the customer is standing there, and the till bills offline. Every rupee on "
        "them is machine-computed — the scan picks the piece, the dated GST slab "
        "back-calculates the tax, the rulebook picks the offer, the vendor's "
        "settlement rate prices the SOR accrual — so the till login exercises no "
        "discretion the floor could be protecting. The allow-list is what keeps that "
        "true: these documents may touch the tender, revenue, tax, cost and stock "
        "accounts of a sale and nothing else, so the exception cannot be borrowed to "
        "post a purchase, a payment or an adjustment from the shop floor. VENDOR_PAYABLE "
        "is in the list only for the SOR/consignment accrual that the sale itself "
        "triggers, and only when the leg names the vendor it is owed to. The PT and "
        "V-flip floor is untouched."
    ),
)


RECEIPT_VOUCHER = declare_floor_exception(
    "core.receipt_voucher_floor",
    doc_types=("RV",),
    accounts=(
        GLAccount.CASH,
        GLAccount.CARD_CLEARING,
        GLAccount.UPI_CLEARING,
        GLAccount.CUSTOMER_ADVANCE,
        GLAccount.FORFEITED_ADVANCE,
    ),
    reason=(
        "Store operations ticket 20 (ST-ORD-1): a customer reservation's advance - and,"
        " ticket 21 (ST-ORD-2), a special order's - is taken at the store, with the "
        "customer standing there, and a receipt voucher (RV) is issued for it. Like the"
        " sale, it is money the store itself receives: the customer's tender in, the "
        "same amount owed back to them as a customer advance. Its refund pays back no "
        "more than the advance still held, the same way it was paid, and its forfeiture"
        " moves no more than that into forfeited advances by the policy frozen on the "
        "reservation or order - never a figure anybody types. The allow-list is the "
        "tender accounts, the advance and the forfeiture income, so the exception "
        "cannot be borrowed to post a sale, a purchase, a payment or an adjustment."
    ),
)


GIFT_VOUCHER = declare_floor_exception(
    "core.gift_voucher_floor",
    doc_types=("GV",),
    accounts=(
        GLAccount.CASH,
        GLAccount.CARD_CLEARING,
        GLAccount.UPI_CLEARING,
        GLAccount.GIFT_VOUCHER_LIABILITY,
        GLAccount.EXPIRED_GIFT_VOUCHERS,
    ),
    reason=(
        "Store operations ticket 19 (ST-POS-4): a gift voucher is sold at the till, with "
        "the customer standing there, as its own document (GV). Like the sale, it is money "
        "the store itself receives: the customer's tender in, the same amount owed to "
        "whoever holds the voucher. Its write-off on expiry moves no more than the voucher "
        "still holds, on the day after its last day - never a figure anybody types. The "
        "allow-list is the tender accounts, the voucher liability and the expired-voucher "
        "income, so the exception cannot be borrowed to post a sale, a purchase, a payment "
        "or an adjustment."
    ),
)
