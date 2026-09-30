# First-store manager workspace: delivery and acceptance record

Recorded 30 September 2026. This record supersedes the provisional status in
[foundation evidence](kdps-first-store-alpha-evidence.md). The
[screen and responsibility map](kdps-manager-workspace-alpha-map.md) and
[goods operating evidence](kdps-first-store-core-acceptance.md) contain the
bounded handler/service/data traces and retained dependencies.

## Verdict and boundary

The consistent manager workspace and the supported first-store foundation are
implemented and currently verified on isolated fictional proof data. Today and
Sell retain their established design. Stock has one authorised-scope workspace;
daily Damage reads canonical goods stock; schedules, independent review and
earlier evidence have a secondary area. Planned placeholders are omitted from
daily manager navigation and remain labelled in the permission-filtered roadmap.

This is **not real-store activation or complete SO-03/SO-08/SO-09 acceptance**.
General assigned blind counts at a trading store remain inactive, the transfer
approval-family route/limit pinning dependency is incomplete, and some retained
specialist/history/report/RTV readers still await their workflow-owned cutover.
Later SOH reconciliation supports reviewed zero deltas and supported shortages;
gains or unresolved ownership/cost/custody stay inactive. These limits are visible
and are not overridden by navigation, a passing baseline or this record.

The unchanged Vaishnavi source is staged only in the fictional preview store.
No source rows have been posted into a real tenant. SBU retirement stays with
SO-04; full offline till cutover stays with SO-09. Other business owners retain
their writers, history and retirement decisions.

## Revision identity and reproducibility

- Starting HEAD: `e6cfc9734e5c20b779d9849473394b66c412b973` on `dev`.
- Foundation start, complete tracked/nonignored tree: SHA-256
  `bbb6eed6d3accade33ccfa7b8e0466a455aec5e7ce333c7b0d7dab41719df9d4`, 1,304 files.
- Manager slice start: `b0966ec95802689e86574146009a80c3af3254f739c22dc00224b920fc594ca5`.
- Final runtime source freeze: SHA-256
  `c9ac208792eb62d58c87f4bd2605c4c42cd2ee5469940386bf5e15ad08049b93`,
  1,362 tracked/nonignored files under `backend/`, `frontend/`, `scripts/`.
- The complete final tree fingerprint and resulting ordered commit IDs are
  emitted after this documentation is written, in the delivery message and
  `.local/manager-alpha-final-fingerprint.json`. The document does not contain
  its own hash. Local runtime records and credentials are ignored and excluded.

For either scope, obtain sorted unique NUL-separated paths with
`git ls-files --cached --others --exclude-standard -z`. For each existing path,
hash `path bytes + NUL + decimal byte length + NUL + file bytes + NUL` with
SHA-256. Hash a symlink's link text, and skip missing deleted paths. The complete
tree includes the preserved untracked raw workbook; the runtime-source scope
does not. Committing an unchanged file does not change either content hash.

The six commits are ordered as foundation, shared layout, stock/navigation,
operational fixes, POS regression adaptation, and acceptance evidence. Checks
validate the aggregate final source, not each intermediate commit as a separate
deployable cutover. No reset, discard, business-history rewrite or remote push
was performed. Credentials and the raw workbook are excluded from commits.

## Positively identified verification environments

Identity was recorded before database-dependent checks and compared again by
the proof wrappers before dispatch. Unidentified databases were not probed.

| Property | Verified identity |
| --- | --- |
| Project / container | `kdps-proof-ff7f4a176291` / `kdps-proof-ff7f4a176291-database-1` |
| Host / mapped container port | `127.0.0.1:55433` / `5432` |
| PostgreSQL / server version number | `17.11` / `170011` |
| Host and container system identifier | `7690899938284695588` |
| Baseline database / user / disposable test database | `kdps_proof` / `kdps_proof` / `kdps_proof_test` |
| Preserved first-store sibling | `kdps_rehearsal_first_store_ce4e1b2ba562`, same cluster/user |
| Dedicated manager browser | proof sibling backend `127.0.0.1:8008`, frontend `127.0.0.1:5178` |
| Baseline browser | normal proof backend `127.0.0.1:8000`, built preview `127.0.0.1:5173` |

The normal owned browser servers were stopped for the baseline launcher test;
only their positively identified processes were stopped. Other processes and
databases were preserved. The normal first-store browser is restored afterwards.
ASGI connection retention is now zero to avoid retaining a connection for every
short-lived worker thread; no PostgreSQL server configuration was changed.

