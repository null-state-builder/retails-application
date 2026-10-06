# First-store alpha QA test plan and execution record

Shop: Vaishnavi, Singh More, Ranchi. Recorded 30 September 2026.
This is the complete acceptance checklist for the agreed first-store scope;
it is not a claim that every case is implemented or passed.

## 3–4 October 2026 initial execution (Phase A)

Baseline recorded before runtime edits. Starting HEAD:
`c6abc3d1467d129b20e917978fe2f892c84be4dc`; runtime SHA-256:
`80661162735ba43c5db9c32df7dbc914cf257e74abf62984859212f62c692a99`;
complete starting SHA-256:
`04a91aa1ae92bd7b1304a16140f0eadab6aead194c852bc2636885619d4cf503`.
The fingerprint algorithm below is unchanged; private manifest:
`.local/goal-alpha-20261003-start-fingerprint.json`. Existing untracked
`.claude/` and supplied workbooks/data were preserved and not used for QA writes.
`docs/product/` and `docs/ui-design/` are absent in this checkout.

The prior proof volume was absent when this run began. `proof.py up` created
an isolated PostgreSQL 17.11 cluster with system ID `7692508198834323494`,
user `kdps_proof`, project/container `kdps-proof-ff7f4a176291` /
`kdps-proof-ff7f4a176291-database-1`, host `127.0.0.1:55433`. The old sibling
`kdps_rehearsal_first_store_ce4e1b2ba562` does not exist on this cluster.
Prior private identities and opening artifacts were preserved in
`.local/goal-alpha-20261003-preserved-previous-cluster/`; no database was
reset or deleted. New blank sibling:
`kdps_rehearsal_first_store_c92c9c32eb49`. Baseline/test DBs remain
`kdps_proof` / `kdps_proof_test`. Ports 8000/5173 were free: launcher and
baseline browser owned and released their processes; unrelated containers
were left running. Dedicated ports remain 8008/5178 and 8010/5180.

| Check | Initial result at starting runtime | Private evidence |
| --- | --- | --- |
| Aggregate verify | **FAIL** at seed-repeatability: references retired `data_steward` role. Remaining components run separately. Browser invocation override disables credential traces (`--trace off`). | `.local/goal-alpha-20261003-initial-baseline.log`; `.local/goal-alpha-20261003-baseline-run.py` |
| Backend baseline | **FAIL**: eight strict mypy errors in dataset/master-sheet code and tests; Django check, migration drift and Ruff **PASS**. | `.local/goal-alpha-20261003-initial-backend.log` |
| Full backend behavior | **PASS: 421 tests**, five staticfiles warnings; original QA-D01/D02/D03 tests pass in this run. This does not disprove the registration mixed-clock defect identified by source review. | `.local/goal-alpha-20261003-initial-pytest.log` |
| Import contracts | **PASS: two contracts** | `.local/goal-alpha-20261003-initial-imports.log` |
| Frontend baseline | **FAIL** at `vercel.json` formatting; strict TypeScript/lint **PASS** with 20 existing warnings. | `.local/goal-alpha-20261003-initial-frontend.log` |
| Frontend behavior/build/assets | **PASS: 62 tests / 15 files**, production build and assets | `.local/goal-alpha-20261003-initial-frontend-tests.log`; `.local/goal-alpha-20261003-initial-build.log` |
| API contract | **PASS: 511 paths** | `.local/goal-alpha-20261003-initial-api.log` |
| Launcher | **PASS**: prerequisite/database/occupied-port denials, worker failure cleanup, ready/start/stop/restart | `.local/goal-alpha-20261003-initial-launcher.log` |
| Baseline browser | **PASS: six tests**, trace disabled | `.local/goal-alpha-20261003-initial-baseline-browser.log` |
| First-store manager/count browser | **BLOCKED**: recorded old sibling absent; no tests executed | `.local/goal-alpha-20261003-initial-manager-browser.log`; `.local/goal-alpha-20261003-first-store-status.log` |
| Operating browser/reconciliation | **BLOCKED**: no completed synthetic ALPHA registration to clone. Neither browser nor reconciliation is claimed run. | `.local/goal-alpha-20261003-initial-operating-create.log` |
| Migration rehearsal | **PASS**: clean/repeated migrations; accounts-0021 upgrade, historical IDs/custom inactive policy, stale-plan denial, revoked target never reactivated | `.local/goal-alpha-20261003-initial-migrations.log` |
| Consolidation / boundaries / checker tests / diff whitespace | **PASS: 3,059 surfaces; 13 unit tests** | `.local/goal-alpha-20261003-initial-consolidation.log`; `.local/goal-alpha-20261003-initial-boundaries.log`; `.local/goal-alpha-20261003-initial-checker-tests.log`; `.local/goal-alpha-20261003-initial-diff-check.log` |

