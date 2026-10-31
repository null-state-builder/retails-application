# Review 10: Accept & put away (delivery step `accept`, Stock on hand, Movements)

Scope: the backend behind `/goods/receive/delivery/:id?step=accept` (`AcceptPanel` in `frontend/src/pages/GoodsAccept.tsx`) and the stock screens that show the accepted goods (`/goods/stock`, `/stock` → `/inventory`, `/goods/movements`). This is a report only. No code, config or data was changed. All measurements ran on `kdps_rehearsal_screens` inside one transaction that was rolled back. Before and after the run, the PT head was identical (`official`, revision 9, live version `10156264-…`) and there were still 2 acceptance events. `scripts/check_so03_consolidation.py --check` reports "SO-03 inventory matches 3196 discovered surfaces". Consolidation items: C05 (SO-07, receiving/PT/labels, which owns this step) and C06 (SO-08, the stock journal and its readers). Reviews 05, 07 and 08 are cited by ID and not repeated.

**Facts that differ from the brief.** The goods are already accepted. On 2026-10-31 at 20:45, `ops.store` (Operations Store Person) opened session `3b81f39b-…` and put away all 22 pieces on **Floor** in two entries, 12 × `8907264512318` and 10 × `8907264512301`. The session is `completed`. The only print job for the version is `83b0489c-…` in state **`prepared`**, which means it was never sent to a printer. Both acceptance events cite `pt_version:10156264-…` as label evidence, not a print job. To measure acceptance on unaccepted goods, the run did all of the following inside the rolled-back transaction: add a `pt.reversal.approve` policy, reverse the PT, reissue it, review, send, approve (v2), and then accept.

## 1. Summary

After the PT is approved, the goods have a cost and a price but still sit in "Receiving", and the till cannot sell them. On this step a store person scans each tag, says where the piece goes (Floor, Backstore, Bin 1), and accepts it. Only then can the till sell it. The safety rules mostly hold. Only pieces covered by the approved PT can be accepted. A tag with the wrong barcode or MRP is refused. Extra or unknown pieces are refused and sent back to the goods receipt. A piece cannot be accepted twice, and pieces left unscanned stay visible instead of being written off. The main gaps: the server never checks that labels were printed; the till prices pieces from the newest PT's MRP, not each piece's own; goods put in "Bin 1" are accepted but never sellable; and reversing a PT after acceptance quietly takes floor stock off sale.

Top problems:

| # | Sev | Problem | Evidence |
| --- | --- | --- | --- |
| C1 | High | Acceptance never requires a printed label. With no print job, the server records the PT version itself as the "label". The delivery step also treats a print job that was only `prepared` (never sent) as printed. | `backend/stockledger/goods_acceptance.py:928-947` (946); `backend/inbound/goods_services.py:4956-4958` (any status) vs `goods_acceptance.py:69` (`PRINTED`); measured: the real acceptance cites `pt_version:…` with the only job in `prepared`; v2 with 0 print jobs accepted a piece (200) while the inbox said `next_step: labels` |
| C2 | High | The till prices every piece of a barcode and season at the MRP of the **newest** origin. It does not use the MRP frozen on that piece's own PT. If a later PT for the same barcode and season carries a different MRP, older tagged pieces are billed at the new price, possibly above the printed MRP. | `backend/sell/services/goods_stock.py:315, 334`; FIFO cost comes from the old origin (`goods_sale.py:198-215`); not verified by a run (needs two PTs with different MRPs) |
| C3 | Medium | "Bin 1" (and zone/fixture) is offered as a put-away place. Goods put there are accepted, but the till can never sell them: ATS needs Floor or Backstore, and the screen gives no warning. | `goods_acceptance.py:61` (`PUTAWAY_KINDS` = transferable kinds); `goods_engine.py:63-65`; measured: 11 put in Bin 1 → `ats_qty 0`, `LOCATION_NOT_ELIGIBLE`, till sellable 1 + 9 only |
| C4 | Medium | An official PT whose goods are already accepted and on the floor can still be reversed. The reversal books every piece back to "Receiving" as unvalued and unaccepted, while the goods physically stay on the floor with tags on. The till stops selling them at once. | `ptmapper/goods_pt_services.py:2545-2557` (blocks only consumed/reserved); `goods_engine.py:535-561`; measured: Floor 22 accepted → Receiving 22 unvalued, `read_shelf` `{}`; reverse 73 q, approve 122 q |
| C5 | Medium | Choosing an "Official line" on the screen fills the MRP box with **paise** (`199900`), but the box is read as **rupees**. Every scan after that is refused `TAG_MISMATCH` unless the person retypes the MRP. | `GoodsAccept.tsx:923` vs `lib/goodsPt.ts:352-360`; not verified in a browser |
| C6 | Medium | `/stock` (→ `/inventory`, "On hand") counts every physical piece as on hand, including unaccepted Receiving and Quarantine stock with blank identity. `/goods/stock` splits the same pieces into accepted, valued and ATS. The two screens agree on the total (90) but disagree on what "on hand" means. | `stockledger/on_hand_projection.py:124`; measured: legacy `units_on_hand 90`, 4 rows with blank brand/design/sku; goods summary `physical 90, valued 22, ats 22` |
| C7 | Medium | The warehouse operator was given a role assignment at the store (site 10) so they could prepare its PT. That assignment carries the whole role, including `stock.accept`, so the person who prepared and sent the PT can also accept and put away its goods at the store. | `accounts/actions.py:229-241`; assignment `c4fccb21-…` `site_ids [10]`; measured: open session as `ops.warehouse` → 201; PRD `docs/prd.md:318, 359` |
| C8 | Medium | Each scan re-reads all acceptance events of the version about three times. Each response returns **every** acknowledged scan key. Cost per scan therefore grows with the session, roughly quadratically. | `goods_acceptance.py:802-806, 386-388, 1269-1274, 1326-1332`; `goods_views.py:448`; constant 84 q per scan at small size; volume growth not measured (§6) |
| C9 | Medium | `pending_acceptance` loads every official receipt, opening and found PT the tenant has ever had, with all their lines and events. It runs on every inbox load (site filter `None`) and every pending-acceptance read. | `goods_acceptance.py:1358-1422`; `inbound/goods_services.py:4963-4966`; measured 14-17 q with 1 PT; growth with history not measured |
| C10 | Medium | No backend test covers `NOT_ON_PT`, `EXCEEDS_REMAINING`, the extra handoff, damaged-at-acceptance, Bin vs Floor ATS, reversal after acceptance, the label gate or the till price. No frontend unit test covers `GoodsAccept`. | §7 |
| C11 | Low | A tag matches a line if it is the frozen alias, a printed alias, **or any effective alias of the SKU** at the site. That includes aliases added after approval, so a code other than the printed barcode can pass. | `goods_acceptance.py:735-764` |
| C12 | Low | Each damaged or wrong scan opens its own `acceptance_discrepancy` exception, keyed by scan. Fifty single-piece wrong scans give 50 exceptions. | `goods_acceptance.py:1135-1144, 1196-1204` |
| C13 | Low | The test-store nightly stock sync is a second "make sellable" writer. It puts gains straight onto the Floor as accepted, with no scan and no tag check. It refuses real stores. | `ptmapper/stock_sync_services.py:477-499, 86-91` |
| C14 | Low | All 22 real pieces were accepted in two entries (qty 12 and qty 10). The PRD allows an explicit counted quantity, but this means individual tags were not scanned. | measured observations `qty 12`, `qty 10`; PRD R-INV-015 `docs/prd.md:714` |

## 2. Path map

Routes are under `/api/goods-v1/`. Every write is one `execute_command`, which writes the command key, attempt, audit and sealed evidence. None needs step-up (`accounts/actions.py:364-377`). One business writer: `stockledger/goods_acceptance.py` → `goods_engine.change_address`/`post`.

