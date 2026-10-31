# Review 03: "Goods arrived" (`/goods/receive/new`) backend

Revision reviewed: `5a38a1f` on `dev`. The working tree had documentation-only changes and no code edits. This is a report only: no code, configuration or data was changed. Measurements ran on the synthetic scratch database `kdps_rehearsal_screens`, inside one transaction that was rolled back at the end (see section 6). The default database `kdps_local` was not touched.

## 1. Summary

When goods reach a store, the person records an "arrival": which site, vendor and brand, when the goods arrived, and optionally which booking they belong to and the invoice number and date. The server checks the important rules itself. The arrival time cannot be in the future, the booking must be a confirmed, open booking for the same vendor and brand, and a second arrival for the same vendor and invoice gets a warning and needs a reason. The weak spot is the invoice. An invoice number typed later, in the "What the invoice claims" step, is not saved anywhere a person can read it again, and the duplicate-invoice check never sees it. The screen also has no way to attach an invoice file, even though the server accepts one, and it shows the booking as a long internal ID rather than its number.

Top problems:

| Rank | Problem |
| --- | --- |
| High | The invoice number and date sent with an invoice claim (E114) are only hashed and audited, never stored. The arrival, the GRN and the duplicate check keep the old value, or none. |
| High | The duplicate-invoice warning runs only when an arrival is created with an invoice number. An invoice added later through the claim step skips it. |
| Medium | Invoice cost on claim lines is shown to, and accepted from, logins without the `cost` field wherever three-way match is off. It is off at OPS-ST. |
| Medium | Nothing stops a past arrival time, and the time can never be corrected. The PT later uses that time to choose its tax and rate versions. |
| Medium | The screen has no invoice-file upload, and the server checks only that the file exists, not what kind of file it is or whose. |
| Low | The arrival read returns only the booking's document UUID, with no number, so the screen prints a raw UUID. |
| Low | The arrivals list reads every arrival and arrival head in the tenant into Python on every call. |

## 2. Path map

| User action | HTTP + route | View | Service | Written (tables) |
| --- | --- | --- | --- | --- |
| Open the form: pickers | `GET /api/goods-v1/vendors`, `/masters/brands`, `/bookings?state=confirmed` (paged) | vendors/masters views (C01/C04) | — | — |
| Choose a booking (prefill) | `GET /api/goods-v1/bookings/{id}` | vendors goods views | `booking_lines` | — |
| Type an invoice number (live warning) | `GET /api/goods-v1/inbound/arrivals/duplicate-warning?site_id&vendor_id&invoice_number` | `GoodsArrivalDuplicateWarningView` goods_views.py:1201-1259 | `duplicate_warning` goods_services.py:396-444 | — |
| Record arrival | `POST /api/goods-v1/inbound/arrivals` | `GoodsArrivalListCreateView.post` goods_views.py:1162-1198 | `parse_arrival` 292-325 → `record_arrival` 537-653 → `check_duplicate_invoice` 447-514 | `inbound_arrival`, `inbound_arrivalhead`, `inbound_duplicatearrivalacknowledgement` (when warned), `inbound_branddispatchdate` (SOR sites), `alerts_goodsexception` `unbooked_arrival` (no booking), `core_auditevent`, `core_commandkey` |
| Arrival panel | `GET /api/goods-v1/inbound/arrivals/{id}` | `GoodsArrivalDetailView` goods_views.py:1262-1282 | `arrival_data` 664-690, `arrival_hash` 701-714 | — |
| "What the invoice claims" | `POST /api/goods-v1/inbound/arrivals/{id}/invoice` | `GoodsArrivalInvoiceView` goods_views.py:1285-1349 | `record_invoice_claim` 815-925, `keep_claim_costs` 938-969 | `inbound_invoiceclaimversion`, `inbound_arrivalhead.revision`; after a GRN also excess holds, `receipt_discrepancy` and three-way exceptions |
| Three-way panel (feature on) | `GET /api/goods-v1/inbound/arrivals/{id}/three-way-match` | `GoodsArrivalThreeWayView` goods_views.py:1352-1428 | `three_way_match.grn_rows/for_viewer` | — |
| Buyer: "no booking" | `POST /api/goods-v1/inbound/arrivals/{id}/no-booking` | `GoodsArrivalNoBookingView` goods_views.py:1509-1548 | `confirm_no_booking` 717-760 | `inbound_arrivaldecision`, resolves `unbooked_arrival` |
| Arrivals list | `GET /api/goods-v1/inbound/arrivals` | goods_views.py:1112-1150 | `arrival_state` | — |

