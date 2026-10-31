# Review 08: Approve PT (delivery step `pt_approve`, PT Work "To approve", Action Needed)

Scope: the backend behind `/goods/receive/delivery/:id?step=pt_approve` (`PtApprovePanel`), the same panel on PT Work, and the approvals inbox. This is a report only. No code, config or data was changed. All measurements ran on `kdps_rehearsal_screens` inside one transaction that was rolled back. Before and after the run the PT head was identical (`official`, revision 9, the same live version), and there was still exactly 1 approval request (approved) and 0 pending. `scripts/check_so03_consolidation.py --check` reports "SO-03 inventory matches 3196 discovered surfaces". Consolidation items: C05 (SO-07, the PT document) and C15 (SO-14, the approval stores and inbox). Review 07 covers preparation and the PT columns. Its findings are cited by ID (07-P1 … 07-P13) and not repeated.

**Facts that differ from the brief.** PT `cea020ec-…` is no longer a draft. On 2026-10-31 the following happened:

- 20:22:34 UTC: a tenant-wide `approval` policy for `pt.approve.receipt` was published as version 1, approved by `syn.owner`. Its payload: `roles: ["owner"]`, `require_distinct: true`, `step_up: true`, no quantity limit and no value limit.
- 20:23:36: `ops.warehouse` sent the PT.
- 20:24:01: `syn.owner` approved it.

The PT is now **SYN-OPS/RPT/26-27/000001 v1**. It has 2 lines and 22 pieces, a pinned value of ₹23,760, and was made under direction `both_supplied`. The receipt send is therefore no longer blocked at OPS-ST. Who published the policy is not verified; the seeds do not do it for this tenant. To measure send and approve without touching the real PT, the run did all of the following inside the rolled-back transaction: request a reversal, approve it, reissue, review, send, approve again, then export. A reversal needs its own policy, and none exists, so one was added inside the transaction.

## 1. Summary

After the warehouse prepares a PT and sends it, a different person (by default the Owner) approves it. That approval gives the PT its official number, freezes every price on it, and puts a cost value on the counted goods. The safety rules hold: the person who prepared, reviewed or sent a PT cannot approve it by any route, an approved PT cannot be edited, and what gets approved is exactly what was sent. The main problem is the approval screen: it shows no PT lines and no value. The Owner therefore approves without seeing MRP, BASIC, P RATE, tax, season or item details. The rehearsal PT was approved with COLOR, GENDER, SUB CATEGORY, TYPE, ITEM and FIT all blank. A wrong official PT also cannot be corrected here: reversal needs a second approval policy that nobody has set up.

Top problems:

