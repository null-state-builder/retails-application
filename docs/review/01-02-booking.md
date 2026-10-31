# Review 01-02: New booking and the Booking page (backend)

**Screens:** Screen 1 "New booking" (`/booking/new`, `BookingNewPage`) and Screen 2 "Booking page" (`/booking/:id`, `BookingDetailPage`: draft → Confirm booking → confirmed, with Correct / Link arrival / Close early / Cancel).
**Revision reviewed:** `dev` at `5a38a1f`, with the uncommitted working tree described in the session's git status. No code, config or data was changed.
**Consolidation area:** C04 · SO-07 (booking) and C14 · SO-13 (open-to-buy, size curve). `scripts/check_so03_consolidation.py --check` reported "SO-03 inventory matches 3196 discovered surfaces" on this tree.
**Method:** I read the code, and measured on the synthetic rehearsal database `kdps_rehearsal_screens`. For the measurement I signed in through `/api/auth/login` as `syn.owner@synthetic.demo`, called the endpoints with the Django test client, and counted queries with `CaptureQueriesContext`. Everything ran inside one transaction that was rolled back. Counts before and after: 2 goods-v1 bookings and 1 legacy booking, so nothing was persisted. `kdps_local` was not touched. Backend tests were **not run**, because the proof database on port 55433 was down and I did not start it.

Facts already established by testing the running app were taken as given and not re-proved: drafts post to `/api/goods-v1/bookings`; size text is resolved against the governed size list and an unknown size text is refused; confirmation numbers the booking from the legal entity's BKG series and freezes its lines; the New booking form picks the engine from the store chosen.

---

## 1. Summary

1. A booking records what the shop expects from one vendor: brand, season, styles, sizes and quantities. After it is confirmed it gets a number, and the lines it was confirmed with are frozen.
2. Two booking systems still run side by side. The older one serves the stores on the old stock system, and the newer goods-v1 one serves the new stores. The same screens talk to both, but the two systems follow different rules: different numbering, and different approval (the older one needs the Owner's sign-off, the newer one is confirmed by the buyer alone).
3. Most rules from the product document are enforced by the server. Two important ones are not: a confirmed booking's store can be changed afterwards, and a person who is not allowed to see cost prices can still change them through two side doors.
4. The booking list asks the database about 2.5 extra questions for every booking it shows. Measured: 25 database queries with 2 bookings and 80 with 24. The screen only ever loads the first 100 bookings, so older ones will drop off the list.
5. Automated tests for this area are very thin: there are no tests for creating, confirming, closing, cancelling, correcting or linking a goods-v1 booking. None of the fixes below should be accepted without adding them.

**Top 5 problems**

| # | Severity | Problem | Evidence |
|---|---|---|---|
| 1 | **High** | A confirmed booking's destination can be changed by a correction (and a draft's by an edit). The document's own site stays the same while the header names a different store. The new store is never checked for goods-v1 contract, retired brand unit or the caller's scope. | `backend/vendors/goods_services.py:1282`, `:1432-1436`; measured: correction 10→9 returned HTTP 201, `document.site_id` stayed 10, header `destination_site_id` became 9 |
| 2 | **High** | Booking cost can be written without the `cost` field grant through `PATCH` draft edit and `POST …/corrections`. Only create checks the grant. A `PATCH` from someone without the grant also silently wipes cost that was already entered. | `backend/vendors/goods_views.py:930-941` (create only), `:991-1030`, `:1193-1231`; verified by reading, not executed |
| 3 | **Medium** | Almost no behaviour tests cover goods-v1 bookings. The six booking tests cover only OTB inputs, the tenant wall, cost projection and session expiry. | `backend/tests/test_so03_booking_inputs.py:115-261`; no test calls `create_booking`, `confirm_booking`, `close_booking`, `link_receipts` or `correct_booking` |
| 4 | **Medium** | A line's `size_value_id` given directly (on create, edit or correction) is never checked against the governed size list. That line can then never auto-link to counted goods. | `backend/vendors/goods_services.py:335`, `:1330`, `inp.vocabulary_id` at `backend/inbound/goods_input.py:120`; measured: create with a random UUID size returned HTTP 201 |
| 5 | **Medium** | The goods-v1 list is an N+1 over every booking in the tenant, filtered in Python. The screen reads only `limit=100` and never follows the cursor. | `backend/vendors/goods_views.py:841-881`, `frontend/src/pages/GoodsBookings.tsx:76`; measured: 25 → 80 queries for 2 → 24 bookings |

---

## 2. Path map

Query counts are totals per request. They include about 15–17 queries of session, tenant and assignment overhead: the cheapest authenticated goods read, the vendors picker, took 17.

