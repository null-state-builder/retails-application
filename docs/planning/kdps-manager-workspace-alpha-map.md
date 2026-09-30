# KDPS manager workspace: alpha screen and responsibility map

Recorded 30 September 2026. This map describes the inspected working tree and
the bounded manager workspace changes. It does not declare SO-03, SO-08, SO-09
or real-store cutover complete. The [consolidation register](so-03-consolidation-register.md)
and [dependency map](so-03-dependency-map.md) retain workflow ownership; inventory
coverage and a visible screen do not establish business acceptance.

## Revision references and comparison method

| Reference | Recorded identity | Meaning |
| --- | --- | --- |
| Earlier repository | `/Users/anand/Developer/KDPS`, application under `app/`; HEAD `cd1df939a62ec95fd354ac7071b902c505b8d2ba` | HEAD was verified read-only in that repository. The earlier recorded whole-tree fingerprint is `73a8b663aca37d0147afe10f1968f47518052d1f863be2348311625b798b918e`; it is a supplied historical reference, not a new fingerprint computed during this screen map. |
| Current repository | `/Users/anand/Developer/kdps-code`; HEAD `e6cfc9734e5c20b779d9849473394b66c412b973` | HEAD was verified read-only. Existing tracked and untracked work was preserved. |
| Current implementation start | SHA-256 `bbb6eed6d3accade33ccfa7b8e0466a455aec5e7ce333c7b0d7dab41719df9d4`, 1,304 files | The starting source fingerprint recorded in [alpha evidence](kdps-first-store-alpha-evidence.md), before the implementation changes. |
| Manager workspace slice start | SHA-256 `b0966ec95802689e86574146009a80c3af3254f739c22dc00224b920fc594ca5` | Parent-recorded intermediate working-tree fingerprint at the start of the workspace slice. It is not the final revision tested. |
| Final source | [Delivery and verification record](kdps-manager-alpha-delivery.md) | Current runtime source fingerprint, inventory/schema checks, browser results and baseline are recorded there. The complete final tree fingerprint and ordered commit IDs are emitted with the delivery. None of the intermediate references substitutes for them. |

The recorded fingerprint method is SHA-256 over sorted tracked and nonignored
untracked paths, with each entry encoded as `path + NUL + byte length + NUL +
file bytes + NUL`. Ignored private runtime files and credentials are outside
that source fingerprint. No credential contents are included in this map.

The earlier HEAD is in the earlier repository's object database, not the current
repository's. The bounded comparison inspected its `app/frontend/src/till/`,
`app/frontend/src/pages/sell/Billing.tsx` and `app/backend/sell/` sources.
`Billing.tsx` has preexisting edits in the earlier checkout, so its committed
version was read with `git show cd1df939a62ec95fd354ac7071b902c505b8d2ba:app/frontend/src/pages/sell/Billing.tsx`.
No edits, tests, database access or checkout changes were made there.

## Alpha boundary

The supported first-store path is: jointly confirmed company registration →
current configuration and personal access review → staged SOH evidence →
reviewed stable mappings and source declarations → independent source/OPT
review → physical acceptance → independently approved readiness → paired
online counter → accepted sale → stock and tender reads.

The Stock workspace changes organise existing contracts and correct read-only
projections. They introduce no second inventory writer. C05/SO-07 owns receiving
and opening; C06–C09/SO-08 own stock, transfers, counts and returns; C10/SO-09 owns
till delivery and the full offline cutover. SBU retirement remains disabled
until SO-04. General company self-service beyond one installation's first
jointly confirmed company is a later capability.

Status vocabulary below is deliberate: **implemented** means the current code
has the path; **focused proof** refers only to named isolated tests or recorded
fictional browser evidence; **retained** means a compatibility workflow still
has an owner and has not been retired; **inactive** means its prerequisites or
supported contract are absent. Final release verification remains separate.

## Daily manager screens

All API paths below are relative to `/api`. Menu action lists and role layouts
are display hints. The request-bound unified `AccessContext`, complete resource
cells and the service's fixed safeguards decide permission. Selecting a store,
brand or URL does not grant authority.

