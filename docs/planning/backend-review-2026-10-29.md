# Backend review against both PRDs — 29 October 2026

**Revision reviewed:** `c6b8128` (HEAD, clean backend tree). **Scope:** every app under `backend/`. **Type:** report only; no code was changed.
**Sources:** [main PRD](../prd.md) (166 `R-` requirements), [store PRD](../store-operations-prd.md) (65 `ST-` requirements), [requirement register](store-operations-requirement-register.md), [SO-03 consolidation register](so-03-consolidation-register.md) and [inventory](so-03-consolidation-inventory.json).

Status labels follow the register's separation of facts. **Exists-tested** means code exists *and* a backend test on this revision exercises the behaviour; it is not real-store acceptance. **Exists-unverified** means code exists but no backend test exercises the behaviour. **Partial** means part of the requirement, or a required refusal path, is absent, or two competing implementations remain. **Missing** means no implementation was found. Activation gates (CA sign-off, OQ-xx) apply regardless of code state and are shown where relevant.

## 1. Executive summary

- **The backend is healthy where it has been hardened.** All 586 tests pass on the proof database. Ruff, import contracts, `manage.py check` and migration state are clean. The goods-v1 kernel (durable commands with replay keys, document heads, central ceilinged numbering, forced row-level security, FIFO piece ranges, separate-person approval) is consistently used and well tested in receiving, PT, transfers, counts, online sale and exchange.
- **Three CI gates are red on this revision:** mypy (5 errors, all in tests), the OpenAPI contract (3 ptmapper upload views) and the SO-03 inventory check (one unclassified frontend consumer). The SO-02 baseline therefore does not pass as committed.
- **The main structural problem is duplication, not missing code.** 183 mounted API routes are still `temporary_supported_legacy`, and most C01–C15 families still have two live writers for the same business state (masters, bookings, stock ledger, transfers, counts, RTV, approvals, alerts, customers). New P1 features (open-to-buy, debit notes, petty cash) were built on the *legacy* approval store, which grows the C15 retirement instead of shrinking it.
- **Unregistered duplicates** worth a retirement owner: three document-numbering engines, two money modules plus ~10 local money parsers/formatters (some using `float`), copy-pasted correction-document scaffolding across RTV/write-off/disposal, and the reporting fact-refresh scaffold repeated in ~10 modules.
- **What's left:** of 65 store requirements, 3 are Exists-tested, 36 Exists-unverified, 14 Partial and 12 Missing. Of 166 main-PRD requirements, 41 are Exists-tested, 41 Exists-unverified, 62 Partial and 22 Missing (12 of the Missing are deliberately SO-24). The largest store gaps are HR/attendance/payroll (no backend at all), credit note as payment, e-bill, campaigns, back-in-stock, GSTR-2B and IRN pending-before-issue.

| | Exists-tested | Exists-unverified | Partial | Missing | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| Store PRD `ST-` | 3 | 36 | 14 | 12 | 65 |
| Main PRD `R-` | 41 | 41 | 62 | 22 | 166 |

| Finding severity | Count |
| --- | ---: |
| High | 8 |
| Medium | 15 |
| Low | 10 |

## 2. Verification results (revision `c6b8128`)

| Check | Command | Result |
| --- | --- | --- |
| Django system check | `manage.py check` | Pass |
| Migration state | `manage.py makemigrations --check --dry-run` | Pass, no changes |
| Lint | `ruff check .` | Pass |
| Import contracts | `lint-imports` | Pass, 2 kept / 0 broken |
| Types | `mypy .` (strict) | **Fail, 5 errors**: `tests/test_installation_registration.py:897`, `tests/test_first_store_inventory.py:193,203,204` (`on_hand_projection.reads` not exported), `tests/test_wardrobe_setup.py:223` |
| Tests | `scripts/proof.py run --cwd backend -- .venv/bin/pytest` | **Pass, 586 passed** in 7 min 54 s |
| API contract | `scripts/api-contract.py check` | **Fail, 3 schema errors**: `ptmapper/legacy_sales_views.py:81,124`, `ptmapper/soh_views.py:303` (no serializer) |
| SO-03 inventory | `scripts/check_so03_consolidation.py --check` | **Fail**: unclassified `api_consumer` in `frontend/src/pages/StockSync.tsx` |
| Duplicate scan | pylint `duplicate-code`, ≥10 similar lines, tests and migrations excluded | 42 blocks (see §5.3) |

**Test reach.** Tests import 203 of the 581 maintained backend modules (about 35%). By app, test files importing it: accounts 47, core 52, masters 42, stockledger 32, sell 25, approvals 16, outbound 16, ptmapper 15, files 12, vendors 9, finledger 7, alerts 5, inbound 4, reporting 4, storefront 3, offers 2, search 2, **mail 0, aiagents 0**. Reporting (42 modules) and offers are the thinnest relative to size.

**Surface inventory** (from the SO-03 inventory, 3,124 records): 829 API routes (442 canonical, 183 temporary legacy, 204 unresolved, of which about 200 are Django admin pages that are mounted only when `DEBUG` and `ENABLE_DJANGO_ADMIN=1`), 306 models, 40 management commands, 17 scheduled jobs, 11 middleware. The largest route owners are SO-08 (161), SO-04 (121) and SO-07 (71).

## 3. Findings (ordered by severity)

Each finding was checked against the code at the cited lines. Suggested fixes are not applied.

### High

