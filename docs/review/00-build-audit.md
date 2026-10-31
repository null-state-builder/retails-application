# Build audit: what exists, how it fits together, where it is weak

**Revision audited:** `5a38a1f` on `dev` (backend, frontend and scripts trees clean; only untracked/modified docs in the working tree).
**Date:** 1 November 2026. **Type:** report only. Apart from this file, no code, config, migration or data was changed.
**Ground truth used:** `AGENTS.md`, `CONTRIBUTING.md`, the [SO-03 register](../planning/so-03-consolidation-register.md) and [inventory](../planning/so-03-consolidation-inventory.json) (area ids C01–C20, A00, P00), `docs/prd.md`, `frontend/src/routes.tsx`, `backend/config/urls.py` and `backend/config/goods_v1_urls.py`. Where a point repeats the [29 October backend review](../planning/backend-review-2026-10-29.md), it was re-checked at this revision.
**Measurements:** all query counts were taken with Django's test client and `CaptureQueriesContext` against the scratch database `kdps_rehearsal_screens` (`KDPS_REHEARSAL_DB`). Each request ran inside one outer transaction that was rolled back, so nothing was persisted. The default database `kdps_local` was not touched. Scripts are in the session scratchpad and are not committed.

---

## 1. Summary

KDPS has a large, working backend: 19 Django apps, about 207,000 lines of Python, 302 business models and 634 mounted API routes, plus about 130 built screens in the React app. Most areas have been built twice. An older ("legacy") version under `/api/...` sits beside a newer target ("goods-v1") version under `/api/goods-v1/...`. The official SO-03 map still lists all 20 consolidation areas (C01–C20) as open, and 185 routes as temporary legacy, 89 of them routes that write data. In several places both versions still change the same business records, and the two can disagree: in a rolled-back test, a GSTIN edited through the old setup screen showed the new value on the old API and the old value on the new one. The goods-v1 core is carefully engineered: replayable commands, append-only records protected by database triggers on 108 tables, row-level security on 160 tables, and separate-person approvals. That discipline is not yet applied everywhere. The old money postings can double-post on a retry, and the Home screen counts only old-style approvals. Speed is fine at today's data sizes, but a typical request costs about 11 database queries before it does any work. Some lists add one or two queries per row, and sign-in costs 104 queries. The full test suite (611 tests) could not be run today because its isolated database was not running. The static checks (lint, types, import rules, migrations, API contract, SO-03 inventory) all pass.

### Top 10 problems (ranked)

| # | Severity | Problem |
| --- | --- | --- |
| 1 | High | Masters (C01) have two writers. Legacy `/api/masters/*` changes store, brand, season and GSTIN rows without a version record, so the new API keeps showing the old value (measured). |
| 2 | High | Approvals (C15) live in two stores. New P1 features (open-to-buy, debit notes, petty cash) still create legacy approvals, and the Home tile and sign-in bootstrap count only the legacy inbox. |
| 3 | High | Stock, transfer, count and correction writers (C06–C09) are mounted twice. Both screen sets are routed, and hidden legacy screens still post writes. The site fence makes this safe today. |
| 4 | High | Legacy money postings (vendor bill, vendor payment, cash movement) have no idempotency key, so a retry or double-click posts twice. |
| 5 | Medium | Bookings (C04) have two engines in one screen. Open-to-buy reads only goods-v1 bookings, so a booking at a legacy store never counts against the budget. |
| 6 | Medium | N+1 queries on hot lists: people list 11+1 per row, stores/brands/vendors 11+2 per row, `/auth/me` one GSTIN query per store, store-feature checks 2 per store (till dataset 89 queries). |
| 7 | Medium | Sign-in bootstrap costs 104 queries, about 77 of them re-checking the same session for each of 10 internal sub-reads. It is also built entirely on legacy endpoints. |
| 8 | Medium | Lists are unbounded: 18 legacy list views have no pagination, the approvals and alerts inboxes are unpaged, the goods booking list loads every booking into Python, and Home downloads whole lists only to count them. |
| 9 | Medium | Cost policy is split. The legacy count variance sends unit cost and value to any scoped user, including store staff whose field ceiling excludes cost. The goods-v1 variance shows quantities only. |
| 10 | Medium | The test suite was not runnable today (514 of 611 tests need the proof database). Tests reach about 56 of 634 API paths directly, and mail and AI helpers have no tests. |

Lower-severity items (three numbering engines, float money parsing, a public 4.5 MB API schema, dead grant-editing code, orphan models and routes) are in §3–§8.

---

## 2. Inventory

### 2.1 Per Django app

Models are counted from the SO-03 inventory. Routes are counted by the module of the handler (Django admin excluded). Lines of code are non-migration `.py` lines, comments included. "Test files" counts files in `backend/tests/` that import the app. That is a reach proxy, not coverage.