| Action | Method + route | View | Service | Models/tables written |
| --- | --- | --- | --- | --- |
| Open accept step | `GET inbound/inbox?view=pending&arrival=<id>` and `…view=history…`; `GET stockledger/pending-acceptance?site_id=10&limit=100`; `GET masters/stores/10/locations?limit=100` | inbox views; `PendingAcceptanceView` `goods_views.py:806` | `_delivery_records` → `pending_acceptance(tenant, None)` (`inbound/goods_services.py:4910-4990`); `pending_acceptance` `goods_acceptance.py:1358` | none |
| Open / resume session | `POST stockledger/acceptance-sessions` `{site_id, source_version_id}` | `AcceptanceSessionCreateView` `goods_views.py:499` (`stock.accept` at the PT site and the given site) | `open_session` `goods_acceptance.py:318` (SITE lock, `_check_ready`, `_check_live_source`; resumes the open session for that site and version) | `AcceptanceSession` (new or resumed) |
| Read session | `GET stockledger/acceptance-sessions/<id>` | `AcceptanceSessionDetailView` :555 (`stock.accept` or `stock.view`) | `acceptance_resource` :433 → `line_progress` :1269, `acknowledged_scan_keys` :1326, `correction_route` :517 | none |
| Scan a tag (check only) | `POST stockledger/acceptance-sessions/<id>/scan` `{observations:[{outcome:"checked_good", condition:"good", alias_value, observed_ticket_mrp_paise, qty, …}], expected_revision}` | `AcceptanceScanView` :615 | `scan` :356 → `_fresh` (replay) → `_resolve_all` :802 (line, then tag/MRP/label, then location) → `lock_lots` → `_apply` :1127 | `AcceptanceEvent` (`checked_good`), `ScanObservation`, session revision. No position moves. |
| Choose location + accept | same route, `outcome:"accepted_good"`, `location_id` | same | `_destination` :971 (non-system floor/backstore/bin/zone/fixture at the site) → `_take` → `engine.change_address` (P09) → `engine.post` | `Position` (location and `accepted_event_id` set), `JournalBatch`/`QuantityLeg`, `AcceptanceEvent`, `ScanObservation`; `acceptance_remaining` exception resolved when 0 remain (:390-397) |
| Damaged at accept | same route, `outcome:"damaged"` | same | `_apply` → P10 → `_hold_damage` :1208 | `Position` → Quarantine, condition damaged; `HoldEvent`/`ActiveHold` damage; journal; `GoodsException` `acceptance_discrepancy`/`DAMAGED_AT_ACCEPTANCE` (one per scan) |
| Wrong / unknown piece | same route, `outcome:"wrong"` | same | `_apply` :1130-1144 | `ScanObservation` and `GoodsException` `WRONG_GOODS` only. No custody and no stock. |
| Excess / over-scan / not on PT | scan with the PT tag beyond the line, or a tag no line carries | same | refused: 422 `EXCEEDS_REMAINING` (`_take` :1085) or 409 `IDENTITY_UNRESOLVED`/`NOT_ON_PT` (`_resolve_line` :852) | none (whole batch refused) |
| Hand extra pieces over | `POST stockledger/acceptance-sessions/<id>/extra` `{alias_value, qty, official_line_id?, note?}` | `AcceptanceExtraReportView` :661 | `report_extra` :562 (advisory lock per GRN) | `GoodsException` `acceptance_discrepancy`/`EXTRA_AT_ACCEPTANCE` keyed to the GRN (one per receipt), or a note event on it |
| Short / finish with pieces left | `POST stockledger/acceptance-sessions/<id>/complete` `{confirm_complete:true, reason_code?}` | `AcceptanceCompleteView` :632 | `complete` :402 (409 `SESSION_INCOMPLETE` without confirmation) | `AcceptanceSession` → completed; `acceptance_remaining` exception kept or reopened. Nothing is written off. |
| Finish (all accepted) | same, nothing remaining | same | `complete` | session completed; `acceptance_remaining` resolved |
| Stock on hand (goods) | `GET stockledger/on-hand|summary|quarantine|in-transit`, `GET stock/availability` (`/goods/stock`, `/inventory?view=location…`) | `_StockReadView` subclasses :709-776 | `goods_reads.resolve_query`, `on_hand_rows`, `summary`, `availability_rows` | none |
| Stock on hand (`/stock` → `/inventory` "On hand") | `GET /api/stockledger/on-hand` (legacy path) | `stockledger/views.py` `StockOnHandView` | `on_hand_projection.projected_rows` (goods-v1 sites read the same goods journal; `net_qty = physical_qty`) | none |
| Till "may I sell" | sale and dataset reads | sell views | `sell.services.goods_stock.read_shelf` → `engine.eligible_portions_for_skus(purpose="sell")` | none |