**H1. Legacy master CRUD is still a live second writer (C01).** `masters/views.py:65-77, 103-118, 124-222` create and update `Store`, `Brand`, `Season` and `Gstin` directly through DRF generics. `masters/urls.py:22-24` describes them as "legacy masters readers only". These writes bypass `MasterVersion`, approval and the governed goods-v1 path (`masters/goods_services.py`), so R-MDM approval and versioning evidence can be skipped. *Fix:* make the legacy routes read-only (refuse POST/PUT) once the remaining consumers call `/api/goods-v1/masters/*`, and record that in the C01 cutover.

**H2. New P1 features write to the legacy approval store (C15).** `vendors/open_to_buy.py:40-41` (ST-BUY-1), `inbound/debit_notes.py` and `inbound/debit_note_views.py` (ST-REC-3), `sell/services/petty_cash.py` (ST-MNY-3), `offers/approval.py`, `accounts/access_changes.py` and `outbound/maker_checker.py` call `approvals.services.request_approval` (the legacy `Approval` model). Receiving, counts, adjustments, RTV, write-off, disposal and SOH reconciliation use `approvals.goods_services.create_request`. Two inboxes and two decision records therefore exist for one approval concept. This also conflicts with the working agreement to put new capabilities on the target. *Fix:* register these consumers against C15/SO-14 and move them to `goods_services` subject handlers before activation.

**H3. Parallel stock, transfer, count and RTV writers remain mounted (C06–C09).** The legacy routes `/api/outbound/transfers/<int>/dispatch` (`outbound/views.py:487-498` → `posting.post_transfer_dispatch`), `/api/outbound/stocktakes/<int>/apply` (`outbound/views.py:2287-2314` → `counting.apply_variance` → `post_adjustment`), legacy adjustments (`outbound/views.py:1773`) and `/api/outbound/rtvs/<int>/submit` (`posting.py:1616`) still write `StockLedgerEntry`/`StockOnHand`. The goods journal is written by `stockledger/goods_engine.py`. A site fence (`stockledger/contracts.py:46-53`) refuses legacy writes at goods-v1 sites, which keeps this safe today. However, on-hand still has two sources, merged in `stockledger/on_hand_projection.py:56-79`, and 58 legacy outbound routes remain. *Fix:* keep the fence covered by tests, and schedule the C07/C08/C09 retirements in SO-08.

**H4. Legacy money writes have no idempotency.** `finledger/views.py:231-248` (vendor bill), `251-282` (vendor payment) and `431-448` (cash movement) allocate a new voucher number and post the subledger and GL on every POST, with no request key. A network retry or double-click double-posts. Goods-v1 writes use `core.commands.execute_command` with `CommandKey` replay, but the legacy money routes do not (R-CTL-007). *Fix:* require an idempotency key, or wrap these in `execute_command`, before SO-12 activation.

**H5. Three central-numbering engines (R-CTL-002, unregistered).**
- Legacy `core.documents.VoucherSeries.allocate` (`core/documents.py:234-257`), used by `finledger/posting.py:106-110`, `vendors/views.py:110-117` and `sell/services/exchange_tax.py:298`.
- goods-v1 `core.numbering.allocate` with published ceilings (`core/numbering.py:109-143`), used by about 18 inventory and booking services.
- `masters.document_series.allocate` with `DocumentSeriesCounter` and `IssuedDocumentNumber` (`masters/document_series.py:288+`), used by sale invoices, debit notes and transfer documents.

Each is gap-free on its own, but the PRD requires one central numbering authority with restore-safe ceilings, and only `core.numbering` has ceilings. *Fix:* add a register row, choose one engine for official documents, and route the others through it.

**H6. Credit note as a tender is not implemented (ST-POS-5, P1).** The `CreditNote` and `CreditNoteRedemption` models exist (`sell/models.py:1193-1206`). However, `_TenderWriteSerializer.mode` has no `credit_note` choice (`sell/serializers.py:148-157`), and `accept.py` never writes a redemption. The till cannot redeem a note, which fails closed (safe), so this is a requirement gap rather than a defect. *Fix:* implement a locked online-only redemption modelled on `gift_vouchers.check_for_bill`, gated by OQ-45.

**H7. Duplicate approval, alert and notification stores are mounted (C15).** Legacy `/api/approvals*` and `/api/alerts*` (`config/urls.py:74-75`) run beside `/api/goods-v1/approvals/*` and `/api/goods-v1/alerts|exceptions/*`. Legacy `Alert` rows are still created by `alerts/checks.py` and by domain code such as `sell/services/cash_count.py:492`. ST-OPS-1 and ST-OPS-2 ask for one inbox. *Fix:* SO-14 cutover, with a single queue model for both.

**H8. Legacy booking writer beside `GoodsBooking` (C04).** `POST /api/bookings` writes `vendors.Booking` inline in the view (`vendors/views.py:356+`), while `POST /api/goods-v1/bookings` uses `vendors/goods_services.create_booking` (`:783`). Open-to-buy only reads goods bookings, so a booking made through the legacy path does not count against the budget (ST-BUY-1). *Fix:* refuse legacy creation wherever a goods-v1 site exists, and complete the C04 migration.

### Medium

