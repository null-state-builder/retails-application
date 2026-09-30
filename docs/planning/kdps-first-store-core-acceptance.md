# First-store alpha: goods operating acceptance evidence

Recorded 30 September 2026. This is focused implementation and proof evidence,
not a real-store activation, workflow retirement or SO-03 completion verdict.
The mapped boundaries remain C05/SO-07 receiving and C06–C08/SO-08 stock,
transfers and controlled corrections. SBU retirement and full offline cutover
remain with SO-04 and SO-09.

Current follow-up: [full-alpha progress](kdps-full-alpha-acceptance-progress.md)
implements the pinned transfer-family route and paused trading-count contracts
and records current proof evidence. The observations and earlier file hashes
below remain historical; their transfer-family gap is superseded by that
checkpoint. Real responsibility configuration, browser coverage and activation
retain the explicit acceptance gates.

## Revision and proof environment

HEAD was `e6cfc9734e5c20b779d9849473394b66c412b973`, with existing uncommitted
changes preserved. These file fingerprints identify this slice; the final
whole-tree fingerprint and baseline belong to the parent delivery evidence.

| File | SHA-256 |
| --- | --- |
| `backend/sell/services/goods_stock.py` | `ac0ce50dbb07169cd60251ff52d8559ba9ebf3563fcf2b17f683f1c019f55440` |
| `backend/tests/test_first_store_goods_operations.py` | `9ab91dcaeb532f244dad3bcc1b4aee85dfba94084a2934353e07f8343a751ab6` |

Every database-dependent command below used `scripts/proof.py run`, which
positively compares the owned container and host PostgreSQL identity before
starting the requested command. It refuses an unavailable or mismatched target.

| Identity | Verified condition |
| --- | --- |
| Compose project / container | `kdps-proof-ff7f4a176291` / `kdps-proof-ff7f4a176291-database-1` |
| Host / container port | `127.0.0.1:55433` / `5432` |
| Database / user | `kdps_proof` / `kdps_proof` |
| Disposable test database | `kdps_proof_test` |
| PostgreSQL | `17.11 (Debian 17.11-1.pgdg13+2)` |
| Matching host/container system identifier | `7690899938284695588` |

No real tenant was read or written. The fixtures create fictional tenants and
reviewed stock through the actual writers; explicit proof configuration sets
the operational site guards. They do not represent real deployment approval.

## Implemented and verified

The confirmed defect was a transfer of SOH-imported merchandise: dispatch,
destination count and physical acceptance succeeded, but the destination POS
could not resolve its barcode. The reviewed alias was scoped to the original
store. The accepted destination position remained real stock while the counter
reported no sellable item for that tag.

`sell.services.goods_stock.barcode_aliases` now includes a label bound to the
exact immutable `Origin.official_line` of goods physically accepted at this
store. It verifies the frozen SKU ID and the current effective source-site or
unscoped alias for that SKU. This is a derived reader: it writes no aliases,
copies no stock balances and changes no origin, approval or posting history.
Its existing conservative ambiguity rule still excludes multiple labels for
one SKU and one label pointing to multiple SKUs. This store's own sale
allocations retain exhausted item identity for later exchange. No cost is
projected to the counter.

The same reader already supplies the canonical inventory compatibility
projection in `stockledger.on_hand_projection`; this fix therefore also carries
the trusted destination label into the supported stock/search readers.

| Contract / actual handler chain | Current acceptance evidence |
| --- | --- |
| Receiving: `/api/goods-v1/inbound/arrivals` → `GoodsArrivalListCreateView` → `record_arrival`; arrival sessions → `open_count_session`; observations → `record_observations`; `/grns` → `GoodsGrnListCreateView` → `issue_grn` → immutable official GRN, custody lots, P01 journal and counted-damage report | Arrival and scan evidence do not add stock. The issued GRN adds quantity-only receiving/quarantine custody; it establishes no purchase origin or sellable quantity. One good and one damaged counted piece retain their distinction. Identical GRN command replay produces one GRN and no second journal effect. |
| Physical acceptance: `/api/goods-v1/stockledger/acceptance-sessions/:id/scan` → `AcceptanceScanView` → `goods_acceptance.scan` → frozen line/tag checks → exact accepted portions and journal | Invalid second tag refuses the whole scan batch with zero business effects. Same command and identical scan redelivery preserve one effect. A reused scan key with different input is refused. Changed site/brand assignment and logout deny acceptance. Accepted opening goods still await site readiness before selling. |
| Damage: `/api/goods-v1/outbound/mark-damaged` → `MarkDamagedView` → `goods_movements.mark_damaged` → P11 hold/condition movement plus pending `DamageReport` | Damage immediately removes affected pieces from availability. Command replay creates no second movement/report. Reporting is allowed during a count freeze; the whole site remains frozen for selling. |
| Damage review: `/damage-reports/:id/decide` → `DamageReportDecideView` → `damage_review.decide` → confirm, or one linked P12 release | Named unified review action and password step-up are checked. Reporter cannot review their own report even after receiving another Owner assignment. Confirmation posts no stock effect; rejection restores the prior address/condition and lifts this report's hold. Second decision and decisions during count freeze are refused. |
| Transfer: `/api/goods-v1/outbound/transfers` → `TransferListCreateView` → `transfers.create`; submit → exact frozen PT; approve → `transfers.approve` → P07 reservation; dispatch session/scans → `dispatch_preparation`; dispatch → P08; destination count → P09/P10; acceptance → destination acceptance evidence | Draft/submit move no stock. Independent approval reserves exact source portions. Scanned dispatch moves once; source manager cannot receive for destination. Counted goods remain unaccepted. Destination manager acceptance makes exactly those good pieces sellable. Original origin/cost persists, no second purchase origin is created, and replay produces no duplicate effects. Changed preparation hash and stock damaged after submission are refused without effects. |
| Counter identity: `goods_stock.barcode_aliases` → accepted local position / this store's sale allocations → exact immutable origin line → currently effective original alias | Unaccepted destination goods and unrelated sites receive no derived barcode. Expired/retired aliases, pending SKU, conflicting local SKU for the same tag and another local tag for the same SKU remain unavailable. Same barcode in another tenant retains that tenant's separate identity and cannot borrow the accepted origin. |