| Screen / responsibility | Actual UI → API → handler/service/data | Scope and field contract | Current condition / owner |
| --- | --- | --- | --- |
| Today `/` | `Home.tsx` → `StoreDashboard.tsx` → `GET /store/dashboard` → `storefront.views.DashboardView` → `resolve_store` / dashboard builder | One resolved accessible store. Operational cards retain their family gates; targets and money require their own permission. | Implemented retained dashboard. This workspace slice does not establish every dashboard card as a canonical goods-journal projection. C13/SO-12 and the card's domain owner. |
| Sell `/sell` | `sell/Billing.tsx` → `TillProvider` / `TillEngine` → `GET /sell/till`, `GET /sell/dataset`, `POST /sell/sales/finalise-online` → `OnlineFinaliseView` → `online.finalise_submission` → accepted `Sale`, stock and tender postings | Server-bound tenant/site/device; current session, one eligible counter assignment, customer fields, stable item identity, approved commercial/tax revision and accepted canonical stock. Cost and margin are not counter fields. | Implemented online-alpha path with focused proof and a recorded fictional accepted sale; real trading is inactive pending configuration/reconciliation. Full offline cutover stays C10/SO-09. |
| Bills `/sell/bills` | Bills page / till bill cache → `/sell/sales` and `/sell/sales/:document` | Accessible accepted documents; a pending outcome is not a second sale. Protected receipt delivery is rechecked before print/reprint. | Implemented accepted-history and recovery surface. Final browser evidence must include refresh, replay, revocation and printer behaviour. C10/SO-09. |
| Till & sync `/sell/till` | Till page → transport → `/sell/till`, pairing, renewal, allocation release/pause and resume handlers | Registered device and immutable numbering history; expired, withdrawn, paused or frozen authority refuses new issue. Current-session dataset projection must load before cached protected data is shown. | Implemented online readiness/recovery boundary. The historical offline implementation remains retained; it is not enabled by this workspace. C10/SO-09. |
| Stock `/inventory?tab=stock` | `Inventory.tsx` → `StockWorkspace.tsx` → `StockOnHand.tsx` → `GET /stockledger/on-hand` → `StockOnHandView` → `on_hand_projection.projected_rows` → canonical origins/positions/journal for goods stores, retained on-hand for other stores | Default active store; explicit All authorised stores; stable bookmarked site. `stock.view`, fields and complete cells remain together. Cost/value are optional server projections, not session-wide grants; excluded/incomplete totals remain disclosed. | Implemented unified workspace and canonical read adapter; focused parser/presentation and stock-projection proof. C06/SO-08. |
| Stock: Available by size | Same workspace → `CrossStoreSearch.tsx` → `GET /stock/availability` → `StockAvailabilityView`; canonical branch uses `projected_rows(... availability=True, values=False)` | Same selected store/query intent; quantities only. No cost, margin or value. Retained cross-store quantity exception remains explicit; exact store/SKU filters narrow both branches. | Implemented single-frame search. Requesting stock reserves nothing. C06/SO-08, with C07 request dependency below. |
| Stock request entry | Workspace header → `/goods/transfers/requests` → `TransferRequests.tsx` → `/goods-v1/outbound/transfer-requests` → `TransferRequestListCreateView` → `transfers.create_request`; stock picker uses `/goods-v1/outbound/stock-search` | Trusted source/destination IDs and SKU lines; destination request authority; allocation/dispatch remain separate governed actions. | Canonical entry implemented. **The existing search row's RequestPanel still posts `/outbound/stock-requests` and opens `/transfer/requests/:id`.** That continuity writer was preserved, not silently migrated or retired. C07/SO-08 owns its cutover and pending-request history. |
| Damage & Quarantine `/inventory?tab=damage` | `Inventory.tsx` → existing `GoodsStockPage` in damage mode → `/goods-v1/stockledger/quarantine` → `GoodsStockQuarantineView` → `goods_reads.resolve_query` / `quarantine_rows`; review context reads `/goods-v1/outbound/damage-reports` | Defaults active site; explicit all/bookmarked sites remain filters. Canonical portions carry site, stable SKU/origin, condition, held/reserved state. Quantity is the default; protected valuation basis can be explicitly refused. Summary is labelled as all stock in selected scope. | Implemented correction: daily damage no longer reads the empty legacy `QuarantineStock` projection for goods stock. Existing component and writer are reused. Focused UI regression; canonical damage/review behaviour has separate goods operating proof. C06/C08/SO-08. |
| Damage reporting / independent review | Physical-stock view in the damage workspace → `POST /goods-v1/outbound/mark-damaged` → `MarkDamagedView` → `goods_movements.mark_damaged` → one P11 hold/condition movement + pending `DamageReport`. Review link → `/goods/movements?site_id=...` → `/damage-reports/:id/decide` → `damage_review.decide` | Server `movement.draft` resource demands govern reporting. Damage is held immediately. `movement.approve`, password confirmation and a different named person govern the decision. Confirming posts no stock; rejection produces one linked P12 release. Count-freeze rules remain enforced. | Existing canonical commands, no new writer. Reporting during count and denial of self-review are separate fixed safeguards. All disposal/valuation outcomes are not accepted by the daily view alone. C06/C08/SO-08. |
| Receive goods `/goods/receive` | `ReceiveInbox.tsx` → `/goods-v1/inbound/inbox?site=ID&view=pending` → inbox and GRN/PT resource handlers → `inbound.goods_services` / physical acceptance | Selected authorised site and trusted source/brand; paginated inbox is followed with `useAllPages`. Counting/GRN, valuation and physical acceptance are distinct. Cost is not exposed merely by receiving access. | Implemented canonical inbox and existing receiving commands. Focused proof covers GRN/acceptance boundaries; full routine invoice→valuation→PT→labels journey remains a C05 acceptance dependency. C05/SO-07. |
| Upload SOH / later snapshot `/goods/opening` | `GoodsOpening.tsx` + `SohImport.tsx` → `/goods-v1/ptmapper/soh-imports` → `soh_views` / `soh_services`; reviewed initial source → immutable manifest children / OPT → `goods_acceptance`; later snapshot → `/goods-v1/outbound/soh-reconciliations` → `goods_soh_reconciliation` | Stable store and reviewed source identity. Opaque upload alone grants no protected financial read and creates no stock. Claims, mappings, physical declarations, exact revisions and independently authorised source review are separate. Original financial footer/notes require qualifying protected fields. | Implemented source workflow and one-initial-parent fence. Initial acceptance and narrow repeated-snapshot reductions/zero have focused proof. The real workbook is staged inactive, not imported as sellable stock. C05 plus C01/C06/C08 dependencies. |
| Transfers `/goods/transfers` | `Transfers.tsx` / transfer detail → `/goods-v1/outbound/transfers` and preparation/dispatch/destination-count handlers → `goods_transfer_views` / `goods_transfers` → exact reserved, transit and accepted portions | Both source/destination operational scope; frozen piece/revision checks; maker/checker and physical receiver remain distinct. Quantity/value history is preserved. | Existing canonical transfer path has focused operating proof. Complete versioned approval-family routes/limits are still incomplete, described in [core acceptance](kdps-first-store-core-acceptance.md). C07/SO-08 with SO-03 authority dependency. |
| Running offers `/offers/running` | `RunningOffers.tsx` → `/offers/running?site=ID` → running-offer/working-set service | Server evaluates the same rules the counter uses; UI search only narrows display. Truncated affected-item lists disclose their limitation. Displaying a rule is not authoring or approval authority. | Existing feature retained. Alpha online sale rechecks offer/cap/stacking commercial revision. Full discount-family configuration/retirement is C03/SO-06. |
| Day summary `/reports/day-summary` | `DaySummary.tsx` → `/store/cash-summary?date=...` + `/sell/flags` → `CashSummaryView` → `sell.services.day.build_cash_summary` → accepted `Sale` / `SaleTender`; flags retain their existing handler | One all-brand Money assignment for the resolved store; protected aggregate demanded before read and rechecked before private delivery. Scope change/error cannot leave a prior-store result as current. | Implemented guarded cash read; parent recorded fictional one-bill ₹900 summary and focused tests. Final command/browser evidence remains in delivery record. No day-confirmation writer was added. C13/SO-12, C16/SO-16. |
| Cash count `/sell/cash-count` | `sell/CashCount.tsx` → `/sell/cash-count`, `/sell/cash-counts`, `/sell/cash-movements` → existing cash-position/count/movement handlers | Configured cash-close feature, current online/numbered-bill reconciliation, authorised count and discrepancy contract. An unresolved or unsent bill prevents a reliable drawer count. A count is evidence, not an invented balancing posting. | Existing feature retained; fictional proof feature was independently reviewed through the governed API. Real cash-close configuration/acceptance remains required. C13/SO-12 and C16/SO-16. |
| Staff `/staff/list` | `StaffList.tsx` → typed `/goods-v1/sell/staff-list?site_id=...` → staff/salesperson projection | Feature-enabled authorised site, stable staff identity and current placement/activity. Personal PIN setup is a credential operation, not exception-review authority. | Existing scoped feature retained. Onboarding/retirement and counter attribution have focused proof; real staff and responsibility review remain required. C02/SO-05, C18/SO-18. |
| Store targets `/money/store-targets` | `MasterPages.StoreTargetsPage` → `/masters/store-targets/locations` + `/masters/store-targets?fy=...` → `StoreTargetLocationView` / `StoreTargetView` | Server Money scope and complete fields; editable only with current management authority. Failed reads remove editable cells. Reporting/target totals do not establish stock ownership. | Parent corrected selected-scope reads and failure presentation; targeted evidence belongs to final delivery record. C13/SO-12. |
| Return to Brand `/inventory?tab=returns` | `OutboundRTV.tsx` → retained `/outbound/rtvs`, returnable pool, submit/approval/credit-note handlers | Existing RTV scope, money/claim conditions and unresolved OQ-26 handling remain in force. The UI was not converted into a canonical goods-origin writer. | **Retained, not a goods cutover acceptance.** C09/SO-08 owns canonical return/disposal handoff, identity/value reconciliation and old writer retirement. |

