"""Stable names for serializer choices that have no shared model enum.

Source ``TextChoices`` classes are used directly by the other enum overrides.
Schema generation warns if one of these value sets changes without an update.
"""

BRAND_LAYOUT_KIND = ("sale", "soh")
BRAND_TERMS_KIND = ("terms", "promotion")
BRAND_CLAIM_STATUS = ("raised", "accepted", "settled", "settled_short")
BRAND_TERMS_DECISION_STATUS = ("waiting", "approved", "rejected", "withdrawn")
BROKEN_SIZE_ACTION = ("transfer", "markdown", "other")
APPROVAL_DECISION_ACTION = ("approve", "reject")
PAYABLE_PAYMENT_MODE = ("bank", "cheque", "upi", "cash")
CONNECTED_SWITCH_MODE = ("manual", "connected")
CUSTOMER_ADVANCE_TENDER_MODE = ("cash", "card", "upi")
GIFT_VOUCHER_STATE = ("active", "used_up", "expired")
