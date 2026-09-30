# First-store full alpha acceptance: current progress

Recorded 30 September 2026 for Vaishnavi at Singh More, Ranchi. **Full alpha
acceptance remains open.** This checkpoint implements and verifies the
transfer-family and trading-count gaps previously recorded in
`kdps-first-store-core-acceptance.md`, plus the newly traced customer-return
acceptance gap. It does not activate the real shop or retire a workflow.
SO-04 retains SBU retirement; SO-09 retains full offline cutover.

## Revision and preserved history

Starting HEAD: `84dce044f0d0f5b1072fa2af8675bde9d090b82b`. Starting complete
working-tree fingerprint: `f12b638d3e4fe8506e1f05a2796611d8f725d50f32605e6c219ac0d4168bc3cf`.
The implementation/check runtime fingerprint is
`8a025f53ac3c0c5c0c88f724ff1229e3e2896a70498af5929bd491e299ca43f1`.

Reproduction: sort the unique existing paths from
`git ls-files --cached --others --exclude-standard -z`; hash each UTF-8 path,
NUL, decimal byte length, NUL, bytes, NUL. Symlinks contribute target text;
deleted paths are omitted. The runtime fingerprint excludes `docs/`, so evidence
can be updated without changing tested runtime sources. Ignored private proof
records are excluded. The complete manifest is retained privately in
`.local/full-alpha-current-fingerprint.json`.

Reviewed dependency commits: `1b30cb8` (common versioned approval authority),
`88a1e87` (canonical stock adapters and safety tests), `dcaa691` (manager UI and
API contract). The proof/inventory/evidence commit follows these. Deploy the
aggregate tested runtime, not an intermediate commit with only one side of a
changed API contract.

Existing commits, working changes, proof databases and business history were
preserved. No real tenant was queried or written. Credentials and the raw SOH
workbook are excluded from commits; no push or production cutover is authorised.

## Implemented contracts

| Requirement / owner | Actual path and verified behaviour |
| --- | --- |
| PRD §4.3 / ST-OPS-5, R-PT-005; A00/C07 | `TransferSubmitView` → `transfers.submit_locked` → `transfer_authority.submit` pins the exact source/head/plan revisions, line hash, stable SKU/origin, both-site/brand cells, original cost, maker/preparer, quantity/value limits and configured `ConfigVersion` route. `TransferApproveView` → canonical `goods_services.decide` → `transfers.decide_approval` records distinct humans at every step. Intermediate steps move nothing; the last step invokes the retained P07 writer. Dispatch rechecks the pinned policy and inputs before P08. Unpinned historical submissions remain readable but require explicit validated resubmission. |
| R-INV-009, ST-INV-3, OQ-57; C08/A00/C10 | `StocktakeListCreateView` → `goods_counts.start_count` requires persisted online till pause/frontier and freezes the site. Blind scans stay blind. `StocktakeSubmitReviewView` → `count_review.submit` pins complete scope, original-cost shortages, all counters/preparer, selected pass hashes, pause, current reason and versioned `count.review`. Generic approval → `count_review._decide` rechecks the unchanged physical book and posts exact P13 reductions once or closes a zero-difference count without a journal. The retained closure writer releases the freeze. Gains, unresolved identity and encumbered/nonordinary shortages remain inactive. |
| R-POS-009, R-POS-010, R-INV-015; C10/C06/C05 | `ReceiveInbox` → `ReceiveDelivery` → `ReturnedGoodsAcceptance` → `ReturnedPiecesView` → existing `goods_sale.accept_returned_pieces` records physical acceptance against the original bill's portions/version. Stable site/brand authority, active ordinary destination and count freeze are checked. Good returns remain unavailable until P09 acceptance; damaged returns stay in quarantine. Replay returns the original accepted quantity from immutable audit evidence and creates no second effect. |
| PRD §4.3 / ST-OPS-5; A00/C05/C06 | Approval inbox/list/decision, receiving inbox and returned-piece reads replay resource/field/session demands immediately before delivery. The command kernel rechecks request-bound authority before execution and commit. Restricted actor names are projected separately; Owner/Admin configuration rights grant no protected business data. |
| R-FIN-015, R-POS-011; C13/C10 | Cash Count now discloses bills/movements arriving after today's saved count, showing next expected cash without changing the prior count or claiming the later activity is settled. This is disclosure, not a new cash-close/shift implementation. |

Additive migrations: `approvals.0017_transfer_route_steps`,
`approvals.0018_transfer_approval_subject`, `outbound.0048_trading_count_pause`.
Historical identifiers, approval outcomes, origins, bills and postings are not
rewritten. Multi-step execution is enabled only for the implemented transfer
adapter; other unsupported multi-step families fail closed.

## Proof identity and current checks

All database commands use identity-checking wrappers. Before database-dependent
commands the owned container and host are compared:

| Item | Verified identity |
| --- | --- |
| Project / container | `kdps-proof-ff7f4a176291` / `kdps-proof-ff7f4a176291-database-1` |
| Host / container port | `127.0.0.1:55433` / `5432` |
| PostgreSQL / user | `17.11` / `kdps_proof` |
| Host/container system ID | `7690899938284695588` |
| Baseline / disposable test DB | `kdps_proof` / `kdps_proof_test` |
| First-store sibling | `kdps_rehearsal_first_store_ce4e1b2ba562` |
| Operating browser copy | `kdps_rehearsal_operating_81646f160a58` |
| Fresh clean / upgrade rehearsals | `kdps_rehearsal_migrations_3a310201d66c` / `kdps_rehearsal_migrations_5c66285a94b9` |

The operating copies are explicitly cloned only from the sole synthetic ALPHA
proof registration. Earlier failed/successful copies and private identity
records remain preserved; `--fresh create` creates another sibling without
resetting or deleting any history. Backend/frontend ports: first-store
`8008/5178`, operating copy `8010/5180`, baseline `8000/5173`.

Exact commands and evidence:

```sh
backend/.venv/bin/python scripts/proof.py run --cwd backend -- .venv/bin/pytest tests/test_first_store_transfer_authority.py tests/test_first_store_trading_counts.py tests/test_first_store_stock_concurrency.py tests/test_first_store_goods_operations.py tests/test_first_store_return_acceptance.py tests/test_soh_reconciliation.py --reuse-db -q
backend/.venv/bin/python scripts/first-store-migration-proof.py
PLAYWRIGHT_BROWSERS_PATH=/Users/anand/Developer/kdps-code/.local/playwright-browsers yarn playwright test --config=playwright.first-store.config.ts
PLAYWRIGHT_BROWSERS_PATH=/Users/anand/Developer/kdps-code/.local/playwright-browsers yarn playwright test --config=playwright.first-store-operating.config.ts
backend/.venv/bin/python scripts/first-store-operating-proof.py run -- backend/.venv/bin/python scripts/first-store-operating-reconciliation-proof.py
backend/.venv/bin/python scripts/check_so03_consolidation.py --check
backend/.venv/bin/python scripts/check_so03_boundaries.py --check
backend/.venv/bin/python -m unittest scripts.test_check_so03_boundaries scripts.test_check_so03_consolidation
python3 scripts/baseline.py verify
git diff --check
```

Playwright commands run in `frontend`; others run at repository root. Logs stay
under `.local/full-alpha-*.log`; traces/video are disabled for credential-bearing
journeys. Focused suite: **65 passed in 76.34s**. Checker unit tests: **13 passed**.
Clean and accounts-0021 upgrade rehearsals passed, repeated migration had no
pending operations, original IDs/custom inactive policy were preserved, stale
plans were denied and revoked linked targets were not reactivated. Frozen
Python source SHA-256:
`5df2fced093301c7f87d7485d831cc246ca093bf11f1b2f453f4f6d2e8263499`.
The migration fixture's revocation remains a replay test; it is not an
independently reviewed compensating rollback rehearsal.

Current final checks on the runtime fingerprint above passed:

- Full baseline: **393 backend tests**, strict mypy (**606 files**), Ruff,
  **2 import contracts**, schema match (**505 paths**), frontend typecheck/lint/
  formatting, **49 frontend tests**, build/assets and **6 baseline browser tests**.
  The backend emitted five existing deprecation warnings; frontend lint emitted
  20 existing warnings and zero errors.
- Dedicated manager/POS/count browser suite: **18 passed**, including populated,
  empty, loading, denied and simulated-failure states at 1440, 1366, 768 and 375px,
  and different-human zero-difference count closure at each width.
- Operating browser: **1 passed** (desktop sale/retry, mobile exchange/physical
  acceptance, post-close disclosure). It is not four-width proof of every
  operating state or a new day-close.
- Inventory (**3,035 surfaces**), boundary checker, checker unit tests and
  `git diff --check` passed. Coverage counts are not a release verdict.

Operating read-only reconciliation: initial acceptance 3, total outgoing sales 3,
returned/accepted 1, remaining sellable 1; three distinct bills/accepted intents,
two cash tenders totalling 190,000 paise and matching cash ledger, balanced GL,
zero unexplained differences. The earlier 90,000-paise count stays immutable;
two later bills belong to the next count, expected 190,000 paise. The original
first-store sibling still has its single 90,000-paise bill/count, two sellable
units, and the real workbook staged inactive with no approved mapping/posting.

