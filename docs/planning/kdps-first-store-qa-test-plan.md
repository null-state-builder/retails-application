# First-store alpha QA test plan and execution record

Shop: Vaishnavi, Singh More, Ranchi. Recorded 30 September 2026.
This is the complete acceptance checklist for the agreed first-store scope;
it is not a claim that every case is implemented or passed.

## Tested source and environment

- HEAD: `3a16c616c259376598b293d4e89e9bc9e3899a66`.
- Runtime SHA-256: `8a025f53ac3c0c5c0c88f724ff1229e3e2896a70498af5929bd491e299ca43f1`.
- Before this QA document, complete working-tree SHA-256:
  `1ae12d7200386dbf8f7e06f73d0682f505071161d3fa95f98b190242907f5d1c`.
- Fingerprint algorithm: sorted unique existing paths from
  `git ls-files --cached --others --exclude-standard -z`; concatenate path,
  NUL, decimal byte length, NUL, contents, NUL into SHA-256. Symlinks use target
  text. Runtime excludes `docs/`; ignored credentials are excluded.
- Owned project/container: `kdps-proof-ff7f4a176291` /
  `kdps-proof-ff7f4a176291-database-1`; host `127.0.0.1:55433`, container `5432`.
- PostgreSQL `17.11`, user `kdps_proof`, system identifier
  `7690899938284695588`, verified against both container and host before tests.
- Baseline/test databases: `kdps_proof` / `kdps_proof_test`.
- Manager browser database: `kdps_rehearsal_first_store_ce4e1b2ba562`,
  API/UI ports `8008/5178`. Operating browser uses a separately recorded fresh
  synthetic ALPHA copy on `8010/5180`.

Use synthetic stock and distinct test humans. Never use real tenant balances,
real credentials or the reference workbook as automatically approved stock.
Preserve every prior proof database, document and posting. Disable browser
traces/video for credential-bearing journeys; keep local logs private.

## Execution and result rules

For each case record case ID, revision/runtime fingerprint, environment,
actor's stable identity and assignment version, steps, expected result, actual
result, evidence path and defect ID. Use **PASS**, **FAIL**, **BLOCKED** or
**NOT RUN**. A denial passes only when the expected denial is observed and
protected contents and business mutations are absent. A server error is not a
successful denial. API coverage does not imply browser coverage.

For mutation cases capture before/after quantities, origins, bill numbers,
tenders, ledger/posting IDs and approval decisions. Compare monetary values
in paise. Reconcile all differences, including exclusions. A matching total
alone does not prove stable identity or exactly-once application.

## End-to-end acceptance cases

Unless stated otherwise, perform the positive path with authorised scoped
actors, then repeat with wrong store/brand, missing field/action, mixed
assignments and revoked/expired session. Refusals must preserve history.

