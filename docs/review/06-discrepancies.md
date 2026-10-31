# Review 06 — Screen 6 "Discrepancies" (delivery step `?step=discrepancies`)

Reviewed revision: `5a38a1f` on branch `dev`. The only uncommitted changes in the working tree are docs. Report only: no code, config or data was changed. All writes made for measuring were done inside one `transaction.atomic()` block and rolled back.
Measured on `KDPS_REHEARSAL_DB=kdps_rehearsal_screens` (synthetic). The default `kdps_local` database was never opened.

## 1. Summary

This step shows the pieces on a goods receipt (GRN) that cannot go on a PT yet, and why. It also lets staff report damage, record why fewer pieces arrived than the invoice said, and ask what should happen to disputed goods. The server already does most of the protective work. Damaged and held pieces cannot get onto an ordinary PT. Extra pieces can go only on a supplement PT. Wrong goods must be named and then approved by someone else before they can go on a PT.
The main gaps are elsewhere. Some decisions are still taken by one person alone, even though the product rules say every decision needs a second person. "Send it back" and "Dispose of it" remove goods without the proper return or disposal paperwork. Damaged goods that arrived with a delivery have no built route out of quarantine at all. Separately, the "who owns it" and "waiting for a second person" boxes can quietly go empty once the business has more records.
On the test site, every decision that needs a second person is currently refused, because no approval policy is set up there, yet the screen still offers all of them.

**Top problems**

| # | Sev | Problem |
|---|---|---|
| P1 | High | Invoice shortage, "Hold it" and "Name the goods" are decided by one person with no checker, and so is any kind whose policy says `require_distinct: false`. PRD §15.2 (OQ-61) says every disposition needs a distinct checker and must be refused when no policy is in force. |
| P2 | High | "Send it back"/"Dispose of it" is a second writer to the `returned`/`disposed` stock boundary, beside `outbound.goods_rtv`/`goods_disposal`. It creates no RTV number, vendor acknowledgement, claim or destruction evidence, and it will take good pieces that match the invoice. |
| P3 | High | Damaged pieces from receiving have no exit route. Receiving refuses to return or dispose of them and points to "tickets 15G/15E", RTV and disposal point back to "its GRN (15G/15E)", and neither ticket is implemented. |
| P4 | High | `source_document_id` accepts any receipt PT at the same site, not only this GRN's PT. A shortage decision that cites another GRN's PT is recorded but never counts against this GRN, so it can be repeated, and each repeat drafts a new debit-note line. This can only be done through the API, not the screen. Traced in code, not executed. |
| P5 | Medium | No approval policy exists on the rehearsal site. Return, dispose, accept_excess, value_damage and accept_wrong are all refused with `APPROVAL_POLICY_BLOCKED` (measured 422), but the form offers every one of them. |
| P6 | Medium | The "Waiting for a second person" panel reads only the first 50 approvals in the whole tenant and matches them to this GRN by title suffix. Older pending decisions disappear from the delivery. |
| P7 | Medium | The Owner and Due columns read the first 100 exceptions tenant-wide, including resolved ones, sorted by due date. They show the first open exception's owner on every line, so a damaged line shows `C-WHO` (the "GRN awaiting PT" owner), not its reviewer. |
| P8 | Medium | "Keep and value the damage" can be approved while the damage report is still pending. Once it is, the reviewer can no longer reject the report. |
| P9 | Medium | Invoice line cost (`invoice_basic_paise`) is sent to every reader of the GRN when the site's three-way-match switch is off, whether or not they hold the cost grant. Not measured, because the synthetic delivery has no invoice claim. |
| P10 | Low | Damage evidence attached to a report is not checked against the site and brand, unlike evidence attached to a disposition. There are also UI wording and drift issues (see sections 4, 5 and 7). |

## 2. Path map