## 3. Correctness

| Rule | Result | Evidence |
| --- | --- | --- |
| Only goods covered by an approved PT | **Holds.** The session needs the live official receipt, opening or found version at the same site (`_check_live_source` :256). Pieces are taken only from that version's `LiveCoverage` portions that are still unaccepted (`_open_slices` :1043). The site's other 67 unvalued Receiving pieces could not be touched. | measured: over-scan 13 of a 12-piece line → 422 `EXCEEDS_REMAINING`, `quantity 12`; scan into the reversed version's old session → 409 `ACCEPTANCE_NOT_READY` |
| Scan must match the tag | **Partly.** Barcode and MRP are compared with the frozen line (`_tag_problem` :912): wrong MRP → 422 `TAG_MISMATCH`, barcode B against line A → 422 `TAG_MISMATCH`, one bad scan refuses the whole batch. But (C1) no print job is required, and (C11) any effective SKU alias passes. The MRP is typed by the person; picking a line pre-fills it wrongly (C5). | measured as stated |
| Untagged / unknown / extra piece | **Holds; refused and routed back.** An unknown tag gives 409 `NOT_ON_PT`. More pieces than the line gives 422 `EXCEEDS_REMAINING`. The screen opens the extra panel, and `…/extra` opens one GRN-keyed handoff (200, 67 q). A person may instead record outcome `wrong`: an observation plus a `WRONG_GOODS` exception, with no custody created. So a physically present unknown piece is not in any stock or quarantine record (C12 for the exception count). | measured; `goods_acceptance.py:1130-1144` |
| Partial acceptance | **Holds.** Check-only moves nothing. Completing with pieces left needs `confirm_complete` (409 `SESSION_INCOMPLETE`, "21 piece(s) are still not accepted"). After that the 21 stay valued in Receiving, unsellable, with `acceptance_remaining` open. The delivery stays at `accept` and a new session can be opened. | measured |
| What makes stock sellable | Good condition, `accepted_event_id` set, an origin, a **Floor or Backstore** location, no hold or reservation, and the site `goods_ready` and not frozen (`goods_engine.py:995-1025`). Bin, zone and fixture are only transferable (C3). | measured: after accept, Floor 1 and Backstore 9 sellable; Bin 1 11 not; damaged 1 quarantined with a hold |
| At which price | The till's price is `Origin.mrp`, which is the frozen PT MRP (`goods_pt_services.py:2506-2534`), but it is taken from the newest origin per barcode+season (C2). Today both lines are ₹1,999 and read back as 199900. | `read_shelf` mrp `[('8907264512301', 199900), ('8907264512318', 199900)]` |
| Stock value from P RATE | **Holds.** `Origin.unit_cost = calculated.p_rate_paise` (`goods_pt_services.py:2505, 2516`), with a DB check that 0 < cost ≤ MRP (`goods_models.py:167-169`). | Owner cost read: 12 pieces → 1,296,000 paise = 12 × 108,000; ticket 2,398,800 = 12 × 199,900 |
| Double acceptance | **Holds.** Accepted portions are excluded (`accepted_event__isnull=True`). Replaying a `scan_key` is a no-op (200, 56 q). Reusing a key with a different qty gives 422 `SCAN_KEY_REUSED`. A session is revision-bound and lots are locked. One open session per site and version: owner and warehouse "open" resume the same one. | measured: re-accept on fully accepted v1 → 422 `EXCEEDS_REMAINING` (`quantity 0`) |
| Reversal after acceptance | **Allowed** unless pieces were sold or reserved (`reversal_blockers` :2545). The goods are pulled from sale and booked to Receiving (C4). After reissue and approval they must be accepted again (pending showed `remaining_qty 22`). If the reissue changed the MRP, the old tags fail `TAG_MISMATCH` until relabelled, which is safe. | measured |

