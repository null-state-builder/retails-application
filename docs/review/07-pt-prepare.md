# Review 07: Prepare PT (delivery step `pt_prepare`, PT Work)

Scope: the backend behind `/goods/receive/delivery/:id?step=pt_prepare` (`GrnStart` → `PtEditor`) and `/goods/pt-work`. This is a report only. No code, config or data was changed. Every measurement ran inside a database transaction on `kdps_rehearsal_screens` that was rolled back. After the runs the DB still holds 1 PT, 3 PT configs and 2 live warehouse assignments for `ops.warehouse`. `scripts/check_so03_consolidation.py --check` reports "SO-03 inventory matches 3196 discovered surfaces". Consolidation item: C05 (SO-07); transfer PT is C07 (SO-08).

## 1. Summary

The PT file is the price ticket list for a delivery. The app builds it from the goods that were counted, and someone fills in season, tax code, cost and selling price for each line. Then a different person approves it. In the seeded setup nobody can prepare the PT for goods delivered straight to a store. Preparing is a Warehouse job, and the warehouse login is only set up for the warehouse. The Owner and the store person are not allowed to prepare, and the product rules leave this question open. The app does not force P RATE = BASIC × 1.2. The 20% is a setting that can be anything. One pricing method also sets BASIC equal to P RATE, and it can be switched on for a single row even where the settings forbid that. Some of the owner's other rules are also not checked: the season does not have to match the booking, and input tax always equals output tax.

Top problems:

| # | Sev | Problem | Evidence |
| --- | --- | --- | --- |
| P1 | High | No seeded role setup lets anyone prepare a PT for goods received at a store. The only route, a Warehouse assignment at the store, also grants counting, receiving, accepting and transfers there. | `backend/accounts/unified_policy.py:150`; `backend/accounts/goods_demo.py:219-225, 654-669`; measured: Owner and Store Person get 403 `ACTION_DENIED`, seeded-scope warehouse gets 404 `NOT_FOUND` |
| P2 | High | P RATE = BASIC × 1.2 is not enforced. `transport_pct` is free tenant config, `ticket_to_purchase` sets BASIC = P RATE, and a per-row `direction_override` is honoured even though the profile says `allow_row_override: false`. | `backend/ptmapper/goods_calc.py:260`; `goods_pt_services.py:365, 377`; `masters/goods_services.py:1385` (validated, never read); measured: row BASIC ₹1,903.81 = P RATE, while the supplied BASIC was ₹1,000 |
| P3 | High | The requested GRN can't get a PT in the rehearsal DB, and no PT there could be sent. The only profile became effective after the goods arrived (`CONFIG_FUTURE`), and there is no receipt approval policy at OPS-ST (`APPROVAL_POLICY_BLOCKED`). | measured `create_wh_as_is` 422, `send` 422 |
| P4 | High | The code's idea of the "right" factor disagrees with the owner. Product Lists treats 1.1 as expected, and the test-store loader and proof scripts seed transport 0 (P RATE = BASIC). | `backend/masters/master_sheet_services.py:69-71`; `backend/accounts/test_store_loader.py:309-310`; `scripts/first-store-opening-proof.py:73` |
| P5 | Medium | INPUT TAX always equals OUTPUT TAX: both come from one slab picked by MRP, with no override. The KDPS sheet works out input tax from BASIC, and Appendix A allows them to differ and be overridden. | `goods_pt_services.py:241-251`; `goods_calc.py:138-140`; `docs/prd.md:2000-2001` |
| P6 | Medium | R-PT-005 "season must match the booking's season" is not enforced. Season is not prefilled from the booking, and a Closed season is accepted. | `goods_pt_services.py:1279-1290, 1206-1212`; no booking reference anywhere in `ptmapper/goods_pt_services.py`; measured: send got past row validation with a Closed season row |
| P7 | Medium | Exporting an official version reads labels, season/brand names and the SUGGESTED hints from today's masters, so the "frozen" file can change (R-PT-010). | `goods_pt_export.py:99, 112, 120` |
| P8 | Medium | Export columns are hard-coded, so a new profile version (R-MDM-006) can't drop NAG or the SUGGESTED columns. | `goods_pt_export.py:26-49`; profile payload `columns: []` is used only for vocab pins and tolerance (`goods_pt_services.py:338-354`) |
| P9 | Medium | A brand file over 8,000 rows is refused with "more rows than one PT may hold", but a PT may hold 50,000 lines. The Madura extract is refused. | `goods_mapper.py:77`; `goods_brand_intake.py:98`; `core/goods_documents.py:36`; measured: truncated at 8,000 rows, 4.25 s, 142 MB peak |
| P10 | Medium | The PT list checks scope for every PT in the tenant, 3 queries each, before it paginates. | `goods_pt_views.py:723-727`; measured: 16 → 19 queries going from 1 to 2 PTs |
| P11 | Medium | Recall, price and rerun have no screen. A preparer can't withdraw a submitted PT or reprice from the UI. | no frontend caller of `/files/{id}/recall`, `/price`, `/rerun` (only `lib/api-schema.ts:4622-4673`) |
| P12 | Low | A typed row edit with a new `line_key` adds a row; only paste refuses this. "One row per counted lot" holds only at submit, through reconciliation. | `goods_pt_services.py:1722` vs `:1763-1767`; measured: rows 3 → 4 |
| P13 | Low | With pricing margin 0, `base_to_ticket` produces MRP = P RATE × 1.05 (margin 4.76%). | measured `base_to_ticket_calc` |