| App | Purpose | Models | Legacy routes | goods-v1 routes | LOC | Migrations | Test files |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| accounts | Logins, sessions, roles and assignments, access policy, staff (A00/C02) | 14 | 40 | 0 | 17,814 | 24 | 49 |
| aiagents | Gemini document-extraction helper (a package, no models) | 0 | 0 | 0 | 127 | 0 | 0 |
| alerts | Legacy alerts, plus goods exceptions, notifications and events (C15) | 8 | 4 | 8 | 2,993 | 30 | 5 |
| approvals | Legacy `Approval`, plus goods `ApprovalRequest`/`Decision` (C15) | 7 | 3 | 3 | 3,170 | 19 | 16 |
| core | Kernel: commands and replay, documents and versions, numbering, money, audit, tenancy, jobs (P00) | 25 | 0 | 1 | 7,616 | 14 | 54 |
| files | Evidence objects, uploads, exports (C15) | 4 | 1 | 4 | 1,626 | 5 | 12 |
| finledger | Vendor, cash and partner ledgers, GL, payables, bank import (C13/C16) | 8 | 16 | 6 | 4,234 | 9 | 7 |
| inbound | Arrivals, GRN, receiving evidence, debit notes, three-way match (C05) | 15 | 0 | 26 | 10,551 | 16 | 4 |
| mail | Gmail OAuth mailbox and send (C17) | 4 | 11 | 0 | 2,299 | 2 | 0 |
| masters | Tenant and organisation, sites, brands, seasons, GSTINs, SKU identity, config, tax, brand terms, feature switches (C01/C03) | 42 | 27 | 98 | 23,964 | 36 | 43 |
| offers | Offer rulebook, EOSS, price list (C03) | 4 | 13 | 0 | 3,463 | 5 | 2 |
| outbound | Transfers, counts, adjustments, write-offs, V-flips, RTV, damage, size balancing (C06–C09) | 48 | 64 | 67 | 43,074 | 50 | 16 |
| ptmapper | PT mapping, vocabulary, opening manifests, SOH import, legacy sync (C05/C08) | 17 | 0 | 47 | 18,297 | 23 | 15 |
| reporting | Reports, fact-refresh jobs, XLSX exports (C14/C15/C16/C18) | 22 | 32 | 0 | 14,849 | 20 | 4 |
| search | Global search (C15) | 0 | 1 | 0 | 559 | 0 | 2 |
| sell | Till and POS, bills, customers, gift vouchers, cash count, day close, petty cash, orders (C10–C13) | 47 | 68 | 17 | 32,709 | 62 | 26 |
| stockledger | Legacy ledger, plus goods custody, position journal, acceptance and ageing (C06) | 24 | 6 | 25 | 11,181 | 26 | 32 |
| storefront | Store dashboard, checklists (C15) | 3 | 9 | 0 | 2,606 | 2 | 3 |
| vendors | Vendors, legacy and goods bookings, open-to-buy, size curve (C01/C04/C14) | 10 | 11 | 21 | 6,213 | 14 | 9 |
| config / drf | Health, schema, docs, command status, operations health (P00) | – | 3 | 2 | – | – | – |
| **Total** | | **302** | **309** | **325** | **≈207,300** | **340** | 60 test files, 13,901 lines |

Other surfaces from the inventory: 41 management commands, 17 scheduled jobs, 4 job handlers, 4 signal receivers and 11 middleware. On the frontend there are 145 screen records (130 built, 11 "planned" placeholders, 4 standalone), 130 navigation entries (10 hidden) and 50 bookmark redirects. The database has 313 tables, 2,047 indexes, 160 tables with forced row-level security and 230 triggers on 108 tables.

### 2.2 Route lifecycle (SO-03 classification, admin excluded)

| Lifecycle | API routes |
| --- | ---: |
| canonical | 446 |
| temporary_supported_legacy | 185 (89 of them write: POST/PUT/PATCH) |
| unresolved | 3 (`/api/offers/*` items) |

Legacy write routes by area: C08 20 · C01 16 · C12 15 · C07 12 · C15 7 · C02 4 · C04 4 · C09 4 · C16 3 · C05 2 · C06 1 · C18 1.

### 2.3 Verification run at this revision

| Check | Result |
| --- | --- |
| `scripts/check_so03_consolidation.py --check` | **Pass**: "SO-03 inventory matches 3196 discovered surfaces" |
| `ruff check .` | Pass |
| `lint-imports` | Pass, 2 kept / 0 broken |
| `mypy .` | Pass, 656 files (red on 29 Oct, now fixed) |
| `manage.py check` | Pass |
| `makemigrations --check --dry-run` | No changes |
| `scripts/api-contract.py check` | Pass, "API contract matches 527 Django paths" (red on 29 Oct, now fixed) |
| `pytest` (full) | **Not run.** The proof database (127.0.0.1:55433) was down and was not started (see §6). |

---

## 3. Duplicates: two implementations of one business job

Area ids are from the SO-03 register. "Both write" means both mounted paths change persisted business state for the same concept today.