| ID | Location | Issue | Suggested fix |
| --- | --- | --- | --- |
| M1 | `config/settings.py:45-46` | `DJANGO_DEBUG` defaults to `1` and `ALLOWED_HOSTS` to `*`. The secret-key and deployment-key guards (`:54-61`, `:198-199`) only fire when DEBUG is 0, so a deployment that omits the variable runs in DEBUG with dev keys. `deploy/compose.yaml:36` sets 0, but other hosts must remember to. | Default to DEBUG off and require hosts explicitly. |
| M2 | `config/settings.py:390-396` | `CSRF_TRUSTED_ORIGINS` defaults to `https://*.emergentagent.com` in every mode, so pages on any subdomain of a third-party host pass `origin_allowed` (`accounts/authentication.py:55-77`). The session-bound double-submit token still applies. | Empty default; set the value per deployment. |
| M3 | `vendors/views.py:89-95`, `finledger/reconciliation.py:112-128`, `offers/price_views.py:349-353` | Money parsed with `float` and Python `round` (round-half-to-even, binary error). This breaks ADR-0004 and differs from `core.money.rupees_to_paise`. `vendors._rupees_to_paise` also maps `0` to `None`. | Use `core.goods_money.paise_from_amount` or `core.money.rupees_to_paise`. |
| M4 | `finledger/posting.py:96-103` | Its own "tolerant" `rupees_to_paise` turns any bad input into `0`. Callers catch `<= 0`, but the error message is misleading, and it duplicates `core.money.rupees_to_paise`. | Delete it and use the core parser with a refusal. |
| M5 | `finledger/views.py:553-575` | Re-uploading the same bank statement creates a new batch and duplicate statement lines, with no hash check. Matching only uses unmatched ledger rows, so nothing is double-matched, but the unmatched queue fills with duplicates (R-FIN-003). | Refuse or link duplicates by content hash. |
| M6 | `vendors/goods_views.py:841-850` | The goods booking list loads every `GoodsBooking`, filters in Python, then paginates in memory. RLS keeps tenancy safe, but cost grows with total bookings. | Push scope, site, brand and number filters into SQL. |
| M7 | `finledger/views.py:158-170` | Vendor ageing loads every vendor ledger entry into Python. | Compute the age buckets in SQL. |
| M8 | `stockledger/views.py:184-190` | The legacy ledger summary aggregates all scoped history with no date window. | Require a window, or read a projection. |
| M9 | `accounts/principal.py:694-726` | `_allows` returns `True` for any demand kind it doesn't recognise. Today only `holds` reaches that line, after `_replay` has checked `_holds`, but a new demand kind would silently pass. | Handle `holds` explicitly and raise on unknown kinds. |
| M10 | `sell/services/customers.py:78-80` | Two concurrent bills for the same mobile can overwrite each other's customer name or GSTIN. No row lock (the code acknowledges this). | Lock the row with `select_for_update`, or upsert through a command. |
| M11 | `offers/resolution.py:10-12` | Docs point to golden vectors at `offers/vectors/*.json`, but there is no such directory. R-PRC-007 needs till and server parity vectors. | Restore the vectors and add a backend parity test. |
| M12 | `ptmapper/goods_mapper.py:77` vs `ptmapper/goods_canonical.py:30` | The PT row cap is 8,000 on the brand-file path but 50,000 on the canonical path, so R-PT-011 behaves differently by intake route. The brand path also re-reads the whole object in the request thread (`goods_brand_intake.py:72-73`). | Use one documented cap, and move parsing to a worker job. |
| M13 | `mail/service.py:334-340` | Gmail send runs synchronously in the request thread. | Queue it. |
| M14 | `masters/master_sheet_views.py:547-555` | A master-sheet workbook of up to 20 MB is parsed in the request thread. | Parse it in a worker after upload. |
| M15 | `sell/services/cash_count.py` | Per the register, the expected-cash formula doesn't yet include advance and recovery flows explicitly (ST-MNY-2), and OQ-08 custody is open. | Extend the formula and add golden tests in SO-12. |

### Low

| ID | Location | Issue |
| --- | --- | --- |
| L1 | `offers/serializers.py:180-181, 245, 254` | `Store`, `Brand` querysets have no explicit tenant filter; isolation relies on forced RLS for inherited masters (`core/schema_guards.py:98-107`). Add `tenant_id=require_tenant_id()` as defence in depth. |
| L2 | `masters/models.py:203` | `Season.code` globally unique (known legacy-season limitation; SO-04). |
| L3 | `accounts/authentication.py:55-58` | Missing `Origin` header is treated as allowed; double-submit token still required. |
| L4 | `accounts/goods_admin_services.py:1663-1710` | `RoleGrant` writer `change_grants` remains though its view is unmounted and runtime authority ignores grants. Delete to prevent a second authority writer returning. |
| L5 | `aiagents/gemini.py:85-116`, `vendors/views.py:237-244` | No timeout on the model call; booking reader returns raw exception text to the client. |
| L6 | `reporting/base.py:401-406` | XLSX export writes text cells unchanged; prefix values starting `= + - @` to prevent formula injection. |
| L7 | `reporting/*_facts.py` (`scheduled_refresh(_tenant_id)`) | Refresh ignores the tenant argument and `ReportRefresh` is keyed by report only; correct under one tenant per deployment, wrong if a database ever holds two. |
| L8 | `config/settings.py` | No `SECURE_HSTS_SECONDS`; rely on proxy or set explicitly. |
| L9 | `sell/models.py:752-754` | `SaleLine` has only the FK index; confirm plans for `sale__in` dataset queries. |
| L10 | Inventory | ~200 Django admin routes carry `unresolved` lifecycle although admin is DEBUG-only and read-only for access models; classify them to reduce noise. |

