# Review 05: "GRN" (issue from the count, booking links, damage holds) backend

Revision reviewed: `5a38a1f` on `dev`. This is a report only: no code, configuration or data was changed. Measurements ran on `kdps_rehearsal_screens` inside one transaction that was rolled back, so GRNs issued to measure scale (including a number `SYN-OPS/GRN/26-27/000002`) did not persist. `kdps_local` was not touched.

## 1. Summary

The GRN is the official goods receipt. It turns the count into numbered lines and puts the goods into the store's custody: good pieces wait in receiving, damaged, wrong and unknown pieces go to quarantine, and nothing gets a cost yet. The server does this well: one GRN per arrival, issued against exactly the count the person reviewed. Counted damage is held and sent for a second person's review straight away, and corrections go through an approved counter-GRN. The problems are around the booking and the screen. When the GRN is issued, every counted line that matches a booked style and size is linked to the booking, damaged and excess pieces included. A booking of 30 therefore reads "30 of 30 received" with one piece damaged, or "101 of 30" with no remark asked. Issuing a GRN for a large delivery is also slow (about 2,400 database queries for 2,100 scans). And the screens put the "Issue GRN" button on the Count tab and call a missing invoice claim "no invoice".

Top problems:

| Rank | Problem |
| --- | --- |
| High | Exact-match booking links ignore condition and booked quantity. Measured: booking 30, link 101 (100 good + 1 damaged) on line L booked 12, no remark, no exception. Damaged pieces count as "received" toward outstanding and open-to-buy. |
| High | Good, SKU-less GRN lines (an unknown code left "good") raise no discrepancy, yet no PT can cover them. The delivery shows "PT prepare" with goods that can never be priced. |
| High | GRN issue does one exception lookup per SKU-less scanned observation. Measured 2,457 queries / 1.2 s for 2,100 scans, 2,104 of them `alerts_goodsexception`. |
| Medium | The GRN detail does one `stockledger_position` query per lot (`reportable_damage`). Measured 57 queries for 4 lines and 153 for 101 lines. |
| Medium | The GRN invoice block is built only from an invoice *claim*, and shows the arrival's number, not the claim's. The tab says "No invoice was recorded" while an invoice number exists. |
| Medium | The inbox moves a delivery to the "GRN" step as soon as a count is *started* (even with 0 scans), but the GRN step has no issue action. The button is on the Count tab. |
| Low | One-GRN-per-arrival lives only in code (no DB constraint); `GoodsGrn.counter_of` is never written. |

## 2. Path map

| User action | HTTP + route | View | Service | Written (tables) |
| --- | --- | --- | --- | --- |
| Issue GRN (Count tab) | `POST /api/goods-v1/inbound/grns` (`count_session_id`, `reviewed_hash`, `remarks[]`, `expected_revision`) | `GoodsGrnListCreateView.post` goods_views.py:1692-1748 | `issue_grn` goods_services.py:1763-2000 → `settled_identities` 1521 → `_grn_lines` 1594 → `compare_claim` 1655 → `new_document/append_revision/allocate/officialise` (core) → `open_lot` per line → `post(P01)` → `sync_excess_holds` (with claim) → `hold_counted_damage` 4290 → `link_exact_matches` (vendors/goods_services.py:1217-1254) → `three_way_match.refresh` | `core_documentidentity/documenthead/draftrevision/officialversion/officialline`, `core_voucherseries` (number), `inbound_goodsgrn`, `inbound_countsession` (issued), `inbound_arrivalhead.grn_count`, `stockledger_custodylot/position/journalbatch`, `stockledger_activehold` (damage, excess), `outbound_damagereport` + `inbound_disposition`, `vendors_bookingreceiptlink` + booking event/head revision, `alerts_goodsexception` (`grn_awaiting_pt`, `receipt_discrepancy`, identity resolved, three-way), audit |
| GRN tab | `GET /api/goods-v1/inbound/grns/{id}` | `GoodsGrnDetailView` goods_views.py:1751-1818 | `grn_coverage` 2171-2239, `grn_invoice` 2242-2254, `grn_comparison` 2332, `grn_count_history` 2355, `grn_disposition_history` 2257, `grn_damage_reports` 4502, `pre_pt_custody.transfers_of_grn` | — |
| GRN tab: arrival facts | `GET /api/goods-v1/inbound/arrivals/{id}` | review 03 | | — |
| Delivery page / rail | `GET /api/goods-v1/inbound/inbox?view=pending&arrival=…` and `view=history` (2 calls) | `GoodsInboxView` goods_views.py:2207-2251 | `inbox_items` 5037-5108, `_delivery_records` 4908-4993, `_delivery_step` 4849-4884, `_booking_figures` 5004-5023 | — |
| Counter-GRN request | `POST …/grns/{id}/counter` | goods_views.py:1821-1897 | `request_counter_grn` 2533-2598 → approval `decide_counter_grn` 2654 | `inbound_countergrndraft`, document draft, `approvals_*` |
| Dispositions / damage report | `POST …/grns/{id}/dispositions`, `…/damage-reports` | goods_views.py:1940-2137 | `record_disposition` 4000, `report_receipt_damage` 4389 | `inbound_disposition`, holds, positions |
| GRN list | `GET /api/goods-v1/inbound/grns` | goods_views.py:1651-1683 | — | — |