Clean/upgrade siblings: `kdps_rehearsal_migrations_e14ce176c783` /
`kdps_rehearsal_migrations_c73f3951cdcd`; frozen Python source SHA-256:
`70098763a0ce7ac1195985e7a2c6d72d3faa2cf00b81d1be9bb0ed7fb08d91c9`.
Every later runtime edit requires new evidence; this initial table is preserved.

Initial triage: QA-D01/D02 fixture chronology was repaired before this run by
`e9c52c1`; future-cutoff/pause guards remain intact. QA-D03 remains a latent
mixed host/database clock defect requiring deterministic regression evidence.
QA-D04: retired-role seed check. QA-D05: eight strict typing failures.
QA-D06: frontend formatting failure. QA-D07: Receiving transfer detail is a
placeholder despite existing canonical handlers (TRF-03/C07/C05).
QA-D08: cancelling password confirmation leaves the awaiting command promise
unresolved (CNT-02/SET-03 and other shared confirmation consumers).
QA-D09: prior proof environment unavailable; fresh synthetic browser setup
must be rebuilt without adopting old IDs or history. CASH-01/02 and DAY-01
remain product/contract **BLOCKED**; RBK-01 and populated browser gaps remain
engineering **NOT RUN** pending new execution. See the unapproved
[decision register](kdps-first-store-alpha-decision-register-2026-10-03.md).

## 4 October 2026 final execution (Phases B–C) — fingerprint recorded

This section **adds to** the initial table above; nothing earlier was edited.
It is synthetic-only evidence. No real KDPS data was read or written, nothing
was committed or pushed, and the shop was not activated. **Full alpha remains
OPEN.**

### Final runtime and environment

- HEAD: `c6abc3d1467d129b20e917978fe2f892c84be4dc` (working tree has uncommitted edits).
- Runtime SHA-256 (algorithm above, `docs/` excluded):
  `4085306b4ed5ef769513af33c979b352001636bc8dac5375fd6273edb8f66149`.
  Complete working-tree SHA-256 before this documentation update:
  `c9af9d4a55d423939e7c7de8da0ed8084e6992782347e9f519b019a1c1f0081b`
  (1,600 files; private manifest `.local/goal-final-fingerprint.json`).
- **Which checks ran at exactly this runtime.** The aggregate
  `baseline.py verify`, manager suite attempt2, operating, transfer, SO-03 and
  `git diff --check` ran after the last edit. Earlier in the same session the
  backend baseline, migration proof, receiving attempt14, damage attempt2 and
  bootstrap attempt3 ran before only *test-file* edits (`browser/first-store-bootstrap.spec.ts`,
  `browser/first-store-stock-review.spec.ts`, `browser/first-store-damage.spec.ts`);
  no application source changed between those runs and the final fingerprint.
  The aggregate verify re-ran the full backend, frontend and baseline-browser
  checks at the final runtime.
- Proof cluster unchanged: PostgreSQL 17.11, system ID `7692508198834323494`,
  user `kdps_proof`, project/container `kdps-proof-ff7f4a176291` /
  `kdps-proof-ff7f4a176291-database-1`, `127.0.0.1:55433`. `proof.py down` was
  never called. Ports 8000/5173 were free for the aggregate verify; dedicated
  suites used 8008/5178 (first-store), 8010/5180 (operating), 8012/5182
  (receiving), 8016 (damage); transfer used its own pair.
- **New synthetic source** (rebuilt so the final suites start from the final
  code): `kdps_rehearsal_first_store_6ffd955ca6a0`. The earlier source
  `kdps_rehearsal_first_store_c92c9c32eb49` and all its history are preserved;
  its identity/evidence files were moved (not deleted) to
  `.local/goal-final-archive-source-c92c9c32eb49/`.