**Reviewed and not raised:** cookie flags (`accounts/sessions.py:308-314`: HttpOnly, Secure, SameSite=Lax); Django admin gating (`config/urls.py:45-85`) and read-only access admins; money-ledger tenant scoping (`finledger/access.py:78-110`); bill UUID idempotency and gift-voucher locking (`sell/services/accept.py:242-256`, `gift_vouchers.py:439-445`); refund ceiling (`sell/services/refunds.py:43-63`); PT separate approver and idempotent approval; goods count sales freeze (`outbound/goods_counts.py:384-454`); `date.today()` in numbering is IST because Django sets the process time zone.

## 4. Requirement coverage

### 4.1 Store PRD (`ST-`)

Owner, phase and activation gate come from the register. "Gate" means real-store activation is blocked by that decision whatever the code state.

| ID | Status | Code (primary) | Evidence | Owner · phase · gate |
| --- | --- | --- | --- | --- |
| ST-CMP-1 GST after discount | Partial | `sell/after_discount.py`, `sell/services/tax_rulebook.py`, `sell/services/accept.py:1064-1104` | Golden tests cover `ptmapper/goods_calc`, not the bill path | SO-06 · P1 · CA |
| ST-CMP-2 Exchanges/returns | Partial | `sell/services/exchange_tax.py`, `sell/return_tax.py` | `test_first_store_exchange.py`; B2B pending-IRN absent | SO-09 · P1 · CA |
| ST-CMP-3 Rate from HSN | Partial | `masters/hsn.py`, `masters/tax_settings.py`, `sell/services/missing_hsn.py` | Access tests only | SO-06 · P1 · CA |
| ST-CMP-4 IRN | Partial | `sell/services/accept.py` (queue after accept), `IrnQueueView` | Pending-before-issue absent | SO-16 · P2/P3 · CA, AATO |
| ST-CMP-5 Numbering | Exists-tested | `sell/services/invoice_numbers.py`, `masters/document_series.py` | `test_first_store_new.py`, snapshot tests | SO-09 · P1 |
| ST-CMP-6 Consent | Exists-unverified | `sell/services/consent.py` | No consent test; P3 OTP absent | SO-10 · P1/P3 |
| ST-CMP-7 Gift stock ITC | Exists-unverified | `sell/services/gift_stock.py`, `reporting/gift_report.py` | — | SO-06 · P1 · CA |
| ST-POS-1 UPI/card | Partial | `sell/serializers.py:173-196` (manual UPI state) | No provider path | SO-16 · P2/P3 · OQ-24 |
| ST-POS-2 Salesperson | Exists-tested | `sell/services/salespeople.py`, `split_shares.py` | `test_wardrobe_setup.py` | SO-05 · P1 |
| ST-POS-3 E-bill | Missing | — | — | SO-17 · P2/P3 |
| ST-POS-4 Gift vouchers | Exists-unverified | `sell/services/gift_vouchers.py` | — | SO-09 · P1 |
| ST-POS-5 Credit note tender | Missing | Models only (H6) | — | SO-09 · P1 · OQ-45 |
| ST-POS-6 Customer display | Exists-unverified | `sell/views.py:529+` | — | SO-09 · P1 |
| ST-CUS-1 Customer record | Partial | `sell/services/customer_rights.py`, `customers.py` (two writers, C11) | Access tests only | SO-10 · P1 · retention |
| ST-CUS-2 Saved size | Exists-unverified | `sell/services/saved_sizes.py` | — | SO-10 · P1 |
| ST-CUS-3 Loyalty | Missing | — | — | SO-20 · P4 · OQ-47 |
| ST-CUS-4 Campaigns | Missing | — | — | SO-17 · P2/P3 |
| ST-CUS-5 Back-in-stock | Missing | — | — | SO-17 · P2/P3 |
| ST-ORD-1 Reservation | Exists-unverified | `sell/services/reservations.py` | Alert scope only | SO-11 · P1 · advance policy |
| ST-ORD-2 Special orders | Exists-unverified | `sell/services/special_orders.py` | — | SO-11 · P1 · CA |
| ST-ORD-3 Alterations | Exists-unverified | `sell/services/alterations.py` | — | SO-11 · P1 · OQ-49 |
| ST-ORD-4 Home delivery | Missing | — | — | SO-20 · P4 · OQ-49 |
| ST-REC-1 Three-way match | Exists-unverified | `inbound/three_way_match.py` | — | SO-07 · P1 |
| ST-REC-2 GSTR-2B | Missing | — | — | SO-16 · P2/P3 |
| ST-REC-3 Debit note draft | Exists-unverified | `inbound/debit_notes.py` (legacy approvals, H2) | — | SO-07 · P1 · OQ-47 posting |
| ST-INV-1 Broken size | Exists-unverified | `stockledger/broken_size.py` | Scope tests only | SO-08 · P1 |
| ST-INV-2 Season ageing | Exists-unverified | `stockledger/goods_ageing.py` | Season-end gate test only | SO-08 · P1 |
| ST-INV-3 Scheduled counts | Exists-tested | `outbound/count_schedules.py`, `goods_counts.py` freeze | `test_first_store_trading_counts.py` | SO-08 · P1 |
| ST-INV-4 Shrinkage | Exists-unverified | `reporting/shrinkage_report.py`, `shrinkage_facts.py` | — | SO-08 · P1 |
| ST-INV-5 Min/max | Missing | — | — | SO-20 · P4 |
| ST-TRF-1 Size balancing | Exists-unverified | `outbound/size_balancing.py` | — | SO-08 · P1 |
| ST-TRF-2 Transfer documents | Exists-unverified | `outbound/transfer_documents.py` | — | SO-08 · P1 · CA |
| ST-BUY-1 Open-to-buy | Exists-unverified | `vendors/open_to_buy.py` | Access tests only | SO-13 · P1 |
| ST-BUY-2 Size curve | Exists-unverified | `vendors/size_curve.py` | — | SO-13 · P1 |
| ST-MNY-1 Settlement match | Partial | `finledger/reconciliation.py` (bank only) | — | SO-16 · P2/P3 · OQ-24 |
| ST-MNY-2 Cash count | Partial | `sell/services/cash_count.py` (M15) | `test_first_store_online.py` | SO-12 · P1 · OQ-08 |
| ST-MNY-3 Petty cash | Exists-unverified | `sell/services/petty_cash.py` | Access test only | SO-12 · P1 · OQ-03/08 |
| ST-MNY-4 Brand payables | Exists-unverified | `finledger/payables.py` | — | SO-12 · P1 · OQ-26 (SOR) |
| ST-BRD-1 Brand terms | Exists-unverified | `masters/brand_terms.py` | — | SO-06 · P1 · OQ-26 |
| ST-BRD-2 Brand reports | Exists-unverified | `reporting/brand_reports.py` | — | SO-13 · P1 |
| ST-BRD-3 Discount claims | Exists-unverified | `sell/services/brand_claims.py` | — | SO-13 · P1 |
| ST-BRD-4 Margin share | Exists-unverified | `reporting/margin_share.py` | — | SO-13 · P1 · OQ-50 |
| ST-BRD-5 SOR ageing | Exists-unverified | `stockledger/sor_ageing.py` | — | SO-13 · P1 |
| ST-BRD-6 Promotion services | Exists-unverified | `masters/brand_terms.py` | — | SO-06 · P1 · CA |
| ST-OFR-1 Return on discount | Exists-unverified | `reporting/offer_return.py` | — | SO-06 · P1 |
| ST-OFR-2 Funding split | Exists-unverified | `sell/services/discount_funding.py` | — | SO-06 · P1 |
| ST-OFR-3 Simulation | Exists-unverified | `reporting/offer_simulation.py` | — | SO-06 · P1 |
| ST-RPT-1 Sales | Exists-unverified | `reporting/sales_report.py` | Scope denials in `test_so03_denials.py` | SO-14 · P1 · OQ-15 |
| ST-RPT-2 Inventory | Exists-unverified | `reporting/inventory_report.py` | — | SO-14 · P1 |
| ST-RPT-3 Brand performance | Exists-unverified | `reporting/brand_performance.py` | — | SO-14 · P1 · OQ-50 |
| ST-RPT-4 Staff performance | Exists-unverified | `reporting/staff_report.py` | Scope denials | SO-14 · P1 |
| ST-RPT-5 GST reports | Exists-unverified | `reporting/gst_report.py` | — | SO-14 · P1 · CA |
| ST-RPT-6 Exceptions | Exists-unverified | `reporting/exceptions_report.py` | — | SO-14 · P1 · OQ-44 |
| ST-HR-1 Staff list | Partial | `accounts/goods_admin_services.py` (C02 open) | `test_so03_admin_delivery.py` | SO-05 · P1 |
| ST-HR-2 Selfie attendance | Missing | — | — | SO-18 · P2 · OQ-53 |
| ST-HR-3 Roster/leave/OT | Missing | — | — | SO-18 · P2 · OQ-19 |
| ST-HR-4 Incentives | Partial | `reporting/staff_report.py`, `staff_targets.py` (no accrual) | — | SO-18 · P2 · OQ-02/19 |
| ST-HR-5 S&E registers | Missing | — | — | SO-20 · P4 |
| ST-HR-6 Payroll export | Missing | — | — | SO-18 · P2 · OQ-19 |
| ST-OPS-1 Approvals | Partial | Two stores (H2, H7) | Goods paths well tested | SO-14 · P1 · OQ-28 |
| ST-OPS-2 Alerts | Partial | Two stores (H7) | `test_so03_alert_scope.py` | SO-14 · P1 · OQ-18 |
| ST-OPS-3 Audit log | Exists-unverified | `masters/audit_log.py`, `core` `AuditEvent` | No audit-log test | SO-14 · P1 |
| ST-OPS-4 Checklists | Exists-unverified | `storefront/checklists.py` | — | SO-14 · P1 |
| ST-OPS-5 Remove old screens | Partial | 183 legacy routes remain (§5) | `test_so03_retired_access_paths.py` | SO-03 · P1→SO-21 |
| ST-OPS-6 Feature switches | Partial | `masters/store_features.py` | `test_first_store_feature_boundary.py` | SO-14 · P1 |