## Review, history and specialist entries

These entries are separated from daily stock operations. They retain their
route, action and feature checks. A label containing “earlier” does not disable
an API or convert a still-mounted writer into a read-only one.

| Entry | Actual contract | Current limitation / responsibility |
| --- | --- | --- |
| Blind count records (non-trading) `/goods/counts` | `GoodsCounts.tsx` → `/goods-v1/outbound/stocktakes`, count sessions/scans/recounts/close → `goods_counts.start_count` / `current_declaration` → exact freeze/snapshot / controlled correction | Only an independently approved non-trading site qualifies. A sell-ready/trading site, tills or sales do not become countable through a menu change. The daily workspace explicitly says trading-store assigned blind counts are inactive. C08/SO-08. |
| Count Schedule `/inventory?tab=schedule` | `CountSchedule.tsx` → `/goods-v1/outbound/count-schedules` | Feature-gated scheduling/owned missed work. An active schedule does not relax the non-trading start contract. General scheduled trading blind-count activation remains pending. C08/SO-08, C15/SO-14 boundary work. |
| Earlier count records `/inventory?tab=count` / `/stock-count` | `StockCount.tsx` → retained `/outbound/stocktakes` family | Earlier sessions, corrections and evidence remain owned dependencies. Legacy writers were not retired by the workspace. C08/SO-08. |
| Earlier damage records `/inventory?tab=damage-history` | Existing `StockOnHand` quarantine view → `/stockledger/quarantine` / `/outbound/mark-damaged` → retained `QuarantineStock` / `MarkDamaged` | Kept behind the earlier section gate. It is not the daily canonical held-stock projection and must not be used to conclude a goods store has no held stock. C06/C08/SO-08. |
| Stock history `/stock/history` | `StockLedger.tsx` → `/stockledger/entries` / `/stockledger/summary` → retained `StockLedgerEntry` | This is the retained ledger reader, not proof that all canonical journal events appear there. Canonical provenance is available from `/goods/stock/origins/:id` and goods journal readers. C06/SO-08 reader cutover remains owned. |
| Canonical stock / origin journey `/goods/stock`, `/goods/stock/origins/:id` | Goods stock summary/on-hand/in-transit/quarantine/availability and origin resource handlers → canonical positions/origins/journal | Quantity and protected valuation are distinct; historical season/valuation incompleteness remains visible. This specialist surface retains broader stock filters; it does not manufacture missing original identities. C06/SO-08. |
| Movements / damage review `/goods/movements` | Existing hold/bin-move/release, damage decision and retained RTV/write-off/disposal actions | Exact origin/condition/holds; independent decision and count-freeze rules. Not every movement type is accepted by the first-store proof. C06–C09/SO-08. |
| Earlier transfers `/transfer/*` | Retained StoreTransfer/PT/receipt/request family | History, unresolved consumers and still-mounted writers remain. Canonical entry is `/goods/transfers/*`; transfer retirement requires source/destination, document, quantity/value and approval reconciliation. C07/SO-08. |
| Missing HSN, ageing, broken sizes, balancing | Existing `/stock/missing-hsn`, `/stock/ageing`, `/stock/broken-sizes`, `/stock/size-balancing` readers | Feature and access gates preserved. These specialist projections are not automatically proven canonical because Stock now has a canonical adapter. Their owners must complete connected report/search proof. C06/C07/SO-08. |
| Inventory report `/reports/inventory` | Existing report route and report service | The fictional manager's observed denial was **feature off**, not evidence of denied access with that feature configured. Enabling it requires its own independently reviewed feature configuration and report acceptance. C14/SO-13 and C06 dependencies. |
| Receive history `/goods/receive/history` | Same canonical inbox with `view=history` | Accepted/received history remains a resource projection; it is not a second receipt writer. C05/SO-07. |
| Application roadmap `/roadmap` | `ApplicationRoadmap.tsx` filters planned entries through existing navigation/action/feature rules | Planned placeholders are omitted from daily manager navigation, but remain discoverable here when permitted. No underlying route, permission or writer was retired. Individual SO owners retain delivery. |