| ID | Steps | Required result / evidence |
| --- | --- | --- |
| SET-01 | Register a fresh fictional company with legal name, trading name, GST/business details and contact; complete first store setup. Refresh and log in again. | One company/store with stable IDs; required fields validated; no duplicate registration on retry. Record the actual submitted fields and resulting IDs. |
| SET-02 | Create separate Owner, Admin, manager and reviewer humans. Assign each explicit actions, fields, complete scope and working periods. | Six initial roles follow configurable policy/fixed safeguards; administration does not grant protected business data. Manager sees only assigned stores. |
| SET-03 | Attempt direct personal assignment expansion; change privileges through the supported administration flow. Confirm password; independently review with another authorised human. | Personal expansion refused. Changes versioned and attributable; affected sessions invalidated. Same human using a second login cannot independently review. |
| SET-04 | Configure working calendar, scheduled assignments, action upgrades, reasons, transfer/count policy versions and limits. Test before/at/after effective boundaries and overdue review. | Missing/ambiguous responsibilities remain inactive. No role-name or legacy-policy fallback; exact versions and independent review recorded. |
| SOH-01 | Manager selects a store and uploads the reference-format file into preview. Verify filename/date, quantities, pre-tax purchase Rate, duplicate rows and stable source keys. | Upload remains staged; no sellable stock or journals. The real reference is 25,689 rows, 11,161 positive rows and 19,896 units; missing positive-row HSN and ₹0.38 Amount discrepancy remain explicit unresolved checks. |
| SOH-02 | Map synthetic rows to reviewed stable SKU/brand IDs. Try duplicate names, renamed labels, conflicting/foreign IDs and unmapped rows. | Labels never establish authority. Conflicts/unresolved rows remain inactive; mapping review/version and reviewer are attributable. |
| SOH-03 | Submit a valid opening import; maker attempts approval; another independently authorised reviewer approves; physically accept to an active ordinary store location. | Maker refused; stock unavailable before physical acceptance; exactly one opening effect afterwards; original source/cost/identity links retained. |
| SOH-04 | Re-upload the identical accepted snapshot and retry interrupted approval/application requests. | No duplicate opening balance, origins, approval or journals. Repeat returns existing outcome or explicit reviewed reconciliation requirement. |
| SOH-05 | Upload a later full-store snapshot with exact cutoff, declared complete coverage and intervening sales/returns/transfers. Review and apply differences. | Approved differences applied once against the reconciled book, never the entire snapshot again. Earlier sales/approvals/journals unchanged. |
| SOH-06 | Include explicit zeroes, omitted items, partial coverage, negative/invalid quantity and unsupported gains. | Full-store omission/zero semantics explicit; partial/unaffirmed coverage refused; gains unresolved until supported owned custody; exclusions disclosed without leaking denied resources. |
| SOH-07 | Change source stock, mapping or policy after preview/freeze; interrupt after the position writer; retry/cancel. | Stale decision refused. Atomic rollback leaves no partial stock/value effect; retry applies once; cancellation retains evidence and releases only its own freeze. |
| POS-01 | Pair authorised online till, synchronise, select salesperson, scan accepted SKU, use configured discount and supported payment, finalise. | Correct store/price/tax/discount/net; one bill, tender, stock deduction and balanced posting; receipt matches saved bill. |
| POS-02 | Lose the response after the server commits; retry the same sale key. Submit concurrently from two workers. | Original bill/outcome returned; one accepted intent, one deduction and one tender/posting effect. No oversell. |
| POS-03 | Try paused/unsynchronised till, frozen store, unknown SKU, insufficient stock, unauthorised discount/tender and stale price/policy. | Appropriate actionable refusal; no mutation or permissive preview fallback. |
| POS-04 | Open bill history and receipt; change filters; request reprint/export then logout/revoke before delivery. | Scope/fields independently projected; bytes/frame denied after invalidation; no protected contents in denial response. |
| RET-01 | Find original bill, select eligible item, exchange using supported equal/higher-value contract and configured settlement. | Original bill unchanged; distinct exchange bill/return links; balanced tender/stock effects. Unsupported cash refund, credit, no-bill or cross-store contract stays inactive. |
| RET-02 | Receive a good customer return; choose ordinary destination and confirm physical inspection; retry acceptance. | Pending return not sellable until acceptance; original origin/brand retained; repeat returns original accepted quantity with no second P09 effect. |
| RET-03 | Accept to wrong store, system/retired location, unresolved brand or frozen store; inspect damaged return. | Refused with no history changes. Damage remains quarantine, unavailable for normal sale. |
| STK-01 | Search active store, then All authorised stores; change query/filter and reload; open old search bookmarks. | URL preserves intent. Only authorised availability appears; cost/valuation separately controlled; incomplete totals labelled. |
| RCV-01 | Complete supported supplier receiving/count/review/physical acceptance with differing quantity and damaged lines. | One canonical writer, stable source links, independent required review; sellable stock only after required acceptance. Historical reads remain accessible under scope. |
| TRF-01 | Prepare transfer using trusted source stock and both-site/brand cells. Submit configured multi-step route; maker and prior checker attempt later steps. | Source/hash/revision/cost/policy/limits pinned; each step uses a separate independently authorised human; no reservation until final required approval. |
| TRF-02 | Replace/withdraw policy or change quantity/revision after submission; explicitly resubmit. Test quantity/value limit edges and unknown values. | Stale decisions/dispatch refused; old decisions retained; route never silently shortened. Unpinned historical requests require validated resubmission. |
| TRF-03 | Dispatch approved transfer; destination receives, counts good/damaged/short and physically accepts; retry each operation. | One movement per command; source/in-transit/destination quantities and value reconcile. Actual Receiving UI must reach supported transfer detail. Populated browser journey still required. |
| DMG-01 | Report scoped damage; inspect quarantine; attempt sale, unauthorised release and repeat report. | Exact quantity/state movement once, original cost/history retained; quarantined goods excluded from sellable stock. |
| CNT-01 | Start trading count before pause, then after every online till is synced/paused. Capture assigned blind counts. | Unpaused start refused; valid start freezes store; counters cannot see book quantity/variance/cost without independent rights. |
| CNT-02 | Submit selected passes and reason. Maker/counters attempt review; independent checker approves zero or shortage. | Exact pass/hash/book/pause/policy pinned; zero closes without inventory journal; shortage posts exact original portions once; closure releases freeze. |
| CNT-03 | Change book by damage, policy or scope after capture; attempt gains/encumbered reductions; race sale against freeze or duplicate decisions. | Stale/unsupported cases inactive; no partial posting/oversell; at most one deciding effect. |
| CASH-01 | Declare opening custody/float, take supported payments, enter blind denomination count, submit exact cutoff. | Required blind custody/shift/day boundary must be approved first. Current store/day screen is not proof of blind per-person shift close. **BLOCKED acceptance contract.** |
| CASH-02 | Submit a discrepancy with reason; attempt own approval/PIN bypass; another independently authorised reviewer decides. | Versioned exact inputs/cutoff and independent review required. Current online variance path intentionally refuses; **BLOCKED functionality.** |
| CASH-03 | Inspect saved zero-variance count; make later sales/movements; revisit cash and daily report. | Prior count immutable; later activity clearly unsettled; next expected cash accurate. This does not constitute a second day-close. |
| DAY-01 | Complete all required cash/stock review, close the day, read report and retry close. | Explicit closed date/custody contract, no unsettled activity silently dropped; unique close and reconciled totals. Final operational close remains open. |
| REC-01 | Independently reconcile opening + receipts + accepted returns − sales − dispatches − approved losses against ending stock, including in-transit/quarantine. | Zero unexplained quantity/value differences by stable resource; exclusions owned and inactive. |
| REC-02 | Reconcile bills, discounts, returns, tender components, cash movements/counts and GL per bill/day. | Paise-exact reconciliation, balanced postings and no duplicate bills or cash effects. Saved count windows preserved. |
| MIG-01 | Fresh install, accounts-0021 upgrade, interrupted/repeated migration, stale plans, reviewed exclusions and revoked targets. | Stable historical IDs/evidence preserved; no unexplained effective-authority expansion; repeat idempotent and later edits/revocations protected. |
| RBK-01 | Rehearse password-confirmed, independently reviewed compensating assignment/policy/mapping changes after new business transactions. | Fresh invalidation, revocations/later edits and transactions preserved; no old authority restored. **Independent operational rollback evidence pending.** |
| ACT-01 | Review real company/people/calendar/tax/mappings, fresh export/cutoff and physical count; separately authorise read-only reconciliation and production window. | Activation blocked until all inputs reviewed, differences explained and explicit production authorisation obtained. No real-tenant QA writes. |

