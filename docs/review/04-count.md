# Review 04: "Count" (delivery `?step=count`) backend

Revision reviewed: `5a38a1f` on `dev`. This is a report only: no code, configuration or data was changed. Measurements ran on `kdps_rehearsal_screens` inside one transaction that was rolled back; the synthetic writes made to measure scale never persisted. `kdps_local` was not touched.

## 1. Summary

Counting is where the receiver scans each piece's barcode, says how many there are and what condition they are in, and the server keeps every scan as permanent evidence. The server side is solid on duplicate-safe scans, on not inventing a product, and on refusing a product code that matches several items until someone chooses. It does not enforce "one count per arrival". A second count can be started on the same arrival, and the person who started a count is the only one allowed to hand it on, so if they leave, the PRD's Owner or Warehouse reassignment cannot happen. The screen silently drops a scanned row whose "What is it" box is empty, even though the server would have accepted it. And a piece whose barcode is unknown is kept as "good" unless the counter changes the condition by hand, which later leaves goods that can never go on a PT.

Top problems:

| Rank | Problem |
| --- | --- |
| High | A second open count session on the same arrival is accepted (measured: 201, 2 open sessions). This breaks "one arrival has exactly one count session" (R-REC-006). |
| High | Only the current holder may hand a count over. The Owner has no `receive.arrival` and gets 403 even reading the count, so a stuck count cannot be reassigned as PRD §15.2 requires. |
| High | The screen drops scanned rows whose code matched a product but whose "What is it" is blank, then says "0 row(s) recorded". The server accepts that row (measured 200). |
| High | An unknown barcode is not forced to "unidentified". A "good" row with no SKU is accepted and becomes a good, SKU-less GRN line that no PT can cover, with no discrepancy raised. |
| Medium | Each record call reloads and returns the whole session. At ~2,100 scans one "Record" returns 563 KB. Per-scan lookups cost 15 queries each (≈30,000 queries for 2,000 scans). |
| Medium | The server trusts the client's `sku_id` and `alias_context` (issuer, profile) without tying them to the arrival's vendor or brand. |
| Low | Scans may be added to an orphan open session after the arrival's GRN is issued, and no session can ever be cancelled. |

## 2. Path map

| User action | HTTP + route | View | Service | Written (tables) |
| --- | --- | --- | --- | --- |
| Open Count step: find open count | `GET /api/goods-v1/inbound/arrivals/{id}/sessions` | `GoodsArrivalSessionView.get` goods_views.py:1442-1468 | `session_observations`, `session_data` goods_services.py:1437-1486, `session_hash` 1489-1510 | — |
| Identity profile / vendor code | `GET /api/goods-v1/masters/configurations?kind=identity_profile`, `GET /goods-v1/vendors?limit=100` | masters/vendors views | — | — |
| Start count | `POST …/arrivals/{id}/sessions` (`counter_id`, `entry_user_id` = me) | goods_views.py:1471-1506 | `open_count_session` goods_services.py:1002-1041, `person_can_receive` 220-231 | `inbound_countsession`, `inbound_arrivalhead.revision`, `core_auditevent` |
| Scan / type code (per row, on Enter and on blur) | `GET /api/goods-v1/masters/skus/lookup?value&site_id&issuer_key&alias_type&as_of&profile_version_id` | `GoodsSkuLookupView` masters/goods_identity_views.py:2361+ | `resolve_alias` | — |
| Record these rows | `POST /api/goods-v1/inbound/count-sessions/{id}/observations` | `GoodsObservationView` goods_views.py:1551-1589 | `parse_observations` 1165-1212 → `record_observations` 1229-1364 → `_open_identity_ambiguities` 1367-1421 | `inbound_scanobservation` (one per row), `inbound_countsession.revision`, `alerts_goodsexception` `identity_ambiguity` (one per ambiguous scan), `core_chainhead`, audit |
| Choose between candidates | `POST /api/goods-v1/masters/identity-picks` (after the record call) | masters identity views | — | `masters_identitypick` bound to `scan_event_id` |
| Hand count over | `POST …/count-sessions/{id}/handover` | goods_views.py:1592-1644 | `hand_over_count` 1044-1130 | `inbound_counthandover`, `inbound_countsession.counter/entry_user/revision` |
| Issue GRN (button on this tab) | `POST /api/goods-v1/inbound/grns` | see review 05 | `issue_grn` | see review 05 |