| User action | Method + route | View | Service function(s) | Models / tables written | Measured queries |
|---|---|---|---|---|---|
| Load form pickers (goods-v1 store) | GET `/api/goods-v1/vendors`, `/api/goods-v1/masters/brands`, `/masters/seasons`, `/masters/entities`; stores come from the session's `sites` | `GoodsVendorListCreateView.get` (`goods_views.py:457`) and the masters goods views | `latest_vendor_versions`, master readers | none | 17 / 33 / 19 / 18 |
| Load form pickers (legacy store) | GET `/api/vendors`, `/api/masters/brands`, `/api/masters/seasons`, `/api/masters/stores` (`Bookings.tsx:461-471`) | `VendorListCreateView` (`views.py:158`), legacy masters views | – | none | not measured |
| Cost-column preview | GET `/api/goods-v1/bookings/access-preview?site_id&brand_id&line_site_ids` | `GoodsBookingAccessPreviewView.get` (`goods_views.py:767`) | `_booking_field_access` (`:728`) | none | 20 |
| Read the vendor's document (AI, legacy only) | POST `/api/bookings/draft` (`Bookings.tsx:612`) | `BookingDraftView.post` (`views.py:229`) | `vendors.agents.read_booking_receipt` → `aiagents.gemini.extract` | `files.StoredFile` | not measured |
| Save draft (goods-v1) | POST `/api/goods-v1/bookings` (`GoodsBookings.tsx:197`) | `GoodsBookingListCreateView.post` (`goods_views.py:917`) | `parse_booking` (`goods_services.py:300`), `create_booking` (`:783`) → `resolve_sizes`, `validate_booking`, `new_document`, `append_revision` | `core.DocumentIdentity`, `DocumentHead`, `DraftRevision`, `DraftLine`, `vendors.GoodsBooking`, command key and audit rows | 63 |
| Save draft (legacy) and send it to the Owner | POST `/api/bookings`, then POST `/api/bookings/<int>/request-approval` (`Bookings.tsx:721`, `:740`) | `BookingListCreateView.create` (`views.py:292`), `BookingSubmitView.post` (`:497`) | `_allocate_booking_number` (`:110`), `vendors.approval.ask_for_approval` | `vendors.Booking`, `vendors.BookingLine`, `VoucherSeries`, approval rows, `Booking.status=submitted` | not measured |
| Edit draft | PATCH `/api/goods-v1/bookings/<uuid>` | `GoodsBookingDetailView.patch` (`goods_views.py:991`) | `update_booking` (`goods_services.py:808`), `place_booking` (`:521`) | new `DraftRevision` and `DraftLine`s, `DocumentIdentity.site` (once), `GoodsBooking.vendor/brand` | not measured; **no frontend caller** (grep finds no `api.patch` in the booking pages) |
| Size-curve: show curves | GET `/api/goods-v1/bookings/size-curve` (`BookingSizeCurve.tsx:70`) | `GoodsSizeCurveView.get` (`goods_size_curve_views.py:178`) | `size_curve.store_curves` (`size_curve.py:200`, reads `sell.SaleLine`) | none | 18 |
| Size-curve: fill a line | POST `/api/goods-v1/bookings/size-curve/fills` (`BookingSizeCurve.tsx:225`) | `GoodsSizeCurveFillView.post` (`goods_size_curve_views.py:256`) | `size_curve.allocate` | `vendors.SizeCurveFill`, audit | not measured |
| Open-to-buy check | GET `/api/goods-v1/bookings/<uuid>/open-to-buy` (`OpenToBuyPanel.tsx:39`) | `GoodsBookingOpenToBuyView.get` (`open_to_buy_views.py:446`) | `booking_check` (`:266`) → `otb.verdict`, `positions`, `booking_facts` (`open_to_buy.py:263`) | none | 29 (draft), 25 (confirmed) |
| Open-to-buy ask | POST `/api/goods-v1/bookings/<uuid>/open-to-buy/request-approval` (`OpenToBuyPanel.tsx:68`) | `GoodsBookingOpenToBuyAskView.post` (`open_to_buy_views.py:459`) | `otb.ask_owner` (`open_to_buy.py:719`) | `vendors.OpenToBuyAsk`, `approvals.Approval` | not measured |
| Confirm booking | POST `/api/goods-v1/bookings/<uuid>/request-approval` (`GoodsBookings.tsx:566`) | `GoodsBookingConfirmView.post` (`goods_views.py:1036`) | `confirm_booking` (`goods_services.py:842`) → `validate_booking`, `core.numbering.allocate(entity,"BKG")`, `open_to_buy.guard_confirmation` (`open_to_buy.py:594`), `officialise` | `OfficialVersion`, `OfficialLine`, `DocumentIdentity.official_number`, `DocumentHead.live_version`, number series row | 76 (with 2 and with 13 other confirmed bookings of the brand; open-to-buy is switched off at the synthetic sites, so `booking_facts` did not run) |
| Correct | POST `/api/goods-v1/bookings/<uuid>/corrections` (`GoodsBookings.tsx:250`) | `GoodsBookingCorrectionsView.post` (`goods_views.py:1193`) | `parse_correction` (`goods_services.py:1297`), `correct_booking` (`:1355`), `open_to_buy.guard_correction` (`open_to_buy.py:631`), `three_way_match.refresh` | `BookingCorrectionEvent`, `DocumentIdentity.site` (once), head revision; three-way match rows (writes not traced) | 71 |
| Link arrival (by hand) | POST `/api/goods-v1/bookings/<uuid>/receipt-links` (`GoodsBookings.tsx:416`) | `GoodsBookingReceiptLinksView.post` (`goods_views.py:1120`) | `link_receipts` (`goods_services.py:923`) → `_append_links` (`:970`), `_close_unbooked_arrival` (`:1257`), `three_way_match.refresh` | `BookingReceiptLink`, `DocumentEvent(receipt_linked)`, head revision, exception resolution | not measured (no GRN in the synthetic data) |
| Link arrival (automatic at GRN issue) | inside GRN issue (C05) | inbound GRN command | `lock_booking_for_receipt` (`:1116`), `link_exact_matches` (`:1217`) → `_append_links` | same as above, reason `GRN_EXACT_MATCH` | not measured |
| Close early / Cancel (goods-v1) | POST `/api/goods-v1/bookings/<uuid>/close` with `short_close` or `cancel` (`GoodsBookings.tsx:586`) | `GoodsBookingCloseView.post` (`goods_views.py:1074`) | `close_booking` (`goods_services.py:882`) | `GoodsBooking.closed_at/close_reason`, `DocumentEvent(short_closed / cancelled)`, head revision | not measured |
| Close / Cancel (legacy) | POST `/api/bookings/<int>/close` (`Bookings.tsx:1156`) | `BookingCloseView.post` (`views.py:453`) | `Booking.end` (`models.py`) | `vendors.Booking` status/close fields | not measured |
| List bookings | GET `/api/goods-v1/bookings?limit=100` (`GoodsBookings.tsx:76`) **and** GET `/api/bookings` (`Bookings.tsx:115`), merged client-side | `GoodsBookingListCreateView.get` (`goods_views.py:823`); `BookingListCreateView` (`views.py:271`) | `booking_readable` (`goods_services.py:533`), `booking_head` (`:557`), `booking_lines` (`:622`), `_names` (`goods_views.py:626`); legacy `scope_bookings`, `BookingSerializer` | none | goods 25 (2 rows) → 80 (24 rows); legacy 21 (1 row) → 91 (11 rows) |
| Open a booking | GET `/api/goods-v1/bookings/<uuid>` (`GoodsBookings.tsx:546`) or GET `/api/bookings/<int>` (`Bookings.tsx:1278`), chosen by the id's shape | `GoodsBookingDetailView.get` (`goods_views.py:972`); `BookingDetailView` (`views.py:416`) | `_booking_resource(progress=True)` (`goods_views.py:657`) → `booking_progress` (`goods_services.py:675`), `_shows_cost` (`goods_views.py:605`), `booking_content_hash` (`goods_services.py:760`) | none | goods 33 (constant from 2 to 24 bookings); legacy 21 |

