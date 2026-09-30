"""OpenAPI read shapes for counter endpoints with hand-built response bodies.

These serializers describe the actual wire payloads. They do not perform the
business writes; the existing view and service validation remains authoritative.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from sell.serializers import ContinuityFlagRowSerializer, IrnQueueRowSerializer


class SellPolicyReadSerializer(serializers.Serializer[dict[str, Any]]):
    version = serializers.CharField(required=False)
    manual_discount_cap_percent = serializers.CharField()
    manual_discount_on_offer_lines = serializers.BooleanField()


class TillStateReadSerializer(serializers.Serializer[dict[str, Any]]):
    tenant_id = serializers.CharField(required=False)
    site_id = serializers.CharField(required=False)
    device_id = serializers.CharField(required=False)
    selling_mode = serializers.ChoiceField(choices=["historical", "online_alpha"], required=False)
    registered = serializers.BooleanField()
    counter_id = serializers.CharField(allow_blank=True)
    series_prefix = serializers.CharField(allow_blank=True)
    authority_until = serializers.DateTimeField(allow_null=True)
    authority_hours = serializers.IntegerField()
    working_set_version = serializers.IntegerField(allow_null=True)
    server_time = serializers.DateTimeField()
    allocation_version = serializers.IntegerField(allow_null=True)
    paused = serializers.BooleanField()
    pause_fy = serializers.CharField(allow_null=True)
    pause_next_seq = serializers.IntegerField(allow_null=True)
    pause_reason = serializers.CharField(allow_blank=True)
    paused_at = serializers.DateTimeField(allow_null=True)


class TillRegisteredReadSerializer(TillStateReadSerializer):
    device_token = serializers.CharField()


class TillPairWriteSerializer(serializers.Serializer[dict[str, Any]]):
    pairing_code = serializers.RegexField(r"^[a-f0-9]{32,64}$")


class TillPairReadSerializer(serializers.Serializer[dict[str, Any]]):
    paired = serializers.BooleanField()
    device_id = serializers.CharField()


class TillAllocationReleasedReadSerializer(serializers.Serializer[dict[str, Any]]):
    version = serializers.IntegerField()
    released_at = serializers.DateTimeField(allow_null=True)
    reason = serializers.CharField()
    paused = serializers.BooleanField()
    pause_fy = serializers.CharField()
    pause_next_seq = serializers.IntegerField()


class TillResumedReadSerializer(serializers.Serializer[dict[str, Any]]):
    paused = serializers.BooleanField()
    resumed_at = serializers.DateTimeField(allow_null=True)


class RegisterReadSerializer(serializers.Serializer[dict[str, Any]]):
    fy = serializers.CharField()
    last_accepted_seq = serializers.IntegerField()
    holes = serializers.ListField(child=serializers.IntegerField())
    hole_count = serializers.IntegerField()
    series_open = serializers.BooleanField()


class RegisterHandoverReadSerializer(serializers.Serializer[dict[str, Any]]):
    resume_from_seq = serializers.IntegerField()
    unsynced_hint = serializers.ListField(child=serializers.IntegerField())
    hole_count = serializers.IntegerField()


class HeldBillsCountReadSerializer(serializers.Serializer[dict[str, Any]]):
    count = serializers.IntegerField()


class IrnQueueReadSerializer(serializers.Serializer[dict[str, Any]]):
    today = serializers.DateField()
    rows = IrnQueueRowSerializer(many=True)
    truncated = serializers.BooleanField()
    pending_count = serializers.IntegerField()
    overdue_count = serializers.IntegerField()


class StoreFlagsReadSerializer(serializers.Serializer[dict[str, Any]]):
    rows = ContinuityFlagRowSerializer(many=True)
    truncated = serializers.BooleanField()
    open_count = serializers.IntegerField()


class DatasetStoreReadSerializer(serializers.Serializer[dict[str, Any]]):
    tenant_id = serializers.CharField()
    site_id = serializers.CharField()
    code = serializers.CharField()
    gstin = serializers.CharField()
    state_code = serializers.CharField()


class DatasetItemReadSerializer(serializers.Serializer[dict[str, Any]]):
    sku_id = serializers.CharField(required=False)
    brand_id = serializers.IntegerField(allow_null=True, required=False)
    barcode = serializers.CharField()
    season = serializers.CharField()
    design = serializers.CharField()
    brand = serializers.CharField()
    item = serializers.CharField()
    size = serializers.CharField()
    color = serializers.CharField()
    hsn = serializers.CharField()
    mrp_paise = serializers.IntegerField(allow_null=True)
    no_discount = serializers.BooleanField()
    season_unknown_historical = serializers.BooleanField(required=False)


class DatasetStockReadSerializer(serializers.Serializer[dict[str, Any]]):
    barcode = serializers.CharField()
    season = serializers.CharField(required=False)
    qty = serializers.IntegerField()


class DatasetBillLineReadSerializer(serializers.Serializer[dict[str, Any]]):
    line_no = serializers.IntegerField()
    direction = serializers.CharField()
    kind = serializers.CharField()
    barcode = serializers.CharField()
    season = serializers.CharField(allow_blank=True)
    design = serializers.CharField(allow_blank=True)
    color = serializers.CharField(allow_blank=True)
    size = serializers.CharField(allow_blank=True)
    brand = serializers.CharField(allow_blank=True)
    item = serializers.CharField(allow_blank=True)
    hsn = serializers.CharField(allow_blank=True)
    qty = serializers.IntegerField()
    mrp_paise = serializers.IntegerField()
    net_paise = serializers.IntegerField()
    gst_rate = serializers.CharField()
    gst_paise = serializers.IntegerField()
    manual_desc = serializers.CharField(allow_blank=True)
    returned_qty = serializers.IntegerField()
    returned_paise = serializers.IntegerField()
    returned_bank_paise = serializers.IntegerField()


class DatasetBillTenderReadSerializer(serializers.Serializer[dict[str, Any]]):
    mode = serializers.CharField()
    amount_paise = serializers.IntegerField()


class DatasetBillReadSerializer(serializers.Serializer[dict[str, Any]]):
    fy = serializers.CharField()
    till_seq = serializers.IntegerField()
    doc_number = serializers.CharField()
    till_number = serializers.CharField(allow_blank=True)
    billed_at = serializers.DateTimeField()
    customer_name = serializers.CharField(allow_blank=True)
    customer_mobile = serializers.CharField(allow_blank=True)
    buyer_gstin = serializers.CharField(allow_blank=True)
    net_paise = serializers.IntegerField()
    lines = DatasetBillLineReadSerializer(many=True)
    tenders = DatasetBillTenderReadSerializer(many=True)


class DatasetGstSlabReadSerializer(serializers.Serializer[dict[str, Any]]):
    hsn_prefix = serializers.CharField(allow_blank=True)
    threshold_paise = serializers.IntegerField()
    rate_below = serializers.CharField()
    rate_above = serializers.CharField()
    effective_from = serializers.DateField()


class DatasetTaxRuleReadSerializer(serializers.Serializer[dict[str, Any]]):
    kind = serializers.CharField()
    hsn_prefix = serializers.CharField(allow_blank=True)
    name = serializers.CharField()
    threshold_paise = serializers.IntegerField()
    rate_below = serializers.CharField()
    rate_above = serializers.CharField()
    rate = serializers.CharField(required=False)


class DatasetTaxOptionsReadSerializer(serializers.Serializer[dict[str, Any]]):
    round_total_paise = serializers.IntegerField()
    cross_gstin_returns = serializers.BooleanField()
    annual_return_filed = serializers.DictField(
        child=serializers.DictField(child=serializers.CharField())
    )
    gift_with_purchase_is_gift = serializers.BooleanField()


class DatasetTaxVersionReadSerializer(serializers.Serializer[dict[str, Any]]):
    version = serializers.IntegerField()
    applies_from = serializers.DateField()
    saved_at = serializers.DateTimeField()
    rules = DatasetTaxRuleReadSerializer(many=True)
    unmatched_rate = serializers.CharField()
    options = DatasetTaxOptionsReadSerializer()


class DatasetTaxSettingsReadSerializer(serializers.Serializer[dict[str, Any]]):
    rules_on = serializers.BooleanField()
    versions = DatasetTaxVersionReadSerializer(many=True)
    after_discount = serializers.BooleanField()
    hsn_digits = serializers.ListField(child=serializers.IntegerField())
    return_tax = serializers.BooleanField()


class DatasetAlterationChargeReadSerializer(serializers.Serializer[dict[str, Any]]):
    sac = serializers.CharField()
    gst_rate = serializers.CharField()
    description = serializers.CharField()


class DatasetConsentWordingReadSerializer(serializers.Serializer[dict[str, Any]]):
    version = serializers.IntegerField()
    bill = serializers.CharField()
    age = serializers.CharField()
    offers = serializers.CharField()


class DatasetOfferReadSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    name = serializers.CharField()
    layer = serializers.CharField()
    brand = serializers.CharField(allow_null=True)
    trigger_type = serializers.CharField()
    # Rule configurations are versioned object maps, not one fixed shape.
    trigger_config = serializers.DictField(child=serializers.JSONField())
    reward_type = serializers.CharField()
    reward_config = serializers.DictField(child=serializers.JSONField())
    item_scope = serializers.DictField(child=serializers.JSONField())
    starts_on = serializers.DateField()
    ends_on = serializers.DateField(allow_null=True)
    combinable = serializers.BooleanField()
    priority = serializers.IntegerField()


class DatasetSalespersonReadSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.CharField()
    name = serializers.CharField()


class DatasetManagerReadSerializer(serializers.Serializer[dict[str, Any]]):
    user_id = serializers.IntegerField()
    name = serializers.CharField()
    till_pin_hash = serializers.CharField()


class DatasetSeasonReadSerializer(serializers.Serializer[dict[str, Any]]):
    code = serializers.CharField()
    name = serializers.CharField()
    status = serializers.CharField()
    sort_order = serializers.IntegerField()
    historical_unknown = serializers.BooleanField()


class DatasetPolicyReadSerializer(SellPolicyReadSerializer):
    return_window_days = serializers.IntegerField()
    cached_bill_days = serializers.IntegerField()


class DatasetCustomerReadSerializer(serializers.Serializer[dict[str, Any]]):
    mobile = serializers.CharField()
    name = serializers.CharField()
    gstin = serializers.CharField(allow_blank=True)


class DatasetDeletedReadSerializer(serializers.Serializer[dict[str, Any]]):
    items = serializers.ListField(child=serializers.CharField())
    offers = serializers.ListField(child=serializers.IntegerField())


class DatasetReadSerializer(serializers.Serializer[dict[str, Any]]):
    commercial_revision = serializers.CharField()
    selling_mode = serializers.ChoiceField(choices=["historical", "online_alpha"])
    """Current full/delta till dataset, including typed nested money-free rows."""

    cursor = serializers.DateTimeField()
    full = serializers.BooleanField()
    working_set_version = serializers.IntegerField(allow_null=True)
    store = DatasetStoreReadSerializer()
    till = TillStateReadSerializer()
    items = DatasetItemReadSerializer(many=True)
    stock = DatasetStockReadSerializer(many=True)
    bills = DatasetBillReadSerializer(many=True)
    gst_slabs = DatasetGstSlabReadSerializer(many=True)
    tax_settings = DatasetTaxSettingsReadSerializer()
    online_only_refusals = serializers.BooleanField()
    manager_pin_rules = serializers.BooleanField()
    split_sale = serializers.BooleanField()
    customer_display = serializers.BooleanField()
    customer_consent = serializers.BooleanField()
    customer_reservation = serializers.BooleanField()
    saved_sizes = serializers.BooleanField()
    special_orders = serializers.BooleanField()
    gift_vouchers = serializers.BooleanField()
    alteration_charge = DatasetAlterationChargeReadSerializer(allow_null=True)
    consent_wording = DatasetConsentWordingReadSerializer()
    offers = DatasetOfferReadSerializer(many=True)
    salespeople = DatasetSalespersonReadSerializer(many=True)
    salesmen = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="Always empty on current servers; retained for older counters.",
    )
    managers = DatasetManagerReadSerializer(many=True)
    seasons = DatasetSeasonReadSerializer(many=True)
    policy = DatasetPolicyReadSerializer()
    customers = DatasetCustomerReadSerializer(many=True)
    customers_whole = serializers.BooleanField()
    deleted = DatasetDeletedReadSerializer()