### 4.2 Main PRD (`R-`), by family

| Family (count) | Exists-tested | Exists-unverified | Partial | Missing |
| --- | --- | --- | --- | --- |
| ORG (7) | 001, 002, 007 | 003, 005 | 004, 006 (opening still blocked by obsolete OQ-54 check per register) | — |
| MDM (11) | 007, 008, 009 (golden costing) | 001–006, 010, 011 | — | — |
| BUY (9) | 001, 009 | 002, 003, 007 (AI draft, no eval set) | 004, 005, 006 (two booking writers, H8), 008 | — |
| REC (6) | 001, 002, 003, 005 | 004, 006 | — | — |
| PT (14) | 001–007 | 008, 009, 010, 012, 013, 014 | 011 (M12) | — |
| INV (27) | 001–007, 009–012, 016, 023, 026 | 015, 017–020, 024, 025 | 008, 013, 021, 022 (dual writers, H3), 027 (legacy distribution only) | 014 (OQ-49) |
| PRC (9) | — | 001–006, 008, 009 | 007 (vectors missing, M11) | — |
| POS (14) | 001, 005, 006, 008, 010 | 012 | 002, 003, 004, 007, 009, 011, 013, 014 | — |
| FIN (24) | — | 017, 018, 019 | 001–006, 008, 010, 013–016, 021, 023 | 007, 009, 011, 012, 020, 022, 024 (all SO-24; OQ-47) |
| HR (8) | — | — | 001, 004, 007 | 002, 003, 005, 006, 008 (SO-24) |
| PAR (5, all SO-24) | — | — | 002, 003 (legacy partner ledger) | 001, 004, 005 |
| AN (13) | 003 (reporting isolation) | 004 | 001, 002, 005–008 | 009–012 (AI escalation/evaluation/edge catalogue not found), 013 (SO-24) |
| CTL (9) | 001 | — | 002 (H5), 003 (two evaluators), 004, 005, 006 (H7), 007 (H4), 008, 009 | — |
| QLT (10) | 010 (import contracts) | — | 002–009 (008/009: three CI gates red, §2) | 001 (operating envelope not measured) |