| Area | Legacy path | Target path | Both write? | Evidence | Notes |
| --- | --- | --- | --- | --- | --- |
| **C01 masters** | `/api/masters/stores`, `brands`, `seasons`, `gstins` (POST/PATCH), `/api/vendors` (POST/PATCH) | `/api/goods-v1/masters/*`, `/api/goods-v1/vendors` | **Yes, same rows** | `backend/masters/views.py:73-77, 109-118, 132-154, 206-226`; `backend/masters/urls.py:22` calls them "readers only" | See D1 |
| **C04 bookings** | `/api/bookings` → `vendors.Booking` | `/api/goods-v1/bookings` → `GoodsBooking` | Yes, split by store contract | `frontend/src/pages/Bookings.tsx:113-124, 612, 721, 740`; fence at `backend/vendors/views.py:338` | See D3 |
| **C05 receiving/PT** | legacy SOH and PT upload: `/api/goods-v1/ptmapper/soh-imports/*` (temporary legacy) | goods receiving and PT | Yes | `frontend/src/pages/PtOpening.tsx` calls `soh-imports/pt-upload` and `/load` | The legacy lifecycle sits inside the goods-v1 namespace, so the URL alone does not tell old from new |
| **C06 stock** | `/api/stockledger/*`, `/api/outbound/mark-damaged` | goods position/journal (`/api/goods-v1/stockledger/*`, `/stock/*`) | Yes, fenced by site | `backend/stockledger/contracts.py:46-53`; merge in `backend/stockledger/on_hand_projection.py:43-115` | On-hand still has two sources, merged per request |
| **C07 transfers** | `/api/outbound/transfers*`, `stock-requests*`, `gap-closures*` | `/api/goods-v1/outbound/transfers*`, `transfer-requests` | Yes | Legacy dispatch/receive still posted from `frontend/src/pages/OutboundTransfers.tsx:999-1003` | Both `/transfer` and `/goods/transfers` are routed (`frontend/src/shell/navConfig.ts:549-557`) |
| **C08 counts and corrections** | `/api/outbound/stocktakes*`, `adjustments`, `writeoffs`, `vflips` | `/api/goods-v1/outbound/stocktakes*`, count-sessions, movements | Yes | Legacy count create/apply in `frontend/src/pages/StockCount.tsx:230, 489`; goods counts in `frontend/src/pages/GoodsCounts.tsx:51` | Two count screens: `/stock-count` and `/goods/counts` |
| **C09 RTV** | `/api/outbound/rtvs*` | goods movements and returns | Yes | `frontend/src/pages/OutboundRTV.tsx` calls 8 legacy routes and 0 goods routes | |
| **C15 approvals** | `/api/approvals*` (`approvals.Approval`) | `/api/goods-v1/approvals*` (`ApprovalRequest`/`Decision`) | **Yes, and growing** | `request_approval(` is still called by `backend/vendors/open_to_buy.py:782`, `backend/inbound/debit_notes.py:695`, `backend/sell/services/petty_cash.py:667`, `backend/offers/approval.py`, `backend/accounts/access_changes.py`, `backend/outbound/maker_checker.py` (3 calls), `backend/vendors/approval.py` | See D2 |
| **C15 alerts** | `/api/alerts*` (`alerts.Alert`, written by the `check_alerts` command) | `/api/goods-v1/alerts`, `exceptions` | Yes | `backend/alerts/views.py:103-115`; bell reads both (`frontend/src/shell/Notifications.tsx:302, 606`) | |
| **C02/A00 staff and identity** | `/api/auth/admin/staff*` (temporary legacy), `User.role_id`/`scope_type`, M2M `user_stores` | `RoleAssignment`, goods staff list | Yes (staff) | `frontend/src/pages/PeopleAccess.tsx` makes 4 legacy calls (POST/PATCH staff, retire) | |
| **P00 numbering (unregistered)** | `core.documents.VoucherSeries.allocate` (19 importing modules) | `core.numbering.allocate` (19 modules), `masters.document_series.allocate` (24 modules) | Three engines | `backend/core/documents.py:234`, `backend/core/numbering.py:109`, `backend/masters/document_series.py:288` | Only `core.numbering` has restore-safe ceilings |
| **P00 money parsing (unregistered)** | `vendors._rupees_to_paise` (float), `finledger.posting.rupees_to_paise` | `core.money.rupees_to_paise`, `core.goods_money.paise_from_amount` | n/a | `backend/vendors/views.py:89-95` (`float` and `round`, and `0` becomes `None`); `backend/finledger/posting.py:96` | |

### D1 (High, C01): two writers for one master record, measured divergence

- **Problem.** Legacy master PATCH changes the `Gstin`/`Store`/`Brand`/`Season` row and writes no `MasterVersion`. The goods-v1 read returns the latest `MasterVersion.payload` whenever one exists (`backend/masters/goods_views.py:367-381`).
- **Measured (rolled back).** Signed in as `owner`, `PATCH /api/masters/gstins/5 {"state_name": "RENAMED-ROLLBACK"}` returned 200. The legacy read then returned `RENAMED-ROLLBACK`. `GET /api/goods-v1/masters/gstins/5` still returned `Synthetic Ops State` at `revision 1`, and the `MasterVersion` count for that GSTIN stayed at 1.
- **Failure scenario.** A manager renames a store or fixes a GSTIN state on `/setup/stores` or `/setup/gstins`, which use the legacy `frontend/src/pages/MasterPages.tsx`. Every goods-v1 screen, document and print keeps the old value. The change also skips the version history and approval evidence that R-MDM needs.

### D2 (High, C15): two approval stores, and Home shows only one

- **Problem.** The Home "approvals" tile reads only `/approvals/inbox` (`frontend/src/pages/Home.tsx:362-363`). The sign-in bootstrap counts only `/approvals/inbox` and `/alerts` (`backend/accounts/bootstrap.py:72-75`). Receipt, PT, count, adjustment, RTV, write-off and SOH approvals live in the goods-v1 store.
- **Failure scenario.** A store has three goods-v1 count approvals waiting, but the Home tile says 0. Only the bell, which reads both stores (`frontend/src/shell/Notifications.tsx:13`), shows them. New P1 features keep adding legacy approval rows, which grows the C15 retirement instead of shrinking it. That conflicts with `AGENTS.md` ("Put new business capabilities in the chosen target").

