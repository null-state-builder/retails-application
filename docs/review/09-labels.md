# Review 09: Labels (delivery step `labels`, `/goods/labels`)

Scope: the backend behind `/goods/receive/delivery/:id?step=labels` (`ReceiveDelivery.tsx` → `LabelsPanel`) and the standalone `/goods/labels` page (`GoodsLabels.tsx`). Both render the same `LabelsPanel`. This is a report only: no code, config or data was changed. Every measurement ran inside one transaction on `kdps_rehearsal_screens` that was rolled back. Before and after the run the DB held 1 print job, 0 print events and 0 `label_print_failed` exceptions. The one print job (`83b0489c…`, `prepared`, 12 + 10 copies) was already there and this review did not create it. `kdps_local` was not touched. `scripts/check_so03_consolidation.py --check` printed "SO-03 inventory matches 3196 discovered surfaces". Consolidation item: C05 (SO-07). The print outputs are `print_output:…GoodsLabels.tsx` and `print_output:…TagPrint.tsx`, both owned by SO-07 (`docs/planning/so-03-consolidation-register.md:453, 458`). Review 07 covers PT preparation. Review 08 (PT approval) was not in `docs/review/` when this was written.

## 1. Summary

Once a PT is approved, the store prints a price tag for each piece. Each tag carries a scannable barcode and the selling price (MRP), both taken from the approved PT. When the goods are scanned in, the app checks each tag against the PT. The printing records are careful: each print is recorded, the person says how many tags actually came out, and a reprint can only replace the missing ones. But the app cannot make a barcode for goods that arrive without one, and per the supplier list that is most KDPS brands. Those goods cannot get a tag from the app at all. The tag also leaves off size, brand and design, so staff cannot read the size off it. Nothing stops someone printing more tags than there are pieces, or printing the whole run again as a "new" job. And no automated test covers any of this.

Top problems:

| # | Sev | Problem | Evidence |
| --- | --- | --- | --- |
| P1 | High | Goods with no vendor barcode cannot be labelled. Nothing generates a KDPS code at PT preparation, approval or print, and printing refuses an empty barcode. 834 of the 1,125 brands that have a supplier sit under "no barcode" suppliers. | `ptmapper/goods_barcode.py:60-61`; `goods_print_services.py:297`; generator only at `masters/goods_identity_views.py:1559-1571`, with no UI caller (`ProductsParties.tsx:1416-1424` sends no `range_version_id`); measured `render_label_svg(alias="")` → "The alias is empty." |
| P2 | High | The tag prints only barcode + MRP. R-PT-014 asks for identity, size and MRP. The seeded profile's `fields: [barcode, mrp, brand, size, design]` is validated but never read. | `goods_barcode.py:170-179`; `masters/goods_services.py:1503`; measured job labels = `alias_as_used`, `mrp_paise`, `svg` only |
| P3 | High | Copies are not tied to the line's QTY or to earlier jobs. 400 copies of a 12-piece line was accepted, and a second full job for the same lines was accepted. That bypasses the rule that a reprint covers only the missing copies. | `goods_print_services.py:134-158, 228-334` (no QTY or prior-job check); measured both 201 |
| P4 | Medium | Labels can be printed from a reversed PT or from a version that is no longer live. Only the document's purpose is checked. | `goods_print_services.py:239`; measured 201 with `DocumentHead.state='reversed'` and with `live_version=NULL` (both simulated, rolled back) |
| P5 | Medium | The receiving inbox moves a delivery past Labels as soon as any print job exists, even one that is only `prepared`, `failed` or 1 copy. | `inbound/goods_services.py:4884, 4956-4958`; measured `next_step: accept` with only the prepared job |
| P6 | Medium | Acceptance counts an `attempted` job (a browser print dialog, possibly cancelled) as printed label evidence. R-PT-014 and the service's own docstring say a dialog proves only an attempt. | `stockledger/goods_acceptance.py:69, 651` vs `goods_print_services.py:456-474` |
| P7 | Medium | No API lists the print jobs of a PT. Job cards live only in browser memory, so after a reload the outcome and reprint path for an earlier job is lost, and the only way forward is a fresh full job (see P3). | `goods_print_urls.py:13-21` (create, detail, outcome only); `GoodsLabels.tsx:119, 138-144` |
| P8 | Medium | There are no tests: no backend, frontend or browser test touches print jobs, outcomes, reprints or `goods_barcode`. | repository search, §9 |
| P9 | Medium | Large PTs are awkward. Copies default to 1 per line (not QTY), and the seeded profile caps a job at 500 copies in total, so a 2,000-piece PT needs at least 4 jobs with copies typed per line. | `GoodsLabels.tsx:250, 335-336`; `goods_print_services.py:144-152`; profile `copies_limit: 500` |
| P10 | Medium | The barcode-range generator only skips values already issued under its own issuer. A KDPS range could re-issue a 7-digit code that an SOH import (`soh:<id>`) or the master sheet (`item`) already holds. Not verified by a run. | `masters/goods_identity_services.py:900-906`; `ptmapper/soh_services.py:471`; `goods_identity_services.py:76` |
| P11 | Low | The delivery's Labels step shows only the primary PT. Supplement PTs, which Accept does show, must be printed from `/goods/labels` by pasting their id. | `ReceiveDelivery.tsx:114, 172-176` vs `:181` |
| P12 | Low | Browser printing makes every job sheet on the screen visible at the same position, so with two job cards open they overlap. Not verified in a browser. | `GoodsLabels.css:42-55` |
| P13 | Low | Copy-limit and reprint-limit refusals use the code `PRINT_ALIAS_INVALID`, which is about the barcode text. | `goods_print_services.py:147, 154, 215, 222` |
| P14 | Low | The panel reads the PT twice (summary, then `?version=`), and both carry all lines: 26 + 27 queries. Lines past 500 are never shown, because `next_cursor` is ignored. | `GoodsLabels.tsx:121-125`; `goods_pt_views.py:49, 600-611` |
| P15 | Low | The legacy floor price tag (`TagPrint`, from Price List) is a second print path. It prints the barcode as plain text with no symbol. It is not a business writer. | `frontend/src/pages/TagPrint.tsx:30-40`; `TagPrint.css:69-73`; `PriceList.tsx:235` |
| P16 | Low | The output is SVG in mm, printed through the browser dialog. There is no ZPL/TSPL output and no control over driver scaling. A 20-character alphanumeric code (the profile maximum) does not fit 60 mm. | `goods_barcode.py:143-161`; measured 20 letters → 68.75 mm needed |

## 2. Path map

All routes are under `/api/goods-v1/` (`ptmapper/goods_urls.py:19` includes `goods_print_urls`). Writes go through `GoodsAPIView.run_command` (command key, attempt/audit rows, idempotent replay).