## 3. Duplicates (legacy vs goods-v1)

- Legacy receiving was deleted (OPS-18). `backend/inbound/models.py:1-24` only re-exports goods-v1 models, and `backend/inbound/goods_urls.py` is the only receiving route family. No `/api/` route outside `goods-v1` records an arrival (`backend/config/urls.py:55-82`). `api/outbound/transfers/<pk>/receive` is C07 transfer receipt, not vendor receiving.
- No receiving screen calls a legacy API. Every call in `GoodsArrivals.tsx`, `ReceiveDelivery.tsx`, `ReceiveInbox.tsx` and `GoodsReceipts.tsx` goes to `/goods-v1/...`. The SO-03 inventory still lists dynamic-URL consumers as `unresolved`: `GoodsArrivals.tsx:1ef46c672d92`, `:e6a4d8233363` and `ReceiveDelivery.tsx:11e914f3973c`, `:490d66ca5c87` (C05, `docs/planning/so-03-consolidation-inventory.json`). `scripts/check_so03_consolidation.py --check` reports "matches 3196 discovered surfaces".
- Booking side (C04): the legacy `vendors.Booking`/`BookingLine` with `received_qty` and `recompute_status` (`backend/vendors/models.py:71-190`) is still mounted under `/api/bookings` (`backend/vendors/urls.py:17-28`), but no non-test code calls `recompute_status`. Legacy bookings therefore never move from goods-v1 receipts. That is one writer (goods-v1 `BookingReceiptLink`) plus a stale legacy reader, not two writers.
- Inside goods-v1, invoice facts have two write paths. Arrival create writes `Arrival.invoice_number/invoice_date/invoice_evidence`. E114 writes `InvoiceClaimVersion.evidence/lines` and drops its own number and date (see 4.2). This is not a legacy duplicate, but the same invoice fact is split across two shapes.

## 4. Correctness vs PRD

| # | Rule (PRD) | Server-side? | Finding |
| --- | --- | --- | --- |
| 4.1 | Arrival time not in future (R-REC-001) | Yes | Enforced in `record_arrival` (`goods_services.py:542-549`, `AFTER_RECORDING`) and by DB check `ck_arrival_not_in_future` (`goods_models.py:44-48`). **Medium:** there is no lower bound and no correction route (no PATCH in `goods_urls.py`), so a mistyped year is permanent and pins the PT's tax/rate versions (section 9). Scenario: `2025-10-31` instead of `2026-10-31` is accepted; the PT reads last year's configuration. |
| 4.2 | Invoice claim kept separately; invoice number on record (R-REC-005, BP-03) | Partly | **High:** E114 parses `invoice_number`/`invoice_date` (`goods_views.py:1300-1301`) but `InvoiceClaimVersion` has no such columns (`goods_models.py:270-286`). `record_invoice_claim` puts them only into `content_hash` (`goods_services.py:886-893`) and `audit_after` (917-922), and never updates `Arrival`. `grn_invoice` then reports `grn.arrival.invoice_number` (`goods_services.py:2250`). Scenario: an arrival is recorded without an invoice, and then `INV-9` is entered in "What the invoice claims". The arrival list, inbox reference (`_arrival_reference` 4818-4824) and GRN all still show no invoice number. |
| 4.3 | Duplicate invoice warning within the receiving legal entity, showing only records the recorder can see, with a reason (R-REC-006) | Yes, at create | Scope is entity + vendor + case-folded number (`goods_services.py:370-444`), filtered by the caller's read scope (435). The hash is pinned under an advisory lock (`check_duplicate_invoice` 447-514). **High:** the check runs only in `record_arrival` (`goods_services.py:606`), and `record_invoice_claim` has none, so the warning is bypassed by recording the invoice after the arrival (scenario as in 4.2). **Low:** a store person whose scope is only the store is never warned about the same invoice already received at the warehouse of the same entity. This matches the PRD's visibility rule but leaves the business exposed. Two vendor master rows for one supplier also evade the check (it matches on `vendor_id`). |
| 4.4 | Booking optional; booking must match | Yes | `BOOKING_MISMATCH` unless the booking is readable, has a site, is live and not closed, and has the same vendor and brand (`goods_services.py:557-579`). An unbooked arrival opens an owned `unbooked_arrival` exception (630-641). |
| 4.5 | Separate arrival per vendor/brand/invoice (R-REC-006) | Yes | One vendor and brand per arrival is enforced by the model. Several invoices on one arrival are not modelled; the claim lines carry no invoice number. |
| 4.6 | Invoice date sanity | No | **Low:** `invoice_date` is not compared with the arrival date or today (`parse_arrival` 316, `record_arrival`). A future invoice date is accepted. |
| 4.7 | Counter/data-entry actors captured (R-REC-001) | At count, not arrival | The arrival records the actor through `EvidenceRow.actor`. Counter and entry user are on the count session (review 04). |