### D3 (Medium, C04): one booking screen, two engines

- **Problem.** `/booking` lists both engines and writes to whichever engine the chosen store uses (`frontend/src/pages/Bookings.tsx:521-524`). Legacy creation is refused at goods-v1 sites (`backend/vendors/views.py:338-348`). In the rehearsal database, 4 of 10 stores are goods-v1. Open-to-buy reads only `GoodsBooking` (`backend/vendors/open_to_buy.py:276`).
- **Failure scenario.** A buyer at one of the 6 legacy stores books goods worth ₹10 lakh. The open-to-buy budget for that brand still shows the full amount free.

### 3.1 Frontend screens still calling legacy APIs

- **Totals.** From the inventory's resolved call sites, 43 of 134 frontend source files call 118 distinct `temporary_supported_legacy` routes. A plain grep of `api.get/post/patch/put/delete("...")` finds 316 literal call sites: 211 non-goods-v1 and 105 goods-v1. Some non-goods-v1 paths are canonical (`/api/auth/*`, `/api/mail/*`, parts of `/api/sell/*`).
- **Most legacy-dependent screens.**

  | File | Screens | Legacy calls | Goods calls |
  | --- | --- | ---: | ---: |
  | `frontend/src/pages/MasterPages.tsx` | `/setup/stores`, `/setup/brands`, `/setup/vendors`, `/setup/seasons`, `/setup/gstins`, `/money/store-targets` | 17 | 1 |
  | `frontend/src/pages/Bookings.tsx` | `/booking`, `/booking/new`, `/booking/:id` | 10 | 3 |
  | `frontend/src/pages/OutboundRTV.tsx` | `/return-to-brand/*` | 8 | 0 |
  | `frontend/src/pages/StockCount.tsx` | `/stock-count`, `/stock-count/:id` | 7 | 0 |

  Six more files make 6 legacy calls and 0 goods calls each: `OutboundVflips.tsx` (`/stock/vflips/*`), `OutboundStockRequests.tsx`, `OutboundInTransit.tsx`, `OutboundWriteoffs.tsx`, `DistributionGrid.tsx` and `OutboundTransfers.tsx`.
- **Reports, till and other canonical-target screens on legacy APIs.** Reports (`SalesReport`, `InventoryReport`, `ShrinkageReport`, `GiftStockReport`, `ExceptionsReport`, `GstReport`, `StaffPerformanceReport`) all read `/api/reports/*` (legacy C15/C16/C18). `frontend/src/till/transport.ts` makes 15 `/api/sell/*` calls. Order screens (`Reservations`, `SpecialOrders`, `Alterations`) use `/api/sell/*` routes that are C12 temporary legacy. Home reads `/masters/summary`, `/approvals/inbox`, `/stockledger/on-hand`, `/alerts` and `/finledger/vendor/ageing`, all legacy (`frontend/src/pages/Home.tsx:360-376`).

---

## 4. Dead or orphan code

| Severity | Item | Evidence | Note |
| --- | --- | --- | --- |
| Medium | **Hidden but live legacy screens.** 10 nav entries are `hidden: true` but still routed, and their writes still work: `/transfer/new`, `/transfer/distribution`, `/transfer/requests`, `/transfer/in-transit`, `/stock-count/adjustments`, `/stock-count/writeoffs`, `/stock/search`, `/offers/price-list`, `/offers/eoss` and `/setup/feature-check` | `frontend/src/shell/navConfig.ts:550-557, 589-591, 649, 834-836, 1090`; routes in `frontend/src/routes.tsx` | A bookmark to `/transfer/new` still posts `POST /api/outbound/transfers`. `AGENTS.md`: "A hidden menu or redirect does not retire code." |
| Low | **Unmounted grant editor.** `GoodsUserGrantsView` is defined but no URL references it. It is the only caller of the legacy `RoleGrant` writer `change_grants` | `backend/accounts/goods_admin_views.py:993-1091`; `backend/accounts/goods_admin_services.py:1663` | Dead code that could revive a second authority writer. A delete candidate under A00 |
| Low | **Models with no reader or writer outside their own definition and admin.** `masters.CategoryMargin`, `sell.CreditNoteRedemption` | `backend/masters/models.py:332`, `backend/sell/models.py:1193` | `CreditNoteRedemption` exists because the credit-note tender was never built (29 Oct H6) |
| Low | **API routes with no frontend caller found** (literal text search, verified by hand; may be ops or API-only use): `GET /api/goods-v1/inbound/queue`; `POST .../masters/configurations/<uuid>/withdraw`; `POST .../inbound/arrivals/<uuid>/no-booking`; `POST .../ptmapper/opening-manifests/<uuid>/rows/<uuid>/season-correction`; `POST .../sell/customers/<pk>/erase`, `/merge` and `/withdraw` | text search of `frontend/src` (no hits) | Customer erase, merge and withdraw are rights functions with no UI. Not verified whether any job or external caller uses them |
| Low | **Static callsite data under-reports callers.** The inventory shows 132 goods-v1 and 90 legacy routes with no frontend callsite, but many are called through built URLs (for example ``api.post(`/outbound/transfers/${id}/${action}`)``) | `frontend/src/pages/OutboundTransfers.tsx:1003`, `frontend/src/pages/SizeBalancing.tsx:125` | The SO-03 "callers" field is a lead, not proof. Do not retire a route based on it alone |
| Low | **Retirement candidates still shipped.** `seed_demo_data`, `seed_outbound_demo`, `seed_sell_demo`, `seed_eoss_demo`, `seed_admin`, `dump_rbac_matrix` and `fix_rtv28_vflip_brand` are classed `retirement_candidate` | SO-03 inventory command records | |
| Low | **Repository clutter.** 11 agent worktrees under `.claude/worktrees/` and 6+ `.local/first-store-migration-source-*` copies each carry full backend test trees | file system | Not code debt, but it confuses repo-wide searches |