Mounts: `/api/goods-v1/inbound/*` (`backend/inbound/goods_urls.py:84-96`), `/api/goods-v1/approvals/*`, `/api/goods-v1/exceptions`, `/api/goods-v1/outbound/damage-reports*` (`backend/outbound/goods_urls.py:272-277`). Frontend: `ReceiveDelivery.tsx:145-150` → `DispositionPanel` (`GoodsReceipts.tsx:1623`).

| User action | HTTP + route | View | Service | Written (models/tables) |
|---|---|---|---|---|
| Read discrepancies (held table, invoice compare, history, damage reports, custody moves) | `GET /goods-v1/inbound/grns/{id}` | `GoodsGrnDetailView.get` `inbound/goods_views.py:1760` | `grn_coverage` `goods_services.py:2171`, `grn_invoice` :2242, `grn_comparison` :2332, `grn_disposition_history` :2257, `grn_damage_reports` :4502, `pre_pt_custody.transfers_of_grn` | none |
| Read owner/due | `GET /goods-v1/exceptions?limit=100` | `ExceptionListView` `alerts/goods_views.py:379` | ORM, filtered client-side by `exceptionsFor` `goodsReceiving.ts:419` | none |
| Read pending decisions | `GET /goods-v1/approvals` (default page 50) | `GoodsApprovalListView` `approvals/goods_views.py:187` | in-memory filter | none |
| Report damage | `POST /goods-v1/inbound/grns/{id}/damage-reports` | `GoodsGrnDamageReportView.post` `goods_views.py:2099` (grants `movement.draft` or `receipt.disposition.decide`, `DAMAGE_REPORT_ACTIONS` `goods_services.py:169`) | `report_receipt_damage` :4389 → `_hold_receipt_damage` :4198 → `engine.post` → `refresh_receipt_discrepancy` → `damage_review.open_for_receiving` `outbound/damage_review.py:169` | `inbound_disposition` (kind `hold_damage`), stockledger Position move to quarantine/`damaged`, `ActiveHold` (`receipt_damage`), JournalBatch/QuantityLeg, `outbound_damagereport` (pending), GoodsException resolve, command/audit rows |
| Damage review confirm/reject | `POST /goods-v1/outbound/damage-reports/{id}/decide` (on `/goods/movements`, the delivery only links to it, `GoodsReceipts.tsx:883-887`) | `DamageReportDecideView` `outbound/goods_views.py:2465` (`movement.approve` + step-up, :2515-2516) | `damage_review.decide` :344 (self-review refused :448); reject → `_post_release` (REL doc, P12) + `damage_report_rejected` `goods_services.py:4486` | `outbound_damagereport` state/reviewer; on reject: REL document, holds released, Position back to receiving/good, exception reopened |
| Request hold_excess / resolve_identity / accept_shortage (no checker by default) | `POST /goods-v1/inbound/grns/{id}/dispositions` | `GoodsDispositionView.post` `goods_views.py:1976` (grant per `disposition_request_actions` :1995) | `record_disposition` `goods_services.py:4000` → `apply_disposition` :3795 directly (:4065-4079) | `inbound_disposition`; hold_excess: Position → quarantine + `ActiveHold receipt_excess`; resolve_identity: Position sku_id; accept_shortage: no stock, `debit_notes.draft_for_shortage` (:3995); exceptions refreshed |
| Request accept_excess / return / dispose / value_damage / accept_wrong | same POST | same; `accept_wrong` also accepts `movement.draft` (`DISPOSITION_REQUEST_ACTIONS` :151) | `record_disposition` → `_disposition_problems` + `_select_portions` (validate only) → `pin` (policy, else 422) → `create_request` `approvals/goods_services.py:150` | `approvals_actiondraft` (subject_kind `disposition`), `ApprovalRequest`, policy pin evidence; value_damage also opens GoodsException `approval_pending` (:4048-4057) |
| Approve / refuse a disposition | `POST /goods-v1/approvals/{id}/decide` | `GoodsApprovalDecideView` `approvals/goods_views.py:236` (step-up on approve) | `decide` `approvals/goods_services.py:348` (SELF_APPROVAL :367) → `decide_disposition` `goods_services.py:4082` (`receipt.disposition.decide` :4088, policy enforce, hash recheck) → `apply_disposition` | `ApprovalDecision`, request state; on approve the same effects as a direct decision: return/dispose end Positions at `returned`/`disposed` + release holds (:3839-3844); accept_excess releases excess hold + sets SKU + receiving (:3916-3936); value_damage writes `Origin` (VALUE_DAMAGE, `unit_cost=mrp`) + `LiveValueBasis` + ValueLeg origin_evidence→stock (:3871-3922); accept_wrong turns Position to `good`, holds kept (:3946-3960) |
| Invoice shortage decision | same POST, `kind: accept_shortage` (`ShortageForm` `GoodsReceipts.tsx:929`) | same | `_select_portions` shortage branch :3655-3667, `require_goods_site(physical=False)` | `inbound_disposition` (lot null) + draft debit note when the site switch is on |