---

## 3. Duplicates (legacy vs goods-v1)

**Does the frontend still call both engines? Yes.** The New booking form calls the legacy pickers, the AI draft, create and request-approval when the chosen store is on the legacy contract (`Bookings.tsx:461-471`, `:612`, `:721`, `:740`). It calls goods-v1 otherwise (`:702`). The list always merges both (`Bookings.tsx:111-127`), and the Booking page opens either engine by the id's shape (`Bookings.tsx:1379-1399`). The inventory agrees: the C04 api_consumer records for `Bookings.tsx` are `temporary_supported_legacy`, and those for `GoodsBookings.tsx` are `canonical`.

**Do both still write? Yes, for different stores.** Legacy `POST /api/bookings` creates `vendors.Booking` and `BookingLine` (`views.py:355-404`), and refuses goods-v1 stores (`views.py:338-350`). Goods-v1 refuses non-goods-v1 stores in `check_destinations` (`goods_services.py:440-453`). So no single booking is written twice, but the workflow has two business writers.

| Finding | Severity | Evidence | Scenario / number |
|---|---|---|---|
| Two endpoints for every booking job: create (`POST /api/bookings` vs `POST /api/goods-v1/bookings`), close (`/api/bookings/<int>/close` vs `/api/goods-v1/bookings/<uuid>/close`), list, detail, and vendor and master pickers (`/api/vendors`, `/api/masters/*` vs `/api/goods-v1/vendors`, `/api/goods-v1/masters/*`). | Medium | `backend/vendors/urls.py:18-28`, `backend/vendors/goods_urls.py:34-80` | A rule fixed in one engine (say over-receipt) stays broken in the other. Product behaviour depends on which stock contract the store has. |
| The same path name `request-approval` means different things: in the legacy engine it **sends the booking to the Owner** (maker-checker), in goods-v1 it **confirms the booking by the buyer alone**. | Medium | `views.py:481-534` vs `goods_views.py:1032-1067` (`"C-BUY confirmation (single actor)"`, `goods_services.py:845`) | A buyer at a legacy store needs the Owner's signature for a booking that a buyer at a goods-v1 store confirms alone. |
| Two booking number series: legacy `BK` per financial year and season (`VoucherSeries`, `views.py:110-117`); goods-v1 `BKG` per legal entity (`core.numbering.allocate`, `goods_services.py:862`). | Low | as cited | Reconciling numbers at C04 cutover needs a mapping between them. The register's "reconcile numbers" item is open. |
| AI document intake exists only on the legacy endpoint. A goods-only buyer has no upload (`Bookings.tsx:753`, `{canLegacy && (`). A dual-role buyer who uploads and then picks a goods-v1 store loses the source file, because `source_file_id` is sent only on the legacy branch (`Bookings.tsx:727`) and `createGoodsBooking` sends no `source_evidence_id` (`Bookings.tsx:702-718`). | Medium | as cited; `HEADER_KEYS` already accepts `source_evidence_id` (`goods_services.py:264`) | R-BUY-007 requires source provenance. A goods-v1 booking made from an uploaded PDF keeps no link to that PDF. |
| Nothing writes the legacy received counter. A grep across `backend/` finds `BookingLine.received_qty` / `inwarded_qty` set only in seed commands. Legacy progress (`models.py:146,151`) and the legacy "cancel only if nothing received" check (`models.py:160`) read a counter nothing updates. | Medium | `backend/vendors/models.py:215-216`; grep of `received_qty` (writers only in `accounts/management/commands/seed_foundation.py` and `outbound/management/commands/seed_demo_data.py`) | Not verified at runtime: a legacy booking whose goods arrived through the legacy PT would still show 0 received, and could be cancelled. |
| Inventory classification is inconsistent. The canonical C14 open-to-buy and size-curve APIs, and the canonical goods services that import them, rest on modules classed `retirement_candidate` under C04: `vendors.open_to_buy`, `open_to_buy_views`, `open_to_buy_models`, `size_curve`, `size_curve_models`, `scoping`, `serializers`, and `vendors.models`, which holds `Vendor`. The booking workspace screens `/booking`, `/booking/new`, `/booking/:id` are `temporary_supported_legacy`, although the register names them as the retained workspace. | Low | `docs/planning/so-03-consolidation-inventory.json` (classification rules for S2501-S2518, S2678-S2680); `goods_services.py:42` imports `vendors.open_to_buy` | A retirement pass that follows the labels would delete live dependencies of the canonical path. |
| Reverse dependencies on legacy `vendors.Booking` are missing from its inventory record: `stockledger.StockLedgerEntry.booking` and `finledger.VendorLedgerEntry.booking`, both `SET_NULL`. Neither are the goods-v1 consumers `inbound` Arrival/GRN `.booking`, `sell.SpecialOrder.booking` and `approvals.unified_authority` reading booking lines. | Low | `backend/stockledger/models.py:141-147`, `backend/finledger/models.py:40-46`, `backend/sell/special_order_models.py:86`, `backend/approvals/unified_authority.py:81-91` | Deleting legacy bookings would silently null stock and vendor ledger links. AGENTS.md asks for an inventory record and an owner for each dependency found. Not added here (report only). |
| A goods-v1 open-to-buy read falls back to the legacy permission source: `on_anywhere` uses `actionable_store_ids(user, section="booking")`. | Low | `backend/vendors/open_to_buy.py:340-347` | AGENTS.md says not to fall back silently to an old permission source. The company-wide budget's on/off state follows legacy section scope. |