## 3. Duplicates (legacy vs goods-v1)

- There is no legacy receiving count API or model (OPS-18, `backend/inbound/models.py:1-24`). The count screen calls only goods-v1 (`GoodsArrivals.tsx:741-1010`).
- `inbound_scanobservation` is shared by four session kinds (receipt count, stocktake pass, acceptance, transfer dispatch), with one exclusive FK each (`goods_models.py:207-232`). That is one table, not a duplicate writer. Stocktake counts (C08, `outbound/goods_counts.py`) write the same table through their own service.
- A duplicate lookup family still exists. Legacy `/api/masters/skus/lookup` (`backend/masters/urls.py:26`, `SkuLookupView`) sits beside goods-v1 `/api/goods-v1/masters/skus/lookup` (`masters/goods_identity_urls.py:31`). The count screen uses the goods-v1 one. The legacy one is C01's retirement.
- Two writers inside goods-v1 at runtime: nothing stops two `CountSession` rows per arrival (4.1). Two counters can each write "the count" of one arrival, and only one becomes the GRN.

## 4. Correctness vs PRD

| # | Rule | Server-side? | Finding |
| --- | --- | --- | --- |
| 4.1 | One arrival has exactly one count session (R-REC-006) | **No** | `open_count_session` refuses only when a GRN exists (`goods_services.py:1013-1018`). There is no check for an open session and no unique constraint (`goods_models.py:94-95` is a plain index). **Measured:** a second `POST …/sessions` on arrival `7d1ea1ce…` returned 201 and left 2 open sessions. The screen avoids it only by reading first (`GoodsArrivals.tsx:741-750`), so two devices racing, or a person after a failed read, create two counts. |
| 4.2 | Stuck count reassigned by Owner/Warehouse (§15.2 "Count handover", R-REC-006) | **No** | `hand_over_count` allows only the current holder (`goods_services.py:1084-1090`), and every count endpoint needs `receive.arrival` (`goods_views.py:1458, 1480, 1568, 1615`). **Measured:** `syn.owner` gets 403 `ACTION_DENIED` on `GET …/sessions`. A count whose holder has left can only be abandoned. Combined with 4.1, the practical workaround is a second count. |
| 4.3 | Any authorised receiver may record into an open count, each scan naming its real actor (R-REC-006) | Yes | `record_observations` checks only `receive.arrival` at the site (`goods_views.py:1568-1570`). Each row's actor is set by `run.record`. |
| 4.4 | Conditions good / damaged / wrong / unidentified (R-REC-002/003) | Partly | The enum is enforced (`goods_services.py:1173-1175`). `unidentified` may not carry a SKU (1284-1290), and a SKU-less row needs a description (1292-1299). **High:** nothing makes a SKU-less row non-good. **Measured:** "good, no SKU, description only" returned 200. The screen tells the counter an unknown code is "counted as unidentified" (`goodsIdentity.ts:63`, `GoodsArrivals.tsx:573-579`) but leaves the condition at its default `good` (`blankRow`, `GoodsArrivals.tsx:460-469`). The result is a good, SKU-less GRN line (review 05, 4.6). |
| 4.5 | Scanned row with resolved code but blank "What is it" | Server accepts; screen drops | Confirmed. `record()` filters `row.qty > 0 && (row.sku_id \|\| row.description.trim())` (`GoodsArrivals.tsx:851`). `row.sku_id` is `null` from `blankRow` (463) and is never set: the resolved SKU lives in `identity[scan_key]` and is applied only after the filter, in `identityForRow` (855-861, `goodsIdentity.ts:96-108`). The toast then reports `rows.length` = 0 (`GoodsArrivals.tsx:900-901`). **Measured:** the same row sent with its resolved `sku_id` and `description: ""` returned 200. Without a SKU it returns 422 `DESCRIPTION_REQUIRED`. An unknown code with a blank description is also dropped silently by the screen; the server would refuse that one. |
| 4.6 | Genuine ambiguity needs an authorised choice bound to that scan (R-REC-006, GSA-T05) | Yes | An ambiguous code opens an `identity_ambiguity` job per scan (`goods_services.py:1367-1421`). The GRN issue refuses until an `IdentityPick` exists (`settled_identities` 1521-1591). **Low:** if a code becomes ambiguous after it was recorded (an alias added later), no job is opened, because jobs are opened only at record time. The GRN is then refused with no owned work item (not verified by a run). |
| 4.7 | Count by style + size (R-REC-002) | Indirect | Identity is per SKU, or per description+attrs+alias for unresolved rows (`_grn_lines` 1594-1644). Booking expectations are not prefilled (R-REC-002 "Prefill booking expectations where present"). Neither the count screen nor the session API reads booking lines. **Low** (gap vs PRD). |
| 4.8 | Duplicate-safe scans | Yes | `scan_key` is unique per session (`goods_models.py` `uq_scan_count_session`). A replay with the same content is a no-op; different content gets `SCAN_KEY_REUSED` (`goods_services.py:1259-1280`). |
| 4.9 | Session closed after GRN | Partly | `record_observations` checks only `state == OPEN` (`goods_services.py:1241-1242`). **Low:** the second (orphan) session from 4.1 stays open after the GRN and keeps accepting scans that never reach any GRN. `CountSession.State.CANCELLED` has no writer. |
| 4.10 | Identity scope belongs to the arrival's vendor | **No** | **Medium:** `alias_context.issuer_key/profile_version_id` and `sku_id` are taken from the client (`_alias_context` 1144-1162, `parse_observations` 1201-1210). They are checked only for SKU existence (1281-1282), not that `issuer_key` is the arrival vendor's code or that the SKU's brand is the arrival's brand. An API client could record another brand's SKU, and the GRN lot is then stamped with the arrival's brand (`goods_services.py:1908`). Not exercised by a run. |