## Stock UI and bookmark contract

`StockWorkspace.tsx` owns one page frame, title, store selector and search.
`StockOnHand` and `CrossStoreSearch` render as embedded panels, avoiding nested
padding. The shared `OperationsPage` / `OperationsTable` components provide
scoped spacing and internal table scrolling; Today and Sell retain their own
screen design. Responsive browser acceptance is a separate current check.

- Default store comes from current server context choices and active store.
  Explicit `scope=all` means All authorised stores, never all tenant data.
- `site=stableID`, old `store=CODE`, `q`, `sku`, `brand`, `group` and `view`
  survive view changes/bookmarks. Unknown or revoked bookmarked stock scope is
  unavailable; it does not silently select another store.
- `/stock` redirects to the workspace while preserving filter intent;
  `/stock/search` preserves its earlier whole-authorised-scope search intent;
  `/stock?view=quarantine` opens canonical daily Damage. Earlier damage history
  has an explicit separate destination.
- Scoped compatibility stock requests set `X-KDPS-Unit` to the selected stable
  site or explicitly empty for all-scope reads; `lib/api.ts` fills the top-bar
  default only when the header is absent. The server still intersects every
  read with current action/field/resource authority. This does not change the
  global top-bar selection or expand the counter's store.
- Loading, unavailable and authorised zero are different states. Absent cost is
  “Unavailable”, not ₹0. Availability states its financial limitation; a stock
  request does not reserve or promise a transfer.