## 2. Path map

All routes are under `/api/goods-v1/ptmapper/` unless noted. Every write goes through `core.commands.execute_command`, which writes the command key, attempt/audit and sealed evidence rows. Draft content is written by `core.goods_documents.append_revision`, which creates `DraftRevision` and `DraftLine` rows and updates `DocumentHead`. That is the single business writer for every way of filling the grid.

| User action | Method + route | View | Service | Models/tables written |
| --- | --- | --- | --- | --- |
| Open PT from GRN (`GrnStart`) | `POST files/from-grn/<grn document id>` | `GoodsPtFromGrnView.post` `goods_pt_views.py:985` → `_create` :905 (`pt.prepare` at GRN site+brand :920) | `pts.load_profile` :317 → `create_receipt_draft` :1560 → `prefill_from_grn` :1618 → `price_line` :374 | `DocumentIdentity`, `DocumentHead`, `GoodsPt` (`ptmapper_goodspt`), `DraftRevision`, `DraftLine`, document event, command/audit rows |
| Create typed / empty draft | `POST files` (`source=typed`, `lines`) | `GoodsPtListView.post` :735 | same as above, with `lines` instead of prefill | same |
| Upload KDPS workbook | `POST /api/goods-v1/files/uploads` (evidence), then `POST files` `source=canonical_upload` | `files` upload view; `_source_lines` :857 | `goods_canonical.lines_from_evidence` :73 → `read_cells` :221 | `EvidenceObject` and the off-box blob, then the draft rows above |
| Upload brand's own file | evidence upload, then `POST files` `source=brand_upload` | `_source_lines` :878 | `goods_brand_intake.lines_from_brand_file` :60 → `goods_mapper.map_file` :1152 (`profiles.py` archetypes, `goods_rulebook.Rulebook`) → `goods_item_match` | same as canonical |
| Edit a cell | `PATCH files/<id>/rows` `{updates:[{line_key, fields}]}` | `GoodsPtRowsView` :1300 (`require_complete_scope` `pt.prepare`) | `edit_rows` :1666 → `_apply_updates` :1694 → `_EditContext.settle` → `price_line` | new `DraftRevision` + changed `DraftLine`s, `draft_corrected` event; a submitted PT gets `supersede_pending` on its `ApprovalRequest` |
| Paste | same `PATCH rows` with `{line_key, canonical:{…}}` | same | `_PasteContext.apply` :1756 → `goods_paste.paste_row` (uses `goods_canonical.read_cells`) | same |
| Rerun / price | `POST files/<id>/price`, `POST files/<id>/rerun` | `GoodsPtPriceView` :1337, `GoodsPtRerunView` :1356 | `reprice` :2067 (clears all review marks) | new revision with every line replaced, `repriced` event |
| Propose new item | `POST /api/goods-v1/masters/styles`, `POST /api/goods-v1/masters/skus` (from `PtPrepare.tsx:1065-1089`), then `PATCH rows` `sku_id` | masters goods views (not traced further, see review 01-02/C01) | product proposal with `originating_revision`; at submit `_identity_problems` :1313 blocks `PROPOSAL_UNCONFIRMED` | `ProductStyle`/`ProductSku` (pending) |
| Propose mapping rule | `POST /api/goods-v1/masters/crosswalks` (`PtPrepare.tsx:1126`); PT Work uses `POST ptmapper/proposals/<id>/decide` and `review/<id>/resolve` | `CrosswalkDecision` `goods_vocab_views.py:592` | rulebook crosswalk proposal | `masters.SourceCrosswalk` (proposed) |
| Save | the same `PATCH rows` (the screen batches edits; nothing else) | | | |
| Mark rows reviewed | `PATCH rows` `{reviewed_rows:[{line_key,row_hash}]}` | `GoodsPtRowsView` | `_review_marks` :2033 | new revision with `reviewed_rows` |
| Send for approval | `POST files/<id>/send` `{reviewed_hash}` | `GoodsPtSendView` :1368 | `submit` :2101 → `checked_lines` → `validate_rows` :1237 → `reconcile_receipt` :1117 → `approvals.goods_policy.pin` → `create_request` | `DocumentHead` → submitted, `ApprovalRequest`, exceptions resolved (`grn_awaiting_pt`) |
| Recall | `POST files/<id>/recall` (no UI caller) | `GoodsPtRecallView` :1412 | `recall` :2198 | head back to draft, request superseded, event |
| Export | `GET files/<id>/export[?draft=true]`, `GET files/<id>/export.xlsx` | `GoodsPtExportView` :1669 | `CanonicalCells` `goods_pt_export.py:89`, `xlsx_response` :1843 | export audit row (`_audit_export` :1780) |
| PT Work list | `GET files?limit=100`; "To prepare" tab `GET inbound/inbox?view=pending` | `GoodsPtListView.get` :715 | `_can_read_pt` per row | none |