- Final per-suite databases: operating `kdps_rehearsal_operating_96467862284b`,
  transfer `kdps_rehearsal_transfer_3a1a8522fa29`, receiving
  `kdps_rehearsal_receiving_156d347f0153`, damage
  `kdps_rehearsal_damage_09c19e54347c`, migrations clean/upgrade
  `kdps_rehearsal_migrations_c4dacc4ae143` / `kdps_rehearsal_migrations_da1982768765`
  (frozen Python source SHA-256 `6dad1d52c740bf37cc29abbed4c139ea1da4c576d524b63784b2c0789bb2a4fd`).
- **Preserved failed and superseded databases** (not reset): twelve receiving
  clones from failed attempts plus the two earlier ones (`6ee87a636ee1` with a
  submitted frozen count; `b2aff2956471`, whose RCV attempt left an arrival,
  GRN and open shortage request), damage `d576aec94717` (one confirmed
  quarantined unit) and `d44ea8860fb4` (attempt 1 of the corrected spec),
  operating `d32dc7cc6cdb`, transfer `64a0c84d649b`. **One unrecorded database,
  `kdps_rehearsal_operating_9387e1248cf9`**, was created when the first
  `first-store-operating-proof.py --fresh create` copied the new source and
  *then* failed its ownership check against the old record (the script
  creates before it verifies). It holds a clean copy of the new source, has no
  identity record and was left in place; fix the create order before reuse.

### Check results at the final runtime

| Check | Result | Private evidence |
| --- | --- | --- |
| Aggregate `baseline.py verify` | **PASS** (exit 0): backend, frontend, API, build, seed repeatability, launcher, 6 baseline browser tests | `.local/goal-final-baseline-verify.log` |
| Backend | **PASS: 472 tests** (7 warnings, all missing-staticfiles), ruff, strict mypy (619 files), migration drift, 2 import contracts. Run twice (stand-alone and inside verify) | `.local/goal-final-backend-baseline.log` |
| Frontend | **PASS: 81 tests / 18 files**, typecheck, lint (0 errors, the same 20 old warnings), prettier, build | `.local/goal-final-frontend-baseline-attempt2.log` |
| API contract | **PASS: 511 paths** | `.local/goal-final-api-check.log` |
| Migration rehearsal | **PASS**: clean and accounts-0021 upgrade, repeat with no pending operations, source IDs and custom inactive policy preserved, stale plan denied without changes, revoked target never reactivated | `.local/goal-final-migrations.log` |
| SO-03 | **PASS**: inventory matches **3,070 surfaces**; boundaries **PASS**; **13** checker tests; `git diff --check` clean. Rules 101–103 regenerated; new rule 104 and re-pointed rules 026 and F0019 reviewed (see progress record) | `.local/goal-final-boundaries.log`; inventory/register/map regenerated with `--write` |
| Bootstrap browser (SET-01/02, SOH-01–04) | **PASS** attempt3 on the new source. Attempts 1–2 failed on a test race (below) | `.local/goal-final-bootstrap-browser-attempt3.log`; `.local/goal-final-opening-reconciliation.log` |
| Manager / POS / count browser | **PASS: 24, 1 recovery-only skip**, four widths, including the two new customer-search tests. Attempt 1 failed only the resume case on an empty-state race in the test | `.local/goal-final-manager-browser-attempt2.log` |
| Operating browser + reconciliation | **PASS** journey (lost-response replay, exchange, physical acceptance, post-count disclosure). Reconciliation: 3 bills / 3 accepted intents, cash tenders = cash ledger = 190,000 paise, GL sum 0, retained count 90,000 paise, 2 later bills unsettled, **0 unexplained difference**; new day-close **not** verified | `.local/goal-final-operating-browser-attempt1.log`; `.local/goal-final-operating-reconciliation.log` |
| Transfer browser + reconciliation | **PASS**: draft, submit, separate Owner approval, scan and dispatch, Receiving link and bookmark, destination count and putaway. Source 1 / destination 1 / transit 0; financial hashes unchanged; original cost preserved | `.local/goal-final-transfer-browser-attempt1.log`; `.local/goal-final-transfer-reconciliation.log` |
| Receiving browser (SOH-05–07, RCV-01, DMG-01) | **PASS: 3 of 3** on attempt14, after 13 earlier attempts on separate preserved clones (causes below) | `.local/goal-final-receiving-browser-attempt14.log` |
| Receiving reconciliation (read-only) | **PASS**: opening 3, sold 1, reviewed SOH reduction 2, receipt accepted good 2, ending sellable 1, valued quarantine 1; value removed 100,000 paise; original bill, tenders and cash unchanged; 0 unexplained in the exercised slice. Its own flags say full REC-01/REC-02 and day-close are **not** verified | `.local/goal-final-receiving-reconciliation.log` |
| Damage | **PASS**: backend **7 of 7**; browser attempt2 (report, password gate, deciding response, replay, changed-command refusal, unauthorised release refusal, blocked sale, correction). Attempt1 failed only on a wrong summary assertion | `.local/goal-final-damage-backend-attempt2.log`; `.local/goal-final-damage-browser-attempt2.log` |
| Customer search reprint (new) | **PASS: 2 of 2**; both **fail** with the fix reverted | `.local/goal-final-pos-customer-search-attempt1.log`; `.local/goal-final-pos-customer-search-prefix-reproduction.log` |
| Sales Report salesperson grouping (new) | **PASS** new test; **fails** with the gate removed | `.local/goal-final-sales-report-team-attempt1.log`; `.local/goal-final-sales-report-team-prefix-reproduction.log` |