- Daily Damage reuses the canonical stock component. It defaults to quantity,
  exposes physical stock for reporting, links the existing independently
  authorised review, and refuses to display retained summary data after a failed
  scope read. Earlier quarantine evidence remains separately reachable.

## POS preservation and intentional alpha differences

| Aspect | Earlier recorded source | Current working tree | Acceptance boundary |
| --- | --- | --- | --- |
| Counter layout and workflow | `Billing.tsx` scan, line grid, payment/customer strip, visible actions, shortcuts, cart pricing and receipt flow | Same business-facing counter structure and pricing/cart routines; workspace styling is scoped outside Sell | The UI work is not a counter redesign or a wholesale business-rule replacement. Individual return, hold, voucher and specialist features retain their gates. |
| Commit point | Earlier `TillEngine.commit` used `commitBill` to move number, local shelf and queue in IndexedDB, then synchronise `/sell/sales` | Approved `online_alpha` uses durable UUID intent → `finalise-online` → accepted server bill → local accepted state / dataset refresh. Network failure keeps the exact pending intent | An uncertain outcome is retried with its UUID, never assumed rejected or charged again. Offline initial issue is refused in alpha. Historical local-queue implementation remains pending SO-09 cutover. |
| Till identity/cache | Earlier `TillProvider` used a single-store code to create the engine | Current provider first obtains server tenant/site/device identity, uses that namespace, and waits for current-session authorised data before displaying cached protected fields | Same displayed shop name cannot establish identity. Logout/revocation and another session must not expose the previous person's cached data. |
| Pricing and offers | Dataset provided item/tax/offer policy to local pricing; goods shelf already existed beside retained on-hand | Dataset remains the counter working set; online issue rechecks effective commercial revision, approved MRP, HSN/tax, no-discount flag, offer/cap/stacking and active salesperson | A visible offer or locally computed total cannot override a changed server policy. Real-shop HSN, tax, MRP and source-costing configuration are required. |
| Stock authority | Earlier dataset could use retained `StockOnHand` or the goods shelf, depending on site | Goods alpha uses accepted canonical portions; opening and later snapshot do not rewrite historical sale/posting rows. Inventory compatibility reads now expose the same canonical source for those stores | No acceptance from an empty old balance projection. Arrival/count/valuation alone are not physical acceptance or sellability. |
| Item text | Original descriptors/style codes were separate from barcode/master identity; imported opening descriptions could expose a technical source-row key | `stockledger.goods_descriptions.origin_item_names` reads public ItemName only through the exact reviewed source → batch → immutable manifest row → origin binding; dataset and on-hand display consume it | Existing style/SKU/barcode IDs and immutable history are unchanged. Text does not bind authority. Tampered, foreign or unreviewed source text is excluded. |
| Print/reprint | Receipt produced after local commit; printer failure preserved the saved bill | Alpha print/reprint reloads the exact accepted document through the protected server read before delivering receipt bytes | Printer failure cannot undo/reissue the accepted sale. Delayed/revoked delivery is an access boundary, not just a hidden button. |
| Numbering and recovery | Historical allocations, holes and manual/offline reconciliation exist | Online device/frontier/expiry and count-freeze checks are serialised; accepted UUID replay can report the earlier outcome without a new posting | Do not erase or renumber historical bills. Full retired-device/offline frontier cutover remains owned by SO-09. |