## UI, accessibility and delivery matrix

Run each reachable core list, populated detail, form and modal at **1440, 1366,
768 and 375px**, height 1000. Include Today, Sell, setup/team, SOH preview/mapping/
review, Stock, receiving/customer return, transfers, damage, counts/review,
bills/receipts, cash and daily reports.

For each screen test populated, empty, loading, field/action denied, validation
failure, backend failure and session expiry. Confirm common padding/headings,
compact aligned filters, readable errors and usable actions. No page overflow;
wide tables scroll inside a labelled keyboard-focusable region. Tab order,
Enter/Space activation, visible focus, modal focus/escape and labels must work.
Do not accept a hidden button as proof of server denial. Verify print/download/
export, notifications/events and mapped jobs recheck actual recipient/session
action, fields, complete source scope and current policy at delivery/commit.
Record synthetic presentation failures separately from real backend failures.

## Commands and current execution

Run from repository root except browser commands, which run in `frontend`:

```sh
backend/.venv/bin/python scripts/first-store-proof.py status
python3 scripts/baseline.py verify
python3 scripts/baseline.py backend
python3 scripts/baseline.py frontend
python3 scripts/baseline.py api-check
backend/.venv/bin/python scripts/first-store-migration-proof.py
PLAYWRIGHT_BROWSERS_PATH=/Users/anand/Developer/kdps-code/.local/playwright-browsers yarn playwright test --config=playwright.first-store.config.ts
backend/.venv/bin/python scripts/first-store-operating-proof.py --fresh create
PLAYWRIGHT_BROWSERS_PATH=/Users/anand/Developer/kdps-code/.local/playwright-browsers yarn playwright test --config=playwright.first-store-operating.config.ts
backend/.venv/bin/python scripts/first-store-operating-proof.py run -- backend/.venv/bin/python scripts/first-store-operating-reconciliation-proof.py
backend/.venv/bin/python scripts/check_so03_consolidation.py --check
backend/.venv/bin/python scripts/check_so03_boundaries.py --check
backend/.venv/bin/python -m unittest scripts.test_check_so03_boundaries scripts.test_check_so03_consolidation
git diff --check
```

Create the operating copy **after** the manager count suite finishes, so it
does not snapshot an intentionally paused till. Never reset a successful copy
to replay sales. Full baseline requires free ports 8000/5173; preserve any
existing user application and report contention instead of stopping it.

Current execution (same runtime fingerprint):