Other PT routes that are not part of this screen: `files/from-manifest/<id>` (opening), `files/<id>/post`, `reverse`, `reissue`; `soh-imports/pt-upload` (store test run, `STORE-TEST-500`); legacy `/api/outbound/transfers/<pk>/pt(.csv|.xlsx)` (`outbound/urls.py:101-103`, C07). There are no non-goods-v1 receipt-PT routes left: the legacy PT mapper models were dropped (`ptmapper/migrations/0018_delete_legacy_pt_mapper.py`), and `ptmapper/views.py` and `urls.py` no longer exist.

## 3. Who can prepare a store delivery's PT

**Answer.** With the seed as written, nobody can prepare a PT for goods received directly at a store. There is one realistic setup that works: give a warehouse person a Warehouse role assignment scoped to the store. The PRD's own acceptance case asks for exactly this, and it is what the rehearsal DB now has. It is not a prepare-only grant, though. It also lets that person receive, count, accept, move and run return-to-brand at the store, which is why the seed refuses to create it until OQ-28 decides.

The trace:

1. The server gate is `access.require("pt.prepare", site_id=grn.document.site_id, brand_id=grn.arrival.brand_id)` (`backend/ptmapper/goods_pt_views.py:920`). Every later command also needs `pt.prepare` over every (site, brand) cell of the PT (`goods_pt_services.py:2301-2317`).
2. Runtime grants come only from role assignments (`accounts/principal.py:747-797`). `pt.prepare` is an explicit step action. It reaches a role only through `permissions_map.step_actions` (`accounts/unified_policy.py:237-264`), and the initial step map gives it to Warehouse only: `"pt.prepare": frozenset({"warehouse"})` (`unified_policy.py:150`). The comment above it says a broader section level "cannot turn a central administrator into … a PT preparer" (`:141-143`).
3. Role step actions in `kdps_rehearsal_screens` (measured): owner has none of the prepare actions, only `pt.approve.*`. store_person has `count.run, label.print, opening.import.stage, receive.arrival, stock.accept, transfer.move`. warehouse has `pt.prepare, pt.prepare.opening, receive.arrival, stock.accept, transfer.move, count.run, rtv.execute, …`.
4. The seed: `ops.warehouse` is defined with `site_code=OPS-WH, prepares_for=(OPS-ST,)` and the note "destination prepare-only duty awaits OQ-28" (`accounts/goods_demo.py:219-225`). `seed_goods_personas` deliberately ignores `prepares_for`: "Warehouse at the destination would also confer counting and acceptance there" (`goods_demo.py:654-669`).
5. The inbox names the waiting role as `C-WHO` for `pt_prepare` whatever the site (`inbound/goods_services.py:4724`). The screen checks `hold(session, "pt.prepare")` anywhere, not at the site (`frontend/src/pages/ReceiveDelivery.tsx:115, 151-165`). So a Store Person or Owner sees "PT prepared by the warehouse…", and a warehouse user scoped to OPS-WH never sees the OPS-ST delivery at all.

Measured on GRN `SYN-OPS/GRN/26-27/000001` (document `cf73933f-…`, site 10 OPS-ST, brand 2):

| Caller | Result | Queries |
| --- | --- | ---: |
| `syn.owner` (owner, all sites/brands) | 403 `ACTION_DENIED` | 21 |
| `ops.store` (store_person at OPS-ST) | 403 `ACTION_DENIED` | 21 |
| `ops.warehouse` with only the seeded OPS-WH assignment (OPS-ST one revoked inside the transaction) | 404 `NOT_FOUND` ("That GRN was not found"); pending inbox shows 0 items | 20 |
| `ops.warehouse` with the OPS-ST assignment now in the DB | passes authority, then 422 `PROFILE_INVALID` / `CONFIG_FUTURE` | 39 |
| same, after inserting backdated copies of the three configs (inside the rolled-back transaction) | 201, draft with 3 lines (10, 11, 8 pieces) | 88 |