| # | Sev | Problem | Evidence |
| --- | --- | --- | --- |
| A1 | High | The approval panel shows no PT rows. Its "Value band" is always blank, because the receipt reconciliation sets `value_paise: null`. The real value (pieces × P RATE) is stored only in `policy_basis`, which the API never returns. | `frontend/src/pages/PtApprovals.tsx:88, 166-378` (lines are used only for the config badges); `backend/ptmapper/goods_pt_services.py:1150`; measured: DTO `totals.value_paise = None` while `policy_basis.value_paise = 2376000` (₹23,760) |
| A2 | High | Approval does not require the PT's merchandising columns. The profile's `columns` is empty, so a line with no attributes passes. Official v1 went out with COLOR, GENDER, SUB CATEGORY, TYPE, ITEM and FIT blank on both lines. | `goods_pt_services.py:1366-1410` (only SKU family and defining attributes are checked); measured: official lines `attributes: []`, export cells `colour…fit = None` |
| A3 | High | No `pt.reversal.approve` policy exists in the rehearsal DB, and the test-store seed publishes none. So an official PT cannot be reversed, and therefore cannot be reissued or corrected. | measured `reverse` 422 `APPROVAL_POLICY_BLOCKED` (55 queries); `backend/accounts/test_store_loader.py:389-390` seeds opening, receipt and transfer only |
| A4 | Medium | With no receipt policy, "Submit for approval" is refused only at the very end, after every row has been reviewed. The screen gives no warning beforehand, and site readiness lets an owner override the missing policy. | `goods_pt_services.py:2140` (pin runs after `validate_rows` and `reconcile_receipt`); `masters/goods_services.py:628-633` (`overridable=True`); `PtPrepare.tsx:960-981` (generic error only) |
| A5 | Medium | The inbox reads every pending request in the tenant, at about 2 queries per request, before it paginates. The screens read only the first 100, oldest first, and never follow `next_cursor`. A PT queued behind 100 older approvals of any kind never appears in PT Work, on the delivery step or in Action Needed. | `approvals/goods_views.py:170-180`; `goods_policy.py:191`; `goods_services.py:477`; `PtApprovals.tsx:408`; `GoodsApprovalRows.tsx:27`; measured: 26 → 59 → 219 queries with +0/+20/+100 pending; a page of 100 older counter-GRN requests with `next_cursor` set |
| A6 | Medium | Home's "Waiting for your approval" tile counts only the legacy `/api/approvals/inbox`. PT approvals never add to it. | `frontend/src/pages/Home.tsx:363`; `backend/approvals/views.py:51-63` |
| A7 | Medium | A returned ("Sent back") PT notifies role `C-WHO`, which no current assignment carries, so nobody is told. The screen also always sends reason `NOT_RIGHT`, and the approver cannot say what is wrong. | `goods_pt_services.py:2335`; `PtApprovals.tsx:102`; measured: recipients for `["C-WHO"]` = 0, for `["warehouse"]` = 1 |
| A8 | Medium | The XLSX export of a reversed PT, asked for without `version`, returns the reversed v1 as a plain "PT" workbook. Nothing in the file says it was reversed. | `goods_pt_views.py:1826, 1854`; measured 200 in state `reversed` |
| A9 | Medium | "Reissue" on the screen posts back the detail page's rows. Those are at most 500 per page and are stripped of cost for a reader without cost. Reissue replaces every line, so a PT over 500 lines, or one reissued by someone who cannot see BASIC, loses lines or BASIC. | `PtApprovals.tsx:149`; `PtPrepare.tsx:991`; `goods_pt_views.py:49`; `goods_pt_services.py:2704-2709`; measured OK at 2 lines; not verified above 500 |
| A10 | Medium | The PRD's approver setting ("office, warehouse or either", plus an optional warehouse check before the office) is not available for receipt PTs. A multi-step route is refused. Naming `warehouse` in the policy does nothing unless the role's step actions are also edited. The decision never requires the cost field. | `docs/prd.md:2164`; `approvals/goods_policy.py:158`; `accounts/unified_policy.py:174-202` |
| A11 | Medium | The self-approval check counts the preparer, the document maker, the row reviewers and the sender. It does not count someone who only edited rows. A person holding both `pt.prepare` and `pt.approve.receipt` (for example the Owner, given prepare as in 07 option (c)) could edit prices and then approve them. | `goods_pt_services.py:2243-2244`; not verified by a run (needs a non-default role setup) |
| A12 | Medium | No backend test sends, approves, rejects, reverses, reissues or exports a receipt PT. Only the browser proof RCV-01 approves one. | §8 |
| A13 | Low | The Store Person is offered "Request reversal" on an official PT. The Owner is not. | `unified_policy.py:40`; measured `allowed_actions` store `['export','reverse']`, owner `['export']` |
| A14 | Low | A reviewer who did not press send still sees the PT in their inbox and an Approve button, which then answers `SELF_APPROVAL`. | `frontend/src/lib/goodsPt.ts:316-321`; `goods_policy.py:195-197` (no `maker_ids` in the receipt basis) |
| A15 | Low | The HSN format check (`refuse_missing_hsn`) runs at approval only, so the checker finds a problem the preparer was never shown. | `goods_pt_services.py:2348` vs `submit` :2101-2165 |
| A16 | Low | The number comes from an entity-wide series in the financial year of the approval date, not the GRN date. | `core/numbering.py:109-111`; measured series `RPT 26-27 E4` |
| A17 | Low | A print job accepts any official version, not only the live one, so a reversed version may still print labels. | `ptmapper/goods_print_services.py:53-61, 238-260`; not verified by a run |