Earlier green results do not verify this revision. Diagnostic failures retained:
missing salesperson correctly blocked the sale; the first exchange test read
net from the finalisation envelope instead of the receipt; the proof browser
cache needed its explicit local path; the operating shop had already counted
that business day. A copy taken while the separate count browser had paused
the till also correctly blocked selling; the final copy was created serially
after that suite ended. No failure was resolved by deleting transactions, weakening
guards or replacing a saved count. Default baseline browser discovery now
excludes dedicated first-store suites that require their separate proof DB.

## Configuration and reconciliation gates

| Required condition | Current condition / owner / validation |
| --- | --- |
| Company/entity/GST registration and reviewed site identity | Vaishnavi at Singh More, Ranchi is the supplied trading name/location; legal entity/GST registration is still required. Owner/CA: review exact stable IDs, registration and business profile. |
| Separate Owner, Admin, manager and independently authorised reviewers | Synthetic separate identities are proof fixtures only. Owner/Admin: reviewed effective assignments with actions, fields, complete scope and working periods together; confirm password change, independent review and session invalidation. No direct personal expansion. |
| Current working calendar and privileged review coverage | Proof calendar exists; real calendar/holidays/coverage unverified. Owner/Admin: independently approve exact version and exercise scheduled boundaries/overdue review; no permissive fallback. |
| Transfer/count action upgrade, routes, roles, limits, reasons | Fixtures publish explicit approval/reason versions. Real family policy absent/unverified. Domain Owner/Admin: configure source and destination coverage, thresholds including unknown values, distinct reviewers, step-up and reason codes; prove both allowed and denied edges. Unsupported families/OQ-28 responsibilities remain inactive. |
| Trusted SKU/brand/source mapping | Real workbook staged as inactive preview only. SO-04/Owner: independently review stable-ID mappings; duplicate/renamed labels do not establish identity. Reconcile omitted items, zeroes, conflicting/foreign links and historical references. |
| Fresh full SOH/cutoff and intervening movements | The 28 September workbook is a format/snapshot reference; Rate is purchase cost before tax. Fresh export/exact cutoff is pending. Store manager/source operator: declare coverage and pause, reconcile all later movements and physical count; apply approved differences exactly once. Gains/unresolved identities stay inactive with owned exclusions. |
| Source quantity/value/tax proof | Original reference: 25,689 rows; 11,161 positive; 19,896 units. Missing HSN on positive rows and ₹0.38 aggregate Amount-vs-Rate discrepancy across 99 rows remain unresolved. Owner/CA/source operator: explain or approve attributable reconciliation; never silently round away differences. |
| Trading/printing/export controls | Online alpha and one fully reconciled paused till are proved; full offline remains SO-09. Delivery owners: configure tax, numbering, supported tenders, exact discount policy, receipt/print/export scope and feature switches. Test logout/expiry and delayed delivery at final source revision. |
| Cash custody/variance/day-close contract | Preserved zero-variance proof count and accurate later-activity disclosure exist. Independent online cash-variance review, blind shift/custody declaration and operational date-close evidence remain open with SO-12; unresolved OQ-08 responsibilities stay inactive. No counter PIN bypass is enabled. |
| Deployment and real activation | Owner/operator: prohibit unguarded admin/obsolete writers, agree single writer and operational window, approve read-only real reconciliation separately. No real-tenant read/write/cutover approval has been provided. |
| Compensating rollback | SO-03/Owner/Admin: rehearse reviewed compensating assignments/policy/mapping changes with fresh invalidation, protecting revocations, later edits and immutable business history. Do not restore old permission authority or reverse sales/postings. Still pending independent operational evidence. |

## Remaining acceptance work and order

1. Finish the SO-12 cash-close boundary and independent discrepancy review under
   approved pilot custody/responsibility policy; bind the exact declaration,
   expected components/cutoff, source bills/movements and deciding session.
2. Add current populated real-backend browser journeys for transfer review and
   destination receiving, fresh onboarding/SOH approval and repeated dated SOH
   reconciliation. Existing API/concurrency proof does not substitute for these.
3. Demonstrate independently reviewed compensating rollback and the remaining
   scheduled-boundary/configuration/browser contracts on disposable proofs.
4. Freeze final runtime sources, repeat affected tests/API/inventory/boundary,
   migration and baseline checks, then retain exact revision/evidence links.
5. Obtain the missing legal/people/calendar/configuration/mapping/fresh-cutoff
   inputs and separately authorised read-only real reconciliation. No shop may
   activate with unexplained stock/money differences or ambiguous identity.
6. After explicit production authorisation, execute the agreed operational
   cutover, invalidate sessions, prove one writer and allowed/denied journeys,
   check stock/bills/tenders/postings and verify compensating rollback readiness.

Acceptance requires all agreed active journeys, current permitted/denied and
replay/concurrency/delayed-delivery evidence, no unexplained quantity or monetary
difference, and an owned inactive exclusion for every unresolved identity or
responsibility. Passing this checkpoint or the baseline alone cannot close it.