**Fact that differs from the brief.** The rehearsal DB now gives `ops.warehouse` a second Warehouse assignment at OPS-ST (site 10). It was created 2026-10-31 19:43:06 UTC with no `AccessChange` row and no legacy link, and the seed does not create it. Its origin is not verified. It was probably added after the observation in the brief. The only rates, tax and profile versions were published at 19:49, after GRN 000001 arrived (18:54) and after GRN 000002 arrived (19:49:00.000). As a result neither GRN can be priced with them (`masters/goods_config.py:325-326`). Even with authority, then, nobody can prepare GRN 000001's PT in this DB as it stands.

**Governing rules:**

- `docs/prd.md:356` (§4.3 step rules): "Prepare and submit the PT — Warehouse". Authority "follows the granted role at the relevant site" (`:350`).
- `docs/prd.md:336`: "PT-making stays warehouse work", and the Owner approves by default.
- `docs/prd.md:1462` AC-03: "Warehouse user prepares PT for direct store arrival — Store remains GRN/stock arrival site; user needs its scope".
- OQ-28 (`docs/prd.md:1659`; `docs/store-operations-prd.md:1074`) leaves the "actual person-to-site/brand assignments; detailed actions and fields" open, and "affected journeys remain gated until decided". `docs/store-operations-prd.md:81` repeats that the action/field detail is gated by OQ-28. No other approved option exists: no prepare-only step rule, no Owner preparing, no Store Person preparing.
- `docs/store-operations-prd.md` has no §5.4. The "store and warehouse operations PRD §5.4" the code cites was merged into `docs/prd.md` v2.0 (`docs/prd.md:112`).

**Realistic setups:**

- (a) An Admin assigns the Warehouse role to the warehouse person at the store's site too. This works, as measured, but gives them full Warehouse floor authority there.
- (b) Assign the custom `warehouse_manager` role (`accounts/warehouse_manager.py`), which is built on Warehouse and scoped to "the warehouse and the store it serves". It holds `pt.prepare` at the store, plus the same extra authority. It is not seeded in this DB.
- (c) Add `pt.prepare` to the Owner role's `step_actions` through the role-policy editor. This is technically possible (`unified_policy.py:244-263`), but it goes against §4.3/B.1. The Owner would then also need a different approver, because the preparer can never approve their own PT.

None of these is a decided rule. OQ-28 has to choose.

## 4. Duplicates

| Area | Finding | Sev | Evidence |
| --- | --- | --- | --- |
| Legacy PT mapper vs goods-v1 receipt PT | The legacy writer is gone: tables dropped, no routes. `profiles.py` survives as configuration for the goods-v1 brand mapper and for the legacy transfer PT. There is one receipt-PT writer. | — | `ptmapper/migrations/0018_delete_legacy_pt_mapper.py`; `ptmapper/models.py:1-6` |
| Transfer PT | Two writers of a transfer PT document. Legacy `outbound.TransferPT` is built at dispatch with BASIC blank, P RATE = origin cost and NAG = dispatched qty. Goods-v1 creates a `GoodsPt(transfer=…)` of kind `TPT`. Both are live. The legacy one is C07 `temporary_supported_legacy` (S1911, S2297). | Medium | `outbound/posting.py:367`; `outbound/transfer_pt.py:55-93`; `outbound/transfers.py:815-835` |
| Transfer PTs in the receipt-PT API | `GoodsPt` rows for transfers share the PT list and detail routes. A preparer command on one fails in `document_target` with `NOT_FOUND`. Not verified end to end. | Low | `goods_pt_services.py:294-314`; `goods_pt_views.py:723` |
| Opening PT | Separate route (`from-manifest`, `pt.prepare.opening`), same `GoodsPt` and document kernel. The store test run adds a second opening source: KDPS PT workbooks parsed by `pt_opening.parse_pt_files` into the SOH path, with auto-approval under a flag. | Low (synthetic only) | `goods_pt_views.py:985-1040`; `ptmapper/pt_opening.py:1-12`; `opening_auto.py` |
| Parsers of the KDPS 22-column workbook | Three independent readers of the same layout. (1) `goods_canonical.read_cells` (receipt upload, also used by paste): exact money, P RATE is a check value. (2) `pt_opening.parse_pt_files` (opening): its own header detection, and P RATE becomes the cost. (3) `goods_mapper` with the KDPS work-sheet profile (`profiles.py:202`), which handles a vendor file in KDPS layout as a brand file. The same file can therefore be read three different ways. | Medium | files cited |
| Other PT layouts | `scripts/normalize_pt_files.py` and `scripts/soh_to_pt.py` write a 21-column "normalised" layout read by `pt_opening`. `scripts/canonical_pt.py` (commit 8c0e4db) writes a third layout with DIVISION, PRICE TIER, MARGIN BAND and so on, deriving BASIC = P RATE ÷ 1.2. These are off-product writers. | Low | `scripts/canonical_pt.py:14-22` |
| Writers of the KDPS layout | Goods-v1 export (`goods_pt_export.KDPS_EXPORT`), legacy transfer PT CSV/XLSX (`profiles.KDPS_COLUMNS`), and the Master Sheet PT template (`masters/master_sheet_template.py`). Three column lists in three places. | Low | files cited |
| Ways to fill the grid | Prefill, typed create, canonical upload, brand upload, paste and typed cell edits all end in `price_line` + `append_revision`. One writer, so there are no duplicate postings. | — | `goods_pt_services.py:1593-1607, 1694-1731` |