---

## 4. Correctness against the PRD (R-BUY-001 / R-BUY-002, BP-02)

| Rule (PRD `docs/prd.md:616`, `:618`, `:424`) | Server-side status | Evidence |
|---|---|---|
| One vendor, brand and season | **Enforced structurally.** One header carries a single `vendor_id`, `brand_id`, `season_id`, each required. Vendor, brand and legal entity must be active and inside the tenant wall. The season must exist and not be retired. **Gaps:** (a) the vendor is not checked against the brand (no `VendorBrand` lookup), so any active vendor can be booked for any brand; (b) a season with `status=closed` is accepted, because only the `MasterVersion` retirement is checked; (c) `Season` has no tenant column (`masters/models.py:195`). | `goods_services.py:300-366`, `:465-476` |
| Style code and quantity required | **Enforced** (`required=["line_key","style_code","qty"]`, positive quantity). A draft may have zero lines. Confirmation needs at least one (`NO_LINES`). | `goods_services.py:324`, `:503-506` |
| Free Size never implied | **Enforced for size text.** A blank size stays unknown, and an unknown text is refused (`resolve_sizes`). A line with no size never auto-links (`exact_booking_line` returns None). **Gap:** `size_value_id` sent directly is unchecked. It is not checked for existence, for being a *size* value (it could be a colour value), or for retirement. Measured: a random UUID was accepted, HTTP 201. Corrections can only send ids (`goods_services.py:1330`). | `goods_services.py:372-417`, `:1137-1167`, `inbound/goods_input.py:120-128` |
| Destination optional, permanent once named | **Partly.** A booking can be saved and confirmed with no site. `DocumentIdentity.site_id` is set only once (`place_booking`). **Gap (High):** the header `destination_site_id` and each line's destination can still change after the site is set: on a draft by `PATCH`, where the code comment says so deliberately (`goods_services.py:824-826`), and on a confirmed booking by a correction (`HEADER_CHANGE_KEYS`, `:1282`). For an already-placed booking, the correction runs no `check_destinations` (goods-v1 contract, retired brand unit) and no scope check on the new site (`goods_views.py:1203-1209`, `goods_services.py:1432-1436`). Measured: correction 10→9 returned HTTP 201. `document.site_id` stayed 10, header became 9, and `names.site` now reads "Synthetic Operations Warehouse" (site 9). | as cited |
| Cancel only when nothing received | **Partly.** Cancel is refused when the net of links (`received_qty - reversed_qty`) is above 0 (`goods_services.py:895-901`). **Gap:** goods counted on an arrival against this booking but not yet issued as a GRN, and so not yet linked, do not block cancel. At GRN issue `link_exact_matches` then skips the cancelled booking (`:1240-1245`), and the goods stay unlinked. Not executed. | as cited |
| Over-receipt allowed **with a remark** | **Not enforced.** A link is capped only by the GRN line's counted quantity (`EXCEEDS_RECEIVED`, `:1066-1076`). Nothing compares it with the booking line's ordered quantity, and no remark is asked for. Every link has a free-text `reason_code` (the UI defaults to `RECEIVED`, `GoodsBookings.tsx:406`). Automatic links use `GRN_EXACT_MATCH` with the full counted quantity (`:1205-1212`). Outstanding is floored at 0 (`:697`). | as cited |
| Nothing auto-closes | **Enforced.** No code path sets `closed_at` other than `close_booking`. Links to a short-closed booking stay allowed (`goods_views.py:692-701`). | as cited |
| Short-close records actor, reason and the original quantity | **Enforced.** Reason required; `DocumentEvent` recorded inside the command, so the actor is captured; lines untouched. `close_reason` is the free code the client sends. `cancel` vs `short_close` is known only from the event kind (`booking_close_kind`, `:563-571`). | `goods_views.py:1078-1084`, `goods_services.py:882-920` |
| Receipt links dated, keeping the actual date and actor | **Enforced, with gaps.** `effective_at` is client-supplied: a future date is refused (`:1001`), but there is no lower bound (a link can be dated before the GRN or before confirmation). `recorded_at` is set by the database. **Gap (Medium):** a hand link does not check that the GRN's brand or site matches the booking, or that the GRN's arrival was received against this booking. The view only checks the caller may manage bookings at the GRN's site and brand (`goods_views.py:1155-1158`). The automatic path does check brand (`goods_services.py:1196`). | as cited |
| Confirmation by an authorised person | **Enforced** as a single C-BUY actor, with a pinned `reviewed_hash` and expected revision, and the open-to-buy guard. It differs from the legacy engine's Owner sign-off (see §3). | `goods_services.py:842-879` |
| AI intake may not create an approved booking; disabled until OQ-21 criteria are approved | The AI read only fills the form, which is compliant. **Gap (Low):** the only gate is whether `EMERGENT_LLM_KEY` is configured (`aiagents/gemini.py:112`). There is no feature switch, although PRD `docs/prd.md:1652` says extraction "stays disabled until its evaluation criteria are approved". The legacy view also passes `{exc}` into the reply (`views.py:240`), and `extract` sanitises it. | as cited |