## 4. Duplicates

- **Business writer.** Store-receipt acceptance has one writer: `goods_acceptance._apply`. Other code also sets `accepted_event_id`, but for other sources: transfers (`outbound/transfers.py:2413`), returned pieces (`sell/services/goods_sale.py:631`), RTV returns (`goods_rtv_shipments.py:1117`), and the **test-store stock sync** (`ptmapper/stock_sync_services.py:498`, C13), which makes SOH gains sellable on the Floor without scanning. No other path accepts receipt-PT coverage.
- **Legacy stock-in.** There is no live legacy GRN-to-stock or put-away writer. `StockLedgerEntry.save`/`bulk_create` refuse every goods-v1 site with `CONTRACT_DISABLED` (`stockledger/models.py:71, 159`; `contracts.py:46-53`). Nothing in the codebase writes `PT_INWARD` any more. Legacy readers still read `PT_INWARD`: `sell/services/postings.py:272-279` (`supplier_of`), `offers/eoss_engine.py:137`, `outbound/returnable.py:69-73`. Goods-v1 acceptance never feeds them. Whether a goods-v1 sale or EOSS calculation reaches them is not verified (lead for C06).
- **Two on-hand writers.** No. A goods-v1 site has no `StockOnHand` rows. Both `/stock` and `/goods/stock` read the same goods journal (`on_hand_projection.py:30-37, 74-125`).
- **Do the screens agree?** On the total, yes. On the meaning, no (C6). Measured with the 22 accepted:

  | Screen / API | What it says for OPS-ST |
  | --- | --- |
  | `/goods/stock` summary | physical 90, valued 22, unvalued 68, ATS 22, transferable 22 |
  | `/goods/stock` on-hand rows | per location, condition and origin; accepted 12 + 10 on Floor; Receiving 16/20/23/… with `accepted 0` |
  | `/stock` → `/inventory` On hand (`/api/stockledger/on-hand`) | `units_on_hand 90`, 6 rows, 4 of them blank brand/design/sku, `identity_complete false` |
  | Till (`read_shelf`) | sellable 12 + 10 (SS26) |

  Between PT approval and acceptance, the 22 valued pieces in Receiving show in `/stock` as "On hand" with full identity, but the till cannot sell them. `/goods/movements` (`/goods-v1/outbound/movements`) is the route to move Bin 1 stock onto the Floor. Its behaviour was not measured here.

## 5. Access

| Who | Accept? | Evidence |
| --- | --- | --- |
| Store Person (`ops.store`, site 10) | Yes | step roles `stock.accept: {store_person, warehouse}` `accounts/unified_policy.py:149`; measured 201 |
| Warehouse operator (`ops.warehouse`, sites 9 and 10) | Yes at the store (C7) | measured 201 on site 10 |
| Store manager M-STR, cashier with receiving grant M-CSH | Yes, by template | `accounts/actions.py:200, 220` |
| Owner | **No** | measured 403 `ACTION_DENIED` |