## 5. PT correctness against the owner's rules

| Rule | Finding | Sev | Evidence / measured |
| --- | --- | --- | --- |
| P RATE = BASIC × 1.2, configured | `P RATE = round(BASIC × (1 + transport_pct))`. `transport_pct` comes from the tenant's approved `rates` config (`goods_pt_services.py:365`) and defaults to 0 when missing. The rehearsal DB has `transport_pct: "20"` (published 2026-10-31 19:49). The in-repo seeds use `"0"` (`accounts/test_store_loader.py:309-310`, `scripts/first-store-opening-proof.py:73`). `EXPECTED_TRANSPORT_PCT = 10` ("owner's decision, 2026-10-01") makes Product Lists warn about 20 (`masters/master_sheet_services.py:69-71, 198`). | High | measured both_supplied: BASIC ₹1,000 → P RATE ₹1,200, t = 20 |
| `ticket_to_purchase` | Sets P RATE = MRP ÷ (1+g) × (1−m) and BASIC = P RATE (`goods_calc.py:256-260`). This is what the PRD says (R-MDM-009, `docs/prd.md:604`; A.4 row 15), so it breaks the owner's rule by design. The golden tests V4 and V5 lock it in. The export then shows the derived BASIC instead of the supplied one (`goods_pt_export.py:168`). | High | measured: MRP ₹2,625 gives P RATE = BASIC = ₹2,500.00; the supplied BASIC ₹1,000 vanishes from the BASIC column |
| Per-row direction | `direction_override` is an editable column (`goods_pt_services.py:105`) and is used as is (`:377`). It is refused only if the profile does not list that direction. `allow_row_override` is validated in config (`masters/goods_services.py:1385`) but never read. | High | measured: profile `allow_row_override: false`, the row is accepted with direction `ticket_to_purchase`, BASIC = P RATE ₹1,903.81, no issue |
| `base_to_ticket` | Derives MRP from P RATE, GST and pricing margin. With the rehearsal pricing margin 0, MRP = P RATE × 1.05. | Low | measured: P RATE ₹1,200 → MRP ₹1,260, MARGIN 4.76 |
| MARGIN | `round_half_up((MRP − P RATE) ÷ MRP × 100, 2)` (`goods_calc.py:134-135`). Correct. | — | measured 39.97 for MRP ₹1,999 / P RATE ₹1,200 |
| Tax slabs in config | Rehearsal `tax_rates`: HSN 6205 and 6203 only, effective `2025-09-22T00:00+05:30`. Slabs: 5% for MRP 0–₹2,625 (both ends inclusive), 18% above. This matches the store-ops baseline "Price ÷ 1.05 ≤ ₹2,500 … the line is an MRP of ₹2,625" (`docs/store-operations-prd.md:123`). The test-store loader seeds the 5% slab only up to **MRP ₹2,500** (`accounts/test_store_loader.py:300-303`), so MRP ₹2,501–₹2,625 wrongly gets 18% there. Any other HSN has no slab and is blocked `TAX_SLAB_MISSING`. | Medium | measured: MRP ₹2,625.00 → 5/5; ₹2,625.01 and ₹2,626 → 18/18 |
| MRP-inclusive vs transaction value | The slab is picked on the **MRP** (tax-inclusive ticket price), never on the discounted transaction value. That fits PT labelling. The till's GST-after-discount rule (ST-CMP-1) is a separate path, not reviewed here. | Low | `goods_calc.py:138-140, 250-252` |
| INPUT TAX vs OUTPUT TAX | Both come from the one slab chosen by MRP (`goods_pt_services.py:241-251`; `goods_calc.py:215-216`). The KDPS sheet uses BASIC ≤ 2,500 for INPUT and MRP ≤ 2,625 for OUTPUT (`docs/data-notes/pt-file-layouts.md:77-101`), giving input 5 / output 18 on 124 NARESH lines. Appendix A calls both "Derived + override" and says the thresholds "may differ" (`docs/prd.md:2000-2001`). There is no override column. | Medium | measured: MRP ₹2,626 with BASIC ₹1,000 → input 18 (sheet: 5) |
| SEASON required | A receipt row needs a real Season row that is not the unknown-historical one (`goods_pt_services.py:1279-1290`). Enforced at send. Prefill leaves season empty on every line (`:1618-1653`; measured `[null, null, null]`). The Season lookup has no tenant or status filter (`:1206-1212`): Season is a legacy shared master. | Medium | |
| SEASON = booking's season (R-PT-005, `docs/prd.md:660`; A.4 row 1) | Not enforced. Nothing in `goods_pt_services.py`, `goods_brand_intake.py`, `goods_canonical.py` or `goods_paste.py` reads the booking's season. The brand intake can even derive a season from the invoice date (`goods_mapper.py` `season_label`). A Closed season is accepted (R-MDM-011 says Open only). | Medium | measured: send passed `validate_rows` and reconciliation with row 1 on Closed season 4, and stopped only at the approval policy |
| One row per counted lot / no added rows | Prefill makes one line per (lot, resolved SKU) (`:1630-1652`). Paste refuses new rows (`:1763-1767`). A typed `fields` edit with an unknown `line_key` creates a row (`:1722`). Over-coverage is still refused at send by `reconcile_receipt` and `_coverage_problems`, so stock cannot be over-covered. | Low | measured: 3 → 4 rows accepted; also 100 and 400 rows of qty 1 accepted into the draft |
| Only good + resolved goods coverable | `receipt_pool` excludes held, damaged and undecided goods (`goods_pt_services.py:764-907`). `_lot_sku_problems` requires the SKU the counted goods resolved to (`:1437-1456`). Pending items block `PROPOSAL_UNCONFIRMED` (`:1313-1356`). Confirmed by reading the code; only the 3 good lines were measured. | — | |
| Config pinned at arrival | Profile, rates and tax must be effective at `arrival.actual_arrival_at` (`goods_pt_services.py:282-291`). Config published after goods arrive can never price them unless it is backdated. Not verified whether the UI offers backdating (`ConfigVersion.backdate_impact` exists). | High (rehearsal) | measured `CONFIG_FUTURE` on GRN 000001 |
| Send needs an approval policy | `pin(...)` refuses when no `receipt` approval policy covers the site and brand (`goods_pt_services.py:2140-2147`). There is none in the rehearsal DB. | High (rehearsal) | measured `send` 422 `APPROVAL_POLICY_BLOCKED`, 84 queries |