Test names behind "Exists-tested" rows: `test_first_store_goods_operations`, `test_first_store_golden_calculations`, `test_first_store_receiving_shortage`, `test_first_store_acceptance_resume`, `test_first_store_transfer_authority`, `test_first_store_stock_concurrency`, `test_first_store_trading_counts`, `test_first_store_damage`, `test_soh_import`, `test_soh_reconciliation`, `test_pt_opening`, `test_legacy_season`, `test_first_store_online*`, `test_first_store_exchange`, `test_first_store_return_acceptance`, `test_online_sale_submission`, `test_first_store_new`, `test_first_store_configuration`, `test_first_store_feature_boundary`, `test_so03_booking_inputs`, `test_vendor_master_scope`, `test_database_foundation`, `test_so03_denials`, `test_first_store_rollback`.

### 4.3 Source conflicts between the two PRDs

The register's reconciliation (§34 decisions) already resolves the conflicts that affect code: offline attendance, online-only billing refusal, shortage debit-note authority, cash boundary, accounting activation (OQ-47), minors and marketing, online store credit, trading-store count freeze (OQ-57), the report performance pair, opening season (OQ-54), B2B/IRN pending, and a single permission authority. Four places where the code still diverges from the reconciled text:
- ST-CMP-4 / R-POS-013: the code queues IRN *after* acceptance; the reconciled rule is pending-before-issue.
- R-ORG-006 / R-INV-010: the opening service still refuses on "OQ-54 unresolved", although OQ-54 is now resolved.
- R-CTL-003: section permissions (`accounts/permissions.py`) still gate the legacy routes beside the action evaluator in `accounts/principal.py`.
- ST-OPS-6: the feature registry can still describe switching off online-only refusals (`masters/store_feature_registry.py`).

## 5. Duplicate implementations

### 5.1 Registered families (C01–C20): is there more than one live writer?

| Family | Live writers today | Retained target | State |
| --- | --- | --- | --- |
| Access (SO-03) | `RoleAssignment` (runtime). `RoleGrant` writer kept but unmounted (L4). Section matrix still gates legacy routes. | `/api/auth/admin/*`, one `AccessContext` | Partly done |
| C01 masters | Legacy CRUD (H1) + goods-v1 masters | goods-v1 governed identity | **Two writers** |
| C02 staff/salesperson | Goods staff; salesperson-match workbench | `/staff/list` | One writer, two readers |
| C03 offers/terms/tax | Offers single path (`/api/offers`); terms and tax on goods-v1 | Versioned commercial rules | One writer each; offer tenant provenance heuristic |
| C04 bookings | Legacy `Booking` + `GoodsBooking` (H8) | `GoodsBooking` | **Two writers** |
| C05 receiving/PT | Goods only; legacy removed | goods receiving/PT | **Done** |
| C06 stock ledger | `StockLedgerEntry` + goods journal (H3), fenced by site | goods journal | **Two writers**, fenced |
| C07 transfers | `StoreTransfer` + `GoodsTransfer` (H3) | goods transfers | **Two writers**, fenced |
| C08 counts/adjustments | Legacy stocktake apply/adjustments + goods counts (H3) | goods counts | **Two writers**, fenced |
| C09 RTV | Legacy `post_rtv` + goods RTV (H3) | goods RTV | **Two writers**, fenced |
| C10 sell/till | Single bill writer `accept_sale`, branching to goods or legacy stock | `/sell` | One writer, two stock families |
| C11 customers | Bill-time `upsert_customer` + goods `customer_rights` | `/customers` | **Two writers** |
| C12 orders | Legacy `/api/sell/` only | Same | One writer |
| C13 money | Legacy finledger subledgers + goods payables (different ledgers) | Specialist money screens | Distinct by design; legacy has no idempotency (H4) |
| C14 buying analytics | Goods open-to-buy; legacy bookings bypass it (H8) | Canonical bookings | Gap |
| C15 approvals/alerts/search/files | Two approval stores, two alert stores (H2, H7); legacy `StoredFile` beside `EvidenceObject` | Shared scoped queues | **Two writers** |
| C16 IRN/bank | Bank import only; IRN post-sale queue | Pending-before-IRN | Lifecycle gap |
| C17 mail | Single | Single | One writer |
| C18 staff/attendance | No attendance writer | — | Not built |
| C19/C20 | Provider edges and P4 | — | Not built |