## Configuration and real-store activation gates

The uploaded real source is `VAS-SGMR_SOH_28-09-2026.xlsx`, SHA-256
`033b5d6dbd7943ba3e8cd07861d1ea1ba0822df65e560698220b6745d8ffb715`:
25,689 source rows, 11,161 positive rows and 19,896 units. The user identified
Vaishnavi at Singh More, Ranchi and confirmed Rate is purchase cost before tax.
Those declarations do not establish trusted brand/SKU/season/category links,
HSN/tax treatment, a current export cutoff or financial reconciliation.

| Required condition | Responsible owner / validation |
| --- | --- |
| Approved company/entity/GSTIN/store identity, timezone, addresses, stable bindings and current business profile; unresolved identity inactive | Real Owner/Admin and C01/SO-04. Review source IDs and immutable references; do not match display names as authority. |
| Six initial roles with approved versions/action upgrades; manager's store/brand/actions/fields in one assignment; Owner policy administration grants no protected business data by itself | SO-03 and independently authorised real signatories. Verify session DTO, allowed/denied resource cells, working periods, password confirmation, distinct reviewer and session invalidation. |
| Approved working calendar, role/workflow family responsibility/limits and owned unresolved OQ-28 exclusions | SO-03 with relevant business owner. Validate ISO calendar configuration and scheduled/effective boundary tests; an unconfigured family stays inactive. |
| Reviewed source mapping, HSN/MRP/tax/source-costing ownership, physical counts, supporting evidence and exact financial/source declarations | C01/C05 and real store/finance owners. Resolve missing identities and anomalies; reconcile all source quantities/amounts and excluded rows without unexplained differences. |
| Document series for opening/PT/sale and any supported count correction; live paired device/authority and reconciled bill numbering | C05/C08/C10, real Owner/Admin. Verify exact series and upgrades; no conflicting historical numbers, retired allocations, holes or unresolved outcomes. |
| Reviewed `sell_policy`, tax settings and relevant feature versions; goods readiness approved only after all non-overridable source-costing gates | C03/C10 and real tax/CA owner. Fictional proof tax configuration and proof-only synthetic transition are not real approval. |
| Feature changes use current access, password confirmation, tenant security lock/session invalidation and a different independently authorised reviewer | SO-03/C01. Parent's focused feature-boundary tests are separate evidence; no feature is enabled for a real tenant by this screen map. |
| Browser and deployment restrictions, protected downloads/printing, real-session expiry/revocation and read-only tenant reconciliation | SO-02/SO-03 and workflow owners. Use a positively identified environment; record command/browser evidence at the final fingerprint. |