---

## 5. Performance (measured)

**Method.** `scratchpad/perf/harness.py` signs in a real user from the scratch database, issues `client.get(url)` inside a savepoint, and records the query count, distinct SQL shapes, the most repeated shape and wall time. The scratch database is small: 10 stores, 11 brands, 34 users, 2 goods bookings, 0 transfers, stocktakes or sales. So the numbers are **floors**. N+1 is reported only where rows exist and the per-row factor was confirmed by changing `?limit=`.

### 5.1 Fixed cost per request

An empty goods-v1 list costs **11 queries**:

- 1 tenant lookup (`backend/masters/tenancy_middleware.py:30`)
- 2 `set_config` calls (`backend/core/tenancy.py:47`)
- 2 `SELECT clock_timestamp()` (`backend/core/commands.py:319` via `backend/accounts/sessions.py:183`)
- session, staff-retired check and security-guard epoch
- role assignments (`backend/accounts/role_assignments.py:65`)
- role policy JSON (`backend/accounts/unified_policy.py:103`)
- the data query itself

Some views resolve access more than once per request. `/api/sell/dataset` reads role assignments 6 times and the policy 6 times, and `/api/auth/me` reads role assignments 4 times.

### 5.2 The most-used reads

| # | Endpoint (screen) | User | Status | Queries | Rows | Repeated shape (count, source) |
| --- | --- | --- | --- | ---: | ---: | --- |
| 1 | `GET /api/auth/me` (every app load) | owner | 200 | **36** | – | GSTIN ×10, one per store (`backend/accounts/session_payload.py:38-47`, no `select_related("gstin")`) |
| 2 | `GET /api/auth/bootstrap` (sign-in) | owner / store person | 200 | **104 / 95** | 10 blocks | 11 session re-authentications, ≈77 queries (`backend/accounts/bootstrap.py:198-206` dispatches each sub-view, which re-runs `backend/accounts/authentication.py:108-117`) |
| 3 | `GET /api/goods-v1/bookings` (`/booking`) | owner | 200 | 25 | 2 | flat. But every `GoodsBooking` is loaded and filtered in Python before paging (`backend/vendors/goods_views.py:841-849`) |
| 4 | `GET /api/bookings` (legacy, same screen) | owner | 200 | 21 | 1 | flat. No pagination (`backend/vendors/views.py:271`) |
| 5 | `GET /api/goods-v1/approvals/inbox` / legacy `/api/approvals/inbox` | owner | 200 | 17 / 11 | 0 | legacy inbox has `pagination_class = None` (`backend/approvals/views.py:57`) |
| 6 | `GET /api/goods-v1/inbound/inbox` (receiving) | owner | 200 | **38** | 2 | document head ×3 (`backend/stockledger/goods_acceptance.py:1383`) |
| 7 | `GET /api/goods-v1/ptmapper/files` (PT work) | owner | 200 | 11 | 0 | – (no data to show scaling) |
| 8 | `GET /api/stockledger/on-hand` (Home stock tile, `/stock`) | owner | 200 | **59** | 3 | site guard ×8, positions ×4, aliases ×4: a per-site loop (`backend/stockledger/goods_reads.py:282, 540, 568-571` via `backend/stockledger/on_hand_projection.py:83, 115`) |
| 9 | `GET /api/goods-v1/outbound/stock-search` (`/goods/stock`) | owner | 200 | 18 | 4 | flat |
| 10 | `GET /api/auth/admin/users` (People & Access) | owner | 200 | **45** | 34 | **11 + 1 per row** (`?limit=5` 16, `?limit=20` 31, all rows 45): `current_revision()` per user (`backend/accounts/goods_admin_services.py:997`) |
| 11 | `GET /api/goods-v1/masters/stores` (`/setup/organisation`) | owner | 200 | **31** | 10 | **11 + 2 per row** (`?limit=2` 15, `?limit=5` 21): `MasterVersion` and `SubjectRevision` per row (`backend/masters/goods_views.py:367-381` via `backend/masters/goods_services.py:150, 190`). `goods-v1/masters/brands` 33 for 11 rows; `goods-v1/vendors` 17 for 6 rows |
| 12 | `GET /api/search?q=sh` (global search) | owner | 200 | **64** | – | the same per-site stock projection as #8 (`backend/search/views.py:349`) |
| 13 | `GET /api/sell/dataset` (till start) | store person | 200 | **89** | 0 | store-feature checks ×13 ×2 (`backend/masters/store_features.py:129-133`) |
| 14 | `GET /api/goods-v1/sell/brand-claims` / `GET /api/goods-v1/finledger/payables` | owner | 200 | **59 / 35** | 0 / 0 | `is_feature_on` per store, 2 queries each (`backend/sell/brand_claim_views.py:323`, `backend/finledger/payable_views.py:392`). Per row as well at `backend/finledger/payable_views.py:347, 361` (int store id, so 3 queries per row) |