### 5.2 Unregistered duplicates (need an inventory row and owner)

| Concern | Implementations | Recommendation |
| --- | --- | --- |
| Official numbering | `core/documents.VoucherSeries` (legacy scope), `core/numbering` (entity v1 with ceilings), `masters/document_series` (prefix counters) | Pick one authority (H5) |
| Money parsing at edges | `core/money.rupees_to_paise`, `core/goods_money.paise_from_amount/json`, `finledger/posting.rupees_to_paise` (tolerant), `vendors/views._rupees_to_paise` (float), `finledger/reconciliation._parse_amount` (float), `offers/price_views._new_price_paise` (float), `ptmapper/pt_opening._paise` (rounds), plus local `_money`/`_paise` wrappers in about 10 modules | One strict parser and one rounding parser in `core` |
| Rupee display | Indian grouping copied in `inbound/debit_notes._money:637`, `sell/services/cash_count._rupees:351`, `sell/services/petty_cash.rupees:323`, `ptmapper/goods_barcode._rupees:81`, `outbound/maker_checker._inr:203`. Other formatters disagree: `offers/serializers._rupees:173` drops paise, `sell/services/brand_claims._rupees:403` and `reporting/brand_performance._rupees:601` use Western grouping, and `reporting/brand_reports._rupees:439` has no grouping. | One `core` formatter; the same amount currently renders differently by screen |
| Financial year label | `core/fiscal.financial_year`, `finledger/posting.financial_year:91`, `vendors/views._financial_year:105`, seed commands | Use `core.fiscal` |
| Version-in-force lookup | `masters/brand_terms.py:104-109` and `masters/tax_settings.py:401+` | Shared effective-dated helper |
| Scope evaluation | `masters/scoping.py` (section-based), `accounts/principal.py` (action-based), `vendors/scoping.py` | Retire the section path with SO-03 |
| Customer search | `sell/views.py:399-400` (bill mobile) and `sell/services/customer_rights.search_customers` | One search over `CustomerNumber` |
| Staff list | `sell/services/dataset.py:858` and `sell/goods_salesperson_views.py:117` | C02 |

### 5.3 Copy-paste within canonical code (pylint, ≥10 lines)

These are not legacy-versus-target duplicates. They are maintenance risk inside the retained path.
- **Correction documents:** `outbound/goods_rtv.py`, `goods_writeoff.py` and `goods_disposal.py` share near-identical draft creation, `draft_payload`, decision and closure code. The largest blocks are rtv 560-606 ≈ writeoff 488-534 (46 lines), disposal 686-724 ≈ writeoff 500-538, and disposal 498-533 ≈ writeoff 320-353, with 12 blocks in total. A fix to one of these is easy to miss in the other two. Extract a shared correction-document module.
- **Reporting facts refresh:** about 10 `reporting/*_facts.py` modules repeat the same advisory-lock, `RefreshResult` and failure-recording scaffold (for example `margin_facts:39-74` ≈ `offer_sim_facts:68-103`, and `brand_sale_facts:57-92` ≈ `funding_facts:39-74`). Extract a refresh runner.
- **OpenAPI page envelope:** `_page_response` is defined five times, in `vendors/goods_views.py:254`, `masters/goods_identity_views.py:516`, `inbound/goods_views.py:150`, `alerts/goods_views.py:134` and `masters/goods_views.py:211`.
- **Input parsing:** `_optional_uuid` and `_int_id` are copied across `outbound/goods_movements.py`, `transfers.py`, `goods_counts.py`, `stockledger/goods_acceptance.py` and `goods_reads.py`.
- **Other:** `outbound/models.py:499-512` ≈ `stockledger/models.py:125-137`; `masters/master_sheet_template.py:32-52` ≈ `ptmapper/profiles.py:14-34`.

### 5.4 Dead-code candidates

None of these should be removed without caller proof.
- `accounts/views.py:605-1001`: `AdminMetaView`, `AccessMatrixView`, `RoleAccessView`, `RoleListCreateView` and related views appear in no URL module.
- `accounts/goods_admin_views.py:993, 1214, 1254`: `GoodsUserGrantsView`, `GoodsRoleAccessView` and `GoodsAccessMatrixView` are unmounted.
- `sell/services/returns.py`: the writer was retired, and no callers remain.
- `CreditNoteRedemption` has a model but no writer (H6).
- `inbound/models.py` and `ptmapper/models.py` are pure re-export hubs.
- `offers/price_views._new_price_paise` is reachable only if repricing is re-enabled (`price_views.py:339-341` always refuses).

## 6. Non-functional requirements