Mutation handlers require the request-bound unified `AccessContext`. The goods
command kernel invokes its guard before execution and before commit, replaying
the action/scope demands and current session/effective period. These tests use
the actual mounted view classes, services, journal and physical projections;
they do not replace the business writer with a mock.

## Remaining approval-family boundary

The internal transfer chain is **not evidence of complete versioned approval
responsibility, routes or limits**. Its protections are present:

- `goods_transfer_views.TransferApproveView.gate` requires the current unified
  `pt.approve.transfer` action at the source and password step-up.
- `unified_policy.workflow_levels` versions the action's section/rung, and
  the command guard rechecks its request demands at execution and commit.
- `transfers.approve` refuses the drafter/preparer as checker, rechecks the
  frozen exact pieces and guards both operational sites. Default Owner may
  approve but does not thereby gain physical receiving/acceptance authority;
  the tests use a separately scoped destination manager for those steps.

However, `transfers.submit_locked` and `transfers.approve` do not call the
canonical approval-family policy pinning service or create a version-bound
`ApprovalRequest`. `approve` officialises with `run.authority`; the generic
`Principal.authority` starts with quantity/value thresholds set to `None`.
Consequently a configured `ConfigVersion` family route, limit or subsequent
withdrawal is not pinned/rechecked by this direct transfer decision path. Its
review history is immutable PT/transfer events, not the unified family's
request/decision spine. This is distinct from missing action permission or
commit guards.

SO-03 owns the responsibility/policy authority dependency; C07/SO-08 owns the
transfer workflow and its preservation/cutover. Before accepting that family
as consolidated, route it through the approved versioned family contract,
binding complete scope, protected fields, exact revision/hash, separate
people and limits, and replay it before dispatch/posting. The family owner must
approve the responsibility/limit configuration and historical pending-request
handling. This slice neither invents those rules nor disables or retires the
existing governed transfer writer.

## Exact checks and limits

```sh
python3 scripts/proof.py run --cwd backend -- .venv/bin/pytest tests/test_first_store_goods_operations.py tests/test_first_store_inventory.py tests/test_first_store_online.py tests/test_first_store_exchange.py --reuse-db --tb=short -q
```

Result: **64 passed in 88.71s**. This included the first 17 goods operations
cases and the inventory/online/exchange regression suites. The production
reader was already at the fingerprint above. The routine incoming-GRN case
was added afterward, so this is not a claim that the final 18-case test file
was present in that combined run.

```sh
python3 scripts/proof.py run --cwd backend -- .venv/bin/pytest tests/test_first_store_goods_operations.py --reuse-db --tb=short -q
```

Result for the final test file: **18 passed in 40.40s**.

From `backend/`:

```sh
.venv/bin/ruff check tests/test_first_store_goods_operations.py sell/services/goods_stock.py
.venv/bin/mypy tests/test_first_store_goods_operations.py sell/services/goods_stock.py
```

Results: Ruff passed; strict mypy reported no issues in two source files.

```sh
git diff --check -- backend/sell/services/goods_stock.py backend/tests/test_first_store_goods_operations.py
shasum -a 256 backend/sell/services/goods_stock.py backend/tests/test_first_store_goods_operations.py
```

Results: scoped diff check passed; fingerprints are recorded above.

This evidence does not cover a new routine arrival all the way through invoice
valuation, receipt PT preparation/review, labels and eventual physical
acceptance: it proves the count/GRN boundary and separately proves the shared
acceptance writer using approved opening goods. It does not prove the complete
transfer approval-family limits/routes contract, all damaged-goods outcomes,
browser coverage, printers, migration rehearsal, real identity reconciliation
or final baseline. Those remain explicit delivery/owner acceptance work; a
passing test bundle alone does not close them.