- Site and brand scope apply to every session read and write (`goods_views.py:415-428`, `access.require` :519-520, :581). Tests cover a site change, a brand change and logout (`tests/test_first_store_goods_operations.py:165-181`).
- There is no maker/checker rule between preparing or sending the PT and accepting its goods (C7). The PRD names the store receiver or manager for BP-05 (`docs/prd.md:444`).
- **Cost visibility.** The acceptance DTO and the pending-acceptance DTO carry no cost or value (`goods_views.py:175, 434, 779-784`). On the stock screens, the Store Person asking `basis=cost` gets 403 `FIELD_DENIED`, and `/stock` rows omit `net_value_paise` without the `cost` grant (`on_hand_projection.py:39-41, 139-151`). The Owner sees cost and ticket value per row. The till reader returns no cost (`goods_stock.py:18-21`).

## 6. Performance (measured)

Django test client with real server sessions, `CaptureQueriesContext`, `KDPS_REHEARSAL_DB=kdps_rehearsal_screens`, one rolled-back transaction, 2 lines and 22 pieces. Times are warm, on a laptop.

| Request | Status | Queries | ms |
| --- | --- | ---: | ---: |
| Inbox pending / history, `arrival=<id>` (store) | 200 | 48 / 48 | 25 (216 cold) |
| Pending acceptance (store) | 200 | 14-17 | 7-12 |
| Locations (store) | 200 | 13 | 8 |
| **Open / resume session** (POST) | 201 | **53-54** | 24-31 |
| Session detail | 200 | 23 | 11-14 |
| **Accept scan, 1 piece → Floor** | 200 | **84** | 40-56 |
| Check-only scan / damaged / wrong | 200 | 73 / 96 / 74 | 39 / 54 / 49 |
| Refused scan (unknown tag, MRP, barcode, location, over-scan) | 409/422 | 50-56 | 26-35 |
| Replay same `scan_key` | 200 | 56 | 29 |
| 5 / 9 / 10 single-piece observations in one request | 200 | 136 / 190 / 201 | 65 / 89 / 105 |
| Complete (with or without confirm) / extra handoff | 200/409 | 58 / 46 / 67 | 19-37 |
| **Goods on-hand** (`/goods/stock`) / summary / availability | 200 | **20** / 21 / 20 | 10-16 |
| Goods on-hand `basis=cost` (Owner / Store) | 200 / 403 | 20 / 12 | 6-11 |
| `/stock` → `/api/stockledger/on-hand` (Store / Owner) | 200 | 40 / 64 | 31-46 |
| Till `read_shelf` (direct call) | n/a | 24 | n/a |
| Reverse request / approve reversal (accepted goods) | 201 / 200 | 73 / 122 | 41 / 63 |

- **Accept-step read:** inbox pending and history, pending acceptance, locations, then the open/resume POST, about **176 queries** before the first scan. Opening is a write, so every visit to the step writes a command row.
- **Per scan:** 84 queries for 1 observation, plus about 13 per extra observation in the same request. The count is constant across 3 sequential scans. Row volume is not constant: `_resolve_all`, the post-scan remaining check and `acceptance_resource` each read all of the version's `AcceptanceEvent` rows and all lines, and the response lists every acknowledged scan key (C8).
- **Estimate for 2,000 pieces (for example 100 lines × 20), not measured:**
  - one entry per piece (the PRD default): 2,000 × about 84 ≈ **170,000 queries**, about 80-110 s of server time over the session;
  - one counted entry per line: 100 × 84 ≈ 8,400 queries;
  - offline-batched (at most 500 observations per request): about 6,600 queries per request.
  
  In the per-piece case, the response grows to about 2,000 × 38 bytes ≈ 76 KB of scan keys, and about 76 MB is downloaded over the session. Event reads grow to about 3 × 2,000 rows per scan, so roughly 12 M event rows are read over the session. Two receivers in one session will hit `REVISION_SUPERSEDED` often (`goods_acceptance.py:291-295`). The screen retries a bounded number of times (`GoodsAccept.tsx:375-400`).