Wall times were 6–200 ms on this tiny data set. They are not meaningful for production and are not reported as findings.

### 5.3 Findings

| Severity | Finding | Evidence | Scenario / number |
| --- | --- | --- | --- |
| Medium | N+1 on people and master lists | `backend/accounts/goods_admin_services.py:997`; `backend/masters/goods_views.py:1423-1431, 1360-1373`; a batch helper `latest_versions_by_kind` already exists (`backend/masters/goods_services.py:194`) | A 100-row page of users costs 111 queries. A 100-row page of stores costs 211 |
| Medium | `/auth/me` does one GSTIN query per reachable store | `backend/accounts/session_payload.py:38, 47` | 10 stores = 10 extra queries on every page load. With 40 stores that would be 40 |
| Medium | Store-feature checks are per store and per row | `backend/masters/store_features.py:128-133` (`state_at` builds a one-store batch) | Till dataset 89 queries. Payables list adds 3 queries per invoice row |
| Medium | Bootstrap re-authenticates per block | `backend/accounts/bootstrap.py:89-125, 198-206` | 104 queries at sign-in, about 74% of them auth |
| Medium | Unbounded lists | 18 legacy `ListAPIView`/`ListCreateAPIView` with no paginator (DRF default is none; see `backend/masters/views.py:65-232`, `backend/outbound/views.py:416-1889`, `backend/approvals/views.py:51-77`, `backend/alerts/views.py:103-111`); `backend/vendors/goods_views.py:841-849`; at least 11 goods-v1 call sites build a Python list before `paginate()` | Home and bootstrap download the whole approvals and alerts lists only to compute `len()` (`frontend/src/pages/Home.tsx:362-371`; `backend/accounts/bootstrap.py:203-206`) |
| Low | Merged on-hand loops per site | `backend/stockledger/goods_reads.py:282, 348, 487, 568-571` | 59 queries for 3 rows. The count grows with sites, not rows |
| Low | Indexes | Hot filters checked: `masters_masterversion (tenant_id, kind, target_key, revision)`, `core_subjectrevision (tenant_id, family, subject_key)`, `accounts_serversession (token_hash)`, `sell_sale (store_id, billed_at)` and `stockledger_position (site_id, sku_id, …)` are indexed. `vendors_goodsbooking` has no `created_at` index for its ordering, which is moot while the whole table is loaded. JSON lookups (`backend/sell/services/goods_stock.py:175` `payload__alias_as_used__in`; `backend/inbound/goods_services.py:4519` `lines__contains`) have no GIN index but are pre-narrowed | **Not verified** with EXPLAIN: the data is too small for plans to mean anything |

---

## 6. Tests

- **How tests run.** `npm run check:backend` runs `scripts/baseline.py backend`. That runs, in proof mode, `manage.py check`, `makemigrations --check`, ruff, mypy, lint-imports and `pytest -q`, all through `scripts/proof.py run`. `backend/tests/conftest.py:10-15` refuses to run unless `KDPS_PROOF_MODE=1`, and `backend/config/settings.py:25-40` then pins the database to `127.0.0.1:55433/kdps_proof`.
- **What was possible today.** The proof database was **down**, and was not started, as instructed.
  - `pytest --collect-only` collected **611 tests**, up from 586 on 29 October.
  - Running the suite with the proof URL pointing at the absent server gave **97 passed** (tests that need no database) and **514 errors** at setup (database unreachable). This is not a failure signal for the code: **the 514 database tests were not verified at `5a38a1f`**. The last recorded full pass is 586/586 at `c6b8128` (29 Oct review).
  - All static gates passed (§2.3).
- **Test files and size.** 60 files in `backend/tests/`, 398 `def test_` functions (611 items after parametrisation), 13,901 lines. `backend/accounts/test_store_loader.py` (985 lines) sits outside `testpaths` and contains no `test_` functions.
- **Reach by area.** Test files importing each app: accounts 49, core 54, masters 43, stockledger 32, sell 26, approvals 16, outbound 16, ptmapper 15, files 12, vendors 9, finledger 7, alerts 5, inbound 4, reporting 4, storefront 3, offers 2, search 2, **mail 0, aiagents 0**.
- **Thin relative to size.** Inbound (10.5k LOC, 4 test files), reporting (45 modules, 4), offers (16 modules, 2), outbound (43k LOC, 16).
- **HTTP-level reach.** Tests reference only **about 56 distinct API paths** literally, out of 634 routes. Most behaviour is tested through services (`backend/tests/first_store_goods.py`). No test sweeps the URL resolver. To fill that gap, this audit called **all 211 parameterless GET routes anonymously**: 208 returned 401, and the only 200/302 responses were the intended public `/api/auth/csrf`, `/api/auth/registration` and `/api/mail/callback`.
- **Obvious untested paths** (no test file imports or calls them):
  - mail send and OAuth
  - Gemini booking reader
  - all `/api/reports/*` endpoints and their XLSX exports
  - legacy finledger posting idempotency
  - legacy master writes against goods-v1 versions (D1)
  - till dataset size
  - customer erase, merge and withdraw

---

## 7. Data model notes