| Area (PRD) | Assessment |
| --- | --- |
| Authentication and sessions (§10.1, R-QLT-006) | Opaque server session in an HttpOnly, Secure, SameSite=Lax cookie with a session-bound CSRF token. Argon2/bcrypt hashers, temporary-password restriction, login attempts recorded. Risks: M1, M2, L3. |
| Fail-closed authorization (§10.1, R-CTL-003) | The goods action evaluator is fail-closed in practice, with one latent default (M9). Legacy routes still use the section matrix. Tenant isolation is enforced by forced RLS on goods and inherited masters, and two-tenant denial tests exist. |
| Separation of duties (§10.2, R-CTL-004) | Self-approval is refused in both approval stacks, and tested on the goods paths. The two stacks themselves are the issue (H2). |
| Immutable evidence and audit (§10.3, §10.6, R-CTL-001) | Append-only `AuditEvent` and document revisions, protected by DB triggers and tested. Legacy money writes and legacy master CRUD do not produce the same command audit (H1, H4). |
| Command safety (R-CTL-007) | Goods writes are durable and replay-safe. About 100 mutating handlers in 26 legacy files don't use the command path. Some, such as special orders, petty cash and reservations, add their own replay keys; the legacy money writes don't (H4). |
| Money precision (§8.4, ADR-0004) | `MoneyField` refuses non-integer writes, and the goods money edges refuse inexact input. Float parsing remains at three legacy edges (M3). |
| Performance (§12.2, store §27: 95/100 standard reports ≤3 s, one-store month ≤5 s) | **Not measured.** No reference dataset or timing harness was found for the report targets. Code risks: in-Python scans (M6, M7, M8), Python-side aggregation in `reporting/inventory_report.py:135-167`, search item lookups per barcode (`search/views.py:386-387`), and request-thread file parsing (M12, M14). Reports read fact copies refreshed by workers (`reporting/base.py`), which isolates billing but makes freshness depend on the worker. |
| Monitoring (R-QLT-007) | `config/goods_ops_views.OperationsHealthView`, `alerts/goods_health.py` and stdout logging of 500 tracebacks exist. There's no test for the operations health endpoint. |
| Recovery and numbering after restore (R-QLT-003) | `core.numbering` ceilings are published off-box. The other two numbering engines have no ceilings (H5). |
| Verification evidence (R-QLT-008/009) | The SO-02 baseline is red on this revision (mypy, API contract, inventory). Test reach is about 35% of modules, with no tests for `mail` or `aiagents`. |

## 7. What's left, by owner and phase

Gates in brackets block activation only. Unless a row says otherwise, code may be built first.

| Owner | P1 (store milestone) | P2–P4 / later |
| --- | --- | --- |
| **SO-02** | Turn the baseline green: 5 mypy errors in tests, 3 OpenAPI views, `StockSync.tsx` inventory row | — |
| **SO-03** | Retire section-matrix gates on legacy routes; delete the `RoleGrant` writer and unmounted access views; register the H5 numbering duplicate and the §5.2 helpers [OQ-28] | — |
| **SO-04** | Retire legacy master CRUD (H1); tenant-scope seasons and SKUs; replace the obsolete OQ-54 opening block [OQ-29 for real opening] | — |
| **SO-05** | Finish C02 so the till picker reads only the staff list | — |
| **SO-06** | Offer golden vectors on the backend (M11); bill-path GST-after-discount and HSN golden tests; tests for brand terms, funding split, simulation and return on discount [CA, OQ-26] | — |
| **SO-07** | Retire the legacy booking writer (H8); tests for three-way match, debit notes, counter-GRN and count handover; move debit-note approvals to goods (H2) [OQ-47 posting] | — |
| **SO-08** | Retire C06–C09 legacy writers (H3); unify the PT row cap (M12); tests for size balancing, transfer documents, e-way, broken size and ageing; R-INV-027 allocation on goods | ST-INV-5 min/max (P4, SO-20) |
| **SO-09** | Credit-note tender (H6) [OQ-45]; B2B pending-IRN path; tests for gift vouchers, customer display and held bills | — |
| **SO-10** | Single customer writer (C11), with the upsert race fixed (M10); consent and saved-size tests [retention decision] | P3 consent OTP |
| **SO-11** | Tests for reservations, special orders and alterations [advance policy, OQ-49, CA] | ST-ORD-4 home delivery (P4) |
| **SO-12** | Idempotent legacy money writes (H4); strict money parsing (M3, M4); include advances in cash count (M15); payables and petty-cash tests; move petty-cash approvals to goods [OQ-03, OQ-08, OQ-26] | SOR payables (P4) |
| **SO-13** | Open-to-buy on goods approvals, counting all bookings; tests for size curve, claims, margin share and SOR ageing [OQ-50] | — |
| **SO-14** | One approval and alert store (H2, H7); audit-log and checklist tests; report timing harness against both targets; SQL aggregation for M6–M8 [OQ-15, OQ-18, OQ-44] | — |
| **SO-16** | — | IRN pending-before-issue (P2), GSTR-2B upload (P2), provider settlement match (P2), bank import dedupe (M5) [CA, AATO, OQ-24] |
| **SO-17** | — | E-bill, campaigns (minors excluded) and back-in-stock (P2 manual, P3 provider); mail send queue (M13) |
| **SO-18** | — | Attendance, roster/leave/OT, incentives and payroll export: **no backend exists** (P2) [OQ-53, OQ-02, OQ-19] |
| **SO-19** | — | Payment, messaging and IRP providers (P3) |
| **SO-20** | — | Loyalty, home delivery, min/max, S&E registers, SOR liability (P4) |
| **SO-21** | Release proof across every channel: measured performance envelope (R-QLT-001), recovery, export and denial matrix | — |
| **SO-24** | — | R-FIN-007, 009, 011, 012, 020, 022 and 024; R-HR-008; R-PAR-001 to 005; R-AN-013 |

## 8. Limits of this review

- This is a static review plus the local test suite on synthetic data. It cannot show that a real store works. Real-tenant reconciliation, performance on the approved reference dataset, browser journeys and CA, OQ and privacy decisions all remain open.
- The five domain passes and the duplicate scan produced leads, and every High and Medium finding above was then re-read at its cited lines. Rows marked Exists-unverified may still have frontend or browser evidence elsewhere; this review counted backend tests only.
- Every result in this report applies only to revision `c6b8128`. Any later change needs a fresh run.