## 5. Access

| Endpoint | Server rule | Evidence |
| --- | --- | --- |
| `GET/POST arrivals/{id}/sessions`, `POST observations`, `POST handover` | `receive.arrival` at site and brand of the arrival. Counter, entry user and handover target must hold an **all-brand** `receive.arrival` assignment at the site | `goods_views.py:1458,1480,1568,1615`; `person_can_receive` `goods_services.py:220-231` |
| `POST handover` | Additionally: caller is the session's current counter or entry user | `goods_services.py:1084-1090` |
| `GET masters/skus/lookup` | Lookup scope (`LookupScope`) | `masters/goods_identity_views.py:2361-2373` |

Measured: `ops.store` gets 200 on both session reads. `syn.owner` gets 403 on both, because the Owner role has no `receive.arrival`.

Cost: count payloads carry no cost or value fields (`session_data`, `goods_services.py:1455-1486`). Nothing cost-related is exposed here.

The UI says "Only they can hand it on" (`GoodsArrivals.tsx:641-645`) but still shows "Record these rows" to everyone. That matches 4.3.

## 6. Performance (measured)

Method: Django test `Client` and `CaptureQueriesContext`, ServerSession issued inside a rolled-back transaction, SAVEPOINTs excluded. Open session `7d6da358…` on arrival `7d1ea1ce…`.

| Call | Queries | Time | Notes |
| --- | --- | --- | --- |
| Count sessions read (issued arrival, 4 scans) | 17 | 10 ms | constant |
| Count sessions read, ~2,100 scans | 17 | 63 ms | **563,306-byte** response, no paging of observations |
| SKU lookup, known barcode | 18 | 14 ms | `resolved` |
| SKU lookup, unknown barcode | 15 | 7 ms | `unknown` |
| 200 sequential lookups | 3,000 | 1.61 s | 15 per scan → ≈30,000 queries, ≈16 s cumulative for 2,000 scans |
| POST 1 scan | 48–50 | 22–25 ms | |
| POST 100 scans (50 distinct unknown codes) | 99 | 79 ms | |
| POST 2,000 scans (50 distinct codes) | 99 | 508 ms | 50 `masters_skualias` reads = one per distinct code, not per scan (good) |
| POST 1 scan after ~2,100 stored | 50 | 119 ms | **563,232-byte** response |

Findings:

- **Medium:** every record call loads all stored observations (`goods_services.py:1243`), and the response re-serialises the whole session plus its hash (`goods_views.py:1587-1589` → `_session_resource` 1061-1072). Bytes and CPU per "Record" grow linearly with the count, even though the query count is flat. At 2,000 pieces recorded a few at a time, the screen downloads roughly 0.5 MB per click.
- **Medium:** the per-scan lookup is one request per row, fired on Enter and again on blur (`GoodsArrivals.tsx:1189-1195`; a double call per scan is not verified). There is no batch lookup endpoint. For a 2,000-piece delivery that is ≈30,000 queries.
- `record_observations` handles 2,000 rows in a constant 99 queries, and `_open_identity_ambiguities` resolves once per distinct (code, scope). No N+1 there.
- **Low:** `useNames("/goods-v1/vendors?limit=100")` supplies `issuerKey` (`GoodsArrivals.tsx:733, 786-789`). For a vendor outside the first 100, `canLookUp` is false and no code can be looked up. Not verified (6 vendors).

## 7. Data

Stored where:

- `inbound_countsession`: arrival, current counter and entry user (a projection), state, revision, `grn`, last activity.
- `inbound_counthandover`: append-only chain of from, to and reason.
- `inbound_scanobservation`: scan key, SKU (nullable), description, `alias_value`, client `alias_context`, attrs, condition, qty, `correction_of`, site.
- `masters_identitypick`: an E091 choice per scan.
- `alerts_goodsexception`: `identity_ambiguity`, one per ambiguous scan.

Duplicated facts and drift:

- **Medium:** two open sessions can exist for one arrival (4.1). The inbox treats any session as "counted" (`_delivery_records`, `goods_services.py:4944-4946`). Scans in the session that does not become the GRN are kept forever, with no state saying they were superseded.
- **Low:** `CountSession.counter/entry_user` are overwritten on handover (`goods_services.py:1119-1123`). The truth is the `CountHandover` chain plus each observation's actor; the GRN header copies whoever held it at issue (review 05).
- **Low:** `ScanObservation.site` copies `Arrival.site` (`goods_services.py:1341`), and the identity scope comes from the client (4.10). The SKU on a row and the pick for that row are two sources merged at GRN time (`_grn_lines`, `goods_services.py:1606`).

## 8. Tests

- Backend:
  - `test_first_store_goods_operations.py::test_routine_receiving_records_actual_count_without_replaying_opening_stock` covers a session with a good and a damaged scan, each carrying `sku_id`.
  - `::test_damage_can_be_reported_during_count_but_decisions_wait`.
  - `test_so03_denials.py::test_arrival_counter_assignment_needs_full_site_brand_authority`.
- Browser: `first-store-receiving.spec.ts` RCV-01 scans a resolved barcode, always fills `ga-desc` (lines ~571-576), records 3 rows and issues the GRN. Because it always types a description, it cannot catch 4.5.
- Frontend unit: no test for `goodsIdentity.ts` or `CountPanel.record`.
- Not run: the backend proof DB (127.0.0.1:55433) was down and was not started. Vitest `goodsReceiving.test.ts` and `goodsReceiptShortage.test.ts`: 10 passed at `5a38a1f`. Neither touches the count.
- Gaps:
  - second-session refusal;
  - Owner/Warehouse reassignment;
  - handover by a non-holder;
  - resolved row with blank description;
  - unknown code left "good";
  - orphan session after GRN;
  - `alias_context`/SKU brand mismatch;
  - observation-response size or query budget;
  - ambiguity that appears after recording.

## 9. PT impact

- Only pieces counted `good` **and** resolved to a SKU are ordinary-PT coverable (`ptmapper/goods_pt_services.py:764-808`, `Position … condition="good", sku_id__isnull=False` at 792). Wrong or unidentified pieces need `accept_wrong` by a different person. Damaged pieces never qualify.
- The barcode must be settled before the GRN issue. `settled_identities` applies only E091 picks and refuses ambiguity (`goods_services.py:1521-1591`). It does **not** settle a code that now resolves to exactly one SKU (for example, an alias added after the count), so such rows freeze SKU-less into the GRN and stay off the PT until a `resolve_identity` disposition.
- A "good" SKU-less row (4.4) becomes a good lot in the receiving location that the PT can never cover, and no discrepancy work item points at it (review 05, 4.6).
- Count rows carry no cost, invoice number or date. Nothing from this step is repeated on the PT.
