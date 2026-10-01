"""Pre-issue validation for the online alpha, using the retained sale writer.

Historical upload accepts an already issued document. Online finalisation must
prove its entire commercial, stock, device and access contract before issue.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.utils import timezone

from core.canonical import content_hash
from core.fiscal import financial_year
from core.refusals import Refusal
from masters.goods_config import ConfigTarget, resolve
from masters.goods_models import SiteGuard
from sell.models import RegisteredTill, Sale, TillPause
from sell.services.accept import AcceptError


@dataclass(frozen=True)
class SellingPolicy:
    version: str
    manual_discount_cap_percent: Decimal
    manual_discount_on_offer_lines: bool
    return_window_days: int

    def as_till_policy(self) -> dict[str, Any]:
        return {"version": self.version,
                "manual_discount_cap_percent": str(self.manual_discount_cap_percent),
                "manual_discount_on_offer_lines": self.manual_discount_on_offer_lines,
                "return_window_days": self.return_window_days, "cached_bill_days": 30}


def online_alpha(store: Any) -> bool:
    return SiteGuard.objects.filter(site=store, selling_mode="online_alpha").exists()


def sale_series_ready(store: Any, fy: str | None = None) -> bool:
    """The retained bill-number writer must have a tenant-owned current series."""
    from core.documents import VoucherSeries
    return VoucherSeries.objects.filter(tenant_id=store.tenant_id, store_code=store.code,
                                        fy=fy or financial_year(), doc_type="SAL", scope_version="legacy").exists()


def prepare_online_sale_series(store: Any) -> None:
    """Bind an empty alpha counter's current-year series without adopting history.

    Registration/renewal is already an authenticated counter setup operation.
    Existing counters, series settings and sequence frontiers are never changed.
    A missing historical series needs reviewed reconciliation rather than a new
    counter pretending the shop has never issued a bill.
    """
    from core.documents import VoucherSeries
    from core.gl import GLEntry
    from finledger.models import CashLedgerEntry
    from sell.services.till_authority import TillError
    if not online_alpha(store):
        return
    if store.gstin.tenant_id != store.tenant_id or store.gstin.legal_entity.tenant_id != store.tenant_id:
        raise TillError("SERIES_RECONCILIATION_REQUIRED", "The store's legal ownership must be reconciled before registering a bill series.")
    fy = financial_year()
    series = VoucherSeries.objects.filter(fy=fy, store_code=store.code, doc_type="SAL").first()
    if series is None:
        number_prefix = f"{fy}/{store.code}/SAL/"
        if (Sale.objects.filter(store=store, fy=fy).exists()
                or GLEntry.objects.filter(doc_number__startswith=number_prefix).exists()
                or CashLedgerEntry.objects.filter(doc_number__startswith=number_prefix).exists()):
            raise TillError("SERIES_RECONCILIATION_REQUIRED", "This financial year has bills but no retained numbering series. Arrange reviewed reconciliation before renewing the counter.")
        series, _ = VoucherSeries.objects.get_or_create(fy=fy, store_code=store.code, doc_type="SAL",
            defaults={"tenant_id": store.tenant_id, "legal_entity_id": store.gstin.legal_entity_id,
                      "scope_version": "legacy"})
    if series.tenant_id != store.tenant_id or series.scope_version != "legacy":
        raise TillError("SERIES_RECONCILIATION_REQUIRED", "The retained bill series is unowned or belongs elsewhere. It must be explicitly reconciled before this counter can issue bills.")


def selling_policy(store: Any) -> SellingPolicy:
    version = resolve(store.tenant_id, "sell_policy", ConfigTarget.of(timezone.now(), site_id=store.pk),
                      match={}, code="SELL_POLICY_REQUIRED", path="selling_policy")
    body = version.payload
    try:
        cap = Decimal(str(body["manual_discount_cap_percent"]))
        stacking = body["manual_discount_on_offer_lines"]
        days = int(body["return_window_days"])
        if not cap.is_finite() or not 0 <= cap <= 100 or type(stacking) is not bool or not 0 <= days <= 365:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise Refusal("SELL_POLICY_INVALID", "Approve valid tenant selling policy before issuing bills.") from None
    return SellingPolicy(str(version.pk), cap, stacking, days)


def commercial_revision(payload: dict[str, Any]) -> str:
    # Stock quantity changes do not stale prices. Stable source identities, HSN,
    # approved MRP, SKU veto, dated offers, tax and policy all do.
    return content_hash({key: payload.get(key) for key in
                         ("items", "offers", "tax_settings", "gst_slabs", "policy", "store")})


#: How long a computed commercial revision may answer for an unchanged store.
#: The marks below decide freshness; this only bounds a missed input's lifetime.
REVISION_CACHE_SECONDS = 300
_revisions: dict[tuple[str, int], tuple[str, float, str]] = {}
_revisions_lock = threading.Lock()


def commercial_marks(store: Any) -> str:
    """A cheap fingerprint of every input the commercial revision is built from.

    The revision hashes the item book (aliases, origins and their seasons, SKU
    vetoes, brand/season names), offers, tax settings and slabs, the selling
    policy and the store's own GSTIN. A sale changes none of them, so issuing a
    bill never invalidates the fingerprint, while any approval that does change
    one moves its count, revision total or newest timestamp. The day is part of
    it because offers and tax versions start and stop on dates.
    """
    from django.db.models import Count, Max, Sum

    from masters.goods_identity_models import ProductSku, SkuAlias, Style
    from masters.goods_models import ConfigVersion, MasterVersion
    from masters.models import Brand, GstSlab, Season
    from masters.store_feature_models import StoreFeatureSwitch
    from masters.tax_setting_models import TaxSettingVersion
    from offers.models import Offer
    from ptmapper.goods_models import OpeningSeasonCorrection
    from stockledger.goods_models import Origin

    def mark(queryset: Any) -> list[Any]:
        names = {field.name for field in queryset.model._meta.concrete_fields}
        aggregates: dict[str, Any] = {"n": Count("pk")}
        for name in ("revision", "version"):
            if name in names:
                aggregates[name] = Sum(name)
        for name in ("updated_at", "created_at", "recorded_at", "effective_from", "effective_to"):
            if name in names:
                aggregates[name] = Max(name)
        return sorted((key, str(value)) for key, value in queryset.aggregate(**aggregates).items())

    tenant = store.tenant_id
    return content_hash({
        "day": timezone.localdate().isoformat(),
        "store": [store.pk, store.code, store.gstin_id, store.gstin.gstin, store.gstin.state_code],
        "aliases": mark(SkuAlias.objects.filter(tenant_id=tenant)),
        "skus": mark(ProductSku.objects.filter(tenant_id=tenant)),
        "styles": mark(Style.objects.filter(tenant_id=tenant)),
        "origins": mark(Origin.objects.filter(tenant_id=tenant)),
        "season_corrections": mark(OpeningSeasonCorrection.objects.filter(tenant_id=tenant)),
        "masters": mark(MasterVersion.objects.filter(tenant_id=tenant, kind__in=["brand", "season", "sku", "style", "alias"])),
        "brands": mark(Brand.objects.all()),
        "seasons": mark(Season.objects.all()),
        "offers": mark(Offer.objects.all()),
        "tax": mark(TaxSettingVersion.objects.filter(tenant_id=tenant)),
        "slabs": mark(GstSlab.objects.all()),
        "configs": mark(ConfigVersion.objects.filter(tenant_id=tenant)),
        "switches": mark(StoreFeatureSwitch.objects.filter(site=store)),
    })


def remember_commercial_revision(store: Any, marks: str, revision: str) -> None:
    with _revisions_lock:
        _revisions[(str(store.tenant_id), store.pk)] = (marks, time.monotonic(), revision)


def current_commercial_revision(store: Any) -> str:
    """The revision a freshly built dataset would carry, without rebuilding it per bill.

    Every bill line is still checked against the server's own item, tax, offer
    and policy decision (``check_lines_before_issue``); this answers only the
    whole-dataset "refresh your counter" question.
    """
    from sell.services.dataset import build_dataset

    marks = commercial_marks(store)
    with _revisions_lock:
        hit = _revisions.get((str(store.tenant_id), store.pk))
    if hit and hit[0] == marks and time.monotonic() - hit[1] < REVISION_CACHE_SECONDS:
        return hit[2]
    revision = str(build_dataset(store, "")["commercial_revision"])
    remember_commercial_revision(store, marks, revision)
    return revision


def check_before_issue(data: dict[str, Any], store: Any, access: Any, device_token: str) -> None:
    from masters.goods_services import require_sell_ready
    from sell.services.till_authority import render_till_number

    _lock_issue_site(store)
    require_sell_ready(store)
    if not online_alpha(store):
        raise AcceptError("ONLINE_STORE_REQUIRED", "This endpoint is for the approved online store journey.", 409)
    # Serialize the store's series before checking the frontier and stock. Device
    # replacements lock this same row; a retiring counter cannot issue underneath.
    till = RegisteredTill.objects.select_for_update().filter(store=store, active=True).first()
    if till is None or not device_token or not secrets.compare_digest(till.device_token, device_token):
        raise AcceptError("DEVICE_REQUIRED", "Pair this browser with the registered counter before selling.", 403)
    now = timezone.now()
    if till.authority_until is None or till.authority_until <= now:
        raise AcceptError("TILL_EXPIRED", "Renew this counter online before issuing the bill.", 409)
    if TillPause.objects.filter(till=till, resumed_at__isnull=True).exists():
        raise AcceptError("TILL_PAUSED", "Billing is paused for this store.", 409)
    fy = financial_year(timezone.localdate(now))
    if not sale_series_ready(store, fy):
        raise AcceptError("SERIES_RECONCILIATION_REQUIRED", "This store has no reconciled current-year bill-number series. Register or renew its counter before issue.", 409)
    if data["fy"] != fy or timezone.localdate(data["billed_at"]) != timezone.localdate(now):
        raise AcceptError("BILL_DATE", "A new online bill must use the current store business day and financial year.", 422)
    # The same request is rechecked after the new bill is written; omit that bill
    # from the frontier so the check asks about its predecessor on both passes.
    previous = Sale.objects.filter(store=store, fy=fy).exclude(idempotency_uuid=data["idempotency_uuid"]).order_by("-till_seq").first()
    expected_seq = previous.till_seq + 1 if previous else 1
    if int(data["till_seq"]) != expected_seq:
        raise AcceptError("BILL_NO_STALE", "The counter number changed. Refresh before preparing another sale.", 409)
    if data.get("till_number") != render_till_number(till.series_prefix, int(data["till_seq"])):
        raise AcceptError("TILL_SERIES", "This bill does not use the registered counter series.", 422)
    if data.get("origin") != "online":
        raise AcceptError("ONLINE_ORIGIN", "Online finalisation requires an online submission.", 422)
    from accounts.goods_models import Staff
    staff_ids = {line["salesperson"] for line in data.get("all_lines", data["lines"])
                 if line.get("salesperson")}
    staff_ids.update(share["salesperson"] for line in data.get("all_lines", data["lines"])
                     for share in (line.get("shares") or []) if share.get("salesperson"))
    # Staff retirement/movement uses this same row lock. An attribution cannot
    # change underneath a newly issued online bill.
    list(Staff.objects.select_for_update().filter(
        tenant_id=store.tenant_id, pk__in=staff_ids).order_by("pk"))
    if not data.get("commercial_revision") or data["commercial_revision"] != current_commercial_revision(store):
        raise AcceptError("PRICING_STALE", "Prices, offers or tax settings changed. Refresh and review the bill before taking payment.", 409)
    if access is None:
        raise AcceptError("AUTH_REQUIRED", "A live deciding session is required.", 403)
    if not access.covers_all_actions({"section.sell.operate"}, [(store.pk, None)], {"customer"}, roles={"store_person"}):
        raise AcceptError("FIELD_DENIED", "This counter requires customer access from its scoped assignment.", 403)


def _lock_issue_site(store: Any, *, allow_frozen: bool = False) -> None:
    from sell.services.till_authority import TillError, lock_site_for_till

    try:
        lock_site_for_till(store, allow_frozen=allow_frozen)
    except TillError as error:
        raise AcceptError(error.code, error.message, error.status) from error


def check_lines_before_issue(data: dict[str, Any], store: Any, lines: list[Any], rulebook: Any, access: Any) -> None:
    from sell.services.tax_rulebook import StoreTaxBooks
    from sell.services.after_discount_check import after_discount_on
    from sell.services.salespeople import active_salespeople
    tax = StoreTaxBooks(store).at(data["billed_at"])
    active_staff = None
    if int(data.get("tax_setting_version") or 1) != tax.version:
        raise AcceptError("TAX_STALE", "This bill does not use the effective tax version.", 409)
    if bool(data.get("gst_after_discount")) != after_discount_on(store):
        raise AcceptError("PRICING_STALE", "The pricing calculation changed. Refresh and review the bill.", 409)
    for line in lines:
        if line.is_return:
            if line.original_missing:
                raise AcceptError("ORIGINAL_REQUIRED", "An online exchange requires the recorded original bill.", 422)
            original = line.original
            if original is None or original.brand_ref_id is None:
                raise AcceptError("IDENTITY_REQUIRED", "The original sold item has no trusted brand identity. Arrange reviewed historical reconciliation before returning it.", 422)
            if not access.covers_all_actions({"section.sell.operate"}, [(store.pk, original.brand_ref_id)], {"customer"}):
                raise AcceptError("FIELD_DENIED", "This assignment does not cover the original returned item and its customer data.", 403)
            continue
        if line.is_alteration:
            continue
        piece = line.goods_piece
        if piece is None or piece.brand_id is None:
            raise AcceptError("IDENTITY_REQUIRED", "The item has no trusted canonical identity.", 422)
        if not line.cost.postable:
            raise AcceptError("SOURCE_COSTING_REQUIRED", "The source ownership, cost or supplier has not been reconciled. Resolve the stable source link before issuing this item.", 409)
        access.require_all_actions({"section.sell.operate"}, site_id=store.pk, brand_id=piece.brand_id)
        if not piece.hsn or not piece.hsn.isdigit() or len(piece.hsn) not in (4, 6, 8):
            raise AcceptError("HSN_REQUIRED", "Every sold item requires a reviewed HSN.", 422)
        if piece.mrp_paise is None or piece.mrp_paise <= 0 or int(line.payload["mrp_paise"]) != piece.mrp_paise:
            raise AcceptError("PRICE_STALE", "The item price does not match its approved opening/receipt MRP.", 409)
        if piece.no_discount and int(line.payload["disc_paise"]) > 0:
            raise AcceptError("NO_DISCOUNT", "This item is marked as ineligible for discounts.", 422)
        expected = tax.line_tax(piece.hsn, line.value_paise, line.qty)
        if expected.rule_missing:
            raise AcceptError("TAX_REQUIRED", "No approved tax rule covers this item.", 422)
        if Decimal(line.payload["gst_rate"]) != expected.split.rate or int(line.payload["gst_paise"]) != expected.split.gst_paise:
            raise AcceptError("TAX_MISMATCH", "The bill tax does not match the effective rule.", 422)
        outcome = rulebook.resolution.by_line().get(line.payload["line_no"])
        expected_offer = outcome.discount_paise if outcome else 0
        claimed = int((line.payload.get("offer_evidence") or {}).get("saved_paise") or 0)
        if claimed != expected_offer or (line.payload.get("offer_id") or None) != (outcome.offer_id if outcome else None):
            raise AcceptError("OFFERS_STALE", "The bill offer does not match the effective offer rules.", 409)
        if active_staff is None:
            active_staff = ({row.staff_id for row in active_salespeople(store, data["billed_at"])}
                            & {row.staff_id for row in active_salespeople(store, timezone.now())})
        seller = line.seller
        if (seller is None or seller.staff is None or seller.staff.pk not in active_staff
                or any(share.staff_id not in active_staff for share in line.shares)):
            raise AcceptError("SALESPERSON_STALE", "The salesperson is no longer active at this store. Refresh and review the bill.", 409)


def finalise_submission(data: dict[str, Any], actor: Any, access: Any, device_token: str) -> tuple[Any, dict[str, Any] | None]:
    """Return acceptance or a persisted definitive refusal under one intent lock."""
    from django.db import transaction
    from sell.models import OnlineSaleSubmission
    from sell.services.accept import accept_sale, payload_fingerprint
    from sell.services.dataset import resolve_till_store
    store = resolve_till_store(actor)
    if store.code.casefold() != data["store"].strip().casefold():
        raise AcceptError("SCOPE_DENIED", "A counter bills for its own store only.", 403)
    with transaction.atomic():
        # Freeze commands take the same site lock before the device/document
        # locks. An already issued UUID remains readable while frozen; its
        # existing acceptance branch never issues a new bill or stock effect.
        _lock_issue_site(store, allow_frozen=True)
        submission, _ = OnlineSaleSubmission.objects.get_or_create(
            tenant_id=store.tenant_id, idempotency_uuid=data["idempotency_uuid"], defaults={"store": store, "payload_fingerprint": payload_fingerprint(data)})
        submission = OnlineSaleSubmission.objects.select_for_update().get(pk=submission.pk)
        if submission.store_id != store.pk:
            raise AcceptError("SCOPE_DENIED", "This submission is outside this counter.", 403)
        if submission.payload_fingerprint != payload_fingerprint(data):
            raise AcceptError("IDEMPOTENCY_CONFLICT", "This submission key already binds a different bill. The original is preserved.", 409)
        if submission.status == "rejected":
            return None, {"code": submission.rejection_code, "error": submission.rejection_message,
                          "status": submission.rejection_status, "not_issued": True}
        try:
            with transaction.atomic():
                result = accept_sale(data, actor, strict_online=True, access=access, device_token=device_token)
        except (AcceptError, Refusal) as exc:
            # Keep rejected intent separate from rolled-back business postings.
            # Live-authority failures stay pending, because they can prevent a
            # caller from learning an earlier accepted outcome.
            code = exc.code
            message = getattr(exc, "message", str(exc))
            status = getattr(exc, "status", 422)
            if status in (401, 403):
                raise
            submission.status = "rejected"
            submission.rejection_code = code
            submission.rejection_message = message[:500]
            submission.rejection_status = status
            submission.save(update_fields=["status", "rejection_code", "rejection_message", "rejection_status"])
            return None, {"code": code, "error": message, "status": status, "not_issued": True}
        submission.status = "accepted"
        submission.sale = result.sale
        submission.save(update_fields=["status", "sale"])
        return result, None