## 3. Duplicates

| Sev | Finding | Evidence | Scenario |
|---|---|---|---|
| High | Two writers to the `returned` boundary. Receiving `return` ends custody directly, and `outbound.goods_rtv` (C09 canonical) is the RTV route with a dispatch number, vendor acknowledgement and claim linkage. The legacy `/api/outbound/rtvs*` (C09 `temporary_supported_legacy`) is a third surface. | `goods_services.py:3839-3844`; `outbound/goods_rtv.py:92`; inventory S0340/S0694-S0697 | 10 good pieces "sent back" from the delivery leave the books as `returned` with no RTV number, no acknowledgement and no credit claim. C09 says "Preserve dispatch number, source receipt… linked credit claim". |
| High | Two writers to the `disposed` boundary. Receiving `dispose` ends custody. `outbound.goods_disposal` (15D, C08 canonical) needs a numbered record, method, event time and destruction evidence, approved by the Owner. Legacy `/api/outbound/writeoffs*` (C08 legacy) also exists. | `goods_services.py:3839-3844`; `outbound/goods_disposal.py:1-30` | Pre-PT good pieces disposed of from receiving carry only `reason_code` and optional evidence. That is not equal to the 15D evidence bar. |
| Medium | Damage review: one writer (`damage_review.decide`), two entry points. The delivery lists reports read-only and links to `/goods/movements` `DamageReviewsPanel` (`GoodsMovements.tsx:333`). Damage can also be reported from the stock screen's mark-damaged (`/outbound/mark-damaged`), which uses a different hold kind (`damage` vs `receipt_damage`, `DAMAGE_HOLD_KINDS` `goods_services.py:165`). | `GoodsReceipts.tsx:835-893`; `outbound/goods_urls.py:270-277` | Not verified: pre-PT pieces reported damaged through mark-damaged may not be valuable via `value_damage`, which only values the receipt key (`_hold_key(lot,"damage")`, :3675, :4193). |
| Medium | Exceptions vs alerts (C15). This screen reads only goods exceptions. Legacy `alerts.Alert*` and `/api/alerts/*` still exist beside them, so a receipt problem is owned in one store and alerted in another. | register line 40 (C15) | Not verified whether any legacy alert is raised for receipt discrepancies. |
| Low | The client recomputes excess per line (`excessByLine`, `goodsReceiving.ts:448`) from `invoice_comparison`, alongside the server's `counted_excess`/`undecided_excess` (`goods_services.py:3106-3157`). | as cited | A server rule change (for example, decided excess) would make the "More than the invoice claimed" reason drift from the hold the server actually places. |

## 4. Correctness (server-side enforcement)