## Current checks and exact commands

| Command from repository root unless specified | Current result / limitation |
| --- | --- |
| `python3 scripts/baseline.py verify` | Exit 0: Django checks, no missing migrations, Ruff, strict mypy (600 files), two import contracts, **359 backend tests**, 504-path API contract, frontend types/lint/format, **49 frontend tests**, build/assets, **six standard browser regressions**, seed repeatability and launcher readiness/cleanup. |
| `PLAYWRIGHT_BROWSERS_PATH=../.local/playwright-browsers node node_modules/@playwright/test/cli.js test --config playwright.first-store.config.ts` in `frontend/` | **14 passed**, 1.2m, actual first-store backend at all four widths. No browser retry configured. |
| `backend/.venv/bin/python scripts/proof.py run --cwd backend -- /Users/anand/Developer/kdps-code/backend/.venv/bin/python -m pytest -q tests/test_first_store_day_delivery.py tests/test_first_store_targets.py tests/test_soh_import.py` | **37 passed**, 105.50s, after correcting immediate-revocation/cutoff test setup to use PostgreSQL time. Included again in the full baseline. |
| `backend/.venv/bin/python scripts/check_so03_consolidation.py --write` followed by `--check` | Reviewed refreshed inventory/register/map; **matches 3,021 discovered surfaces**, protected boundary checks passed. Inventory coverage does not establish resource acceptance. |
| `backend/.venv/bin/python -m unittest scripts/test_check_so03_consolidation.py scripts/test_check_so03_boundaries.py` | **13 passed**. |
| `backend/.venv/bin/python -m ruff check scripts/first-store-*.py` | Passed, including isolated-source migration and read-only reconciliation helpers. |
| `backend/.venv/bin/python scripts/first-store-migration-proof.py` | Clean/current and accounts-0021 upgrade from a frozen isolated source copy, repeat migration, stale-plan refusal, preserved IDs/custom inactive policy, one linked assignment and revoked-target replay. See [migration evidence](kdps-first-store-migration-evidence.md). |
| `backend/.venv/bin/python scripts/first-store-proof.py run -- backend/.venv/bin/python scripts/first-store-day-close-proof.py` | Current fictional cash-count/document-series/tax-settings switches independently reviewed through password-confirmed Admin API and different Owner API. Original early fixture events retained; this is no real tax/CA approval. |
| `backend/.venv/bin/python scripts/first-store-proof.py run -- backend/.venv/bin/python scripts/first-store-reconciliation-proof.py` | Passed read-only assertions below, with identical business fingerprint before/after. |
| `git diff --check` | Passed; rerun on the final delivery tree. |

Five backend warnings concern the absent development `backend/staticfiles/`
directory. ESLint reports 20 existing warnings and zero errors. The build's
chunk-size advisory remains. These are recorded, not hidden or represented as
additional verified functionality. Earlier baseline failures are not green
evidence: an initial run had 356 passes/three clock-dependent test failures;
the next had 357 passes/two immediate-revocation test failures. All five cases
now use the authority's database clock and pass in the final 359-test run.

The browser journeys verify populated stock, empty searches, preserved filters
and explicit all-authorised scope, invalid-store bookmarks, keyboard-reachable
internal table scrolling, compact Damage controls, count/receiving/transfer
empty or denied states, the manager's real Money-scoped target row, Sell draft
layout, bills and tenders, saved cash count, daily totals, and delayed reprint
refusal after actual logout. Loading is a delayed real response; simulated 503
and transport failures test presentation only. Inventory report denial in this
fixture is **feature off**, not configured-report permission acceptance.

The first browser cash journey saved the fictional zero-variance count once;
repeated runs read that saved count and the empty subsequent drawer window.
The existing fictional sale was issued through the real browser in the earlier
first-store journey. Committed manager tests read its accepted bill and draft
another cart without issuing another sale. Actual sale issue/retry/concurrency,
exchange, receiving, transfer and damage effects are additionally exercised by
the backend integration tests; the 14 browser cases do not establish every
populated detail/modal, every role or every delayed export channel.