| Severity | Note | Evidence |
| --- | --- | --- |
| High | **The same master fact is stored twice.** Legacy columns on `Store`/`Brand`/`Season`/`Gstin`/`LegalEntity` and the versioned `MasterVersion.payload` JSON. Goods reads prefer the JSON, legacy reads and writes use the columns, and nothing reconciles them (D1) | `backend/masters/goods_views.py:367-381`; `backend/masters/views.py:73-226` |
| Medium | **Stock on-hand has two sources.** Legacy `StockLedgerEntry`/`StockOnHand` for legacy-contract stores, and goods `Position`/journal for goods-v1 stores, merged at read time | `backend/stockledger/on_hand_projection.py:43-115`; `backend/stockledger/contracts.py:33-53` |
| Medium | **Approvals, alerts, bookings, transfers and counts each have two tables** (see §3). **Login scope is held three ways**: `User.role_id`/`scope_type`/`entity_id` with M2M `user_stores`/`user_brands`, plus `RoleAssignment`, plus historical `RoleGrant` (append-only). `session_payload` still reads `effective_grants` | `accounts_user` columns; `backend/accounts/session_payload.py:30` |
| Medium | **Three numbering engines** for official documents | §3, P00 numbering |
| Info (strength) | **Immutability is enforced in the database.** 108 tables carry `*_forbid_update_delete` or `*_final` triggers (230 triggers in all), including `masters_masterversion`, `core_officialline`, `finledger_vendor_entry`, `inbound_debit_note`, `approvals_approvaldecision` and `core_commandkey`. 160 tables have forced row-level security. Document versions use the draft/official chain (`core_draftrevision`, `core_officialversion`, `core_officialline`) and command replay (`core_commandkey`/`commandoutcome`) | rehearsal `information_schema.triggers` and `pg_class` |
| Medium | **Legacy master tables are mutable with no history**, while their goods twins are append-only. A legacy PATCH overwrites with no audit row, which is the weak side of D1 | `backend/masters/views.py:109-118` |
| Low | **JSON used as queried data.** 158 `JSONField`s (outbound 24, masters 23, ptmapper 18, core 18, sell 16). `MasterVersion` is a generic store whose `payload` is filtered by key for vocabulary, approval policy and role policy (`payload__dimension`, `payload__action`, `payload__has_key="role_policy"`), and role policy JSON is read on every authenticated request | `backend/masters/goods_services.py:600, 1670`; `backend/outbound/transfer_authority.py:110`; `backend/accounts/unified_admin.py:121`; `backend/accounts/unified_policy.py:103` |
| Low | **JSON containment used for matching**, with no GIN index | `backend/inbound/goods_services.py:4519` (`lines__contains=[{"lot_id": …}]` OR-ed per lot); `backend/sell/services/goods_stock.py:175` |

---

## 8. Security and access quick pass