## 3. Duplicates (legacy vs goods-v1)

- The legacy GRN model and readers were deleted (OPS-18, `goods_urls.py:1-5`, `inbound/models.py:1-5`). The only GRN writer is `issue_grn`, and corrections go only through `CounterGrnDraft` and approval. No screen calls a non-goods-v1 receiving API: `GoodsReceipts.tsx` (GRN, dispositions, damage) uses `/goods-v1/inbound/grns/*` only. The inventory still marks `GoodsReceipts.tsx:26f76868177d/6ab539be0dbf/e6a4d8233363` `unresolved` (dynamic URL).
- Booking receipt links have two callers, the buyer's E112 `link_receipts` and the GRN's `link_exact_matches`. Both go through one `_append_links` (`vendors/goods_services.py:970-1113`), so there is one writer with two triggers. Legacy `vendors.Booking.recompute_status` (`vendors/models.py:171`) has no runtime caller, so legacy bookings never see these receipts. Stale reader, not a second writer (C04).
- Two "received" figures exist for the same booking: goods-v1 `booking_figures` derived from links (`vendors/goods_services.py:733-757`), and the legacy `received_qty`/`received_total` on `vendors.BookingLine` exposed by `/api/bookings` (`vendors/serializers.py:140, 201`). They can disagree for any booking that exists in both.

## 4. Correctness vs PRD