| Rule | Status | Sev | Evidence | Scenario / measurement |
|---|---|---|---|---|
| Maker ≠ checker on every decision | Partly | High | `CHECKER_KINDS` `goods_services.py:136`, `disposition_needs_checker` :2919-2933, direct path :4062-4079; PRD `docs/prd.md:1560`, OQ-61 :1701 | Measured on site 10: no policy exists, and `disposition_needs_checker` is False for accept_shortage, hold_excess and resolve_identity, so these are recorded by one person. A policy with `require_distinct:false` makes return, dispose and accept_excess one-person too. The test `test_direct_accepted_shortage_survives_reload_without_an_approval_draft` (`backend/tests/test_first_store_receiving_shortage.py:110`) treats this as intended. Where a request exists, self-approval is refused (`approvals/goods_services.py:195,367`; tested at `test_first_store_receiving_shortage.py:161`). |
| Damaged never on an ordinary PT | Yes | — | PT pool `_coverable` `ptmapper/goods_pt_services.py:765-807` (only `condition="good"`, minus every `ActiveHold`); accept_wrong skips damaged pieces and damage holds (`goods_services.py:3549-3560`, refuses `DAMAGE_STAYS_HELD`) | Measured: the damaged line shows `held 1, damage_held 1, reportable 0`. |
| Accepted excess only on a supplement PT | Yes | — | `ptmapper/goods_pt_services.py:789,1018-1019` | — |
| Wrong/unidentified need naming and a different person's accept_wrong | Yes | — | naming precondition `IDENTITY_UNRESOLVED`/`SKU_MISMATCH` :3566-3600; `ALWAYS_CHECKED_KINDS` :140; settled identity :3492; PT pool uses only approved `accept_wrong` portions (`ptmapper/goods_pt_services.py:783`) | Store request reached validation (measured 400 `No SKU has that ID` for a random SKU). |
| Disposal/return only against this GRN | Partly | High | `_disposition_problems` :3480-3489 accepts any `Purpose.RECEIPT` document at the same site; shortage remaining counts only `grn_source_documents` :3031-3040 | Physical kinds pick pieces from this GRN's own lots, which is safe. But `accept_shortage` citing another GRN's PT passes `_select_portions` (:3655-3667), is recorded with the foreign `document_id`, never reduces `remaining`, and each repeat drafts a new debit-note line (`inbound/debit_notes.py:445-452` dedupes by disposition id). Traced in code, not executed. |
| Return/dispose not for damaged pieces | Yes | — | :3704-3711, refusal `DAMAGE_OUTCOME_ELSEWHERE` :3750-3765 | Measured 403 `DISPOSITION_NOT_AUTHORISED` for owner dispose on the damaged line. |
| Return/dispose limited to discrepant pieces | No | High | `_select_portions` return/dispose branch has no excess or discrepancy check (:3700-3725) | Measured: owner "return 1 good piece" on a line with no invoice and no excess got past selection and stopped only at `APPROVAL_POLICY_BLOCKED` (422). With a policy in place it would end matched goods. |
| Count never edited | Yes | — | dispositions and reports write Positions, holds and Disposition rows only, never OfficialLine; corrections are a counter-GRN (`request_counter_grn` :2533) | — |
| Due dates/owners assigned | Yes (server) | Medium (display) | `EXCEPTION_DEFAULTS` `alerts/goods_services.py:98-100` (`grn_awaiting_pt` C-WHO 2wd, `receipt_discrepancy` C-INV 3wd, `approval_pending` C-INV 1wd); `open_exception` :275 | Measured: GRN has one open exception, `grn_awaiting_pt`/C-WHO due 2026-11-02. A damage report closes `receipt_discrepancy` (`resolved_reason="DAMAGE_REPORTED"` :4459-4464), so pending damage has no exception or owner of its own on this screen. Its owner is the reviewer queue. |
| value_damage requires a reviewed report | No | Medium | `_select_portions` needs only the damage hold (:3675, :3714-3718); rejecting is blocked once valued (`outbound/damage_review.py:465-471`) | Request and approve value_damage while the report is pending, and the reviewer's "reject" is refused with `MOVEMENT_INVALID`. The two-person damage decision is pre-empted. |
| Damage report evidence scoped | No | Low | `report_receipt_damage` checks only that the evidence exists (:4408-4410); dispositions check `covers_cell` (:3456-3474) | An evidence ID from another site or brand can be attached to a report. |
| Pending requests don't reserve pieces | Note | Low | `record_disposition` validates but holds nothing (:4011) | Two requests on the same pieces both go pending, and the second fails `DISPOSITION_STALE` at approval. This is safe, but the checker's work is wasted. |
| Measured damage flow | — | — | — | Store reports 1 damaged piece: 201, 96 queries. The line becomes `held 1, damage_held 1, reportable 9`. The store person cannot decide it (403 `ACTION_DENIED`, no `movement.approve`). The Owner confirms with step-up: 200, 45 queries. All rolled back. |