## Remaining count and reconciliation boundaries

General trading-store assigned blind counts are **inactive**. Existing
`goods_counts.current_declaration` requires an approved non-trading capability
and refuses trading/sell-ready sites. Moving Count Schedule to Review does not
resolve R-INV-009, ST-INV-3 or OQ-57.

The separate implemented SOH snapshot adapter supports a narrower protocol:
approved fresh full-store source; all relevant tills paused and numbered/UUID
outcomes reconciled; exact cutoff at or after persisted pause; whole-store
physical/fullness/omission-as-zero declarations; current one-assignment scope;
exact source/review/config versions and canonical layer/journal watermark;
independent `count.review` with required protected fields; unchanged freeze and
pause/frontier at decision/posting. It closes zero deltas or posts supported
P13 reductions at original portion cost through the retained correction writer.
It preserves sales, document numbers, origins, values and approval evidence.

Gains, encumbered/nonordinary custody, unresolved source ownership/costing,
unknown responsibilities and full offline frontiers remain owned inactive
exclusions. A new file hash cannot replay another opening balance; the existing
approved initial parent's deterministic children can resume without another
effect. No generic overwrite or adjustment bypass was introduced.

Real-shop snapshot activation still needs the real fresh cutoff, scope/mapping
and physical/financial review. A gain requires a supported owned origin/cost
and receipt/correction contract; this implementation does not invent it.
Transfer family routes/limits, general scheduled trading counts and remaining
legacy history/request/RTV readers must be accepted under their named owners
before broader workflow cutover.

## Verification attached to this UI slice

After the final production correction, from `frontend/`:

```sh
npx vitest run src/lib/stockWorkspace.test.ts src/pages/Inventory.test.ts src/pages/StockWorkspace.test.tsx src/pages/StockOnHand.test.tsx src/pages/GoodsStock.test.tsx src/lib/api.test.ts
npx tsc --noEmit
npx eslint src/pages/Inventory.tsx src/pages/Inventory.test.ts src/pages/GoodsStock.tsx src/pages/GoodsStock.test.tsx src/shell/navConfig.ts
npx prettier --check src/pages/Inventory.tsx src/pages/Inventory.test.ts src/pages/GoodsStock.tsx src/pages/GoodsStock.test.tsx src/shell/navConfig.ts
```

The final focused run at 14:58:18 local time returned **20 passed in six files**;
TypeScript, focused ESLint/Prettier and `git diff --check` passed. These tests
prove URL/state, one-frame presentation, unavailable-value behaviour, explicit
scope headers, canonical damage read destinations and retained history gates.
The mocked/SSR presentation tests do not prove server authorisation or a stock
write. A TypeScript optional-record assertion was corrected before this final
test run; the prior compile failure is not counted as a pass.

The four actual retained availability-filter cases are
`backend/tests/test_stock_workspace_filters.py`; the parent reported their
identified-proof run passed alongside four feature-boundary cases. Canonical
opening/acceptance/damage/transfer and online posting evidence is recorded
separately in [core acceptance](kdps-first-store-core-acceptance.md) and
[alpha evidence](kdps-first-store-alpha-evidence.md), with their own revision
and coverage limits. No database-dependent command was run as part of this
UI/documentation slice.

The real-backend responsive `frontend/browser/manager-workspace.spec.ts`,
inventory drift/boundary checks, generated API contract, current full baseline
and final source fingerprint are parent delivery checks. Historical green
checks are not final verification. Real tenant reconciliation, deployment,
reviewed cutover and rollback readiness remain mandatory before trading or
consolidation acceptance.