| # | Rule | Server-side? | Finding |
| --- | --- | --- | --- |
| 4.1 | One GRN per arrival (R-REC-006) | Yes, in code | `issue_grn` refuses a second GRN under the arrival-head lock (`goods_services.py:1791-1800`), and `open_count_session` refuses after a GRN (1013). **Low:** there is no DB unique constraint on `GoodsGrn.arrival` (`goods_models.py:288-301`, only `count_session` is one-to-one). The two-open-sessions defect (review 04, 4.1) is caught here only by this code check. |
| 4.2 | Issued against the exact reviewed count | Yes | `reviewed_hash` must equal `session_hash` (1786-1790) and `expected_revision` must match. Hash covers holder and every observation (`session_hash` 1489-1510). |
| 4.3 | Corrections only by counter-GRN with distinct approval (R-REC-004) | Yes (request side) | `request_counter_grn` opens an approval for `receipt.counter_grn.approve` (2533-2598). Distinct-person enforcement is delegated to the approvals module and was not traced here (**not verified**). |
| 4.4 | Exact-match booking links by style + size (R-BUY-002) | Yes, but too wide | `exact_booking_line` matches normalised style code + size, rejects a differing colour, and returns nothing on several candidates (`vendors/goods_services.py:1137-1167`). **High:** `exact_booking_matches` links every line with a SKU whatever its condition (`vendors/goods_services.py:1191-1213`, no `condition` test), at its full counted quantity, and `_append_links` caps only at the GRN line's count (`EXCEEDS_RECEIVED`, `vendors/goods_services.py:1066-1076`), never at the booked quantity. **Measured (synthetic GRN `SYN-OPS/GRN/26-27/000001`):** links 10+11+8+**1 damaged** = 30, so booking 30 reads "30 of 30". **Measured (rolled back):** 100 good + 1 damaged of size L on a booking line of 12 gave links `[100, 1]`, line outstanding 0, booking figures booked 30 / received 101. No remark was asked and no exception was opened (only `grn_awaiting_pt`). R-BUY-002 requires "over-receipt with remark". |
| 4.5 | Should damaged pieces count as "received" on the booking? (R-BUY-002 vs R-REC-002/003) | Product decision needed | R-REC-002 says damage "does not reduce or hide the physical received count". R-BUY-002 counts "cumulative **valid** receipts", and R-REC-003 keeps booking fulfilment separate. Today damaged (and wrong goods given a SKU) reduce outstanding and add to open-to-buy spend (`vendors/open_to_buy.py:124-127, 229-256` read the same links). **Recommendation:** decide whether "valid" excludes damaged; if so, filter `condition == "good"` (or good + accepted wrong) in `exact_booking_matches`. |
| 4.6 | Missing identity stays visible; good stock awaits PT (R-REC-003) | **No** for good SKU-less lines | `discrepancies` flags only non-good lines and claim differences (`goods_services.py:1703-1729`). A `good` line with `sku_id: null` therefore opens a good lot in `receiving` (1893) with no `receipt_discrepancy`. **Measured (rolled back):** 2,000 unknown-code scans counted good produced 100 good SKU-less lines, and the only exception was `grn_awaiting_pt`. The PT can never cover them (`ptmapper/goods_pt_services.py:792`). `resolve_identity` does accept them (`goods_services.py:3713-3716`), but no work item points anyone there. |
| 4.7 | Damaged into quarantine and reported at once; decided by another person (R-REC-002, R-REC-005) | Yes | Non-good lines open in a `quarantine` location (1893). `hold_counted_damage` places the damage hold and a pending report in the same command (1942). **Measured:** the synthetic GRN's damaged line has `held 1, damage_held 1, unheld_uncovered 0`, with damage report `confirmed`. |
| 4.8 | Invoice vs count, shortage/excess remark (R-REC-003) | Yes, when a claim exists | `compare_claim` plus `REMARK_REQUIRED` (`goods_services.py:1812-1832`). With no claim there is no comparison, and excess is defined only against the claim, so the over-booking in 4.4 creates no excess hold. |
| 4.9 | Re-resolve barcodes at issue | Partly | `settled_identities` re-judges only for ambiguity and applies picks (1521-1591). A code now resolving to exactly one SKU is **not** settled, so the line stays SKU-less (review 04, section 9). |

## 5. Access

| Endpoint | Server rule | Evidence |
| --- | --- | --- |
| `POST grns` (issue) | `receive.arrival` at the arrival's site and brand. Any receiver at the site may issue another person's count | `goods_views.py:1715-1717` |
| `GET grns`, `GET grns/{id}` | Any `READ_ACTIONS` at the GRN site and arrival brand | `goods_views.py:1654-1665, 1764-1768` |
| `POST grns/{id}/counter` | `receive.arrival` at `held_site_id` | `goods_views.py:1839` |
| `POST dispositions` | `receipt.disposition.decide`; `accept_wrong` may be *requested* with `movement.draft` | `goods_services.py:151-153`, `goods_views.py:1993` |
| `POST damage-reports` | `movement.draft` or `receipt.disposition.decide` | `goods_services.py:169` |