Data note: the brief says the synthetic damaged piece is "pending review". In `kdps_rehearsal_screens` that report is already `confirmed` (source `receiving`).

## 5. Access

| Decision | Who may request | Who may approve | Evidence |
|---|---|---|---|
| accept_wrong | `receipt.disposition.decide` or `movement.draft` (Store person, Warehouse) | `receipt.disposition.decide` + policy roles, ≠ maker, step-up | `goods_services.py:151`, :4088; `approvals/goods_views.py:247-255` |
| every other kind | `receipt.disposition.decide` | same as above | `disposition_request_actions` :2914 |
| report damage | `movement.draft` or `receipt.disposition.decide` | review: `movement.approve` + step-up, ≠ reporter | :169; `outbound/goods_views.py:2515-2516`; `damage_review.py:448` |

Measured grants. `ops.store`: `movement.draft`, `receive.arrival`, `approvals.view`, `exception.view`; no `receipt.disposition.decide`, no `movement.approve`; field grants `{customer}` only. `syn.owner`: decide, movement.approve, all field grants.

| Sev | Finding | Evidence | Scenario |
|---|---|---|---|
| Medium | The form offers kinds the server will refuse when no policy exists. | `dispositionKindsFor` `goodsReceiving.ts:589-593` shows every kind to the decide grant; `pin` `approvals/goods_policy.py:111-140` | Measured: Owner on site 10 gets 422 `APPROVAL_POLICY_BLOCKED` for return. The same applies to accept_excess, dispose, value_damage and accept_wrong (store included). |
| Medium | Invoice cost is shown without the cost grant when three-way match is off. | `goods_views.py:1796-1800` (`show_cost=show_value or not is_feature_on(...)`) | A store person with `receive.arrival` reads `invoice_basic_paise`. Not measured (no claim in this DB). |
| Low | The value_damage form asks for unit cost and MRP from anyone with the decide grant. The history then hides those values without the cost grant. | `GoodsReceipts.tsx:533-553`; `grn_disposition_history` :2311-2321 | A decide holder without the cost grant must type a cost they are not allowed to read back. |
| Low | Approve/Send back buttons appear for any non-maker who can view the request. They do not check the policy's approver role (`eligible_checker` is used only by the inbox). | `GoodsReceipts.tsx:1128-1165`; `approvals/goods_views.py:199-205` vs :176-178 | A C-INV with the decide grant sees Approve on an Owner-only policy and is refused on click. Not verified with a live policy. |
| Low | The store person sees "Open the damage reviews", which leads to a list they cannot decide on. The disposition form is correctly hidden from them unless wrong lines exist (`gg-decide-denied` hint). | `GoodsReceipts.tsx:883-887`, :1646-1652 | — |
| Low | Read scope uses `document.site_id`, write scope uses `document.held_site_id`. | `goods_views.py:1764-1768` vs :1993-1997, :2108-2110 | If custody moves sites, a person may be able to write but not read, or the reverse. Not verified with data (site 10 = held 10 here). |

## 6. Performance (measured)

Django test `Client` + `CaptureQueriesContext`, session cookie issued inside a rolled-back transaction, 3 runs each. The GRN has 4 lines (3 good, 1 damaged) and 1 damage report.

