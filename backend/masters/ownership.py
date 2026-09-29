"""Whose stock is this - KDPS's own, or a brand's held on SOR/consignment?

One question with one answer, kept in `masters` because that is where the fact
lives: ownership is an axis of the brand's commercial model, not a property of
any one document that happens to move the piece. Every value posting that has to
choose between INVENTORY and the SOR memo pair asks here - the inward, the
transfer, the write-off, the sale - so the four can never drift apart.
"""

from __future__ import annotations

from masters.models import Brand


def brand_is_owned(brand_name: str) -> bool | None:
    """Whose stock is this - KDPS's, or the brand's held on SOR/consignment?

    The answer decides which accounts a value posting may touch: owned stock is
    an on-book asset, brand-owned stock lives behind the SOR memo pair and never
    inside INVENTORY. Getting it wrong is the "model-blind liability" defect the
    30 June review found, so it is asked once, here.

    ``None`` means the masters cannot say, which is not the same as "ours".
    Every caller must treat it as a reason to refuse rather than to assume.
    """
    if not brand_name:
        return None
    # A V-flipped piece is displayed as "V <brand>" and is KDPS-owned by
    # definition - the flip is the ownership change (see ``outbound.post_vflip``),
    # so the original brand's row must not be consulted for it.
    if brand_name.startswith("V "):
        return True
    brand = Brand.objects.filter(name=brand_name).first()
    return None if brand is None else bool(brand.ownership == Brand.Ownership.OWNED)