## 5. Access

| Endpoint | Who may call (server) | Evidence |
| --- | --- | --- |
| `POST arrivals` | `receive.arrival` at that site and brand (a brand-limited assignment is enough) | `goods_views.py:1177` |
| `GET arrivals`, `GET arrivals/{id}`, `GET three-way-match` | Any of 9 `READ_ACTIONS` at the arrival's site and brand (`receive.arrival`, `pt.prepare`, `pt.view`, `stock.view`, counter/disposition, `booking.manage`, no-booking, `exception.view`) | `goods_services.py:109-119, 179-198` |
| `GET duplicate-warning` | `receive.arrival` reaching the site, any brand; candidates filtered by read scope | `goods_views.py:1231-1234` |
| `POST invoice` | `receive.arrival` **or** `pt.prepare` at site and brand | `goods_views.py:1308` |
| `POST no-booking` | `arrival.no_booking.confirm` | `goods_views.py:1525` |

Measured (synthetic users): `ops.store` holds `receive.arrival, stock.accept, stock.view, pt.view, exception.view, movement.draft, pt.reversal.request` and fields `customer` only. `syn.owner` has no `receive.arrival`: it gets 200 on the arrival detail and 403 `ACTION_DENIED` on the duplicate warning.

Cost visibility:

- **Medium:** claim-line `invoice_basic_paise` is hidden only when three-way match is on at the site. `GoodsArrivalInvoiceView` returns it when `show_cost or not is_feature_on(...)` (`goods_views.py:1340-1343`). `keep_claim_costs`, which stops a non-cost login from setting cost, runs only when the feature is on (`goods_services.py:832-833`). At OPS-ST the feature is off (measured: three-way read returns `FEATURE_OFF` 403 for both users). There, `ops.store`, which has no `cost` field, can send `invoice_basic_paise` and read it back on the claim and on the GRN detail (`goods_views.py:1797-1800`). The screen does not send cost when the feature is off, but the API accepts it. Code path verified by reading; no write was exercised.
- `invoice_mrp_paise` is not treated as cost (`CLAIM_COST_KEYS`, `goods_services.py:928`). That is consistent with MRP being public.
- **Low:** `invoice_evidence_id` and the claim `evidence_id` are checked only for existence (`_evidence_missing`, `goods_services.py:234-237`), not for evidence kind, site or purpose. Row-level security still confines them to the tenant (`core/goods_base.py:62-70`).

## 6. Performance (measured)

Method: Django test `Client` (host `localhost`) with a `ServerSession` issued inside an outer `transaction.atomic()` that was rolled back. `CaptureQueriesContext` was used, with SAVEPOINT statements excluded. Data: two arrivals at OPS-ST (site 10), 6 vendors.

| Read | ops.store | syn.owner |
| --- | --- | --- |
| Arrival detail `GET arrivals/{id}` | 25 queries, 15 ms | 25 queries |
| Arrivals list | 14 | 14 |
| Duplicate warning | 15 (41 ms cold) | 403 (10) |
| Arrivals list after +40 synthetic arrivals (rolled back) | 14 | — |
| Inbox pending (the screen after "Record arrival") | 39 → 40 with +40 arrivals | 38 |

Findings:

- **Low:** the arrival detail reads `Arrival`, `masters_store`, `vendors_goodsbooking` and `DuplicateArrivalAcknowledgement` twice each (2× per table in the capture). `arrival_data` is called by `_arrival_resource` and again by `arrival_hash` (`goods_views.py:1037-1058`, `goods_services.py:701-714`). This costs a constant ~8 extra queries.
- **Low:** the arrivals list query count is constant, but `ArrivalHead.objects.all()` plus every `Arrival` is loaded and filtered in Python before paging (`goods_views.py:1119-1125`). Memory and time grow with every arrival the tenant has ever recorded. Not measured at volume.
- The duplicate warning uses the `ix_arrival_invoice_match` expression index (`goods_models.py:52-58`). It is constant per call. The screen re-asks after a 250 ms settle (`useSettled`, `GoodsArrivals.tsx:102-140`).
- **Low (frontend-caused):** `useNames("/goods-v1/vendors?limit=100")` and brands `?limit=100` (`GoodsArrivals.tsx:1559-1560`) resolve names from one page only. With more than 100 vendors, the panel shows an ID (not verified at volume; 6 vendors here).

