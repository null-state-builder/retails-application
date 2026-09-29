"""Kinds whose empty brand snapshot deliberately describes a whole-site condition."""

WHOLE_SITE_KINDS = frozenset({
    "document_number", "broken_size", "count_due", "cash_variance",
    "reservation_expiry", "sor_ageing", "no_bill_return_cap", "checklist_missed",
    "stock_ageing", "in_transit_aging",
})