Final in-app browser inspection also confirmed the manager can reach **Receive
Goods → Opening stock** and its scoped upload/history controls, then return to
Stock showing two units. Compact canonical Damage filters were inspected in
the same restored application. Fictional screenshots are retained privately in
`.local/first-store-browser-evidence/10-manager-stock-final.png`,
`11-manager-damage-final.png` and `12-manager-opening-final.png`; they contain
no credentials and are excluded from commits.

## Stock, bill, tender and history reconciliation

The read-only helper refuses all but the identified fictional ALPHA/FIRST
registration and records its database identity before reading. It verifies:

| Evidence | Verified value |
| --- | --- |
| Reviewed/accepted initial source | One source, three units |
| Remaining sellable shelf | Two units |
| Accepted bill | One `26-27/FIRST/SAL/1`, net ₹900 |
| Online accepted intent / tender / cash collection | One each, ₹900 |
| Value ledger sum for that bill | Zero |
| Cash count | One count containing that bill; expected/count ₹900, variance ₹0 |
| Current three explicit feature switches | Enabled revision 2; each has a different captured authorised reviewer |
| Business-row fingerprint before and after the read | `9085b0d6be05b7bb1052c73c123bf9ebef649a69a4929a50784f3df106642c6b` |
| Real workbook preview | Uploaded revision 1, unmapped, no approval/batches/positions, not sell-ready |

This proves the fictional journey reconciles without another opening, sale,
tender or posting. It does not reconcile a real store or validate the supplied
workbook's tax/identity/valuation inputs.

## Configuration and reconciliation checklist

All “verified” entries below refer to code or fictional proof configuration.
No real configuration, mapping or deployment approval is implied.

| Required setting / decision | Current verified condition | Required activation condition | Responsible owner / validation |
| --- | --- | --- | --- |
| Installation identity and registration | One deployment-bound, jointly confirmed genesis; signup closes permanently after tenant creation; password-change restriction enforced | Real legal entity/store and separate Owner/Admin signatures; secure deployment binding | Owner/Admin + SO-03/SO-04; blank-install and duplicate/foreign/CSRF tests, review actual legal/GST/address/timezone data |
| Six roles/action upgrades/policy | Six configurable roles; genesis pins complete initial registry; `opening.import.stage` is separate from valuation preparation; request authority derives assignments | Existing installations deliberately upgrade approved actions without silently widening protected fields; preserve custom/revoked policies | SO-03/independent reviewer; policy version and per-cell before/after migration report, mixed assignment and action-denial tests |
| Manager/team assignments | Fictional personal identities and scoped operating assignment created through ordinary administration and reviewed | Stable real human IDs; store/brand/actions/fields together; no direct personal expansion | Owner/Admin + SO-03; own-expansion, wrong-store, field withdrawal, retirement and session tests |
| Working calendar and privileged review | Approved proof `working_calendar` version 1; separate review captured; feature commands require password/session invalidation | Approved real calendar/timezone/working periods and independently authorised reviewer within due period | Owner/Admin + SO-03; scheduled boundaries and review-history evidence; absence remains inactive |
| Workflow authority/routes/limits | Opening and receipt have approved proof `approval` versions; current action levels and commit demand replay verified | Every enabled family must pin complete scope, source revision/hash, policy version, limits and separate people; unresolved OQ-28 families inactive | SO-03 and each workflow owner; source adapters, limit edges, policy withdrawal/concurrency/downstream proof. Transfer family dependency remains incomplete |
| Stable item/brand/season/size ownership | Fictional reviewed source IDs; immutable accepted-origin barcode propagation and public description lookup tested | Explicitly reviewed real source mappings; duplicate/renamed labels never establish authority | SO-04 + receiving/report owners; reviewed binding manifest, foreign/conflict/rename tests and historical links |
| SOH source/cutoff/fullness | Real workbook parsed/staged only; reviewed initial parent fence and supported later snapshot shortages tested | Fresh store-specific export/cutoff and subsequent movements, all omitted/zero rows declared, physical verification, frozen frontier/watermark | Store manager prepares; warehouse/finance and independent reviewer; explained quantities/monetary differences, exact source hash and zero duplicate effects |
| Valuation/HSN/MRP | User confirms Rate is purchase cost before tax; proof values/tax/mappings complete | All real positive rows have trusted HSN and prices; explain −₹0.38 Amount difference across 99 rows, missing sizes/seasons and price anomalies | Finance/CA + source owner; basic cost/MRP/Amount reconciliation and approved exceptions |
| ConfigVersion spine | Proof business profile, identity profile, vocabulary, rates, profile, tax rates, selling policy and calendar each effective version 1; series published versions 1–4 | Real effective versions and correct scope/ownership; no ambiguity, withdrawal or stale pin | Owner/Admin + business/CA reviewers; setup gates and revision-specific read/write tests |
| Commercial policy/features | Proof `sell_policy` 10% manual cap; synthetic tax only; three current switches independently reviewed at revision 2 | Real approved discount/offer/tax policy, CA/statutory evidence and only accepted feature contracts enabled | SO-06/SO-09 + CA; edited-threshold, price/offer recheck, exactly-once issue and field denial tests |
| Numbering/counter/readiness | Fictional prefix/series, active paired online device, physical acceptance and approved goods/selling readiness | Current-year reconciled document series/device frontier, real physical/readiness sign-off | SO-07/SO-09 + Owner/Admin; numbering replay, expired/retired allocation, uncertain outcome and frozen-site tests |
| Cash/day close | Fictional zero variance count and daily totals reconcile; variance requires recorded independent authority | Reviewed real cash feature, opening float/tenders/posting configuration and discrepancy review contract | SO-12/SO-16 + finance; exact expected/count/replay, unavailable counter and independent review |
| Reports/exports/history | Main stock/day/bill projections verified; protected delivery rechecked; report placeholders hidden | Explicit independently reviewed report features, canonical source/totals and delayed protected delivery evidence | SO-03/SO-13 + report owners; feature-on allowed/denied and export/download/print tests. Specialist readers are not accepted by menu changes |
| Deployment restrictions | Proof mode only accepts owned isolated targets; generated credentials private; ASGI request connections close | Production secure cookies/HTTPS/origins, secret deployment key, real synthetic/proof mode disabled, owned environment | Platform owner + SO-02/SO-03; configuration review and controlled smoke tests without printing secrets |
| Retirement/rollback | History/source IDs retained; legacy granting evidence not revived; SBU/offline retirement unchanged | Attributable real cutover and compensating rollback rehearsal, no deletion or restored legacy permission source | SO-03/SO-04/SO-09 and workflow owners; evidence-preserving review/session invalidation and post-cutover reconciliation |