- **Stock reads** are 20 queries at 90 pieces. `reads.load` replays every site position in Python, so time (not query count) will grow with the size of the store. Not measured at scale. `pending_acceptance` grows with tenant history (C9). Not measured.

## 7. Tests

| Area | Tests | Run here? |
| --- | --- | --- |
| Whole-batch tag comparison before any effect (bad MRP → `TAG_MISMATCH`, no stock change) | `backend/tests/test_first_store_goods_operations.py:134` | No (needs the proof DB) |
| Replay idempotence, changed scan refused | same file :148 | No |
| Site, brand or logout change denies a scan with no effect | same file :165 | No |
| Resume an open session, completion hides it, no posting | `tests/test_first_store_acceptance_resume.py:27` | No |
| Opening and SOH acceptance end to end | `tests/test_soh_import.py:324`, `tests/test_pt_opening.py:381`, `tests/test_first_store_goods_readiness.py:98` | No |
| Damage and availability, transfer acceptance at original cost | `test_first_store_goods_operations.py:192, 283`; `test_first_store_damage.py`; `test_first_store_return_acceptance.py` | No |
| Browser: approved PT unsellable until accepted; accept 2 pieces to Floor; complete | `frontend/browser/first-store-receiving.spec.ts:770-795` (RCV-01) | No (needs the proof stack) |
| Frontend stock screens (`GoodsStock`, `StockOnHand`, `stockWorkspace`) | `frontend/src/pages/GoodsStock.test.tsx`, `StockOnHand.test.tsx`, `lib/stockWorkspace.test.ts` | **Yes, 11/11 passed** (vitest, no DB) |

pytest was not run. The proof DB on port 55433 is down and was not started.

Gaps (C10). No test (searched `backend/tests`, `frontend/src`) covers:
- `NOT_ON_PT`, `EXCEEDS_REMAINING`, `SESSION_INCOMPLETE`, the E251 extra handoff, damaged or wrong outcomes at acceptance;
- Bin vs Floor ATS (C3);
- that a print job is required (C1);
- reversal of an accepted PT (C4);
- the till price across two origins (C2);
- the line-select MRP prefill (C5).

There is no unit test for `frontend/src/pages/GoodsAccept.tsx` or `frontend/src/lib/goodsAcceptance.ts`.

## 8. PT impact

What this step means for the PT file: at acceptance the PT row stops being a document and becomes sellable stock. After this, the only correction is reverse, reissue, approve and re-accept (C4).

- **What it becomes:**

  | On the PT | Becomes |
  | --- | --- |
  | P RATE | Each piece's cost: stock value, and cost of goods sold, FIFO by origin |
  | MRP | The till price and the ticket value; also the MRP the tag must show (exact match, in paise) |
  | Barcode as used | The code the receiver scans and the till looks up |
  | SEASON | The till's season group |
  | HSN | The bill's HSN, frozen on the origin |

- **What must be right on the PT before goods hit the floor:**
  1. **MRP equals the printed tag, exactly.** Otherwise every scan is refused `TAG_MISMATCH` and the PT has to be reversed. A different MRP on a later PT of the same barcode and season changes the till price of every older piece too (C2).
  2. **P RATE is right.** The cost cannot be corrected after acceptance without a reversal, which takes the goods off sale (C4). 07-P2 noted that P RATE = BASIC × 1.2 is not enforced.
  3. **The barcode is the one on the tag**, and labels are printed before put-away for untagged goods. The server does not check printing (C1), so check that the label job was really sent.
  4. **SEASON and HSN are set**, because the till groups and taxes by what is frozen on the origin (08-A2 found blank merchandising columns).
  5. **QTY matches the count.** Acceptance cannot take more than the PT line. Extra pieces go back to the goods receipt and a supplement PT.
- **For the store:** put sellable goods on Floor or Backstore. Bin, zone and fixture stock must be moved before it can sell (C3).