| Action | Method + route | View | Service | Models/tables written |
| --- | --- | --- | --- | --- |
| Open labels step: PT header and lines | `GET ptmapper/files/<doc id>`, then `GET ptmapper/files/<doc id>?version=N` | `goods_pt_views` PT detail → `pt_resource` `:591` | `_content`, `_GridRead` (cost projected by `_shows_cost`) | none |
| Choose label profile (list) | `GET masters/configurations?kind=label&limit=100` | `GoodsConfigurationListCreateView` (`masters/goods_views.py`; read allowed by `label.print` via `LABEL_CONFIG_READ_ACTIONS` `:3334`; per-site rows `:3348-3359`) | config listing | none |
| Create print job | `POST ptmapper/print-jobs` `{pt_version_id, template_version_id, lines[{official_line_id, copies}]}` | `GoodsPrintJobCreateView.post` `goods_print_views.py:182` (`label.print` at the PT's held site and GRN brand, `:203-205`) | `parse_lines` `:108` → `create_print_job` `:228` → `check_contract` → `check_pinned(label)` `:263` → `_check_copies_within_profile` `:134` → `render_label_svg` `goods_barcode.py:125` | `ptmapper_printjob` (`lines` JSON with frozen alias, MRP, copies and SVG; `label_profile` JSON; `template_version`; `status=prepared`), plus command/audit rows |
| Choose printer | none. The browser `window.print()` dialog (`GoodsLabels.tsx:504-512` `printInBrowser`) | none | none | none |
| Record printer outcome / scan-back | `POST ptmapper/print-jobs/<uuid>/outcome` `{outcome, usable_counts[], reason_code, scanned_alias, matched_line_id, expected_revision}` | `GoodsPrintJobOutcomeView.post` `:245` (`_load` with read actions, then `label.print`) | `record_outcome` `:502` → `_validate_usable_counts` `:390` → `_verify_scan_back` `:422` → `_status_advances_to` `:456`; `open_exception`/`resolve_exceptions` (`alerts`) | `ptmapper_printevent` (EvidenceRow), `ptmapper_printjob.revision`/`status`; `GoodsException` `label_print_failed` opened on `failed`, resolved on `confirmed`/`partial` (including the whole reprint chain, `:477-499`) |
| Reprint missing copies | `POST ptmapper/print-jobs` with `reprint_of_id`, `reason_code` | same create view | `create_print_job` → lock parent (DOCUMENT rank) `:280` → `_validate_missing_copy_reprint` `:179` (latest counted event minus earlier sibling reprints) | new `ptmapper_printjob` row linked by `reprint_of` (DB check `ck_printjob_reprint_reason`, `goods_models.py:266-270`) |
| Preview / download label file | `GET ptmapper/print-jobs/<uuid>` | `GoodsPrintJobDetailView.get` `:309` (`label.print` or `pt.view`) | `print_job_data` `:574` | none. SVG strings are inlined with `dangerouslySetInnerHTML`, one per copy (`GoodsLabels.tsx:550-566`). No PDF/ZPL file and no `evidence` object is ever written (`PrintJob.evidence` stays null). |
| Label profile setup | `POST masters/configurations` (draft), then approve | masters config views | `validate_config_payload` `masters/goods_services.py:1841` with the `label` schema `:1497-1507` | `ConfigDraft`, `ConfigVersion` |
| Scan in against tags (consumer) | acceptance endpoints (`stockledger`) | — | `_Source.load` reads jobs with `status in ('attempted','confirmed','partial')` `goods_acceptance.py:651`; `_tag_problem` `:912` checks the alias and the frozen MRP | acceptance rows (out of scope) |

## 3. What a label carries

| Item | Finding | Evidence |
| --- | --- | --- |
| Barcode symbol | Code 128 of the official line's `alias_as_used` (the PT BARCODE column), as is. Leading zeros are kept (measured `0123456`). Printable ASCII only, at most `max_payload_characters`. | `goods_print_services.py:297`; `goods_barcode.py:59-78` |
| Human-readable text | Two lines: the alias, and MRP as `₹1,999` with Indian grouping and paise kept. | `goods_barcode.py:81-101, 166-179` |
| MRP | `calculated.mrp_paise`, else `supplied.mrp_paise`, of the official line. Refused if missing, not an integer or negative. | `goods_print_services.py:88-105` |
| Size, brand, design, colour, season, price tier | **Not printed.** The line's cells carry them (measured: brand "Van Heusen", size "L", design "VHSF325001"). The profile `fields` list is validated and copied into `label_profile`, but nothing renders it (P2). | `goods_barcode.py:170-180`; `masters/goods_services.py:1503` |
| Internal ids | None in the symbol. Matches R-PT-014. | `goods_barcode.py:142` |
| Format | SVG sized in mm (`width_mm × height_mm`). Module width = `min_module_dots / printer_dpi × 25.4` (0.250 mm at 203 dpi / 2 dots). A 10-module quiet zone each side. Bars at least 8 mm tall. A label that would not fit is refused, never shrunk. No PDF, ZPL or TSPL. | `goods_barcode.py:23-37, 143-161` |
| Measured sizes (60 × 25 mm, 203 dpi) | 7-digit `1234567`: 90 modules, 22.5 mm symbol, 1.9 KB SVG. EAN-13 `8907264512318`: 123 modules, 30.75 mm, 2.5 KB. `VHSF325001-L`: 39 mm. 20 letters: refused (needs 68.75 mm). Rendering time 0.05-0.08 ms per label; 2,000 distinct 7-digit labels in 65 ms. | pure-Python run of `render_label_svg` |
| Copies | Whatever the operator types per line, from 1 up to the profile's `copies_limit`, with the job's total also capped at `copies_limit`. **Not tied to QTY.** The UI defaults to 1 when a line is ticked. | `goods_print_services.py:125, 134-158`; `GoodsLabels.tsx:250`; measured 400 copies on a QTY-12 line → 201 |
| Config needed | One approved, effective `label` ConfigVersion in scope for the site, pinned by id as `template_version_id`. Required keys: `width_mm`/`height_mm` 20-150, `symbology: code128`, `copies_limit` 1-5000, `fields[]`, `printer_dpi` ∈ {203, 300, 600}, `min_module_dots` 2-10, `max_payload_characters` 1-128. | `masters/goods_services.py:1497-1507`; `goods_print_services.py:263-273` |
| Seeded? | **Rehearsal DB: yes.** One tenant-wide profile `31635cb6…`: 60 × 25 mm, 203 dpi, 2 dots, `copies_limit` 500, max 20 characters, fields `[barcode, mrp, brand, size, design]`, `service_code='seed'`. **Repository: no seeder.** No backend file other than the validator and renderer mentions `printer_dpi` or `copies_limit`, so a fresh tenant has no profile until someone drafts and approves one in Configuration. Until then every print is refused `PRINT_SOURCE_INVALID`. | DB read; repository search |

## 4. Barcodes

| Question | Finding | Sev | Evidence |
| --- | --- | --- | --- |
| Branded goods with a vendor barcode | Kept. The count's `raw_alias`, the canonical upload's BARCODE or the brand file's barcode becomes `alias_as_used`, and the label re-prints that same code. Matches R-PT-001 "not re-barcoded". The PRD also says a piece that already carries a valid barcode gets no replacement tag, but the app does not stop someone printing one. | — | `goods_pt_services.py:1641-1643`; `goods_canonical.py:252`; `goods_brand_intake.py:162` |
| Untagged goods (no barcode) | **No generated code.** R-PT-001 says "the product then generates one barcode per resolved identity". Nothing does this at PT preparation, approval or print. `alias_as_used` stays null, and `create_print_job` turns that into `""`, which is refused "The alias is empty." (422 `PRINT_ALIAS_INVALID`). The only way out is for the preparer to type a code into BARCODE, which is free text with no range and no uniqueness check (P1). | High | `goods_print_services.py:297, 309-310`; `goods_barcode.py:60-61`; `goods_pt_services.py:103, 126` (free `alias_as_used`) |
| Is there a generator? | Yes, but disconnected. A `barcode_range` config (`issuer`, `prefix`, `start`, `end`, `symbology: code128`) and `allocate_generated_value` issue `prefix + zero-padded counter` (width = `len(str(end))`) under an advisory lock plus an `AliasRangeCounter` row lock. It runs only when `POST masters/aliases` is sent with `range_version_id`. No screen sends that (`ProductsParties.tsx:1416-1424`). The rehearsal DB has 0 `barcode_range` configs. | High (part of P1) | `masters/goods_services.py:1488-1495`; `goods_identity_services.py:860-915`; `goods_identity_views.py:1530-1572` |
| Format / length vs KDPS practice | A range of `start=1000000, end=1999999, prefix=""` would produce KDPS-style 7-digit codes starting with 1 (§12.1). Code 128 renders them in 22.5 mm with no check digit, which matches "PT-style codes that carry no checksum". Nothing ships such a range, though. | — | `docs/data-notes/item-master-vocabulary.md:636-645`; measured `1234567` |
| Uniqueness | The generator skips only values already issued under the same issuer (P10). SOH-imported codes use issuer `soh:<source id>` and master-sheet codes use `item`, so a KDPS range could re-issue a code already on goods. The alias overlap check and the DB exclusion constraint are per SKU, so another SKU with the same value is allowed (by design, it becomes an ambiguous pick at scan). Not verified by a run. | Medium | `goods_identity_services.py:900-906`; `soh_services.py:471`; `goods_identity_views.py:1361-1382`; `goods_identity_models.py:100-113` |
| When | If it were wired in, at alias creation (a masters write), not at PT approval or print. A print job never allocates a code: it freezes whatever `alias_as_used` the official line holds. | — | `goods_print_services.py:294-321` |
| Till / acceptance resolution of printed codes | Acceptance accepts the line's frozen alias, aliases on this line's print jobs, and the SKU's effective aliases. The till also matches `official_line.payload.alias_as_used`, so a hand-typed BARCODE does scan at the till. That path is not verified end to end. | — | `goods_acceptance.py:736-760`; `sell/services/goods_stock.py:175, 186` |

## 5. Correctness

| Rule | Finding | Sev | Evidence |
| --- | --- | --- | --- |
| Labels only from approved PTs | Partly. An `OfficialVersion` exists only after approval, and the purpose must be receipt or opening (transfer refused). But the document head's state and live version are not checked, so a reversed PT or a superseded version can still be printed (P4). Measured 201 for both simulated states. | Medium | `goods_print_services.py:29, 239-244` |
| Frozen values | Yes. Alias, MRP, copies and SVG are written into `PrintJob.lines`, and the profile into `label_profile`, at creation. Later master or profile changes do not reach back. Acceptance compares the tag's MRP with the frozen line MRP. | — | `goods_print_services.py:313-334`; `goods_acceptance.py:912-925` |
| Outcome rules | Strong. `confirmed` needs a full count on every line; `partial` needs a known shortfall; `failed` cannot report a complete count; `unknown` stays null, never zero. `attempted` cannot overwrite a counted status. `scan_verified` must match the frozen alias (measured wrong alias → 409 `PRINT_VERIFY_FAILED`). Stale `expected_revision` → `REVISION_SUPERSEDED`. | — | `goods_print_services.py:361-474, 518-519` |
| Reprint rules | Enforced on the reprint path only. Measured: missing = 2 after `partial` 10/12. Reprinting 3 → 422; no reason → 422; 2 with a reason → 201; one more → 422 (missing now 0). The parent job is locked, so concurrent reprints serialise. **But a plain new job for the same lines is always allowed (measured 201), so the limit can be sidestepped (P3).** | High | `goods_print_services.py:179-225, 275-292` |
| Partial prints | Requested copies are kept on the job, and usable counts go on events. A failure opens `label_print_failed` (measured: 1 opened), and a counted reprint resolves the whole chain. | — | `goods_print_services.py:547-569` |
| QTY changes after printing (counter-GRN) | An official PT never changes, so its QTY and labels stay as printed. Acceptance is bounded by the line's QTY (`_acceptable_qty`), so surplus tags are left over. Nothing voids or flags existing jobs. Not verified by a run. | Low | `goods_acceptance.py:777-786` |
| PT reversal / reissue after printing | Jobs keep `PROTECT` FKs to the old version and are never voided. A reissue creates new `OfficialLine` ids. Old jobs do not cover the new lines, but the same alias still matches through the line's frozen alias, and an MRP change makes the old tags fail acceptance ("The tag's MRP differs"). Nothing marks old tags as superseded, and printing from the old version stays possible (P4). | Medium | `goods_models.py:245-247`; `goods_acceptance.py:916-917` |
| Step completion | Any job at all, even `prepared` or `failed`, marks Labels done in the inbox (P5). Acceptance counts `attempted` as printed (P6). Goods with vendor tags may skip to Accept while Labels is open (`OPEN_EARLY`). | Medium | `inbound/goods_services.py:4956-4958`; `goods_acceptance.py:69`; `goodsReceiving.ts:800` |
| Idempotency | Replaying the same `command_id` returned 201 with 28 queries instead of 44 (the replay path). That the same job id comes back was not checked. | — | measured |

## 6. Duplicates

| Path | What it is | Writer? | Status |
| --- | --- | --- | --- |
| Goods-v1 print jobs (`ptmapper/goods_print_*`, `GoodsLabels.tsx`) | The governed label path. | Only writer of `PrintJob`/`PrintEvent`. | Target |
| `TagPrint.tsx` modal from `/offers/price-list` (`PriceList.tsx:235`) | Legacy 50 × 50 mm floor price tag: brand, design/colour/size, MRP and the barcode as Courier text, with **no scannable symbol**. Rows come from the price list (`/api/offers/price-list`). | No server writer and no print record. | Second print output, registered (`register:458`, owner SO-07; the price-list API is under SO-06, `:242-243`). It cannot act as label evidence, but staff could use it as a "tag" for untagged goods, and those tags will not scan. |
| Transfer PT (`outbound/transfer_pt.py`, `GET /api/transfers/<id>/pt[.csv|.xlsx]`) | A packing-list document, not labels. BARCODE = `line.sku_code`. Transfer goods keep their existing tags, and print jobs refuse transfer purpose by design (`goods_print_services.py:26-29`). | No label writer. | C07 (SO-08). No duplicate label writer. |
| Legacy backend label/barcode endpoints | None found. The only `barcode` routes are price-list lookups and repricing (`offers/urls.py:19-20`). | — | — |

Result: there is one business writer for label evidence. The duplicate is a second, non-scannable print output (P15).

## 7. Access

| Check | Finding | Evidence |
| --- | --- | --- |
| Who may print / reprint / record outcomes | `label.print`, held by `store_person` and `warehouse` only, at the PT's held site and GRN brand. Measured: store 201, warehouse 201, owner 403 `ACTION_DENIED` on create and on outcome. | `accounts/unified_policy.py:152`; `goods_print_views.py:203-205, 270-271` |
| Who may read a job | `label.print` or `pt.view` at the job's site/brand; otherwise 404. Measured: store 200, owner 200 (16 queries). | `goods_print_views.py:28, 164-173` |
| Label profile read | Any `label.print` holder may list `label` configs. A per-site profile is hidden from other sites. Measured: store, owner and warehouse each see 1 profile. | `masters/goods_views.py:3332-3359` |
| Cost leak through labels | **None found.** The job DTO has no cost-like keys (measured). The store's view of the PT line carries no BASIC/P RATE/margin values, only an `origins.BASIC: "person"` marker (measured). The SVG holds only alias and MRP. | `goods_print_services.py:574-596`; measured |
| Same person prints and verifies | Allowed: the printer may record their own outcome and scan-back. Nothing in the PRD requires a separation here. | — |
| Out of scope, noted only | The store's PT detail returned `allowed_actions: ["export","reverse"]`. Whether a store person should see `reverse` belongs to the PT approval/reversal review. Not verified here. | measured |

## 8. Performance

Method: Django test `Client` with real `ServerSession`s issued inside one outer `transaction.atomic()` that was rolled back. `CaptureQueriesContext`, with SAVEPOINT statements excluded. PT `SYN-OPS/RPT/26-27/000001` (document `cea020ec…`, version `10156264…`, 2 lines, 12 + 10 pieces, site OPS-ST). Users `ops.store`, `syn.owner` and `ops.warehouse` (`@synthetic.demo`).

| Request | Status | Queries | Time |
| --- | --- | --- | --- |
| PT summary `GET files/<id>` (store) | 200 | 26 | 15-172 ms (first call cold) |
| PT detail `GET files/<id>?version=1` (store) | 200 | 27 | 16 ms |
| Label configs `GET masters/configurations?kind=label` | 200 | 15 | 8-10 ms |
| **Labels panel load total** | — | **68** | — |
| Print job detail `GET print-jobs/<id>` | 200 | 16 | 7 ms |
| **Create print job, 1 line / 12 copies** | 201 | **44** | 25 ms |
| **Create print job, 2 lines / 22 copies** | 201 | **44** | 21 ms |
| Create refused (copy limit) | 422 | 40 | 18 ms |
| Create replay (same command id) | 201 | 28 | 13 ms |
| Outcome `attempted` / `partial` / `scan_verified` | 200 | 47 / 48 / 47 | 20-22 ms |
| Outcome `failed` (opens exception) | 200 | 54 | 26 ms |
| Reprint (allowed) | 201 | 47 | 23 ms |
| Receiving inbox (store) | 200 | 48 | 36 ms |

Create is constant in the number of lines (44 for 1 and for 2 lines), because lines load in one query and rendering is pure CPU (about 0.05 ms per distinct line). The response carries one SVG per line, not per copy: about 2.5-3 KB per line.

**Estimate for a 2,000-piece PT** (say 200 lines of about 10 pieces):
- Panel load: still about 68 queries. The two PT reads would each return about 200 lines (about 0.55 MB each at the measured ~2.8 KB per line). That is fine below 500 lines; above 500, lines are cut off (P14).
- Printing: the seeded `copies_limit` of 500 is per job total, so at least 4 jobs: about 4 × 44 = 176 queries, about 100 ms of server time, and about 0.6 MB of SVG in total.
- Outcomes: one per job, about 4 × 48 = 192 queries.
- Total backend about 440 queries. The backend is not the bottleneck.
- The browser renders copies × SVG. That is 2,000 inline SVGs of about 26-35 `<rect>`s each, roughly 60,000 DOM nodes, sent through one browser print dialog per job. Not measured; this is the likely slow part.

## 9. Tests

- **None found.** Searching all tracked test files (`backend/tests/*.py`, `frontend/src/**/*.test.*`, `frontend/browser/*.spec.ts`) for `print-jobs`, `print_job`, `PrintJob`, `goods_print`, `goods_barcode`, `render_label_svg`, `labels-` and `step=labels` gives no hits. Git history shows no deleted print tests either. The only nearby coverage is a role toggle asserting `label.print` in `backend/tests/test_first_store_rollback.py:301-368`. No browser spec opens `/goods/labels` or the Labels step.
- The proof DB on port 55433 was down (`pg_isready`: no response) and was not started, so no DB-backed tests ran.
- Pure-Python check (not a committed test): `render_label_svg` measured as in §3. Empty, non-ASCII and too-wide aliases are refused as designed.
- AGENTS.md asks for targeted behaviour and denial tests on the tested revision. For this step, the gaps that need tests are: reprint arithmetic, the outcome-consistency matrix, reversed/superseded source, over-QTY copies, owner denial and the acceptance `PRINTED` set.

## 10. PT impact

What this step means for the PT file:

- **BARCODE column → the symbol.** It is the only identity on the tag. It must be non-empty, printable ASCII, at most the profile's `max_payload_characters` (20 in the seed), and fit the label width: an EAN-13 or 7-digit code fits 60 mm easily, but alphanumeric codes over about 14 characters do not. Leading zeros survive only if the column is kept as text.
- **MRP column → the price line.** `calculated.mrp_paise`, else supplied. It must be a whole, non-negative number of paise. Acceptance later refuses any tag whose MRP differs from this frozen value.
- **QTY → only advisory for copies.** Nothing enforces copies = QTY. The operator types it per line.
- **Not on the tag:** SEASON, BRAND, COLOR, GENDER, SUB CATEGORY, TYPE, ITEM, FIT, SIZE, DESIGN, HSN, cost and tax columns. SIZE and DESIGN are on the PT but do not reach the tag (P2).
- **Branded rows** (supplier flagged YES): BARCODE holds the vendor's code, the tag is optional, and acceptance can cite the PT source file instead of a print job.
- **Untagged rows** (supplier flagged NO, most brands): BARCODE must be filled before approval or the row can never be labelled (P1). Today that means a hand-typed code that nothing checks for uniqueness. KDPS's 7-digit practice would need a `barcode_range` config plus wiring the generator into PT preparation or approval.
- **Get it right before approval.** After approval, BARCODE and MRP are frozen. Fixing a wrong code or price means reversing and reissuing the PT. Printing from the old version is not blocked today (P4).