| Read | User | Status | Queries | Time (cold / warm) |
|---|---|---|---|---|
| `GET /goods-v1/inbound/grns/{id}` (discrepancies read) | ops.store | 200 | **57** | 188 ms / 28 ms |
| same | syn.owner | 200 | **54** | 51 ms / 25 ms |
| same, after 1 extra damage report | syn.owner | 200 | 54 | — |
| `GET /goods-v1/outbound/damage-reports?site=10` (damage review list) | ops.store / syn.owner | 200 | **13** / 13 | 7-13 ms |
| `GET /goods-v1/exceptions?limit=100` | both | 200 | 11 | 5-9 ms |
| `GET /goods-v1/approvals` | both | 200 | 17 | 8-11 ms |

Opening the step costs about 85 queries before the inbox read. The top tables are `stockledger_position` (7), `core_officialline` (6), `inbound_countergrndraft` (5), and `documenthead`/`custodylot`/`activehold` (4 each).

| Sev | Finding | Evidence | Scenario |
|---|---|---|---|
| Medium | Queries per lot inside loops. | `Position.objects.filter(lot_id=view.lot_id)` in `reportable_damage` `goods_services.py:4361`, `_select_portions` :3700, `_wrong_goods_portions` :3549; `ActiveHold` per view :3690 | The detail grows linearly with lots: a 200-line GRN would be on the order of hundreds of queries (extrapolated, not measured). |
| Medium | The approvals list loads every ApprovalRequest in the tenant, computes subject cells for all of them, and filters in Python before paging. | `approvals/goods_views.py:193-206` | Cost grows with history. It is also the P6 visibility bug (50-row page, client-side title match `GoodsReceipts.tsx:1037-1046`). |
| Low | The damage-report lookup builds an OR of JSON `lines__contains` per lot. | `goods_services.py:4518-4523` | Needs a GIN index on `lines` at scale. Not verified whether one exists. |
| Low | The history filters ActionDraft by JSON key `payload__grn_id`. | `goods_services.py:2267-2269` | Full scan of drafts as they accumulate. Not verified. |

## 7. Data

Where things live:
- **Holds:** `stockledger.ActiveHold`, kinds `receipt_damage`, `receipt_excess` and `damage`, keyed by `_hold_key(lot, kind)`.
- **Condition and location:** `stockledger.Position`.
- **Decisions:** `inbound.Disposition`, including `hold_damage` rows created by reports.
- **Requests:** `ActionDraft` (subject `disposition`) + `ApprovalRequest`/`ApprovalDecision`.
- **Damage reports:** `outbound.DamageReport`. Its `lines` JSON freezes lot, portion, hold keys and return location.
- **Owners and due dates:** `alerts.GoodsException`.
- **Damage memo value:** `Origin` (VALUE_DAMAGE) + `LiveValueBasis` + ValueLeg.
- **Shortage money:** draft debit notes (`DebitNoteSource.disposition_id`).

| Sev | Finding | Evidence | Drift risk |
|---|---|---|---|
| Medium | Directly decided dispositions (shortage, hold, name) do not appear in the screen's decision history. The history reads only ActionDrafts. | `grn_disposition_history` :2257-2329; test asserts `dispositions == []` (`test_first_store_receiving_shortage.py:124`) | Who decided a shortage cannot be seen on the delivery. The evidence exists only in `inbound_disposition` and audit. |
| Medium | The damage memo stores `Origin.unit_cost = MRP` and posts the MRP value into the operational `stock` value bucket. | `goods_services.py:3871-3912` | Any reader that treats `unit_cost` or the stock bucket as cost will value damaged pieces at MRP. The UI says "no books entry". Not verified which reports read it. |
| Medium | A damaged piece is recorded twice: `Disposition(hold_damage).portion` and `DamageReport.lines[].portions`. A rejection is undone by cross-reading the report JSON (`_rejected_damage_holds` :2989-3003). | as cited | A hand-edited or migrated report would change what counts as decided without touching the Disposition. |
| Medium | The UI links an approval to its GRN by the server title string `"<kind> on <GRN number>"`. | `goods_services.py:4043`; `GoodsReceipts.tsx:1043` | Renaming the title format, or a GRN number that is a suffix of another, breaks or crosses the link. |
| Low | The "Reported damaged, waiting for review" reason stays on the line after the report is confirmed. | `goodsReceiving.ts:435-437` keys on `damage_held_qty`, which a confirmation keeps | Measured: the line still has `damage_held 1` while the report is `confirmed`. |
| Low | The requested value (draft payload) and the frozen value (sum of Origins) are two facts. | :2286-2329 | The code handles this deliberately (it shows "Requested" vs "Frozen"). |