| Severity | Finding | Evidence | Scenario |
| --- | --- | --- | --- |
| Medium | **Legacy count variance shows cost to staff without the cost field.** `StocktakeVarianceView` is `IsAuthenticated` plus store scope, and returns `unit_cost_paise`, `variance_paise` and the recount `unit_cost_paise`. Store staff have the field ceiling `{"customer"}`. The goods-v1 variance is quantities-only and needs `count.review` | `backend/outbound/views.py:2233-2249`; `backend/outbound/counting.py:369-371, 394`; `backend/accounts/unified_policy.py:209-215`; `backend/outbound/goods_count_views.py:1098-1121` | A cashier who opens `/stock-count/:id` sees the landed cost of every SKU. The docstring says this is deliberate (#76), but it contradicts the policy ceiling. Needs a product decision |
| High | **Legacy money writes are not idempotent** (also in the top 10) | `backend/finledger/views.py:231-248, 259-282, 431-448`; `backend/finledger/access.py:61-75` guards authority only | A double-click on "Record payment" creates two vendor payments and two GL postings |
| Low | **The OpenAPI schema is public.** `GET /api/schema/` returned 200 and 4.5 MB to an anonymous client, and `/api/docs/` returned 200. That is the full map of 527 paths | `backend/config/urls.py:80-81`; `SPECTACULAR_SETTINGS` at `backend/config/settings.py:332` sets no `SERVE_PERMISSIONS` | Hands an attacker the complete surface. Gate it to authenticated users or `DEBUG` |
| Low | **Insecure defaults (re-checked, still present).** `DJANGO_DEBUG` defaults to `1` and `ALLOWED_HOSTS` to `*` | `backend/config/settings.py:45-46` | A deployment that omits the variable runs in DEBUG (29 Oct M1) |
| Info | **No unauthenticated business reads.** An anonymous sweep of 211 GET routes gave 208 × 401. `GoodsAPIView` sets `permission_classes = []` and relies on `resolve_access` in each handler (`backend/accounts/goods_api.py:46-56`), which held in the sweep. Write routes were not swept | measured |
| Info | **Cost-field scan.** GET responses for store person, warehouse, admin and owner contained no cost-like keys. **Not verified as a control**: most lists are empty in the scratch database, so the scan proves little. Code-level gating exists for legacy outbound (`backend/outbound/serializers.py:66-110`), bookings (`backend/vendors/serializers.py:38, 220-222`) and reports (`backend/reporting/base.py:51, 91-110`) | |
| Low | **No timeout on the AI call, and the booking reader returns raw exception text** (29 Oct L5, not re-checked) | `backend/aiagents/gemini.py:85-116`, `backend/vendors/views.py:237-244` | |

---

## 9. Appendix: API families and their status

"FE-called" counts the routes for which the SO-03 inventory found a frontend call site. Built URLs make this an under-count (§4). Area ids are the primary owners.

| Family | Mounted by | Routes | Lifecycle | FE-called | Areas |
| --- | --- | ---: | --- | ---: | --- |
| `/api/health`, `/api/schema/`, `/api/docs/` | `backend/config/urls.py:51, 80-81` | 3 | canonical | 0 | P00 |
| `/admin/*` (only when DEBUG and `ENABLE_DJANGO_ADMIN=1`) | `backend/config/urls.py:84-85` | 201 | unresolved | 0 | P00 |
| `/api/auth/*` | `accounts.urls` | 40 | 34 canonical, 6 temporary legacy (staff) | 29 | A00, C02 |
| `/api/masters/*` | `masters.urls` | 27 | 26 temporary legacy, 1 canonical | 18 | C01, C13 |
| `/api/vendors*` | `vendors.urls` | 5 | temporary legacy | 3 | C01 |
| `/api/bookings*` | `vendors.urls` | 6 | temporary legacy | 6 | C04 |
| `/api/stockledger/*` | `stockledger.urls` | 5 | temporary legacy | 5 | C06 |
| `/api/stock/*` | `stockledger.urls_stock` | 1 | temporary legacy | 0 | C06 |
| `/api/outbound/*` | `outbound.urls` | 64 | 58 temporary legacy, 6 canonical (partner billing) | 40 | C06–C09, C13 |
| `/api/finledger/*` | `finledger.urls` | 16 | 12 canonical, 4 temporary legacy (bank) | 13 | C13, C16 |
| `/api/sell/*` | `sell.urls` | 67 | 42 canonical, 25 temporary legacy (orders, IRN) | 43 | C10, C12, C13, C16 |
| `/api/offers/*`, `/api/offers/running` | `offers.urls`, `backend/config/urls.py:72` | 14 | 11 canonical, 3 unresolved | 10 | C03 |
| `/api/approvals*` | `approvals.urls` | 3 | temporary legacy | 3 | C15 |
| `/api/alerts*` | `alerts.urls` | 4 | temporary legacy | 2 | C15 |
| `/api/mail/*` | `mail.urls` | 11 | canonical | 8 | C17 |
| `/api/search` | `search.urls` | 1 | temporary legacy | 1 | C15 |
| `/api/store/*` | `storefront.urls` | 9 | 7 temporary legacy, 2 canonical | 6 | C15, C10, C13 |
| `/api/reports/*` | `reporting.urls` | 32 | 21 temporary legacy, 11 canonical | 32 | C14, C15, C16, C18, C03 |
| `/api/files/*` | `files.urls` | 1 | temporary legacy | 0 | C15 |
| `/api/goods-v1/masters/*` (incl. identity, config, tax, brand terms, audit) | `masters.goods_urls` | 98 | canonical | 59 | C01, C03, C11, C15, A00 |
| `/api/goods-v1/vendors*` | `vendors.goods_urls` | 5 | canonical | 1 | C01 |
| `/api/goods-v1/bookings*` | `vendors.goods_urls` | 13 | canonical | 12 | C04, C14 |
| `/api/goods-v1/open-to-buy*` | `vendors.goods_urls` | 3 | canonical | 3 | C14 |
| `/api/goods-v1/inbound/*` | `inbound.goods_urls` | 26 | canonical | 16 | C05, C13 |
| `/api/goods-v1/ptmapper/*` | `ptmapper.goods_urls` | 47 | 35 canonical, 12 temporary legacy (SOH import, legacy sales, stock sync) | 25 | C05, C08 |
| `/api/goods-v1/stockledger/*` | `stockledger.goods_urls` | 16 | canonical | 8 | C06 |
| `/api/goods-v1/stock/*` | `stockledger.goods_urls_stock` | 9 | canonical | 4 | C06, C14 |
| `/api/goods-v1/outbound/*` | `outbound.goods_urls` | 67 | canonical | 39 | C06, C07, C08 |
| `/api/goods-v1/sell/*` | `sell.goods_urls` | 17 | canonical | 8 | C10, C14, C02 |
| `/api/goods-v1/finledger/*` | `finledger.goods_urls` | 6 | canonical | 2 | C13 |
| `/api/goods-v1/approvals*` | `approvals.goods_urls` | 3 | canonical | 3 | C15 |
| `/api/goods-v1/alerts`, `exceptions*`, `events` | `alerts.goods_urls` | 8 | canonical | 7 | C15 |
| `/api/goods-v1/files/*`, `exports*` | `files.goods_urls`, `files.goods_export_urls` | 4 | canonical | 4 | C15 |
| `/api/goods-v1/history` | `core.goods_history_urls` | 1 | canonical | 0 | C15 |
| `/api/goods-v1/commands/<uuid>`, `operations/health` | `backend/config/goods_v1_urls.py:44-45` | 2 | canonical | 2 | C15, P00 |

**Retirement guardrails already in place:** `protected_boundaries` in the inventory blocks the return of `/api/goods-v1/auth/admin/` and of the SBU retire route. It also lists the legacy permission modules (`accounts.actor_policies`, `accounts.permissions`, `accounts.goods_admin_services`) and symbols (`RoleGrant`, `ROLE_TEMPLATES`, `effective_grants`, `configured_role_access`) with a reviewed allowlist. No business-area legacy prefix (C01–C20) has been retired into `protected_boundaries` yet.