## 2. Path map

Routes are under `/api/goods-v1/`. Every write is one `execute_command`, which writes the command key, the attempt, the audit and the sealed evidence. Both approve routes call the same `approvals.goods_services.decide`, so there is one business writer.

| Action | Method + route | View | Service | Models/tables written |
| --- | --- | --- | --- | --- |
| Submit / send | `POST ptmapper/files/<id>/send` `{reviewed_hash}` | `GoodsPtSendView` `goods_pt_views.py:1368` (`pt.prepare`, every cell) | `pts.submit` `goods_pt_services.py:2101` → `_require_reviewed` → `checked_lines` → `validate_rows` → `reconcile_receipt` → `goods_policy.pin` :2140 → `set_state` → `create_request` | `DocumentHead` (submitted), `DocumentEvent` `submitted`, `ApprovalRequest` (any earlier one superseded, with an `ApprovalDecision` `superseded`), `grn_awaiting_pt` exception resolved |
| Recall | `POST ptmapper/files/<id>/recall` `{reason_code}` (no UI, 07-P11) | `GoodsPtRecallView` :1412 | `pts.recall` :2198 (preparer only, :2202) | `ApprovalRequest` superseded and its decision, `DocumentHead` draft, event `withdrawn` |
| Edit while submitted | `PATCH ptmapper/files/<id>/rows` | `GoodsPtRowsView` | `edit_rows` :1679 supersedes the request | new revision; the request is superseded, so the approver sees `REVISION_SUPERSEDED` |
| Approve (E234) | `POST approvals/<request id>/decide` `{decision:"approve", reviewed_hash, expected_revision}`, with step-up | `GoodsApprovalDecideView` `approvals/goods_views.py:213` (step-up :255) | `decide` `approvals/goods_services.py:348` → `_approve_receipt` `goods_pt_services.py:2320` → `enforce` (policy) → `check_site_for_goods` → `checked_lines` → `refuse_missing_hsn` → `validate_rows` → `lock_lots` → `reconcile_receipt` → `allocate` :2373 → `officialise` → `_value_and_cover` :2412 → `open_exception` | `ApprovalRequest` approved and `ApprovalDecision`; `core_voucher_series.next_seq`; `DocumentIdentity.official_number` (first time only); `OfficialVersion`, `OfficialLine`; `DocumentHead` official; events `officialised` and `posted`; stockledger `Origin` per line, cover `CoverageEvent`, `LiveCoverage` and the P04 journal batch; `acceptance_remaining` exception; possibly a ceiling `OutboxIntent`. No GL, vendor or cash rows (nothing in `goods_pt_services`/`goods_engine` imports finledger). |
| Approve (E129 adapter) | `POST ptmapper/files/<id>/post` `{reviewed_hash}` | `GoodsPtPostView` :1548 (step-up :1581) | finds the pending request and calls the same `decide` | same |
| Refuse / return | `POST approvals/<id>/decide` `{decision:"reject", reason_code}` (screen: "Send back") | same | `_approve_receipt` reject branch :2324-2337 | `DocumentHead` draft, event `rejected`, notification outbox (role `C-WHO`, A7), request rejected and its decision |
| Request reversal | `POST ptmapper/files/<id>/reverse` `{reason_code, evidence_ids}` | `GoodsPtReverseView` :1449 (`pt.reversal.request`) | `request_reversal` :2570 → `_refuse_unsafe` → `pin(pt.reversal.approve)` | `ApprovalRequest`; events `reversal_requested` and `reversal_evidence` |
| Approve reversal | `POST approvals/<id>/decide` | `GoodsApprovalDecideView` | `_approve_reversal` :2634 → `lock_lots` → `_refuse_unsafe` → `engine.counter_coverage` (P06) → `sync_excess_holds` | counter `CoverageEvent`, `LiveCoverage` deleted, positions moved back to receiving or quarantine, P06 journal batch, holds, `DocumentHead` reversed (`live_version` null), event `reversed`, exception resolved |
| Reissue | `POST ptmapper/files/<id>/reissue` `{corrected:{header,lines}, reason_code}` | `GoodsPtReissueView` :1516 (`pt.prepare`) | `reissue` :2683 → `price_line` → `append_revision(replace_lines, reviewed_rows=[])` | `DraftRevision`, `DraftLine`, `DocumentHead` draft (same number), event `reissue_started` |
| Export official PT | `GET ptmapper/files/<id>/export[.xlsx][?version=n \| ?draft=true]` | `GoodsPtExportView` :1669 | `_export_version` :1821, `CanonicalCells`, `_audit_export` :1780 | one audit/command row only |
| Approvals inbox | `GET approvals/inbox?limit=100` | `GoodsApprovalInboxView` `approvals/goods_views.py:164` | `subject_cells_many` → `eligible_checker` per row → `paginate` → `parent_documents` → `approval_dto` | none |
| "Your PT approvals" | `GET approvals?limit=100` | `GoodsApprovalListView` :187 | the same, over every request ever made in the tenant | none |
| Legacy inbox (Home tile, Action Needed "older") | `GET /api/approvals/inbox`, `POST /api/approvals/<int>/decide` | `approvals/views.py:51, 123` | legacy `approvals.Approval` | no PT rows |