## 7. Data

Stored where:

- `inbound_arrival`: site, vendor, brand, sub-brand, actual and recorded time, transporter reference, booking (FK to the `GoodsBooking` row pk), invoice evidence, invoice number and date.
- `inbound_arrivalhead`: revision and `grn_count`.
- `inbound_duplicatearrivalacknowledgement`: the warning hash, reason and matched arrival IDs.
- `inbound_branddispatchdate`: SOR sites only.
- `inbound_invoiceclaimversion`: append-only claim lines (JSON, including cost), evidence and hash. No invoice number or date.
- `alerts_goodsexception`: `unbooked_arrival`.
- `core_auditevent`: before/after, including the E114 invoice number and date (the only place they survive).

Duplicated facts and drift risks:

- **Medium:** the invoice number and date exist on `Arrival` and, for E114, only in an audit row, so the two drift by design (4.2). The invoice file has two slots (`Arrival.invoice_evidence` and `InvoiceClaimVersion.evidence`), and nothing says which one is current.
- **Low:** vendor and brand sit on both `Arrival` and `GoodsBooking`. They are checked equal at create (`goods_services.py:571-572`), and booking corrections cannot change them (`HEADER_CHANGE_KEYS`, `vendors/goods_services.py:1282`), so drift is unlikely.
- **Low:** the booking has two identifiers. `Arrival.booking_id` is the `GoodsBooking` pk, while the API exposes the booking's `document_id` (`arrival_data`, `goods_services.py:665-679`). Neither is the human number, which only the inbox computes (`_booking_fields` 5026-5034). This is why the screen prints a UUID (`GoodsArrivals.tsx:1608`).
- `GoodsGrn.booking` copies `Arrival.booking` at issue (review 05).
- Synthetic data observed: both arrivals carry invoice numbers (`ABF/26/77188`, `ABF/26/77120`) and 0 claim versions. That is why the GRN tab says "No invoice was recorded" (review 05).

## 8. Tests

- Backend: `backend/tests/test_first_store_goods_operations.py::test_routine_receiving_records_actual_count_without_replaying_opening_stock` posts an arrival with no booking and no invoice. `backend/tests/test_so03_denials.py::test_arrival_counter_assignment_needs_full_site_brand_authority` covers `person_can_receive`.
- Browser: `frontend/browser/first-store-receiving.spec.ts` RCV-01 (lines 519+) records an arrival with an invoice number and a claim of 4.
- Not run: backend tests need the proof DB at 127.0.0.1:55433, which was down and was not started. Ran `npx vitest run src/lib/goodsReceiving.test.ts src/lib/goodsReceiptShortage.test.ts` at `5a38a1f`: 10 passed. These cover none of the findings above.
- Gaps (no test found):
  - duplicate warning, its hash and reason;
  - a claim invoice number differing from the arrival's, or added later;
  - duplicate bypass through E114;
  - claim cost hidden or refused with the feature off;
  - past-dated arrival;
  - booking mismatch refusals;
  - evidence kind/site validation;
  - query-count budgets.

## 9. PT impact

- `actual_arrival_at` is the PT's configuration date. `pt_target` uses `grn.arrival.actual_arrival_at` (`backend/ptmapper/goods_pt_services.py:282-291`), and origins are stamped `source_time=grn.arrival.actual_arrival_at` with `source_time_basis: "arrival"` (2392, 2523). A wrong arrival time therefore selects the wrong tax and rate versions, with no correction route (4.1).
- `Arrival.brand_id` becomes the lot's `brand_id` (`goods_services.py:1908`) and is part of the PT target brand set.
- The invoice number and date are not PT columns (Appendix A, `docs/prd.md:1912+`; no `invoice_*` in `ptmapper/goods_canonical.py` or `goods_pt_export.py`). Nothing is repeated on the PT. The claim's cost is compared only, never copied into PT cost (`InvoicePanel` text, `keep_claim_costs`).
- An unbooked arrival still flows to a PT. The `unbooked_arrival` work item only blocks the arrival step until counting starts (`_delivery_step`, `goods_services.py:4863`).
