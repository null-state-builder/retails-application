"""The two doors into the deferred-costing queue (#186).

A bill that could not be costed on the day is released by something happening
somewhere else - a PT pricing the cohort, or somebody typing a brand's commercial
model into the masters. Neither of those places should have to know that a till
exists, so the arrow points this way round: `sell` listens to `masters`, and
`masters` and `stockledger` are left as leaves under it (ADR-0002).

Signals rather than an explicit call at each site, and that is a deliberate
trade. A call would be easier to follow; it would also have to be added to every
present and future path that prices a cohort or names a supplier, and the one
that got forgotten would leave a bill silently uncosted with a green trial
balance. Listening to the *fact* - a cohort now has a price, a brand now has a
model - cannot be forgotten by a path nobody has written yet.

There is one wait no door here can open, and it is a wait by design: a cohort
whose inwards were booked against **two** vendors. Nothing can say whose piece
just sold, so `supplier_of` refuses rather than picking the later booking, and
the line stays in the queue for a person (the rule predates this module - see
`sell.services.postings.supplier_of`). The Dashboard counts it and the nightly
ageing flags it; naming the vendor is a screen the daily check will carry (#188).

Every receiver is cheap on the answer it almost always gives, which is "nothing
is waiting for this": one indexed count against a table that is empty in a
healthy shop. The legacy PT inward posting fired the first of them once per
valued row; it left with legacy receiving (OPS-18).
"""

from __future__ import annotations

from typing import Any

from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver

from masters.models import Brand, Cohort
from sell.services.costing_sweep import sweep_brand, sweep_deferred
from vendors.models import Vendor


@receiver(post_save, sender=Cohort, dispatch_uid="sell.release_priced_cohort")
def release_priced_cohort(sender: Any, instance: Cohort, **kwargs: Any) -> None:
    """A PT has priced this (barcode, season): pay off whoever sold it early."""
    if int(instance.unit_cost_paise or 0) <= 0:
        return
    sweep_deferred(instance.barcode, instance.season)


@receiver(post_save, sender=Brand, dispatch_uid="sell.release_placed_brand")
def release_placed_brand(sender: Any, instance: Brand, **kwargs: Any) -> None:
    """The masters now know this brand - so they may now know whose stock it was.

    Fired on every brand save rather than only on a *new* brand, because the
    release turns on the brand being placeable now, and a brand flipped from
    KDPS-owned to brand-owned changes the answer for a line that is still waiting
    just as much as a brand created this morning does.
    """
    sweep_brand(instance.name)


@receiver(post_save, sender=Vendor, dispatch_uid="sell.release_woken_supplier")
def release_woken_supplier(sender: Any, instance: Vendor, **kwargs: Any) -> None:
    """A supplier has been edited - so a brand of theirs may now have one.

    `supplier_of` will only name an **active** vendor, so a brand whose sole
    supplier was dormant answers "nobody can say" and its lines wait. Switching
    that vendor back on is the edit that releases them, and it touches no m2m row
    at all - without this receiver the queue would have no door for it and the
    lines would sit there until somebody noticed by hand.
    """
    for name in instance.brands.values_list("name", flat=True):
        sweep_brand(name)


@receiver(m2m_changed, sender=Vendor.brands.through, dispatch_uid="sell.release_named_supplier")
def release_named_supplier(sender: Any, instance: Any, action: str, **kwargs: Any) -> None:
    """A supplier has been linked to a brand - the last thing an SOR line waits for.

    Only additions are acted on. Removing a vendor from a brand cannot release
    anything, and un-posting what was already posted is a reversal, which is a
    document somebody writes and never a side effect of an edit.

    The signal fires from both ends of the relation (`vendor.brands.add(brand)` and
    `brand.vendor_set.add(vendor)`), so which object is `instance` and which is in
    `pk_set` depends on the caller. Both shapes are read here rather than assumed.
    """
    if action != "post_add":
        return
    for name in _brand_names(instance, kwargs.get("pk_set") or set(), kwargs.get("reverse", False)):
        sweep_brand(name)


def _brand_names(instance: Any, pk_set: set[int], reverse: bool) -> list[str]:
    """The brands this m2m change touched, whichever way round it was made."""
    if reverse:  # `brand.vendors.add(...)` - the brand is the instance
        return [instance.name] if isinstance(instance, Brand) else []
    return list(Brand.objects.filter(pk__in=pk_set).values_list("name", flat=True))