## 8. Tests

Backend tests that touch this screen (all under `backend/tests/`, all needing the proof DB):
- `test_first_store_receiving_shortage.py`: two tests. Shortage decided directly, and checked with an approve/reject plus SELF_APPROVAL case.
- `test_first_store_damage.py`: four tests on damage report scope, replay, independent decision and sale blocking. These are mainly stock-screen reports.
- `test_first_store_goods_operations.py:192,227`: damage availability, and reporting allowed during a count while decisions wait.

There are no tests in the repo for E254 receiving damage reports via `/grns/{id}/damage-reports`, `accept_wrong`, `resolve_identity`, `value_damage`, `return`/`dispose`, `hold_excess`/`accept_excess`, the `source_document_id` rule, or the denial matrix for this route. A grep for these names in `test*.py` found only the shortage file.

Frontend: `src/lib/goodsReceiving.test.ts` (2 tests, transfer links only) and `src/lib/goodsReceiptShortage.test.ts` (8). Nothing tests `dispositionKindsFor`, `coverableQty`, `heldReasons` or `DispositionPanel`.

Run at `5a38a1f`: `vitest run src/lib/goodsReceiving.test.ts src/lib/goodsReceiptShortage.test.ts` gave 10/10 passed. Backend tests were not run: the proof DB on port 55433 is down (`pg_isready` gave no response), and the conftest requires it, so it was not started. `check_so03_consolidation.py` was not run because no code changed.

## 9. PT impact

| Piece state on this step | Can it get a PT row? | Server evidence |
|---|---|---|
| Good, matched, uncovered | Yes, primary PT | `_coverable` `ptmapper/goods_pt_services.py:765-807` |
| Good but counted excess, undecided or held (`receipt_excess`) | No (the hold excludes it) | `sync_excess_holds` `goods_services.py:3162`; `excluded` = all ActiveHolds |
| Excess after an approved `accept_excess` | Supplement PT only, never primary | `ptmapper/goods_pt_services.py:789,1018-1019` |
| Damaged (counted or reported), confirmed or pending, valued or not | Never. Value is memo only (MRP frozen on a VALUE_DAMAGE Origin, no PT row, ATS 0) | `_coverable` `condition="good"` only; `goods_services.py:3871-3922`; disposal refuses memo value (`goods_disposal.py:538-546`) |
| Wrong/unidentified, only named (`resolve_identity`) | No | `accepted_wrong` reads only `accept_wrong` portions (`goods_services.py:3639`) |
| Wrong/unidentified, named and accepted by a second person | Yes, primary or supplement, as the named SKU. If another hold is on the piece it stays off until that hold's own route ends | `goods_services.py:3946-3960`; `ptmapper/goods_pt_services.py:782-805` |
| Returned/disposed from receiving | No (custody ended). The GRN keeps `disposed_qty` | `goods_services.py:3839-3844`, :2057 |
| Shortage | No pieces, so no PT row. The invoice/PT difference is explained by the Disposition (plus a debit note if the switch is on) | `_select_portions` :3655-3667 |

PT-file consequence: the PT file covers only good, unheld, uncovered pieces, so held quantities stay outside it until a decision. A supplement PT is the only way accepted excess reaches the file. Named wrong goods reach the file under their resolved SKU only after `accept_wrong` is approved. Damaged goods never reach a PT, and with P3 they also have no built way out of quarantine.