## 6. Columns

Where each of the 22 KDPS columns comes from in a goods-v1 PT line (`LINE_KEYS` `goods_pt_services.py:115-147`) and in the export (`KDPS_EXPORT` `goods_pt_export.py:26-49`):

| KDPS column | Line field | Export source |
| --- | --- | --- |
| SEASON | `season_id` | current `Season.name` (:99) |
| BRAND | `describing.brand_id`, else SKU style brand | current `Brand.name` |
| COLOR, GENDER, SUB CATEGORY, TYPE, ITEM, FIT, SIZE | `attributes[]` (vocabulary value or supplied text), else SKU attrs | current `value_labels` (:112) |
| BARCODE | `alias_as_used` | line |
| DESIGN | `describing.design`, else style code | line / SKU |
| HSN, QTY | `hsn`, `qty` | line |
| MRP | `supplied.mrp_paise` or `calculated.mrp_paise` | calculated, else supplied |
| BASIC | `supplied.basic_paise`; `calculated.basic_paise` | **calculated first** (= P RATE in ticket_to_purchase) |
| P RATE | `calculated.p_rate_paise` only; a file's P RATE is `supplied.check_p_rate_paise` | calculated |
| INPUT TAX / OUTPUT TAX | `calculated.*_tax_pct` (one slab) | calculated |
| NAG | not stored | `qty` at export time |
| MARGIN | `calculated.margin_pct` | calculated |
| SUGGESTED SUB CATEGORY / TYPE | not stored | `Rulebook.load(now)` ITEM hint (:120) |

Measured: the export column list is exactly the 22 in order for Owner and for the preparing warehouse user. BASIC, P RATE and MARGIN are dropped without the cost grant.

- **COLOR as price tier (PREMIUM / MEDIUM / ECONOMY).** The tiers pass through as ordinary COLOR vocabulary values (`ptmapper/goods_rulebook_seed.py:93-94`; `profiles.py:390`). No separate tier field exists in goods-v1. Only the off-product `scripts/canonical_pt.py` splits out a PRICE TIER column. Appendix A.7 allows "colour, or a configured grade/tag, or both". How the product handles the tags is still open (`docs/data-notes/pt-file-layouts.md:47`). Sev Low.
- **Export not frozen (P7).** Labels, names and SUGGESTED hints are re-read from current masters on every export, including exports of an official version. R-PT-010 and A ("reproduce the frozen profile and frozen values", `docs/prd.md:2078`) are not met. Sev Medium.

What dropping NAG and the two SUGGESTED columns would touch:

