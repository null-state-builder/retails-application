"""Goods-v1 masters routes (design §6.1), under ``/api/goods-v1/masters/``.

Every masters route the goods contract owns lives here, including the ones that
used to share a path with a legacy reader (stores, locations, brands, seasons,
gstins, entities, summary). Those legacy readers keep answering at
``/api/masters/...`` with their own contract and nothing else (GSA-T01).
"""

from __future__ import annotations

from django.urls import include, path

from masters.goods_audit_log_views import GoodsAuditLogExportView, GoodsAuditLogView
from masters.goods_brand_terms_views import (
    GoodsBrandPromotionProposeView,
    GoodsBrandTermsDecideView,
    GoodsBrandTermsDetailView,
    GoodsBrandTermsProposeView,
    GoodsBrandTermsView,
)
from masters.goods_consent_wording_views import (
    GoodsConsentWordingVersionCreateView,
    GoodsConsentWordingView,
)
from masters.goods_document_series_views import (
    GoodsDocumentPrefixView,
    GoodsDocumentSeriesView,
    GoodsNumberingSettingView,
)
from masters.goods_store_feature_views import (
    GoodsStoreFeatureListView,
    GoodsStoreFeatureProbeView,
    GoodsStoreFeatureSwitchView,
)
from masters.goods_tax_settings_views import (
    GoodsTaxSettingsView,
    GoodsTaxSettingVersionCreateView,
)
from masters.goods_views import (
    GoodsBrandDetailView,
    GoodsBrandListCreateView,
    GoodsBrandRetireView,
    GoodsConfigurationDetailView,
    GoodsConfigurationListCreateView,
    GoodsConfigurationSubmitView,
    GoodsConfigurationWithdrawView,
    GoodsEntityDetailView,
    GoodsEntityListCreateView,
    GoodsEntityRetireView,
    GoodsLocationDirectoryView,
    GoodsRegistrationDetailView,
    GoodsRegistrationListCreateView,
    GoodsRegistrationRetireView,
    GoodsSeasonDetailView,
    GoodsSeasonListCreateView,
    GoodsSeasonRetireView,
    GoodsSiteDetailView,
    GoodsSiteListCreateView,
    GoodsSiteLocationDetailView,
    GoodsSiteLocationListCreateView,
    GoodsSiteLocationRetireView,
    GoodsSiteReadinessView,
    GoodsSiteRetireView,
    GoodsSiteSbuListView,
    GoodsSiteSbuRetireView,
    GoodsSubbrandDetailView,
    GoodsSubbrandListCreateView,
    GoodsSubbrandRetireView,
    GoodsSummaryView,
    GoodsTenantView,
)