Other correctness findings:

| Severity | Finding | Evidence | Scenario |
|---|---|---|---|
| Medium | Create and `PATCH` check scope only for the booking's *home* site. Other line destinations are not scope-checked unless a cost is sent. | `goods_views.py:925-929`, `:1002-1010` | A buyer granted only site A creates a booking whose lines go to site B of the same entity. Verified by reading, not executed, because the synthetic owner has all sites. |
| Medium | `PATCH` replaces all lines with what the caller sends. A caller who cannot read cost sends no `cost_paise`, so cost entered earlier by a colleague is lost. | `goods_services.py:832-834`, `goods_views.py:991-1030` | Not executed. No UI calls `PATCH` today. |
| Low | `/api/goods-v1/bookings/access-preview` needs only a session, no booking action. It answers 404 vs 200 for any brand or site id, which lets a caller probe which brands and sites exist inside the tenant. | `goods_views.py:767-786` | – |
| Low | `sell.services.special_orders` resolves a booking by number for a store user without `booking_readable`, and reports whether the booking has a line for a style at that store. | `backend/sell/services/special_orders.py:553-580` | It leaks the existence of bookings outside the user's booking scope (C12 channel). |

---

## 5. Access

| Endpoint | Who may call it | Cost (`cost_paise`) projection |
|---|---|---|
| GET `/api/goods-v1/bookings` (list) | Any of `booking.manage`, `receive.arrival`, `pt.prepare`, `pt.view` (`goods_services.py:49`). Rows are filtered by `booking_readable` (site, brand, entity; a booking with no site needs a grant that does not need a site). | **No cost on any row.** The item keys are `arrived_qty, booked_qty, brand_id, created_at, due_at, id, kind, mrp_value_paise, names, number, owner_role, purpose, record_contract, season_id, site_id, state, updated_at, vendor_id` (measured). |
| GET `/api/goods-v1/bookings/<uuid>` (progress) | Same read actions plus `booking_readable` (`goods_views.py:581-587`) | Omitted unless one read grant carrying `cost` covers every (site, brand) cell of the header and lines (`_shows_cost`, `:605-618`; `_without_cost`, `:621-623`). Proven by `test_booking_cost_projection_requires_action_fields_and_every_destination_together`. |
| POST create | `booking.manage` at home site, brand and entity | **Writing** cost needs `cost` over every line cell (`:930-941`). The response is projected. |
| PATCH draft | `booking.manage` on the booking and the new home | **No cost-write check** (High, item 2 of the top 5). The response is projected. |
| POST confirm, close | `booking.manage` on the booking | The response is projected. Open-to-buy refusals carry no money (`open_to_buy.py:698-706`). |
| POST corrections | `booking.manage`. The new site is checked only when the booking has none yet. | **No cost-write check:** `line_changes[].cost_paise` is accepted (`goods_services.py:1335-1336`). The response is projected. Because `guard_correction` judges only a *rise* in budget use (`open_to_buy.py:637-644`), a person without cost visibility can *lower* a line's cost and so free open-to-buy room. Verified by reading, not executed. |
| POST receipt-links | `booking.manage` on the booking and at the GRN's site and brand | The response is projected. |
| GET access-preview | Any session (Low, §4) | Returns field names only. |
| GET size-curve, POST fills | `booking.manage` at store and brand, plus the size-curve feature switch | No cost. |
| GET / POST open-to-buy (booking) | `booking.manage`, plus `cost` over every cell (`open_to_buy_views.py:243-264`) | Cost-gated, refused otherwise (test above). |
| GET `/api/goods-v1/open-to-buy` | Any session. Budgets are filtered by `reaches` (`booking.manage` plus `cost`; `open_to_buy.py:308-317`, `:350-355`) | Gated per budget. |
| Legacy `/api/bookings*` | Legacy section ladder `require_section("booking", …)` (`views.py:61-67`) plus `scope_bookings`, plus unified `guard_legacy_write` on writes | Omitted by `BookingSerializer.to_representation` unless `shows_booking_cost` covers every cell (`serializers.py:210-222`). Proven by `test_legacy_booking_requires_every_scope_cell_and_projects_cost`. `estimated_value_paise` is MRP × qty, not cost (`views.py:405-409`). The legacy AI draft hides extracted cost unless the user has an all-sites, all-brands cost grant (`views.py:248-254`). |
| Downstream readers of booking cost | The three-way match strips `COST_KEYS` for a viewer without cost (`inbound/goods_views.py:1418`). Traced only that far: **not verified in depth**. Booking figures on the arrivals list use `booking_figures`, which carries no cost (`goods_services.py:733-757`). | – |