- Export: `goods_pt_export.py:26-49`, plus `export_columns` and the `CanonicalCells` hints (:114-123, 152-157).
- Brand-file mapping: `profiles.KDPS_COLUMNS` (`profiles.py:13-36`) and `WORK_SHEET_HEADERS` (:202, which includes NAG as a detection header); `goods_mapper.map_row` derived cells (`goods_mapper.py:1141-1144`, :1043-1044); `goods_item_match.py:81-97`.
- Legacy transfer PT rows (`outbound/transfer_pt.py:86`, migration `0015_backfill_transfer_pt.py`).
- Master Sheet PT template (`masters/master_sheet_template.py`), the frontend grid (`frontend/src/lib/ptGrid.ts`, `ptGridSheet.test.ts`, `OutboundTransfers.tsx`), and tests `test_master_sheet_import.py:362` and `test_pt_opening.py`. The demo seed commands also use the columns.
- R-MDM-006 says column sets are versioned in the profile (`docs/prd.md:598, 1927`). In the code the profile's `columns` don't drive the table or export at all (seed payload `columns: []`). Dropping the columns is therefore a code release, not a new profile version (P8). The readers ignore extra columns, so old files that still carry NAG or SUGGESTED would still load: `pt_opening` requires only barcode/qty/mrp/prate/hsn, and `goods_canonical` maps by header. Not verified for every vendor profile.

## 7. Access to cost columns

| Read path | Cost hidden without grant? | Evidence |
| --- | --- | --- |
| PT detail / grid (`GET files/<id>`) | Yes. `supplied.basic_paise`, `check_p_rate_paise` and `calculated` p_rate/basic/margin/pricing_margin/transport are left out. `cells` uses `export_columns(shows_cost)`. `row_hash` only for cost holders. | `goods_pt_views.py:58-61, 150-177, 226-239`; measured: Store Person detail 200 with no cost keys; Owner and preparing Warehouse get cost |
| Export JSON / XLSX (official and `?draft=true`) | Yes. Columns and lines are redacted the same way, and the audit records `cost_fields`. | `:1701-1728, 1780-1800`; measured: Store Person draft export has no cost values. Only `origins.BASIC = "person"` is visible, which shows a cost was typed but not its value. |
| PT list | No cost fields in the summary | `:693-709` |
| Send response / approval request | Receipt reconciliation totals carry `value_paise: null`. The approvals read shows the value only with the pinned field grant. | `goods_pt_services.py:1150`; `approvals/goods_views.py:157`, `goods_services.py:474` |
| Warehouse preparer | Sees cost only on PTs they prepared (`cost_own_pt`). A second warehouse user on the same draft sees no cost and gets no `row_hash`, so they cannot mark rows reviewed. | `:163-165, 236-237` (Low) |
| Inference | With base_to_ticket, MRP = P RATE × (1+g) ÷ (1−m). Anyone who knows the pricing margin can work back to P RATE from the visible MRP. | Low |
| Labels / print jobs, history, search | Not verified for cost. History events carry only state and details. | — |

## 8. Performance (measured)

Measured with the Django test client and `CaptureQueriesContext` on `KDPS_REHEARSAL_DB=kdps_rehearsal_screens`, with real server sessions issued inside one transaction that was rolled back. GRN 000001 (3 good lines). Creating the PT needed backdated copies of the rates, tax and profile configs, inserted inside the transaction, because the published ones start after the arrival. `ops.warehouse` used its existing OPS-ST assignment. No temporary grant was needed.

| Request | Lines | Queries | ms |
| --- | ---: | ---: | ---: |
| PT create from GRN (prefill) | 3 | 88 | 85 |
| PT detail (rows read) — warehouse / owner / store | 3 | 25 / 25 / 25 | 13–14 |
| Rows edit (4 cells × 3 rows) | 3 | 89 | 49 |
| Price `ticket_to_purchase` / rerun `base_to_ticket` / price `both_supplied` | 3 | 89 / 89 / 89 | 45–48 |
| Rows review marks | 3 | 88 | 46 |
| Send (refused at policy) | 3 | 84 | 40 |
| Export JSON draft / XLSX draft | 3 | 49 / 49 | 22–26 |
| PT Work list (`files?limit=100`), 1 → 2 PTs in tenant | — | 16 → 19 | 9–15 |
| Rows add 100 / 400 typed rows | 103 / 503 | 89 / 89 | 66 / 179 |
| Detail at 103 / 503 lines | | 26 / 26 | 17 / 30 |
| Price at 103 / 503 lines | | 88 / 88 | 54 / 95 |
| Edit one cell at 103 / 503 lines | | 89 / 89 | 51 / 80 |
| Export JSON / XLSX at 503 lines | | 49 / 49 | 36 / 84 |
| List after the scale runs (owner) | — | 19 | 34 |

- Query counts do not grow with lines on any PT document path. Writes cost about 88 queries each, mostly kernel, authority replay and evidence.
- The list grows by 3 queries per PT in the tenant, because `_can_read_pt` → `pt_cells` runs per row over the whole queryset before pagination (`goods_pt_views.py:723-727`). `pt_cells_many` exists but is unused there. At 1,000 PTs that is about 3,000 queries per list call (extrapolated, not measured). Sev Medium.
- `pt_cells` reads `DraftLine` for every revision of the document (`goods_pt_services.py:2274`), so the rows scanned grow with each reprice. Brands from deleted lines also stay in the PT's scope. Sev Low.
- Parser cost, `goods_mapper.map_file` only (pure, 0 queries) plus one barcode lookup query. Only row counts and timings are recorded; no file values were copied.