urlpatterns = [
    # Setup > Feature Switches (store operations PRD ST-OPS-6).
    path("store-features", GoodsStoreFeatureListView.as_view(), name="goods-store-feature-list"),
    path(
        "store-features/switch",
        GoodsStoreFeatureSwitchView.as_view(),
        name="goods-store-feature-switch",
    ),
    path(
        "store-features/probe",
        GoodsStoreFeatureProbeView.as_view(),
        name="goods-store-feature-probe",
    ),
    # Setup > Audit Log (store operations PRD ST-OPS-3), read-only.
    path("audit-log", GoodsAuditLogView.as_view(), name="goods-audit-log"),
    path(
        "audit-log/export.xlsx",
        GoodsAuditLogExportView.as_view(),
        name="goods-audit-log-export",
    ),
    # Setup > Tax Settings (store operations PRD §6, ticket 03).
    path("tax-settings", GoodsTaxSettingsView.as_view(), name="goods-tax-settings"),
    path(
        "tax-settings/versions",
        GoodsTaxSettingVersionCreateView.as_view(),
        name="goods-tax-setting-version-create",
    ),
    # Setup > Consent Wording (store operations ticket 15, ST-CMP-6).
    path("consent-wording", GoodsConsentWordingView.as_view(), name="goods-consent-wording"),
    path(
        "consent-wording/versions",
        GoodsConsentWordingVersionCreateView.as_view(),
        name="goods-consent-wording-version-create",
    ),
    # Store operations ticket 23 (ST-BRD-1, ST-BRD-6): Brands > Terms.
    path("brand-terms", GoodsBrandTermsView.as_view(), name="goods-brand-terms"),
    path(
        "brand-terms/<int:brand_id>",
        GoodsBrandTermsDetailView.as_view(),
        name="goods-brand-terms-detail",
    ),
    path(
        "brand-terms/versions",
        GoodsBrandTermsProposeView.as_view(),
        name="goods-brand-terms-propose",
    ),
    path(
        "brand-terms/promotion",
        GoodsBrandPromotionProposeView.as_view(),
        name="goods-brand-promotion-propose",
    ),
    path(
        "brand-terms/decisions",
        GoodsBrandTermsDecideView.as_view(),
        name="goods-brand-terms-decide",
    ),
    # Store operations ticket 04: Setup > Document Numbering.
    path("document-series", GoodsDocumentSeriesView.as_view(), name="goods-document-series"),
    path(
        "document-series/prefixes",
        GoodsDocumentPrefixView.as_view(),
        name="goods-document-series-prefix",
    ),
    path(
        "document-series/setting",
        GoodsNumberingSettingView.as_view(),
        name="goods-document-series-setting",
    ),
    # Product masters and identity (E041-E060, E090, E091).
    path("", include("masters.goods_identity_urls")),
    path("tenant", GoodsTenantView.as_view(), name="goods-tenant"),
    path("summary", GoodsSummaryView.as_view(), name="goods-masters-summary"),
    path("locations", GoodsLocationDirectoryView.as_view(), name="goods-location-list"),
    # Literal routes before dynamic ones.
    path("subbrands", GoodsSubbrandListCreateView.as_view(), name="goods-subbrand-list"),
    path(
        "subbrands/<uuid:pk>/retire",
        GoodsSubbrandRetireView.as_view(),
        name="goods-subbrand-retire",
    ),
    path("subbrands/<uuid:pk>", GoodsSubbrandDetailView.as_view(), name="goods-subbrand-detail"),
    path(
        "configurations",
        GoodsConfigurationListCreateView.as_view(),
        name="goods-configuration-list",
    ),
    path(
        "configurations/<uuid:pk>/withdraw",
        GoodsConfigurationWithdrawView.as_view(),
        name="goods-configuration-withdraw",
    ),
    path(
        "configurations/<uuid:pk>/submit",
        GoodsConfigurationSubmitView.as_view(),
        name="goods-configuration-submit",
    ),
    path(
        "configurations/<uuid:pk>",
        GoodsConfigurationDetailView.as_view(),
        name="goods-configuration-detail",
    ),
    path("entities", GoodsEntityListCreateView.as_view(), name="goods-entity-list"),
    path("entities/<int:pk>/retire", GoodsEntityRetireView.as_view(), name="goods-entity-retire"),
    path("entities/<int:pk>", GoodsEntityDetailView.as_view(), name="goods-entity-detail"),
    path("gstins", GoodsRegistrationListCreateView.as_view(), name="goods-registration-list"),
    path(
        "gstins/<int:pk>/retire",
        GoodsRegistrationRetireView.as_view(),
        name="goods-registration-retire",
    ),
    path(
        "gstins/<int:pk>", GoodsRegistrationDetailView.as_view(), name="goods-registration-detail"
    ),
    path("brands", GoodsBrandListCreateView.as_view(), name="goods-brand-list"),
    path("brands/<int:pk>/retire", GoodsBrandRetireView.as_view(), name="goods-brand-retire"),
    path("brands/<int:pk>", GoodsBrandDetailView.as_view(), name="goods-brand-detail"),
    path("seasons", GoodsSeasonListCreateView.as_view(), name="goods-season-list"),
    path("seasons/<int:pk>/retire", GoodsSeasonRetireView.as_view(), name="goods-season-retire"),
    path("seasons/<int:pk>", GoodsSeasonDetailView.as_view(), name="goods-season-detail"),
    path("stores", GoodsSiteListCreateView.as_view(), name="goods-site-list"),
    path("stores/<int:pk>/retire", GoodsSiteRetireView.as_view(), name="goods-site-retire"),
    path(
        "stores/<int:pk>/readiness",
        GoodsSiteReadinessView.as_view(),
        name="goods-site-readiness",
    ),
    path("stores/<int:site_id>/sbus", GoodsSiteSbuListView.as_view(), name="goods-site-sbus"),
    path(
        "stores/<int:site_id>/sbus/<uuid:pk>/retire",
        GoodsSiteSbuRetireView.as_view(),
        name="goods-site-sbu-retire",
    ),
    path(
        "stores/<int:site_id>/locations/<uuid:pk>/retire",
        GoodsSiteLocationRetireView.as_view(),
        name="goods-site-location-retire",
    ),
    path(
        "stores/<int:site_id>/locations/<uuid:pk>",
        GoodsSiteLocationDetailView.as_view(),
        name="goods-site-location-detail",
    ),
    path(
        "stores/<int:site_id>/locations",
        GoodsSiteLocationListCreateView.as_view(),
        name="goods-site-location-list",
    ),
    path("stores/<int:pk>", GoodsSiteDetailView.as_view(), name="goods-site-detail"),
]