The earlier targeted SOH pytest that was interrupted during collection was not
re-run on its own; its tests are inside the 472-test backend runs above.

### Case results (all 36 rows)

Result words: **PASS**, **PASS + residual NOT RUN** (the evidence covers the
named slice only), **BLOCKED**. Backend coverage is API-level and does not
replace the browser coverage the matrix asks for. "BE" = the 472-test backend
run at the final runtime. Browser evidence uses the log names in the table
above. Actor identities are generated fictional humans (Owner, Admin, store
manager, count checker, warehouse); assignment versions are those created by
the bootstrap and are recorded in the private credential files.

| ID | Result | Evidence and residual |
| --- | --- | --- |
| SET-01 | **PASS** | Bootstrap: joint signup through the form (four-width overflow checks), separate confirmations, closed registration after reload, no duplicate on retry. BE: atomic claim, rollback/retry, database-chronology ±5. Real legal/GST data is ACT-01. |
| SET-02 | **PASS + residual NOT RUN** | Bootstrap creates Owner, Admin and proposed manager; manager suite shows manager sees only the assigned store; separate count checker reviews. BE covers the six-role policy. Residual: browser run of every role with explicit field/period cells. |
| SET-03 | **PASS + residual NOT RUN** | BE: personal expansion refused, versioned attributable change, session invalidation, same human on a second login cannot review (`test_first_store_rollback`, access safeguards). Baseline browser case 4 (policy save confirms identity and revokes the session). Residual: browser flow of an independently reviewed privilege change; password-cancel regression has no case that ran (the Escape case is the skipped recovery-only one). |
| SET-04 | **PASS + residual NOT RUN** | BE: scheduled assignments, action upgrades, versions, boundaries. Baseline browser case 5 (scheduled assignment editing). Residual: browser coverage of calendar, transfer/count policy versions and the overdue-review boundary. |
| SOH-01 | **PASS + residual NOT RUN** | Bootstrap: upload stays staged, zero stock, zero journals (synthetic file). Residual: the real reference workbook's 25,689 rows / 11,161 positive / 19,896 units and its HSN and ₹0.38 discrepancies were **not** run (hard rule; ACT-01/OQ-29). |
| SOH-02 | **PASS** | Bootstrap mapping UI with stable IDs; BE duplicate/renamed/conflicting/foreign IDs stay inactive. |
| SOH-03 | **PASS** | Bootstrap: maker denied, independent approval, physical acceptance, stock only after acceptance; opening reconciliation: all 3 units independently accepted. |
| SOH-04 | **PASS** | Bootstrap "same accepted upload returns the existing effect"; BE interrupted-approval retry. |
| SOH-05 | **PASS** | Receiving attempt14 case: later full snapshot, reviewed differences applied once (reduction 2); original bill/tenders/cash unchanged. |
| SOH-06 | **PASS** | Same case: stale and gain snapshots refuse, explicit zeroes and omissions; BE partial/unaffirmed coverage refused. |
| SOH-07 | **PASS** | Same case plus BE stale decision, interrupted real-position rollback and retry, cancellation keeps evidence. |
| POS-01 | **PASS** | Bootstrap first bill; operating sale; manager bills/drawer reconcile at four widths. |
| POS-02 | **PASS** | Operating lost-response replay; BE two-worker concurrency. |
| POS-03 | **PASS + residual NOT RUN** | BE: paused/unsynced till, frozen store, unknown SKU, insufficient stock, unauthorised discount/tender, stale price/policy all refuse without mutation. Residual: browser refusal journeys. |
| POS-04 | **PASS + residual NOT RUN** | Manager suite: bills drawer, delayed reprint after logout, customer-search reprint (fresh exact-bill read) and delayed reprint; BE sales-report workbook recheck. Residual: delayed notification, job and SSE delivery beyond those channels. |
| RET-01 | **PASS** | Operating exchange (original bill unchanged, distinct exchange bill); BE unsupported contracts inactive. |
| RET-02 | **PASS** | Operating physical acceptance; BE retry returns the original quantity with no second effect. |
| RET-03 | **PASS + residual NOT RUN** | BE wrong store, system/retired location, unresolved brand, frozen store refuse; damaged return stays quarantined. Residual: browser inspection of a damaged return. |
| STK-01 | **PASS** | Manager suite stock bookmarks keep filters and scope; BE cost-field denial and revocation. |
| RCV-01 | **PASS** | Receiving attempt14 + reconciliation: claimed 4 / counted 3, damaged line held, independent shortage review, PT prepared by warehouse and approved by a separate Owner, physical acceptance of 2. Residual: real supplier file formats are OQ-gated. |
| TRF-01 | **PASS + residual NOT RUN** | Transfer browser: draft, submit, separate Owner approval, scan and dispatch. BE: multi-step route with a separate human per step, pins, no reservation before the final step. Residual: browser run of a multi-step route. |
| TRF-02 | **PASS + residual NOT RUN** | BE only: replaced/withdrawn policy, changed revision, limit edges and unknown values, explicit resubmission. Browser NOT RUN. |
| TRF-03 | **PASS** | Transfer browser: Receiving UI reaches the transfer, destination count and physical acceptance; reconciliation source 1 / destination 1 / transit 0. |
| DMG-01 | **PASS** | Damage browser attempt2, damage backend 7/7, and the receiving DMG case: quarantine once, original cost kept, unauthorised release and sale refused, repeat report replays. |
| CNT-01 | **PASS** | Manager suite: paused blind count at four widths; BE unpaused start refused and counters cannot see book or cost. |
| CNT-02 | **PASS + residual NOT RUN** | Browser: zero-difference count closed by a separate human at four widths. BE: exact shortage posts original portions once. Residual: browser shortage closure. |
| CNT-03 | **PASS + residual NOT RUN** | BE only: stale book, gains, encumbered reductions, sale-versus-freeze race, duplicate decisions. Browser NOT RUN. |
| CASH-01 | **BLOCKED** | Blind custody/shift contract not approved (OQ-08). Not implemented. |
| CASH-02 | **BLOCKED** | Independent variance review not approved (OQ-08/28). The online refusal of a variance is verified by BE (denial passes; capability stays blocked). |
| CASH-03 | **PASS** | Operating: saved 90,000-paise count immutable, later bills disclosed as unsettled, next expected 190,000 paise. Not a day-close. |
| DAY-01 | **BLOCKED** | Closed-date/custody contract not approved. |
| REC-01 | **PASS + residual NOT RUN** | Four slice reconciliations with zero unexplained difference (opening, receiving, operating, transfer). Residual: no single database exercises the whole lifecycle, so one end-to-end reconciliation is **NOT RUN**. |
| REC-02 | **PASS + residual NOT RUN** | Operating reconciliation: bills, tenders, cash ledger and GL exact in paise, no duplicates, saved count preserved. Residual: per-day aggregation after a new day-close. |
| MIG-01 | **PASS** | Migration proof at the frozen source hash above. |
| RBK-01 | **PASS + residual NOT RUN** | BE `test_first_store_rollback`: compensating assignment/role-policy changes after real sale postings, stale/resurrection/self-review refusals, later edits and revocations preserved. **Positive reviewed mapping compensation is not implemented or proved**; fences refuse (see decision register). Not real operational approval. |
| ACT-01 | **BLOCKED** | Needs real legal/people/calendar/tax/mapping inputs, fresh export/cutoff, physical count and explicit production authorisation. No real-tenant writes were made. |