**Answer to "is cost hidden without the cost grant on every read path?"** Yes on every read path I traced: list, progress/detail, the responses of every mutation, open-to-buy, legacy list and detail, and the legacy AI draft. The gaps are on the **write** side: `PATCH` and corrections accept cost from people who cannot see it.

The access contract is not reviewed. Every C04 API record in the inventory has `access_contract.trace_status = "pending"`, `protected_fields = "unreviewed"` and `named_tests = []` (14 records; `docs/planning/so-03-consolidation-inventory.json`).

---

## 6. Performance (measured on `kdps_rehearsal_screens`, rolled back)

| Request | Bookings in tenant | Queries | Notes |
|---|---|---|---|
| GET `/api/auth/me` (baseline) | – | 36 | – |
| GET goods list `limit=100` | 2 confirmed | 25 | – |
| GET goods list `limit=100` | 24 (13 confirmed, 11 draft) | **80** | +55 for 22 rows ≈ **2.5 queries per row**: `core_documenthead` 24, `core_officialline` 13, `vendors_bookingcorrectionevent` 13, `core_draftline` 11 |
| GET goods list `limit=100&state=confirmed` | 24 | 58 | 3 per confirmed row |
| GET goods list `limit=5` | 24 | 29 | Still loads all 24 `GoodsBooking` rows into Python before slicing (`goods_views.py:841-867`) |
| GET goods detail | 2 → 24 | 33 → 33 | Constant, but repeats work: corrections read 3×, head 2×, official lines 2× (`_shows_cost` → `booking_lines`, `booking_progress` → `booking_lines`, `booking_content_hash` → `_corrections`; `goods_views.py:657-721`) |
| POST create draft (1 line) | – | 63 | – |
| POST confirm | 2 other confirmed | 76 | Open-to-buy is off at the synthetic sites, so `verdict` returned early (`open_to_buy.py:520-522`) |
| POST confirm | 13 other confirmed | 76 | Same. The open-to-buy-on path is **not measured**. |
| POST correction | – | 71 | – |
| GET open-to-buy check (draft / confirmed) | – | 29 / 25 | – |
| GET legacy list `/api/bookings` | 1 | 21 | – |
| GET legacy list `/api/bookings` | 11 (10 cloned inside the rollback) | **91** | **+7 queries per row**: `shows_booking_cost` resolves assignments and grants again for each booking (`serializers.py:16-45`, called from `:219`) |
| GET legacy detail | 11 | 21 | – |
| GET goods-v1 brands picker | – | 33 | `masters_masterversion` 12, `core_subjectrevision` 11: per-row version reads |