Cost fields on the GRN:

- Disposition values are shown only with the `cost` field (`show_value`, `goods_views.py:1777-1781`).
- **Medium:** the invoice block on the GRN shows claim `invoice_basic_paise` to everyone when three-way match is off (`goods_views.py:1797-1800`). See review 03, section 5. OPS-ST has it off; measured `FEATURE_OFF`.
- The GRN payload itself carries no cost. Measured: `ops.store` (fields `customer` only) and `syn.owner` (with `cost`) both read the GRN detail with 200.

## 6. Performance (measured)

Method: Django test `Client`, `CaptureQueriesContext`, ServerSession inside a rolled-back transaction, SAVEPOINTs excluded.

| Call | Queries | Time | Notes |
| --- | --- | --- | --- |
| GRN detail, synthetic GRN (4 lines) | 57 (store) / 54 (owner) | 25–35 ms | 7 `stockledger_position`, 5 `inbound_countergrndraft`, 6 `core_officialline` |
| GRN detail, 101–102 lines | 153–154 | 88–91 ms | 104 `stockledger_position`: one per lot |
| GRN list | 13 | 7 ms | loads every tenant GRN into Python (`goods_views.py:1658-1665`) |
| Inbox pending (site) | 39 | 21 ms warm | constant with +40 arrivals (40) |
| Delivery page | 39 + 39 (pending + history inbox) + 25 (arrival) | — | each inbox call builds the whole site inbox, then filters to one arrival |
| Issue GRN, ~2,100 scans (2,050 unknown-code + 52 others) | **2,457** | 1,198 ms | **2,104** `alerts_goodsexception` SELECTs; 102 position INSERTs; 51 alias lookups |
| Issue GRN, 2,000 unknown + 100 SKU scans | **2,351** | 1,132 ms | same pattern |

Findings:

- **High:** `_resolve_identity_ambiguities` (`goods_services.py:1424-1434`, called at 1809) calls `resolve_exceptions` once per SKU-less observation with a code, even when no ambiguity job was ever opened. At 2,000 scans that is about 2,000 extra round trips inside the GRN's locks (site guard, arrival head, booking head), which blocks other receiving at the site for about a second.
- **Medium:** `reportable_damage` queries `Position` per lot (`goods_services.py:4361`). `_select_portions` and `_wrong_goods_portions` have the same pattern (3549, 3700). The GRN detail grows about 1 query per line.
- **Medium:** `_booking_figures` → `booking_figures` calls `booking_lines(booking, booking_head(booking))` per booking (`vendors/goods_services.py:750`). That is about 3 queries per distinct booking in the inbox (seen: `core_documenthead` ×3, `core_officialline` ×2, `vendors_bookingcorrectionevent` ×2 for 2 bookings).
- **Medium:** the inbox reads every tenant arrival with `select_related("site")` and computes every step before filtering pending/history and the `?arrival=` record in Python (`goods_services.py:5050-5055`, `goods_views.py:2236-2250`). It also loads `pending_acceptance(tenant, None)` for the whole tenant (`goods_services.py:4963-4966`). The query count is flat, but rows grow with history. The delivery page asks twice (`ReceiveDelivery.tsx:284-293`), plus `DeliveryQueue` pages the whole inbox (`useAllPages`, 222-225; whether it renders depends on screen width, not verified).

## 7. Data

Stored where:

- GRN identity, number and immutable lines live in `core_*` document kernel tables.
- `inbound_goodsgrn` links document, arrival, session and booking.
- Custody is held in `stockledger_custodylot/position`, with `journalbatch` (P01 plan).
- Holds are in `stockledger_activehold`.
- Damage is in `outbound_damagereport` plus `inbound_disposition`.
- Booking receipt is in `vendors_bookingreceiptlink`, with booking received *derived* on read (good).
- Work items are in `alerts_goodsexception`.

Duplicated facts and drift:

- **Medium:** the GRN invoice block takes `invoice_number`/`invoice_date` from `Arrival` (`goods_services.py:2250-2251`) and lines from the latest `InvoiceClaimVersion`. It returns `None` when there is no claim (2244-2246). The screen maps `None` to "No invoice was recorded" (`GoodsReceipts.tsx:1297-1301`), right under an "Invoice" fact showing the arrival's number (1286-1287). Synthetic data: 0 claims, invoice `ABF/26/77188` on the arrival. Cause of the known symptom. The claim's own number is never stored (review 03, 4.2).
- **Low:** `GoodsGrn.booking` copies `Arrival.booking` at issue (`goods_services.py:1869-1875`), while later buyer links can tie an unbooked GRN to a booking through `BookingReceiptLink` only. Readers must use `three_way_match.booking_of` (link-aware), not the column.
- **Low:** `ArrivalHead.grn_count` duplicates "a `GoodsGrn` exists" (`goods_services.py:1882-1884`).
- **Low:** the GRN header copies the session's counter and entry user at issue (`goods_services.py:1844-1851`). The real counters are in `CountHandover` and observation actors.
- **Low:** `GoodsGrn.counter_of` (`goods_models.py:298-300`) is never written (`GoodsGrn.objects.create` only at 1869), yet readers filter on it. It is dead schema.
- **Low:** the lot quantity (`issued_qty`), the GRN line `qty` and `Position` portions hold the same count three ways. Counter-GRNs adjust lines through `grn_line_state` (2099-2128).

## 8. Tests

- Backend:
  - `test_first_store_goods_operations.py::test_routine_receiving_records_actual_count_without_replaying_opening_stock` issues a GRN (good + damaged), checks 1 GRN, no origin value, damage report pending and identical replay.
  - `test_first_store_receiving_shortage.py` (2 tests) covers shortage dispositions.
  - `::test_damage_can_be_reported_during_count_but_decisions_wait`.
- Browser:
  - `first-store-receiving.spec.ts` RCV-01 covers claim 4 / count 3, remark-required issue, GRN step `?step=grn`, shortage, PT review and acceptance.
  - DMG-01 covers damage.
- Not run: the backend proof DB (127.0.0.1:55433) was down and was not started. Vitest (10 tests) passed at `5a38a1f`. None cover GRN issue.
- Gaps (no test found):
  - exact-match booking links: damaged, excess over booked, multiple candidates, colour mismatch;
  - good SKU-less lines raising work;
  - "No invoice" vs invoice number;
  - second-session path to a GRN;
  - counter-GRN distinct approver;
  - GRN-issue and GRN-detail query budgets;
  - inbox scale.

## 9. PT impact

- PT rows bind to GRN lots. `grn_lots` reads every lot opened by the GRN and its approved counter-GRNs (`goods_services.py:2027-2036`), and PT coverage is `LiveCoverage` over lot portions. One primary PT per GRN plus supplements (BP-04, `docs/prd.md:440`). The GRN issue opens the `grn_awaiting_pt` work item (`goods_services.py:1958-1966`).
- Only good, SKU-resolved, unheld, uncovered pieces are coverable (`ptmapper/goods_pt_services.py:764-808`). Damaged pieces are excluded by condition and by damage hold (measured: damaged line `unheld_uncovered 0`). Excess over the *claim* is held (`sync_excess_holds`). Excess over the *booking* is not, and goes on the ordinary PT (4.4/4.8).
- The barcode must be known before issue. Lines freeze their SKU at issue (`_grn_lines`), and a SKU-less good line is never coverable and is not flagged (4.6).
- Arrival time, not GRN time, pins the PT's tax and rate versions (`pt_target`, `ptmapper/goods_pt_services.py:282-291`; origin `source_time_basis: "arrival"` at 2523). The GRN number's financial year comes from the issue date (`allocate(run, entity, "GRN")`, `goods_services.py`).
- The invoice number and date are not repeated on the PT (Appendix A columns). The claim cost never becomes PT cost.