Totals: **PASS 18**, **PASS + residual NOT RUN 14**, **BLOCKED 4** (CASH-01, CASH-02,
DAY-01, ACT-01). No case is FAIL at the final runtime.

### Defects: status at the final runtime

| ID | Status | Notes and regression |
| --- | --- | --- |
| QA-D01, QA-D02 | **Fixed in tests; passing** | SOH reconciliation and online-concurrency tests pass in both full backend runs; the SOH-05–07 browser case passed in all 12 receiving runs that reached it. Future-cutoff and pause guards unchanged. Two full runs are not a proof of no flake. |
| QA-D03 | **Fixed** | Initial authority is dated from the command's database time; new `test_joint_registration_and_first_password_change_use_database_chronology[5/-5]` cover host/database clock offsets. |
| QA-D04 | **Fixed** | Seed-repeatability check passes inside `verify`. |
| QA-D05 | **Fixed** | Strict mypy: 619 files. |
| QA-D06 | **Fixed** | Prettier clean (`vercel.json`, and the new POS spec formatting). |
| QA-D07 | **Fixed** | Receiving reaches transfer detail; transfer browser proves the link and bookmark. |
| QA-D08 | **Fixed in source; regression evidence limited** | Cancelling password confirmation now rejects the awaiting command. The Escape-cancel browser case is the recovery-only one and was skipped. |
| QA-D09 | **Addressed** | Fresh synthetic source and suites rebuilt; old IDs not adopted. |
| QA-D10 | **Fixed** | Sales Report grouped by salesperson (JSON and XLSX) returned names/codes without the team-field demand. The server now refuses `group_by=salesperson` unless the viewer sees the team at every store, hides the option, and rechecks at export delivery. Test: `test_so03_denials.py::test_sales_report_salesperson_grouping_needs_team_sight_at_every_store`. |
| QA-D11 | **Fixed** | Customer search reprinted the bill fetched at Open time. It now re-reads the exact bill at print time and clears the copy on refusal. Tests: the two `customer search` cases in `first-store-pos.spec.ts`. |
| QA-D12 | **Fixed** | The receipt's approvals panel did not refresh after a shortage was requested, so the own-request notice never appeared until reload. |
| QA-D13 | **Fixed** | Shared goods error feedback had no `role="alert"`, so errors were not announced. |
| QA-D14 | **Fixed** | The searchable dropdown closed itself when focusing its input scrolled the page or grid. It now scrolls only its own list and re-anchors on scrolls just after opening. |
| QA-D15 | **Fixed** | At phone width the PT grid's four frozen columns covered nearly all of the region. Item and review columns now scroll below 480px. |
| QA-D16 | **OPEN, unexplained** | In 2 of about 11 receiving runs (attempts 7 and 11) clicking "Record arrival" after the four-width resize sent no request. A guard that presses again only when nothing was sent never fired in the last three runs. Root cause not established; not shown to affect a user. |
| QA-D17 | **Test-harness defects, fixed** | Fixture alias id lookup; damage deciding-response capture and review-summary assertion; PT season control; PT history panel assertion; scan-recorded race before Complete; bootstrap review/confirm race; stock-review empty-state race; operating create order (see the unrecorded database above, **still open in the script**). |

Reproduction commands are the ones in "Commands and current execution", plus:

```sh
backend/.venv/bin/python scripts/proof.py run --cwd backend -- .venv/bin/pytest tests/test_so03_denials.py -k salesperson_grouping --reuse-db -q
PLAYWRIGHT_BROWSERS_PATH=/Users/anand/Developer/kdps-code/.local/playwright-browsers yarn playwright test --config=playwright.first-store.config.ts -g "customer search"
```

(the first from the repository root; the second from `frontend`).

## Historical tested source and environment (30 September 2026)

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

Historical execution (30 September runtime; superseded for this run):

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

### Historical defects and diagnostic boundaries

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