The raw source SHA-256 remains
`033b5d6dbd7943ba3e8cd07861d1ea1ba0822df65e560698220b6745d8ffb715`
(2,590,284 bytes). Its 25,689 rows include 11,161 positive rows / 19,896 units;
all positive rows need evidenced HSN. The source is preserved and uncommitted.

## Owned incomplete work and real activation sequence

1. **SO-03/SO-08 transfer approval family:** pin tenant-owned versioned routes,
   limits, exact source/destination cells/fields and frozen inputs before
   decision/posting. Preserve existing IDs/history and validate stale requests,
   separate checker, limits and downstream exactly-once effects. See the
   concrete transfer handler gap in goods operating evidence.
2. **C08/SO-08 trading counts:** obtain the approved trading assigned-count
   contract/OQ-57 decision; implement its named writer and independent review
   rather than removing the current non-trading safeguard. Accept only with
   scheduled-boundary, blind-field, freeze/frontier, variance and correction
   evidence. Unsupported snapshot gains remain inactive meanwhile.
3. **C06/C09/SO-13 readers and specialist channels:** close the retained
   stock-history/RTV/specialist report source and delivery traces individually;
   retain owners/legacy evidence until their named retirement tests pass.
4. **Real reconciliation, separately authorised:** positively identify the
   real read-only environment, review source/target authority, legal identity,
   stable mappings, business history, fresh cutoff and all quantities/values.
   Approve explained exclusions, real tax/features/calendar/family configuration
   and independently authorised operational review. Do not import this old
   workbook as a live opening or overwrite an existing store balance.
5. **Final cutover:** freeze exact deployable revision/fingerprint, rerun
   affected proof/browser/migration checks, approve the operational window and
   compensating rollback evidence, deploy reviewed versions, invalidate
   sessions and smoke-test allowed/denied journeys. Reconcile stock, bills,
   tenders and postings again before removing any fence.

Completion requires current revision-specific evidence for every active core
contract, no unexplained authority/quantity/monetary expansion, one business
writer, and owned inactive exclusions for unresolved identities/responsibilities.
The remaining product input is the real legal/tax/mapping/cutoff review and the
owned trading-count/approval-family decisions; no UI approval is requested again.