N+1 patterns:

| Severity | Pattern | Evidence | Number |
|---|---|---|---|
| Medium | The goods list calls `booking_head()` per row, although `document__head` is already `select_related`, then `booking_lines()` per row (draft lines, or official lines plus corrections), and `booking_state()` → `booking_close_kind()` per closed row. The batch helper `booking_close_kinds` is used only for the `state=` filter. | `goods_views.py:843-846`, `:876-878`, `:891`; `goods_services.py:557-590` | 2.5 queries per row measured; +1 more per closed row (by reading) |
| Medium | The list loads every booking of the tenant, unfiltered in SQL, applies scope, site, brand and number filters in Python, and then paginates the Python list. | `goods_views.py:841-867` | Memory and time grow with tenant history, not with page size (limit=5 still read 24 rows) |
| Medium | The frontend reads only the first page, `limit=100`, the server maximum, and drops `next_cursor`. | `frontend/src/pages/GoodsBookings.tsx:74-80`, `frontend/src/lib/goodsScreen.tsx:289-305`, `accounts/goods_api.py:339` | From the 101st goods-v1 booking, older bookings disappear from `/booking` |
| Medium (not measured) | `booking_facts(brand, season)` loads **every** goods booking of the brand, across all seasons, because the season lives only in the header JSON, and calls `booking_head` and `booking_lines` for each. It runs in confirm, correct, the open-to-buy check and the open-to-buy list (via `positions`) whenever open-to-buy is on. | `open_to_buy.py:263-289`, `:362-371` | About 3 queries per booking of the brand per budget key, estimated from the list's per-row cost |
| Low | `booking_figures` (arrival list) and `three_way_match` call `booking_lines` per booking. | `goods_services.py:733-757`, `inbound/goods_services.py:5004-5023` | Not measured |
| Low | The legacy list resolves cost access per row. | `serializers.py:210-222` | +7 per row measured |

---

## 7. Data: what is stored where

| Fact | Where it lives | Duplicated / drift risk |
|---|---|---|
| Header (vendor, brand, season, entity, destination, label, vendor ref, expected date, notes, evidence ids) | Draft: `core.DraftRevision.payload`. Confirmed: `core.OfficialVersion.canonical_payload`. After that, `BookingCorrectionEvent.payload.header_changes` are applied on read (`goods_services.py:636-640`). | – |
| Lines (style, size id, colour id, qty, destination, MRP, cost) | Draft: `core.DraftLine.payload`. Confirmed: `core.OfficialLine.payload`, plus `line_changes` replayed on every read. | The "current" lines are never stored. Every read replays corrections, which is the cost behind §6. |
| Vendor and brand | `vendors.GoodsBooking.vendor_id/brand_id` **and** the header `vendor_id`/`brand_id` strings | Kept in step by `update_booking` (`goods_services.py:836-838`). Corrections cannot change them. Reads mix the two: `_names` takes the vendor from the header and the brand from the row (`goods_views.py:644-652`). Low drift risk today, but no constraint ties them together. |
| Site | `core.DocumentIdentity.site_id` (set once) **and** the header `destination_site_id` plus each line's `destination_site_id` | **Drift proven:** after the 10→9 correction, scope, receiving permission and `context.site_id` use 10 while `names.site` and the header say 9. Receiving reads the header destination (comment at `goods_services.py:824-826`). |
| Season | Only the header JSON string, with no column and no FK. `masters.Season` is global (no tenant). | Open-to-buy has to scan every brand booking to filter by season (§6). A season deleted or recoded is not protected by any FK. |
| Legal entity, number | `DocumentIdentity.entity_id`, `official_number` (set once) | – |
| State | Draft vs confirmed: `DocumentHead.live_version_id`. Closed: `GoodsBooking.closed_at/close_reason`. Cancel vs short-close: the latest `DocumentEvent.event_kind`. | Three places. `close_reason` is free text from the client, so it can contradict the event kind (for example `reason_code="SHORT_CLOSED"` sent with `action="cancel"`). |
| Progress | `BookingReceiptLink` rows (positive, plus counters), derived on read; no counter is kept | Good. Matches the "no mutable received counter" rule. |
| Size-curve evidence | `vendors.SizeCurveFill` (store, brand, season, style, total, split) | **Not linked** to the booking or line it filled. The audit cannot show which booking used which fill. |
| Open-to-buy ask | `vendors.OpenToBuyAsk` (FK `GoodsBooking`, pinned draft hash) plus `approvals.Approval` | – |
| Legacy booking | `vendors.Booking` / `BookingLine` with mutable `received_qty`, `inwarded_qty`, `status`; referenced by `StockLedgerEntry.booking` and `VendorLedgerEntry.booking` (SET_NULL) | Received counters have no writer (§3). C04's "link legacy booking identifiers to GoodsBooking" has no implementation yet. |
| Special-order link | `sell.SpecialOrder.booking` plus `booking_line_key` | It stores the line key current at the time. A later correction issues a replacement key. `booking_lines` keeps a root map, but whether the special order follows the replacement was **not verified**. |