| Vendor file | Size | Rows mapped | Time | Peak memory |
| --- | ---: | ---: | ---: | ---: |
| `JOCKEY-NARAYANI.xls` | 1.5 MB | 30 | 0.16 s | 1 MB |
| `JOCKEY_DD SALES.xls` | 100 KB | 185 | 0.08 s | 1 MB |
| `Madura Fashion Brand AS - VH - LP.xlsb` | 5.1 MB | 8,000 (truncated, file has ~114k rows) | 4.25 s | 142 MB |

The Madura file would be refused whole (`goods_brand_intake.py:97-98`). The rest of the intake (item matching per row, inside the request) was not measured.

## 9. Tests

| Area | Tests | Run here? |
| --- | --- | --- |
| Calculation (Appendix C V1–V16, price_line seam) | `backend/tests/test_first_store_golden_calculations.py:145, 176` | **Yes.** 20/20 passed, run directly from a scratch runner with `DATABASE_URL` pointed at a dead port. pytest refuses outside proof mode (`tests/conftest.py:10-13`) and the proof DB on 55433 is down, so pytest itself was not run and the proof DB was not started. |
| Transfer PT scope | `test_so03_denials.py:262` | No (needs DB) |
| PT through approval → acceptance | `test_first_store_goods_operations.py`, `test_first_store_golden_calculations.py:189+` (V17 transfer), `test_first_store_goods_readiness.py`, `test_warehouse_manager_access.py` | No (DB) |
| Opening PT / KDPS workbook | `test_pt_opening.py`, `test_opening_auto_approval.py`, `test_master_sheet_import.py:362` | No (DB) |
| Browser: warehouse prepares PT from GRN, Owner approves | `frontend/browser/first-store-receiving.spec.ts:519-760` (RCV-01, `from-grn` at :710) | No (needs the proof stack) |
| Grid sheet logic | `frontend/src/lib/ptGridSheet.test.ts` | Not run |

Gaps (Medium): no backend test calls `from-grn`, rows, paste, price, rerun, send, recall, `canonical_upload`, `brand_upload` or export redaction directly. No test covers a store-direct GRN being prepared by a warehouse user scoped to the store (AC-03), and none covers `allow_row_override`, booking-season match or the 8,000-row cap. The golden tests V4/V5 assert BASIC = P RATE, which the owner's rule contradicts.

## 10. PT impact summary: what has to change for the owner's PT

1. **Who prepares (decision first).** Close OQ-28 for direct-to-store deliveries. Either (a) a step rule "warehouse may prepare (only) for stores it serves", or (b) Warehouse assigned at each store, accepting the extra authority, or (c) let the store person or Owner prepare, with another person approving. Then seed it (`goods_demo.py` `prepares_for`), cover AC-03 with a test, and make the delivery screen check the authority at the delivery's site, not anywhere.
2. **P RATE = BASIC × 1.2.** Record the owner's 20% as a decision and fix the expected constant (`master_sheet_services.py:71` says 10). Publish `transport_pct: 20` in every tenant's rates. Fix or retire the seeds that use 0 or the 2,500 boundary. Decide whether `ticket_to_purchase` is allowed at all: as written it makes BASIC = P RATE, which needs a PRD change to R-MDM-009 and A.4 row 15, and the golden V4/V5 would change with it. Enforce `allow_row_override` on `direction_override`.
3. **Tax.** Keep the MRP ≤ ₹2,625 = 5% slab (matches the baseline; CA still to confirm). Decide whether INPUT TAX should follow the vendor's purchase value (sheet: BASIC ≤ 2,500) rather than MRP, and add the "override" that Appendix A promises. Publish slabs for every HSN in use, with the 22 Sep 2025 start.
4. **Season.** Prefill season from the booking. Block a mismatch with the booking season (R-PT-005), and block Closed seasons (R-MDM-011).
5. **Columns.** If NAG and the SUGGESTED columns go, change the six code locations in §6 and the PRD's A.4, or make the profile's `columns` drive the export as R-MDM-006 intends. Freeze label and hint values into the official version so exports don't drift (R-PT-010). Decide how PREMIUM/MEDIUM/ECONOMY are carried.
6. **Setup that blocks the rehearsal today.** Publish the receipt approval policy for OPS-ST. Publish (or backdate) the profile, rates and tax so they cover the arrival times of goods already on the floor.
7. **Smaller fixes.** Make the brand-file cap consistent with the PT size, or chunk large files. Use `pt_cells_many` in the list. Give recall and reprice a control on the screen. Refuse typed rows that do not come from a counted lot.