| Check | Current result | Private local evidence |
| --- | --- | --- |
| Full backend baseline | **FAIL: 376 passed, 17 failed**, five warnings; system checks, migration drift, Ruff, strict mypy (606 files) and two import contracts passed | `.local/qa-current-backend.log` |
| Frontend | **PASS: 49 tests / 13 files**, typecheck, lint and formatting; existing lint warnings retained | `.local/qa-current-frontend.log` |
| Production build/assets | **PASS** | `.local/qa-current-build.log` |
| API contract | **PASS: 505 Django paths** | `.local/qa-current-api.log` |
| Manager/POS/count browser | **PASS: 18 tests**, four widths, including separate-human count review | `.local/qa-current-browser.log` |
| Fresh operating browser | **PASS: 1 journey**, sale response-loss retry, exchange, physical acceptance and post-close disclosure | `.local/qa-current-operating-browser.log` |
| Read-only operating reconciliation | **PASS: zero unexplained stock/money differences**; opening 3, sold 3, accepted return 1, remaining 1; three bills/intents, cash tenders/ledger 190,000 paise, GL sum zero. Existing count 90,000 paise preserved; two later bills unsettled; **new day-close not verified** | `.local/qa-current-reconciliation.log` |
| Clean/upgrade migration rehearsal | **PASS**, repeated migrations, source IDs/custom inactive policy preserved, stale plan refused, revoked target not reactivated | `.local/qa-current-migrations.log` |
| Consolidation/boundaries/checker tests | **PASS: 3,035 surfaces; 13 checker tests** | Current terminal execution |
| `git diff --check` | **PASS** | Current terminal execution |
| Aggregate `baseline.py verify` | **BLOCKED at launcher**: existing app occupies 8000/5173; no application was stopped. Components above run separately. Default six baseline browser cases not rerun in this QA pass. | `.local/qa-current-baseline.log` |

Fresh operating database: `kdps_rehearsal_operating_b91ab5d17e6c`.
Clean/upgrade databases: `kdps_rehearsal_migrations_4a04ad16438a` /
`kdps_rehearsal_migrations_1326d1dc217a`; same verified proof cluster/user.
Migration frozen Python SHA-256:
`5df2fced093301c7f87d7485d831cc246ca093bf11f1b2f453f4f6d2e8263499`.

### Current defects and diagnostic boundaries

- **QA-D01: 12 SOH reconciliation failures**. Preparation frequently rejects
  the test cutoff as later than command time. One observed host-generated
  cutoff was `14:57:39.652240Z`, while the command database time was
  `14:57:39.650519Z`. Another case passed preparation but failed the
  pause/cutoff/source-created ordering check. Host/database clock ordering is
  a diagnostic lead, not a verified complete cause. Keep future-cutoff and
  pause safeguards; make source/test chronology trustworthy.
- **QA-D02: three online snapshot concurrency failures** in
  `test_first_store_online_concurrency.py` fail source preparation on cutoff
  before reaching their intended race assertions. Those assertions are not
  currently verified by this run.
- **QA-D03: two registration failures**. Immediate effective Owner authority
  was empty in `test_atomic_joint_claim_has_complete_versions_and_only_two_restricted_logins`;
  the newly issued session received 401 instead of the expected setup response
  in `test_setup_summary_does_not_advertise_ready_from_a_stale_selling_flag`.
  Investigate assignment/session timing independently; no bypass is justified.
- A targeted reproduction command is
  `backend/.venv/bin/python scripts/proof.py run --cwd backend -- .venv/bin/pytest tests/test_soh_reconciliation.py tests/test_first_store_online_concurrency.py tests/test_installation_registration.py -q --reuse-db`;
  its separate evidence is `.local/qa-current-failure-rerun.log`.
  **Rerun: 22 passed, 15 failed in 119.67s.** Failures remain reproducible,
  with varying immediate-time cases: the counter-resume case and setup-summary
  case passed this time. This variability supports investigation of chronology;
  it does not clear the original failures or justify disabling timestamp checks.

Historical counts in `kdps-full-alpha-acceptance-progress.md` are not reruns.
The current failures supersede its earlier green backend result for release
assessment, even though runtime sources are unchanged. No safeguards were
weakened and no business history was reset to obtain these results.

## Release decision

Accept only after all agreed active cases have revision-specific evidence,
every safety failure is fixed or the affected contract explicitly inactive,
zero unexplained stock/money differences, independent review, delivery checks
and compensating rollback proof. Real activation additionally requires legal/
GST details, named separate people, approved custody/calendar/policies, trusted
mappings/tax and fresh SOH/cutoff. SBU retirement stays with SO-04; full offline
till cutover stays with SO-09. Full alpha remains **OPEN**.