## 3. Approval policy

**What it is.** A receipt approval policy is a configuration version of kind `approval` (`masters/goods_services.py:1437-1459`). Its payload holds:

- `action: "pt.approve.receipt"`, plus `roles`, an optional `steps` list, an optional `purpose`, `site_ids` and `brand_ids` (each must equal the version's scope).
- `require_distinct`, which cannot be `false` for this action (`goods_services.py:1733-1739`, `accounts/actions.py:381-391`).
- Optional `qty_max` and `value_max`, both inclusive limits.
- `step_up` (password confirmation) and `unknown_value` (`refuse` by default, or `quantity_only`).

There is one limit per version and no tiers. This is the PRD rule (`docs/prd.md:1621`). The value judged is pieces × the pinned P RATE, which is cost (`goods_pt_services.py:2167-2177`). Exactly one version may be in force for (action, scope, time). Overlaps fail closed.

**Where it is configured.**

- Screen: Setup → Configuration (`/setup/configuration`, kind "Approval policy", `FirstStorePolicyEditor`; template in `frontend/src/pages/Configuration.tsx:122-130`).
- API: `POST /api/goods-v1/masters/configurations` (draft), then `…/configurations/<id>/submit`. A distinct Owner then decides it as `config.approve` through `approvals/<id>/decide`.
- There is also a per-action policy draft writer (`masters/goods_views.py:4115-4160`) and admin policy lists (`accounts/goods_admin_views.py:1267-1288`). Whether those two are mounted is not verified.
- Seeds: `accounts/test_store_loader.py:389-400` publishes, for test stores, receipt, opening and transfer policies with roles `["owner"]`, a quantity limit of 1,000,000 and a value limit of 100,000,000,000 paise (₹1,000,000,000, in effect no limit). It publishes **no `pt.reversal.approve` policy** (A3).
- The rehearsal DB has one receipt policy, tenant-wide, with no limits (see the scope note).

**With no policy.** Yes, send is refused: 422 `APPROVAL_POLICY_BLOCKED`, issue `CONFIG_MISSING`. The message reads "The approval configuration has no effective approval configuration for this site, brand and purpose." This is the PRD rule (`prd.md:1621`). Review 07 measured it for send. Here it was measured for reversal (55 queries). A band failure gives the same code with `QTY_ABOVE_LIMIT`, `VALUE_ABOVE_LIMIT` or `VALUE_UNKNOWN`. At decision time the pinned policy is locked and checked again. If it has been superseded, withdrawn, expired, has lost scope or has become ambiguous, the result is 409 `APPROVAL_STALE` and the preparer must send again (`goods_policy.py:239-289`, `enforce` :292).

**Who may approve.** The checker must meet all of these, inside the deciding transaction:

1. Hold a role named in the policy.
2. That role must carry the step action `pt.approve.receipt`. Only the Owner does by default (`unified_policy.py:174-202`; measured owner yes, warehouse no).
3. The grant must cover every (site, brand) cell of the PT (`require_complete_scope`, `pt_cells`).
4. The checker must not be the sender (`goods_services.py:367`), the preparer, the document maker or any row reviewer (`goods_pt_services.py:2243-2244`).
5. A step-up must have been confirmed recently. The views always require it for an approve, whatever the policy says.

Measured outcomes:

- The warehouse preparer through both routes: 403 `ACTION_DENIED`.
- The same person after `pt.approve.receipt` was added to the Warehouse role inside the transaction: 403 `SELF_APPROVAL` through both `decide` and `post`.
- The Store Person: 404 `NOT_FOUND`.

**Cost visibility.** The decision does not require it. `pin` sets no `fields`, so `_check_approver` checks no field grant (`goods_policy.py:222`). The Owner has `cost`. A policy naming a role without cost would let someone approve without ever seeing BASIC or P RATE (A10).

**Value bands.** A qty and value limit are supported, but the screen cannot show them. Its "Quantity band" and "Value band" are the reconciliation's proposed pieces and a value that is always null (A1). The thresholds sit in `policy_basis.thresholds`, and the DTO does not return them.

**Is the refusal clear before the preparer does the work?** No.

- The policy check is the last step of `submit`. It runs after every row is reviewed and reconciliation passes (`goods_pt_services.py:2140`).
- The PT editor has no pre-check and shows only the server message (`PtPrepare.tsx:960-981`).
- Site readiness reports "no receipt PT approval policy for …", but marks it overridable (`masters/goods_services.py:628-633`), so a site can be made goods-ready without one (A4).

## 4. Correctness

| Question | Finding | Sev | Evidence |
| --- | --- | --- | --- |
| Is the frozen revision exactly what was reviewed? | Yes. Send requires `reviewed_hash == revision.content_hash`. Decide requires the request's hash, and the handler requires the head to be `submitted` with that same draft hash. Any edit after send supersedes the request. Approval reads the immutable `revision_lines` of that revision. The hash covers header and lines but not the review marks; reviewers are read from the same revision. | — | `goods_pt_services.py:2109-2110, 2238-2241, 1679-1680`; `core/goods_documents.py:157-160` |
| Are values re-derived or frozen? | Every line is re-priced under its pinned profile, rates and tax at the arrival time. Any difference refuses (`ROW_STALE` → `PT_ROWS_INVALID`), so nothing is silently re-derived. The stored values, minus `issues`, are frozen as `OfficialLine` and as `Origin.unit_cost = P RATE` and `mrp`. Labels, names and SUGGESTED hints are still re-read at export (07-P7). | — | `:1479-1497`, `:2376-2387`, `:2497-2542` |
| Season / booking check at approval | Not done. Approval re-runs the same `validate_rows` as send: a real season is required, but there is no booking match and Closed seasons are allowed (07-P6). | Medium (07) | `:2349-2355` |
| Merchandising columns | Not required (A2). | High | |
| Can the preparer approve by any route? | No. Both routes go through `decide` and the handler's maker check (measured above). The auto path `decide_without_second_person` has no rule registered for `pt.approve.receipt`, so it refuses `AUTO_APPROVAL_NOT_ALLOWED`. Only opening PTs have one (`opening_auto.py:61-67`). Gap: a person who only edited rows is not counted as a maker (A11). | Medium | `approvals/goods_services.py:279-306` |
| Two roles / Owner preparing | The Owner holds no `pt.prepare` (07). If it were added (07 option (c)), an Owner who prepares is refused as approver. This tenant has 2 owner assignments, so the other Owner could approve. With a single Owner, nobody could. | — | measured: 2 owner assignments |
| Right number series? | `RPT` per legal entity, per financial year of the approval date: `SYN-OPS/RPT/26-27/000001`. The number is given once. Reissue keeps it (measured v2 = same number), and a refusal later in the command rolls back the series increment. Approval refuses `SERIES_NOT_READY` if the series or its published ceiling is missing. | Low (A16) | `core/numbering.py:109-138`; `goods_pt_services.py:2373` |
| Is the approved PT immutable? | Through the app, yes: rows edit on an official PT gives 409 `OFFICIAL_IMMUTABLE` (measured), and the official tables are append-only evidence rows. Database-level proof (R-PT-008, OQ-33) is not verified here. | — | `:1547-1549` |
| Approval ≠ acceptance | Correct. The goods stay held, an `acceptance_remaining` exception opens, and the UI links to held stock. | — | `:2400-2408` |
| Reversal | Needs `pt.reversal.request` (receive-goods operate: Store Person and Warehouse, not the Owner, A13) and a separate `pt.reversal.approve` policy (none exists, A3). It refuses when any covered portion was sold, dispatched, returned to the vendor, adjusted, written off or disposed of, or is reserved. It returns put-away goods to receiving or quarantine and keeps origins and number. Measured: decided 200, state `reversed`. It does not void printed labels (not verified). | High (A3) | `:2545-2567, 2634-2680`; `stockledger/goods_engine.py:1341-1361` |
| Reissue | Only from `reversed`. Takes corrected lines (any profile and direction the profile allows), re-prices them, clears review marks and keeps the number. It then needs fresh review, send and a distinct approval (measured: v2, same number, 87 queries). The UI body is lossy above 500 lines or without cost (A9). | Medium | `:2683-2717` |
| Repeat approval (R-PT-007) | Idempotent by `command_id` through `execute_command`. A second decide on the decided request gives `STATE_CONFLICT`. Not measured. | — | `core/commands.py:535`; `approvals/goods_services.py:258-262` |

## 5. Duplicates

| Area | Finding | Sev | Evidence |
| --- | --- | --- | --- |
| Two approval stores | Receipt PTs use **only** goods-v1 `ApprovalRequest` and `ApprovalDecision`. Legacy `approvals.Approval` and `ApprovalPolicy` still serve offers and vendors, and list a "PT reversal" family label that nothing uses. There is no legacy PT approval path. This is C15. | — | `approvals/models.py:548`; `offers/approval.py:34`; `vendors/approval.py:18` |
| Two approve routes | `files/<id>/post` (E129) is a thin adapter over the same `decide` used by `approvals/<id>/decide` (E234). Both use the same command action `approvals.decide.pt.approve.receipt`, so there is no second writer. Measured 175 vs 170 queries. The screen uses only E234. | Low | `goods_pt_views.py:1548-1609` |
| Two policy stores | Goods-v1 `ConfigVersion(kind="approval")` vs legacy `approvals.ApprovalPolicy`. Only the first governs PTs. A Setup user might configure the wrong one; no legacy policy screen was found in the frontend. | Low | |
| Inbox surfaces | (1) PT Work "To approve" and the delivery step `pt_approve` (the same `PtApprovePanel`) read the goods inbox and list, capped at 100. (2) Action Needed merges the legacy and goods inboxes, also capped at 100 (A5). (3) The Home tile counts the legacy inbox only (A6). So Action Needed **does** show PT approvals, but Home does not. | Medium | `PtApprovals.tsx:388-430`; `Approvals.tsx:62-66`; `Home.tsx:363` |
| Transfer and opening PTs | They use the same panel (`PT_ACTIONS` includes opening and reversal). Opening has an auto-approval rule for test stores. Not reviewed further (C01/C07). | — | `PtApprovals.tsx:45` |

## 6. Access

- **Approval queue.** `eligible_checker` shows a pending request to a person who covers every cell with the action, through one of the policy roles, and who is not the sender. It also shows it to reviewers (A14). "Your PT approvals" shows requests the person made, or may view with `approvals.view` and the action. The screen gate is `pt.approve.*` or `pt.prepare*`. Measured:
  - Warehouse inbox with its own PT pending: not listed (22 queries).
  - Owner inbox: listed.
  - The maker's name is hidden unless the reader has the `personal` field. The Owner saw "Operations Warehouse Operator".
- **Cost shown to the approver.** The detail read returns BASIC, P RATE and MARGIN to the Owner (it has `cost`), but the approval panel renders none of them (A1). The DTO's reconciliation value would appear only with `cost`/`layer_value`, and it is null anyway.
- **Official export.** `GET …/export` (JSON) and `…/export.xlsx`. Default is the live version; `?version=n` gives a past version; `?draft=true` gives a labelled preview. Each export writes an audit row with `cost_fields`. Measured columns:
  - Owner and preparing Warehouse: the 22 KDPS columns in order, SEASON … MARGIN, SUGGESTED SUB CATEGORY, SUGGESTED TYPE.
  - Store Person: 19 columns, with BASIC, P RATE and MARGIN left out.
  - Official v1 line 1: Spring/Summer 2026, Van Heusen, size L, barcode 8907264512318, design VHSF325001, HSN 6205, QTY 12, MRP ₹1,999, BASIC ₹900, P RATE ₹1,080 (= BASIC × 1.2), tax 5/5, NAG 12, MARGIN 45.97, with COLOR, GENDER, SUB CATEGORY, TYPE, ITEM and FIT blank.
- **The screen's export buttons** appear only while the PT is `official` (`PtApprovals.tsx:341`). The API still serves a reversed version as an ordinary "PT" file (A8).

## 7. Performance (measured)

Django test client with real server sessions (step-up set), `CaptureQueriesContext`, `KDPS_REHEARSAL_DB=kdps_rehearsal_screens`, one rolled-back transaction, PT with 2 lines and 22 pieces. A `pt.reversal.approve` policy was added inside the transaction (none exists). The receipt policy already existed.

| Request | Status | Queries | ms |
| --- | --- | ---: | ---: |
| Inbox, Owner, 0 pending | 200 | 17 | 9–177 (cold) |
| List `approvals?limit=100`, Owner (1 request in tenant) | 200 | 25 | 36 |
| PT detail (any reader) | 200 | 26 | 14 |
| Export JSON / XLSX, official v1 (Owner, Warehouse, Store) | 200 | 49 / 49 | 21–50 |
| Reverse request, no reversal policy | 422 `APPROVAL_POLICY_BLOCKED` | 55 | 35 |
| Reverse request, policy added | 201 | 73 | 38 |
| Decide reversal (Owner) | 200 | 123 | 69 |
| Export after reversal (JSON / XLSX) | 200 | 50 / 50 | 24 |
| Reissue (UI body) | 201 | 87 | 42 |
| Rows review marks | 200 | 88 | 42 |
| **Send** (submit for approval) | 200 | **113** | 54 |
| **Inbox**, Owner, 1 pending PT | 200 | **26** | 12 |
| List, Owner / Warehouse | 200 | 31 / 29 | 14–16 |
| Decide by the Warehouse preparer (no approve action) / Store Person | 403 / 404 | 23 / 23 | 10–11 |
| Decide / post by the preparer with `pt.approve.receipt` granted | 403 `SELF_APPROVAL` | 50 / 54 | 21–24 |
| **Approve** via `files/<id>/post` (E129, then rolled back to a savepoint) | 200 | 175 | 90 |
| **Approve** via `approvals/<id>/decide` (E234) | 200 | **170** | 87 |
| **Official export** v2 JSON / XLSX; XLSX `?version=1` | 200 | **49** / 49 / 50 | 24–30 |
| Rows edit on the official PT | 409 `OFFICIAL_IMMUTABLE` | 45 | 20 |
| Inbox with +20 / +100 pending requests | 200 | 59 / 219 | 27 / 93 |
| Inbox with +120 older non-PT pending requests | 200 | 239 | 99 (page of 100, 0 PT, `next_cursor` set) |

- Inbox cost grows by about 1.9 queries per pending request in the tenant, before pagination: the `decisions` read in `eligible_checker` (`goods_policy.py:191`) and in `approval_dto` (`goods_services.py:477`). Every pending request is loaded into Python (`goods_views.py:170-174`). Sev Medium (A5).
- The list endpoint loads every request ever made in the tenant, decided ones included, before pagination (`goods_views.py:193-206`). Measured only at 1 request. Its growth is not measured.
- Approve at 170 queries for 2 lines. How that grows with line count was not measured: `_value_and_cover` records one origin per line and covers per interval (`goods_pt_services.py:2430-2457`), so growth with lines is likely. Not verified.

## 8. Tests

| Area | Tests | Run here? |
| --- | --- | --- |
| Self-approval and approval decision for other subjects (damage, transfer) | `tests/test_first_store_goods_operations.py:192-224`; `test_warehouse_manager_access.py:104-180` | No (needs the proof DB) |
| Role step actions: Owner holds `pt.approve.receipt`, Warehouse does not | `tests/test_so03_denials.py:1080-1084`; `test_role_assignment_migration.py:218-373` | No (DB) |
| Receipt policy fixture and readiness | `tests/test_first_store_goods_readiness.py:89`; `test_first_store_online.py:83`; `test_first_store_new.py:170` | No (DB) |
| Opening PT approval and auto-approval | `test_opening_auto_approval.py`, `test_first_store_opening_authority.py:111-127`, `tests/first_store_goods.py:158-175` | No (DB) |
| Browser: Warehouse prepares a PT from the GRN, `SELF_APPROVAL`, Owner approves on `step=pt_approve` | `frontend/browser/first-store-receiving.spec.ts:650-765` | No (needs the proof stack) |
| Frontend approval helpers (`isLikelySelfApproval`, `reconciliationView`, ordering) | `frontend/src/lib/goodsPt.test.ts` | **Yes, 9/9 passed** (vitest, no DB) |

pytest was not run. The proof DB on port 55433 is down and was not started.

Gaps (A12): no backend test calls `files/<id>/send`, `recall`, `post`, `reverse`, `reissue` or `export`. None covers receipt-PT `_approve_receipt`, its rejection, `request_reversal`/`_approve_reversal`, `reissue`, a missing or stale receipt policy, a value band, the row-editor-as-approver case (A11), the inbox cap (A5) or the rejection notification (A7).

## 9. PT impact

- **What becomes official and frozen at approval.** The exact submitted revision becomes official version *n*, carrying its number (`ENTITY/RPT/YY-YY/nnnnnn`). Each line's SEASON id, SKU and attributes, barcode as used, design, HSN, QTY, supplied MRP and BASIC, and calculated P RATE, MRP, BASIC, input and output tax, margin and direction are frozen, together with the pinned profile, rate and tax version ids. NAG is not stored: the export writes QTY into it. One cost origin per line is created with unit cost = P RATE and the frozen MRP. Brand, season and attribute **names** and the SUGGESTED columns are still read from today's masters at export (07-P7).
- **When labels can print.** Only after approval, from official lines (`goods_print_services.py:238-260`). Goods stay unsellable until they are accepted. After a reversal, the old version's lines may still be printable (A17, not verified).
- **What the approver should check** (the screen does not show it today; A1). Open the draft export (`…/export.xlsx?draft=true`) or PT Work, and check:
  - every line has SEASON matching the booking, plus COLOR, GENDER, SUB CATEGORY, TYPE, ITEM and FIT;
  - BASIC equals the invoice, and P RATE = BASIC × 1.2 (07-P2: not enforced);
  - MRP and the tax slab are right (07-P5: input tax always equals output tax);
  - the QTY reconciliation (counted, proposed, held) adds up;
  - the total cost value is plausible.

  After approval nothing on the PT can be edited. The only fix is reverse, then reissue, then approve again.
- **What the owner must set up.**
  1. A receipt approval policy for each site and brand. The rehearsal tenant-wide one has no limits; decide whether to set a quantity or value limit and `unknown_value`.
  2. A **`pt.reversal.approve` policy**. Without it no official PT can be corrected (A3).
  3. A numbering series and a published ceiling for `RPT` for each entity and financial year.
  4. A profile that requires the PT's merchandising columns (A2; 07-P8).
  5. Decide the approver setting in B.5 rule 3 (office, warehouse or either, plus the optional second check). Code for it does not exist yet (A10).
  6. Do not override the "no receipt PT approval policy" readiness gap (A4).
