"""Django settings for the KDPS backend.

Foundation slice (Phase 0): the empty K0 skeleton is grown into the foundation —
auth (custom user + server sessions, the `accounts` app), the masters spine (`masters` app),
DRF + drf-spectacular for the typed API seam (ADR-0001), and CORS for the PWA.
The money/ledger kernel lives untouched in `core`.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import dj_database_url
from corsheaders.defaults import default_headers
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Normal local commands take their database target from the private .env, rather
# than from a stale exported shell variable. SO-02 proof commands deliberately
# reverse that precedence: their launcher supplies a separate disposable target
# and this branch must never read the working development .env.
PROOF_MODE = os.environ.get("KDPS_PROOF_MODE") == "1"
if PROOF_MODE:
    target = urlsplit(os.environ.get("DATABASE_URL", ""))
    if not (
        target.scheme in {"postgres", "postgresql"}
        and target.hostname == "127.0.0.1"
        and target.port == 55433
        and target.path == "/kdps_proof"
        and target.username == "kdps_proof"
        and bool(target.password)
        and os.environ.get("KDPS_TEST_DB_NAME") == "kdps_proof_test"
    ):
        raise RuntimeError(
            "KDPS_PROOF_MODE requires the isolated kdps_proof target on "
            "127.0.0.1:55433 and KDPS_TEST_DB_NAME=kdps_proof_test."
        )
else:
    load_dotenv(BASE_DIR / ".env", override=True)

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "ci-secret-not-for-production")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",")

# A forgeable SECRET_KEY forges every signed cookie and session token this
# process issues, up to and including an Owner login — the one placeholder that
# must never survive into a real deployment. DEBUG is the same signal Render/CI
# already use to tell
# "this is a real deployment" from "this is dev/CI", so the guard rides it
# rather than adding a second flag nobody remembers to set.
if not DEBUG and SECRET_KEY in {
    "ci-secret-not-for-production",
    "kdps-dev-secret-not-for-production",
}:
    raise RuntimeError(
        "DJANGO_SECRET_KEY is still the dev/CI placeholder with DJANGO_DEBUG=0. "
        "Set a real, random DJANGO_SECRET_KEY before running non-DEBUG."
    )

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third-party
    "rest_framework",
    "drf_spectacular",
    "corsheaders",
    # KDPS apps
    "core.apps.CoreConfig",
    "masters.apps.MastersConfig",
    "accounts.apps.AccountsConfig",
    "files.apps.FilesConfig",
    "vendors.apps.VendorsConfig",
    "inbound.apps.InboundConfig",
    "ptmapper.apps.PtmapperConfig",
    "stockledger.apps.StockledgerConfig",
    "finledger.apps.FinledgerConfig",
    "approvals.apps.ApprovalsConfig",
    "outbound.apps.OutboundConfig",
    "sell.apps.SellConfig",
    "alerts.apps.AlertsConfig",
    "offers.apps.OffersConfig",
    # Per-person Google Workspace inbox in the top bar. Reads no other app and
    # is read by none — mail writes no ledger and no document.
    "mail.apps.MailConfig",
    # Composition roots: read across the domain apps, imported by none of them.
    "search.apps.SearchConfig",
    "storefront.apps.StorefrontConfig",
    # Reports (store operations PRD §17): read the reporting copy, imported by none.
    "reporting.apps.ReportingConfig",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves Django's own static (admin, DRF browsable API) when
    # DEBUG=False, so the API can run as a single process behind Render/uvicorn
    # without a separate static server. The React app is a separate static site.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Goods-v1 tenant binding (design §4.2): the deployment's tenant, from the
    # deployment key, mirrored onto the connection for row-level security.
    "masters.tenancy_middleware.TenantMiddleware",
    # The business unit / brand the caller picked in the top-bar switcher (#88).
    # Narrows what the scoping helpers answer; it can never widen it.
    "masters.unit_context.ActiveContextMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

ASGI_APPLICATION = "config.asgi.application"
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=os.environ["DATABASE_URL"],
        # ASGI serves concurrent requests on short-lived worker threads. Do not
        # retain a connection per thread after the request has finished.
        conn_max_age=0,
    )
}
# A throwaway rehearsal database beside this one (`rehearse_salesperson_move`,
# store operations ticket 07). Honoured only for a name starting kdps_rehearsal,
# so it can never point a process at a real database.
if os.environ.get("KDPS_REHEARSAL_DB", "").startswith("kdps_rehearsal"):
    DATABASES["default"]["NAME"] = os.environ["KDPS_REHEARSAL_DB"]
# Parallel checkouts of this repository can run their test suites against one
# Postgres at the same time; each names its own throwaway test database.
if os.environ.get("KDPS_TEST_DB_NAME"):
    DATABASES["default"]["TEST"] = {"NAME": os.environ["KDPS_TEST_DB_NAME"]}

# The kernel's invariants — append-only ledgers, cross-store isolation — are
# meaningless on SQLite. Fail loudly rather than let the foundation tests pass
# vacuously on the wrong engine.
_engine = str(DATABASES["default"].get("ENGINE", ""))
if not _engine.endswith("postgresql"):
    raise RuntimeError(
        f"KDPS requires PostgreSQL, got ENGINE={_engine!r}. "
        "Point DATABASE_URL at a postgres:// URL."
    )

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.Argon2PasswordHasher",
]

REST_FRAMEWORK = {
    # One login for the whole application (goods-v1 design §4.2): an opaque
    # server-side session in an HttpOnly cookie, with a session-bound CSRF token
    # on every write. No bearer tokens.
    "DEFAULT_AUTHENTICATION_CLASSES": ("accounts.authentication.ServerSessionAuthentication",),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "config.api_errors.goods_exception_handler",
}

# The deployment binding: exactly one tenant per deployment (design §4.2). The
# default is the fixed development/CI key; a real deployment must set its own.
_DEV_DEPLOYMENT_KEY = "7c1d6f0e-8a52-4f3b-9d61-2b0f5e4a9c10"
KDPS_DEPLOYMENT_KEY = os.environ.get("KDPS_DEPLOYMENT_KEY", _DEV_DEPLOYMENT_KEY)
if not DEBUG and KDPS_DEPLOYMENT_KEY == _DEV_DEPLOYMENT_KEY:
    raise RuntimeError("KDPS_DEPLOYMENT_KEY is still the development key with DJANGO_DEBUG=0.")

# Write-once evidence store (design §4.4). Local folder adapter until a provider
# is selected (OPEN-09); production readiness is not claimed on this adapter.
KDPS_OFFBOX_ROOT = os.environ.get("KDPS_OFFBOX_ROOT", str(BASE_DIR / ".offbox"))

# Store feature switches (store operations PRD ST-OPS-6, ticket 01): who may
# change one is `accounts.role_lists.STORE_FEATURE_EDITOR_ROLES`.
# Two demo-only features that let the browser suite prove switching end to end
# before any real feature is registered. Never on in a non-DEBUG deployment
# unless deliberately asked for.
KDPS_STORE_FEATURE_DEMO_PROBES = (
    os.environ.get("KDPS_STORE_FEATURE_DEMO_PROBES", "1" if DEBUG else "0") == "1"
)

# Setup > Audit Log (store operations ticket 02). Both are safe-side choices for
# Anand to confirm: an entry held at no store (a role or login change) is shown
# only to a reader whose `audit.view` is tenant-wide; and an export larger than
# this many rows is refused with "narrow the filters" rather than cut short.
KDPS_AUDIT_LOG_SITELESS_TENANT_READERS = (
    os.environ.get("KDPS_AUDIT_LOG_SITELESS_TENANT_READERS", "1") == "1"
)
KDPS_AUDIT_LOG_EXPORT_MAX_ROWS = int(os.environ.get("KDPS_AUDIT_LOG_EXPORT_MAX_ROWS", "5000"))
# Customer data retention (store operations ticket 17, §23; baseline B12): a
# customer with no purchase for this many months has their profile removed each
# night - only where the ticket's switch is on, which its gate keeps off at every
# real store until Anand confirms the proposed 3 years. Bills are never touched.
KDPS_CUSTOMER_RETENTION_MONTHS = int(os.environ.get("KDPS_CUSTOMER_RETENTION_MONTHS", "36"))
# Reports (store operations ticket 10). A refresh re-reads the bills changed since
# the last run began, less this overlap, so a bill whose transaction was still open
# then is not missed. The longest period one report request may cover.
KDPS_REPORT_REFRESH_OVERLAP_MINUTES = int(
    os.environ.get("KDPS_REPORT_REFRESH_OVERLAP_MINUTES", "15")
)
KDPS_REPORT_MAX_DAYS = int(os.environ.get("KDPS_REPORT_MAX_DAYS", "366"))
# Shrinkage report (store operations ticket 44). A write-off counts as shrinkage
# only when its reason code is one of these (compared in capitals). A write-off
# for any other reason (damage, mildew) is a loss, but not shrinkage.
KDPS_SHRINKAGE_WRITEOFF_REASONS = tuple(
    code.strip().upper()
    for code in os.environ.get(
        "KDPS_SHRINKAGE_WRITEOFF_REASONS", "SHRINKAGE,LOST,MISSING,THEFT,STOLEN"
    ).split(",")
    if code.strip()
)
# Exceptions report and no-bill return caps (store operations ticket 48, B10).
# Returns without a bill a month, per customer phone number and per staff member,
# before the bill is flagged and an alert raised (never refused). A bill that
# reaches head office more than this many minutes after the till printed it is a
# late sync.
KDPS_NO_BILL_RETURN_CAP_PER_PHONE = int(os.environ.get("KDPS_NO_BILL_RETURN_CAP_PER_PHONE", "2"))
KDPS_NO_BILL_RETURN_CAP_PER_STAFF = int(os.environ.get("KDPS_NO_BILL_RETURN_CAP_PER_STAFF", "5"))
KDPS_LATE_SYNC_MINUTES = int(os.environ.get("KDPS_LATE_SYNC_MINUTES", "60"))
# Store task checklists (store operations ticket 49): how many days back a missed
# checklist item stays on Today and as an alert, and can still be ticked late.
KDPS_CHECKLIST_MISSED_DAYS = int(os.environ.get("KDPS_CHECKLIST_MISSED_DAYS", "7"))
# Petty cash (store operations ticket 42). A spend above this many paise waits
# for the Owner in the approvals inbox (PRD section 23: Rs 2,000 per spend).
KDPS_PETTY_CASH_APPROVAL_ABOVE_PAISE = int(
    os.environ.get("KDPS_PETTY_CASH_APPROVAL_ABOVE_PAISE", "200000")
)
# The expense heads a petty cash spend is booked under, until the tenant's full
# catalogue is decided (OQ-03). "Other" needs a note saying what it was.
KDPS_PETTY_CASH_HEADS = tuple(
    head.strip()
    for head in os.environ.get(
        "KDPS_PETTY_CASH_HEADS",
        "Tea and refreshments,Cleaning and housekeeping,Stationery and printing,"
        "Local transport and courier,Repairs and maintenance,Electricity and water,"
        "Staff welfare,Other",
    ).split(",")
    if head.strip()
)
# Offer simulation (store operations ticket 30). The most bills one period of a
# simulation reads. A larger period is refused rather than cut short, so an
# estimate never quietly leaves bills out. The engine prices a bill in about
# 0.03 ms, so two full periods stay within a few seconds of one web request;
# 4 weeks of every KDPS store today is about 45,000 bills.
KDPS_OFFER_SIM_MAX_BILLS = int(os.environ.get("KDPS_OFFER_SIM_MAX_BILLS", "60000"))
# Return on each offer (store operations ticket 31). The most sold lines one
# reading goes through (the offer's run and its baseline together). A larger
# one is refused rather than cut short, so a return never quietly leaves bills
# out. Each line is read in Python inside one web request (about the budget of
# the offer simulation's 60,000 bills); a brand offer over a few weeks is a
# small fraction of it.
KDPS_OFFER_RETURN_MAX_LINES = int(os.environ.get("KDPS_OFFER_RETURN_MAX_LINES", "300000"))
# HSN on every item (store operations ticket 12). An HSN is digits only, of one
# of these lengths; anything else ("NA", "-", "6205.20") is "no HSN": PT approval
# refuses it where the switch is on, the item is listed for fixing, and under
# saved tax settings it takes the "no rule" rate and the bill is flagged (B26).
# The till is sent the same lengths. A safe-side choice for Anand to confirm.
KDPS_HSN_DIGITS = tuple(
    int(n) for n in os.environ.get("KDPS_HSN_DIGITS", "4,6,8").split(",") if n.strip()
)

# Three-way match at receiving (store operations ticket 37, ST-REC-1). A line
# matches when counted and invoiced differ by no more pieces than this, and the
# invoiced cost differs from the booked cost by no more than this over the line.
KDPS_THREE_WAY_QTY_TOLERANCE = int(os.environ.get("KDPS_THREE_WAY_QTY_TOLERANCE", "0"))
KDPS_THREE_WAY_COST_TOLERANCE_PAISE = int(
    os.environ.get("KDPS_THREE_WAY_COST_TOLERANCE_PAISE", "100")
)
# Size-balancing suggestions (store operations ticket 34, ST-TRF-1). A store may
# send what it holds beyond this many weeks of its own sales; a suggestion is made
# only when it moves at least this many pieces or this much at MRP (Anand, B7).
KDPS_SIZE_BALANCE_WEEKS = int(os.environ.get("KDPS_SIZE_BALANCE_WEEKS", "8"))
KDPS_SIZE_BALANCE_MIN_PIECES = int(os.environ.get("KDPS_SIZE_BALANCE_MIN_PIECES", "3"))
KDPS_SIZE_BALANCE_MIN_MRP_PAISE = int(os.environ.get("KDPS_SIZE_BALANCE_MIN_MRP_PAISE", "300000"))
# Customer reservation (store operations ticket 20, ST-ORD-1). How many days a
# reservation lasts, and how many while a sale period is running at the store.
# The advance policy is what happens to an advance on expiry or on a customer's
# cancellation: "refund" or "keep" (forfeited). Frozen on each reservation when
# it is made, so a change applies only to new ones. When the expiry alert
# shows is the alert policy's (``reservation_expiry``), like every alert kind.
KDPS_RESERVATION_DAYS = int(os.environ.get("KDPS_RESERVATION_DAYS", "7"))
KDPS_RESERVATION_SALE_PERIOD_DAYS = int(os.environ.get("KDPS_RESERVATION_SALE_PERIOD_DAYS", "3"))
KDPS_RESERVATION_ADVANCE_POLICY = os.environ.get("KDPS_RESERVATION_ADVANCE_POLICY", "refund")
# Gift vouchers (store operations ticket 19, ST-POS-4). How many calendar months a
# voucher can be used for: the last day is the day before the same date that many
# months on. Frozen on each voucher when it is sold.
KDPS_GIFT_VOUCHER_MONTHS = int(os.environ.get("KDPS_GIFT_VOUCHER_MONTHS", "12"))
# SOR ageing (store operations ticket 24, ST-BRD-5). An SOR piece is flagged this
# many calendar months after the brand's dispatch date, a month before the brand
# must invoice it (CGST Act s.31(7): at supply or at 6 months, whichever is earlier).
KDPS_SOR_ALERT_MONTHS = int(os.environ.get("KDPS_SOR_ALERT_MONTHS", "5"))
KDPS_SOR_INVOICE_MONTHS = int(os.environ.get("KDPS_SOR_INVOICE_MONTHS", "6"))

SPECTACULAR_SETTINGS = {
    "TITLE": "KDPS Operating System API",
    "DESCRIPTION": "Deterministic retail ERP for KDPS Lifestyle Pvt Ltd.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # Distinct choice sets share generic field names across apps. Pin semantic
    # names so client generation never receives hash-suffixed enum names.
    "ENUM_NAME_OVERRIDES": {
        "EveryEnum": "outbound.count_schedule_models.CountEvery",
        "ChecklistEveryEnum": "storefront.checklist_models.ChecklistEvery",
        "ContinuityFlagKindEnum": "sell.models.ContinuityFlag.Kind",
        "BrandLayoutKindEnum": "config.openapi_enums.BRAND_LAYOUT_KIND",
        "BrandTermsKindEnum": "config.openapi_enums.BRAND_TERMS_KIND",
        "ContinuityFlagStatusEnum": "sell.models.ContinuityFlag.Status",
        "CountWorkflowStatusEnum": "outbound.models.CountStatus",
        "OfferStatusEnum": "offers.models.Offer.Status",
        "BrandClaimStatusEnum": "config.openapi_enums.BRAND_CLAIM_STATUS",
        "BrandTermsDecisionStatusEnum": "config.openapi_enums.BRAND_TERMS_DECISION_STATUS",
        "BrokenSizeActionEnum": "config.openapi_enums.BROKEN_SIZE_ACTION",
        "ApprovalDecisionActionEnum": "config.openapi_enums.APPROVAL_DECISION_ACTION",
        "PayablePaymentModeEnum": "config.openapi_enums.PAYABLE_PAYMENT_MODE",
        "ConnectedSwitchModeEnum": "config.openapi_enums.CONNECTED_SWITCH_MODE",
        "CustomerAdvanceTenderModeEnum": "config.openapi_enums.CUSTOMER_ADVANCE_TENDER_MODE",
        "StockAdjustmentReasonEnum": "outbound.models.AdjustmentReason",
        "GapClosureReasonEnum": "outbound.models.GapReason",
        "StoreTransferReasonEnum": "outbound.models.TransferReason",
        "GiftVoucherStateEnum": "config.openapi_enums.GIFT_VOUCHER_STATE",
        "StockRequestSourceEnum": "outbound.models.StockRequestSource",
        "OfferTriggerTypeEnum": "offers.models.Offer.Trigger",
    },
}

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOW_CREDENTIALS = True
# Cross-origin dev (PWA on :3000 → API on :8000) must be allowed to send the
# switcher's context headers, or every call silently falls back to network view.
# `x-csrf-token` is the double-submit header every unsafe request carries
# (`lib/api.ts`) — without it here, cross-origin dev cannot log in or write at
# all (discovered while QA'ing ticket 02: the harness's own smoke spec failed
# the same way, on every browser-driven login, not on anything this ticket
# changed).
CORS_ALLOW_HEADERS = (*default_headers, "x-kdps-unit", "x-kdps-brand", "x-csrf-token")
# The broad Emergent-preview + localhost regexes are a credentialed wildcard on a
# *shared* preview domain (any `<x>.emergentagent.com` could ride the cookie), so
# they are gated to DEBUG (preview/dev) only. In production (DEBUG=0) CORS is driven
# purely by the explicit, exact-origin CORS_ALLOWED_ORIGINS allowlist below.
CORS_ALLOWED_ORIGIN_REGEXES = (
    [
        r"^https://.*\.emergentagent\.com$",
        r"^https://.*\.preview\.emergentagent\.com$",
        r"^http://localhost:\d+$",
        r"^http://127\.0\.0\.1:\d+$",
    ]
    if DEBUG
    else []
)
# Exact origins for non-Emergent / production deployments (comma-separated env).
CORS_ALLOWED_ORIGINS = [o for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if o]
CSRF_TRUSTED_ORIGINS = [
    o
    for o in os.environ.get(
        "CSRF_TRUSTED_ORIGINS",
        "https://*.emergentagent.com",
    ).split(",")
    if o
]

# Session cookies are Secure unless explicitly running over plain-http localhost.
# `JWT_COOKIE_SECURE` is still read so an existing local .env keeps working.
KDPS_COOKIE_SECURE = (
    os.environ.get("KDPS_COOKIE_SECURE", os.environ.get("JWT_COOKIE_SECURE", "1")) == "1"
)

# Railway (and Vercel in front of it) end TLS at their proxy and forward plain
# http with X-Forwarded-Proto. Trust that header only in a real deployment, so
# Django knows the request was https (secure cookies, CSRF origin checks).
# Locally there is no proxy, and nothing should be able to fake the header.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# WhiteNoise compressed storage (no manifest, so a missing reference can never
# 500 the admin). Run `manage.py collectstatic` at build time.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "Asia/Kolkata"
LANGUAGE_CODE = "en-us"

# --- Logging -----------------------------------------------------------------
# Django's default config sends `django.request` 500 tracebacks to `mail_admins`
# and nowhere else once DEBUG is off, and ADMINS is empty here - so on the Render
# alpha a real outage printed one bare line, "POST /api/auth/login 500 Internal
# Server Error", with no cause. That is how a suspended Postgres (22 Aug 2026)
# read as an unexplained login failure. Send the traceback to stdout, which is
# what Render collects.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "%(levelname)s %(asctime)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        # `propagate: False` keeps the traceback off the root logger, so it is
        # printed once rather than twice.
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "django.db.backends.base": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
}