---

## 8. Tests

| File | What it covers for this path |
|---|---|
| `backend/tests/test_so03_booking_inputs.py` (6 tests, `:115-261`) | Open-to-buy source reads the actual draft scope and pinned hash; refuses a changed revision and foreign ownership; the tenant wall refuses foreign document links; the detail, access-preview and open-to-buy cost projection need the action and cost field over every destination; booking and open-to-buy reads refuse delivery after logout or expiry |
| `backend/tests/test_so03_denials.py:169-260` | Legacy booking receipt file hidden by integer id; legacy `scope_bookings` and `BookingSerializer` cost projection per scope cell |
| `backend/tests/test_vendor_master_scope.py:81` | The vendor master hides foreign and unassigned brands (picker) |
| `backend/tests/test_search_access_so03.py`, `test_so03_brand_identity.py` | Legacy `Booking` rows as fixtures for search and brand identity |
| `frontend/src/lib/bookingModel.test.ts` | Client-side card and status mapping only |

**Not run.** The proof database at 127.0.0.1:55433 was down. `tests/conftest.py` refuses to run without `KDPS_PROOF_MODE=1` (`scripts/proof.py`). I did not start it.

**Gaps.** No test anywhere calls the goods-v1 booking commands. Searching `backend/tests` for `create_booking|confirm_booking|close_booking|link_receipts|correct_booking|link_exact` finds only imports of parse and hash helpers. Missing:

- create, edit and confirm (number, frozen lines, `reviewed_hash` stale refusal, `NO_LINES`);
- unknown size text refused, and an unknown or retired or wrong-dimension `size_value_id` refused;
- destination fixed once named (draft edit and correction);
- cost write refused without the grant on `PATCH` and corrections;
- cancel refused after receipt (and with a pending arrival);
- over-receipt with remark;
- `EXCEEDS_RECEIVED`, `DUPLICATE_LINK`, `COUNTER_INVALID`;
- links on short-closed vs cancelled bookings;
- GRN brand or site mismatch on hand links;
- automatic exact-match linking at GRN issue (one match, several, no size);
- correction `BELOW_RECEIVED` and `EFFECTIVE_BEFORE_PREVIOUS`;
- list pagination and the state filter, with a query-count budget;
- size-curve fill (feature off, no history, retired season);
- legacy create fence for goods-v1 stores; legacy submit, approve and close.

---

## 9. PT impact

- **Season and brand source.** The booking holds `brand_id` (column plus header) and `season_id` (header JSON only). The PT and opening upload (`ptmapper`, and `scripts/soh_to_pt.py` per commit `c576b9c`) takes season and brand from the PT or SOH file's own columns. It does not read the booking (no booking reference in `ptmapper/*.py` other than the vocabulary read and the manifest note). The booking season therefore does not flow into the PT. The two can disagree, and nothing reconciles them. The size curve reads last year's season by matching **text** on `sell.SaleLine.season` and brand by `brand__iexact=brand.name` (`size_curve.py:216-226`). That makes it sensitive to the season-as-date and brand alias problems listed in `docs/data-notes/data-quality-and-import-rules.md` (B-1, B-2).
- **Style code plus size links counted goods.** At GRN issue, each counted line's SKU is reduced to `(brand, style_code, size_value_id, colour_value_id)` (`masters/goods_identity_services.py:1014-1038`). It links only when exactly one booking line has the same normalised style **and** the same governed size id (`goods_services.py:1137-1214`). The consequences:
  - a booking line with no size never auto-links, which is correct for Free Size;
  - a line stored with an invalid `size_value_id`, accepted today (§4), never links;
  - a correction that changes size or style after receipts can leave earlier links pointing at a line that no longer matches;
  - a style code that differs between the booking and the item master leaves goods for manual linking. The booking's style code is not checked against the product master, and the PRD does not require it to be.
- **Booking cost and MRP are indicative only.** `cost_paise` and `mrp_paise` on a booking line are optional (`goods_services.py:281-284`, PRD `docs/prd.md:616`). The binding cost comes from the official PT. The booking cost is used for open-to-buy commitments and the three-way match's `booked_cost_paise` vs `invoiced_cost_paise` comparison (`inbound/three_way_match.py:72-73`, `:257-266`). Because `PATCH` and corrections can write cost without the cost grant (§5), the indicative figure that open-to-buy and the cost comparison read can be changed by someone who cannot see it. The PT's own cost is unaffected.
- **For the PT file format.** Nothing on this screen writes PT rows. The useful link for a PT is the booking number (BKG series per legal entity; legacy `BK-<season>-…` for legacy stores) and the booking line's style code plus governed size. Legacy bookings keep their sizes as free text (`vendors/models.py:207`), so a PT built for a legacy-contract store cannot rely on governed size ids from the booking.
