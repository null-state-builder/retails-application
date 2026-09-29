"""The customer master - one row per mobile, born from a bill (#242).

`masters.Customer` lives one layer below `sell` (ADR-0002: domain modules read
down into `masters`, never the other way), but the rule for what a row should
say only exists where the mobile actually shows up - a bill. So the rule lives
here and is called from two places that never see each other: the accept
pipeline's `on_commit` hook, one bill at a time, and the one-off data migration
that seeds the table from every bill already on the books. Sharing the
function is what keeps "latest non-blank wins" one rule rather than two that
can drift.

Nothing here posts, blocks, or refuses a bill (Rule 5) - the accept hook wraps
this in try/except and the migration is a plain loop over rows it already
trusts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sell.gstin import normalise as normalise_gstin


def normalise_mobile(value: str) -> str:
    """Digits only, then collapsed to the bare 10-digit Indian mobile, so
    '+91 98765-43210', '09876543210' and '9876543210' are one customer."""
    raw = "".join(ch for ch in (value or "") if ch.isdigit())
    if len(raw) == 12 and raw.startswith("91"):
        return raw[2:]
    if len(raw) == 11 and raw.startswith("0"):
        return raw[1:]
    return raw


def upsert_customer(
    customer_model: Any,
    *,
    mobile: str,
    name: str,
    gstin: str,
    made_at: datetime | None = None,
) -> None:
    """Get-or-create by mobile; latest non-blank name wins; gstin fills when
    supplied, upper-cased as the column promises. A blank name or gstin never
    wipes what is already stored - only a non-blank, *different* value
    overwrites (api-contract steps 6c/6d).

    Saves only when something actually changed, since `updated_at` drives the
    till's dataset delta pull - an unconditional save on every bill would push
    the whole customer list down every till forever.

    `mobile` is `varchar(15)`; a mobile whose digits are blank or exceed that is
    skipped rather than raising, so a malformed historic row cannot crash the
    backfill (and, symmetrically, cannot fail a live bill either).

    A bill made (`made_at`) before staff corrected the customer at their request
    (ticket 16, `corrected_at`) changes nothing: an offline till's older name or
    GSTIN must not undo the correction when it syncs.

    Ticket 17: a record closed by a merge changes no more, and a bill made
    before somebody moved away from its number never creates a record for that
    number again - the bill is the mover's history (``customer_numbers``).
    """
    mobile = normalise_mobile(mobile)
    if not mobile or len(mobile) > 15:
        return
    name = (name or "").strip()
    gstin = normalise_gstin(gstin or "")

    customer = _existing(customer_model, mobile, name, gstin, made_at)
    if customer is None:
        return
    corrected_at = getattr(customer, "corrected_at", None)
    if made_at is not None and corrected_at is not None and made_at < corrected_at:
        return

    # Only the fields this bill actually changes are written back. Saving both
    # every time would let two tills billing the same mobile at once blank each
    # other's work: the second writer holds a row it read before the first one
    # committed, and an unconditional save would push that stale blank over the
    # gstin the first bill had just supplied.
    changed = []
    if name and name != customer.name:
        customer.name = name
        changed.append("name")
    if gstin and gstin != customer.gstin:
        customer.gstin = gstin
        changed.append("gstin")
    if changed:
        customer.save(update_fields=[*changed, "updated_at"])


def _existing(
    customer_model: Any, mobile: str, name: str, gstin: str, made_at: datetime | None
) -> Any:
    """The open record a bill may update, or None when there is nothing to update:
    the bill created the record just now, the record is closed by a merge, or the
    bill was made before the number was the record's - before somebody moved away
    from it, or before this record moved onto it (ticket 17)."""
    customer = customer_model.objects.filter(mobile=mobile).first()
    if customer is None:
        if made_at is not None:
            from sell.services.customer_numbers import moved_away_after

            if moved_away_after(mobile, made_at):
                return None
        customer, created = customer_model.objects.get_or_create(
            mobile=mobile, defaults={"name": name, "gstin": gstin}
        )
        if created:
            return None
    if getattr(customer, "merged_into_id", None) is not None:
        return None
    if made_at is not None and hasattr(customer, "mobile_since"):
        from sell.services.customer_numbers import not_the_holders

        # A bill made before the number was this customer's is somebody else's.
        if not_the_holders(customer, made_at):
            return None
    return customer


def backfill_customers(sale_model: Any, customer_model: Any) -> None:
    """One row per distinct mobile in `sell_sale`, oldest bill to newest.

    Walking the bills in that order and handing each one to `upsert_customer`
    is the whole algorithm: its latest-non-blank-wins rule does the "newest
    bill's name, newest B2B bill's gstin" work for free, and running this
    twice converges to the same rows both times (idempotent, keyed on mobile).

    Streamed rather than read whole: this runs inside a migration, on a book of
    bills that only ever grows, and a deploy step must not hold every bill in
    memory to find the handful of mobiles it has never seen.
    """
    rows = (
        sale_model.objects.exclude(customer_mobile="")
        .order_by("billed_at", "id")
        .values_list("customer_mobile", "customer_name", "buyer_gstin")
        .iterator(chunk_size=2000)
    )
    for mobile, name, gstin in rows:
        upsert_customer(customer_model, mobile=mobile, name=name, gstin=gstin)
