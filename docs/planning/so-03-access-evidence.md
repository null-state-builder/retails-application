# SO-03 access consolidation evidence

**Recorded:** 30 September 2026. **State:** implementation continues; SO-03 **not complete**. The existing tracked and untracked working tree remains preserved and uncommitted. Other business workflows retain their delivery owners; SBU retirement remains disabled until SO-04.

**Verified content:** HEAD `b9e1e259d9b8696507f13123b4e8b7b4a7a4c783`; working-tree SHA-256 `52a8ce4343a828f2495de736701c649c13dc4737d5d9f7dd003372067880f2e0` over 1302 sorted tracked and non-ignored untracked paths, excluding this evidence file. Hash each relative path plus NUL, its bytes (or `<deleted>`), then NUL. No implementation or inventory changes followed this final freeze. This evidence file is the only excluded subsequent write. The checkpoint is `/tmp/so03-final-checkpoint-20260930.json`.

## Current implementation dispositions

The [completion ledger](SO-03-access-consolidation), [review](so-03-interrupted-implementation-review.md), [inventory](so-03-consolidation-inventory.json), [register](so-03-consolidation-register.md) and [dependency map](so-03-dependency-map.md) distinguish implemented slices from unfinished acceptance.

- Scope helpers require explicit authority parameters; absent parameters fail closed. Specialist gates now demand stored actions from qualifying assignments. Missing actions in an existing workflow version stay disabled until an explicit upgrade; malformed policy has no permissive fallback.
- Stable brand references and append-only identity bindings preserve original business snapshots. Reviewed mapping rejects stale, foreign and conflicting links. Printed bills remain retained when historical brand identity is unresolved; no label or foreign stock row supplies authority. Writer propagation and real historical reconciliation remain open.
- Alert inbox/history filters tenant, stable brand and complete assignment tuples before serializing protected titles/object IDs. Financial/customer/private-employee kinds require fields in that same assignment. Explicit whole-site alerts require all-brand scope and cannot be reconciled to one brand. Alert jobs validate tenant ownership before writes and preserve established brand references.
- The alert seen cursor writes only the caller's timestamp through request-bound `AccessContext.guard_legacy_write`. Session expiry or assignment revocation during the write refuses commit and rolls back the cursor. A missing server session refuses the write.
- Logout now awaits server revocation before clearing local state or navigating. The profile shows a retryable failure when acknowledgement fails. Real-backend browser regressions deliberately delay acknowledgement, abort the first request and reload immediately after confirmed logout.
- Scheduled browser editing now uses a real future assignment and checks its preserved ID and exact start time on edit/removal. Synthetic proof setup explicitly versions missing section thresholds and the Owner staff step through canonical password-confirmed policy endpoints. That setup does not approve any real-tenant policy upgrade.
- The preserved assignment, staff-retirement, queued-export/download/event/till-replay safeguards and six-role denial tests remain in the final backend suite. Passing particular tests does not establish completion of all registered resource traces.

## Final verification

All commands below used the isolated PostgreSQL 17.11 proof environment where a database was required. No real-tenant mapping or cutover was applied.

| Command | Final outcome and coverage |
| --- | --- |
| `python3 scripts/baseline.py backend` | Django check, migration drift and Ruff passed; mypy passed on **558 files**; two import contracts passed on **921 files / 5,650 dependencies**; pytest **116 passed, 1 warning**. The warning concerns the absent local backend staticfiles directory. Fresh test databases apply the current migrations, including 0022 and stable-brand migrations. |
| `python3 scripts/baseline.py frontend` | TypeScript and Prettier passed; **15 Vitest tests passed**. ESLint: **0 errors, 18 warnings**. |
| `python3 scripts/baseline.py api-check` | Generated API contract matched **488 Django paths**. |
| `python3 scripts/baseline.py browser` | Production build/assets and **6 Playwright journeys passed**: authentication/dashboard/confirmed logout; failed logout and retry; bookmark/scoped denial; policy change/session revocation; real scheduled editing/removal; offline app shell without cached API responses. Build retains its bundle-size warning. |
| `python3 scripts/baseline.py seed` | Synthetic foundation reseed preserved user/role/store/brand counts, password and access settings. |
| `backend/.venv/bin/python scripts/check_so03_consolidation.py --write`, then `--check` | Register and dependency map regenerated; drift check matched **2,929 surfaces**. |
| `backend/.venv/bin/python scripts/check_so03_boundaries.py` | Passed. This is drift evidence, not proof of every dynamic permission dependency. |
| `backend/.venv/bin/python -m pytest scripts/test_check_so03_boundaries.py scripts/test_check_so03_consolidation.py scripts/test_render_so03_dependency_map.py scripts/test_so03_backend_inventory.py scripts/test_so03_frontend_inventory.py -q` | **20 passed**. |
| `git diff --check` | Passed. |

Focused alert tests passed **8/8** before the final full backend run. They cover duplicate/renamed labels, foreign ownership, crossed assignment tuples, whole-site scope, protected titles, disabled workflow authority, tenant job writes, session/assignment withdrawal during cursor writes and missing-session denial. The final suite includes the brand-mapping, scheduled-assignment, migration, approval separation, queued-delivery and till regressions; their presence does not close untested entry-point contracts.

## Failures retained in the ledger

Intermediate failures were corrected before final verification; no final required baseline failed:

- Inventory generation refused the new `alerts.scope_contract` module until its owner, lifecycle, invariant and evidence were registered.
- Older stored proof policy correctly denied newly missing actions. Intermediate browser runs had two failures, then one. The proof fixture now applies an explicit canonical policy upgrade; runtime evaluation remains fail closed.
- The first new alert-cursor run had **1 failed / 7 passed**: a live session was not resolved from the request. The handler now uses `resolve_access(request)`; the rerun passed **8**.
- A backend run stopped on two mypy errors importing model re-exports in the new test. Imports now come from `accounts.goods_models`; subsequent full baselines passed.
- A finalization browser run had **1 failed / 4 passed** after immediate post-logout reload. Trace showed the logout POST cancelled and the next `/auth/me` returning 200. The awaited logout implementation and deterministic delayed/failure regressions passed **6** on rerun.
- Earlier source-brand fixture issues (an overlong LegalEntity code and a missing stored specialist step) were corrected before the focused vendor/brand suite and full baseline passed. Those earlier green runs were superseded by the final checks above.

## Read-only migration reconciliation

Explicit synthetic tenant: `d588b75d-9732-4d31-b5cb-6af20a527e93`. It is checked as synthetic and runs only under proof mode.

`python3 scripts/proof.py run --cwd backend -- .venv/bin/python manage.py migrate_role_assignments --tenant d588b75d-9732-4d31-b5cb-6af20a527e93 --output /tmp/so03-proof-reconcile-20260930.json` produced a per-tenant dry run: **20 candidates, 21 existing assignments, 17 blocked people, 0 policy changes**. The extra assignment belongs to the browser's scheduled proof fixture. Inventory: **46 humans, 34 users, 39 legacy grants, 19 roles, 10 sites, 11 brands and no unbound users**. Session counts are transient proof activity, not a reconciliation invariant.

`python3 scripts/proof.py run --cwd backend -- .venv/bin/python /tmp/so03_reconcile_readonly.py` compared candidate identities, roles, scopes, periods, revocations and source links inside `SET TRANSACTION READ ONLY`: **20 unchanged, 0 would-migrate, 0 candidate conflicts, 17 excluded people, 0 policy changes, 0 database writes**. Detailed synthetic output is `/tmp/so03-proof-source-target-20260930.json`. These source IDs and fingerprints are proof evidence, not permission to apply real mappings.

Exclusion findings: **15 UNMAPPED_ROLE, 10 UNMAPPED_GRANT, 1 EMPTY_INTERSECTION, 1 PLATFORM_IDENTITY and 2 PLATFORM_ONLY**; one person can have several findings. Three platform grants are retained retired evidence. Unsupported responsibilities were not mapped to Owner. Missing identity, ambiguous scope and OQ-28 responsibilities remain inactive activation blockers.

The latest full suite verifies stale-preview refusal, fixed selected scope, revoked source/target preservation and repeat application. The older 0021→0022 upgrade and two proof applications are retained below as historical evidence. A fresh final-version upgrade/rollback rehearsal preserving later edits and transactions remains a completion gate; it is not inferred from a clean test database or a read-only report.

## Open completion and activation gates

1. **Resource audit:** all **92 registered permission-reference edges** have dispositions (66 assignment-backed adapters, 11 historical evidence, 14 unmounted legacy, 1 disabled callback), but only **54** have verified resource traces; **38** remain pending. Entry-point contracts contain **6 verified and 1,214 pending** records; 1,709 other component records have no entry-point contract. These totals include development/disabled surfaces and are coverage bookkeeping, not a claim that all are active. Static inventory completeness does not establish access acceptance.
2. **SO03-B02 — approval authority:** active `ApprovalPolicy`/`ApprovalRoute` and three caller role snapshots remain outside unified versioned workflow responsibility. Complete full resource scope, protected projection, policy/input revision, limits, maker/checker and live write-guard enforcement before acceptance. This gap exists outside the frozen import-reference count.
3. **SO03-B03 — session UI contract:** GoodsBookings, OpenToBuy and TransferDetail still consume the global `field_visibility` hint. Replace it with scope/resource decisions, remove the union schema/client and verify mixed-assignment UI behaviour.
4. **SO03-B01 — identity propagation:** complete every affected writer, immutable binding, derived reader/report/file path and incomplete-total disclosure. Brand labels never authorize a row; unresolved historical identity cannot be treated as complete migration.
5. **Migration/browser coverage:** fresh upgrade and rollback proof, real reconciliation/mapping, independent beneficiary review and delayed delivery browser journeys are still required. Current six journeys do not claim those scenarios. SO-09 retains full offline cutover acceptance.
6. **Real-tenant reconciliation:** no safely identified real-tenant connection is available. Required read-only totals, owned exclusions and reviewed mappings remain unavailable. Proof exclusions cannot resolve real-tenant identity/scope/OQ-28 questions. Real writes require separately reviewed mapping and cutover authority.

SO-03 remains open. Later business duplicates retain their named replacement, owner and retirement gate; none is accepted or retired by these access changes.

## Historical evidence — 29 September 2026

The snapshot below is preserved verbatim apart from heading depth. Its counts, fingerprints, “latest” wording and findings apply only to that earlier content and are superseded by the current ledger above.

### Prior access consolidation evidence

**Recorded:** 29 September 2026. **State:** latest implementation checks passed in the isolated proof environment; SO-03 **not complete**. The shared working tree was preserved and remains uncommitted. No later business workflow was retired.

**Revision identity:** `HEAD b9e1e259d9b8696507f13123b4e8b7b4a7a4c783`; SHA-256 `daaef974bbff5aa9e2e3183b4d2afcae9aaec54dddb516b22f680c4b9afe748c` over 1,282 sorted tracked and non-ignored untracked paths, excluding this evidence file. Each relative path is followed by its bytes or a deletion marker, with NUL separators. The shared working tree is uncommitted; only planning documentation and inventory metadata changed after the final application/test runs.

#### Implemented and traced

- `/api/auth/admin/users/<id>/assignments` and People & Access now retain existing assignment IDs and exact start dates. Future rows edit in place, removed future rows are revoked, and current rows retain successor versioning. The existing command boundary validates ownership, revision, step-up and dates, records versions/audit and invalidates affected sessions.
- `accounts.assignment_migration` locks and re-plans inside apply. Source/target changes after preview refuse the stale plan; source-linked rows, revoked targets and later policy edits are preserved. Unlinked existing target authority blocks a fresh legacy link rather than widening access.
- Global search's tenant-owned stock path constructs explicit all-scope predicates inside the tenant filter. It does not substitute global SKU/cohort metadata for tenant stock data.
- Outbound transfer response serializers and PT JSON/CSV/XLSX file builders project cost and margin through request-bound unified access. Nested mutation responses use the same projection; protected writes are refused. The shared transfer list/detail/file/search query now covers every snapshot-brand line through complete assignments. Warehouse's own-PT exception remains distinct from booking cost and general valuation.
- SBU retirement's mounted POST, UI action and `RoleGrant`-revoke helper were removed. SO-04 owns safe restoration. The pre-cutover access-change approval hook now refuses legacy payload application; the transaction rolls back. Production URL configuration cannot mount Django admin through `ENABLE_DJANGO_ADMIN` alone.
- The generated client reflects the scheduled-assignment request, the removed SBU POST and the two reviewed stock descriptions. Shared legacy season end is blocked pending SO-04; tenant category policy requires tenant-wide authority.
- Staff retirement locks and ends current/scheduled `RoleAssignment` rows under the actor's complete access scope, preserves IDs, records revocation evidence and bumps the target's security epoch. It no longer appends `RoleGrant` revocations. The assignment migration also blocks inactive and retired source identities from regaining access.
- Online sale replay resolves the caller's current till store before the idempotency lookup and compares the stored bill's site in ordinary, concurrent and in-transaction replay branches. The till dataset refreshes session/assignment authority after its payload is built. Special-order transfer commands now carry a live session guard to commit.
- Queued exports reauthorise at work start, before publication and in the artifact transaction. Export-create and detail response paths require current access before exposing a file link. Downloads recheck after off-box read, while event delivery rechecks on reconnect and within a batch.
- Tax, consent wording and document-series history reads require one explicit tenant-wide setup assignment rather than a global section hint. Whole-site stock and SOR ageing reports now require all-brand authority at that site; arrival counter assignment requires the target person's all-brand `receive.arrival` authority and tenant match. The protected-boundary inventory records a disposition for all 88 frozen reference edges; 48 have resource-level traces and 40 remain pending.

#### Verification on this implementation content

| Check | Result |
| --- | --- |
| Focused `tests/test_so03_denials.py tests/test_role_assignment_migration.py -q --reuse-db` | 41 passed before the final export-response regression and in-transaction replay branch were added. The final full backend run includes those additions. |
| `python3 scripts/baseline.py backend` | Django check and migration drift passed; Ruff passed; mypy passed on 549 files; import contracts passed on 908 files / 5,603 dependencies; pytest **83 passed, 1 warning**. An intermediate run stopped on three typing errors in a new test; they were fixed before this full rerun. |
| `python3 scripts/baseline.py frontend` | TypeScript, Prettier and 15 Vitest tests passed. ESLint: 0 errors, 18 warnings. |
| `python3 scripts/baseline.py api-check` | Generated contract matched **486 Django paths**. |
| `python3 scripts/baseline.py browser` | Production build/assets and **5 Playwright journeys** passed, including scoped access, policy change/session revocation and scheduled-row payload. |
| `backend/.venv/bin/python scripts/check_so03_consolidation.py --write`, then `--check`; `backend/.venv/bin/python scripts/check_so03_boundaries.py` | Generated register/dependency map for **2,908 discovered surfaces**; final drift and boundary checks passed. The boundary ledger has 88 classified reference edges; 40 are still flagged `needs_resource_trace`, and SO03-B01 names the text-brand scope blocker. |
| Focused inventory, scanner, dependency-map and boundary tests | **20 passed** on the final manifest. |
| `git diff --check` | Passed. |
| Proof DB migration 0022 rehearsal | Unapplied 0022 to 0021, then applied 0022 successfully. Clean pytest databases apply the same migration. |
| Proof tenant `migrate_role_assignments --apply` twice (earlier rehearsal) | First: 20 unchanged, 0 created, 0 conflicts, 1 policy default change; second: 20 unchanged, 0 created, 0 conflicts, 0 policy change. The existing cutover version was not replayed. |
| `python3 scripts/proof.py run --cwd backend -- .venv/bin/python manage.py migrate_role_assignments --output /tmp/so03-proof-reconcile-20260929.json` | Read-only dry run: one synthetic tenant, 20 candidates, 20 existing assignments, 17 blocked people, 0 policy changes; 46 humans, 34 users, 39 legacy grants and 36 active sessions inventoried. No real tenant was touched. |

The proof migration report has 20 source-linked candidates and **17 blocked people**. Findings are 15 `UNMAPPED_ROLE`, 10 `UNMAPPED_GRANT`, one `EMPTY_INTERSECTION`, one `PLATFORM_IDENTITY` and two `PLATFORM_ONLY`; one person can have more than one finding. Three platform grants are retired evidence. These synthetic proof figures cannot substitute for read-only reconciliation of real tenants.

#### Failed or incomplete acceptance gates

1. The frozen inventory contains **88 legacy permission-reference edges**: 59 assignment-backed adapters, 14 unmounted legacy references, 11 historical evidence references, three active legacy-authority paths and one disabled callback. **Forty** references still need their actual handler → data → authorization/projection chain verified. SO03-B01 records name-based brand scope in legacy alerts, search, stock and outbound readers: rows lack stable brand IDs, so duplicate or renamed display names cannot establish assignment ownership. Section-wide permission hints alone do not prove resource authority. The active-path audit is incomplete.
2. A read-only connection probe to the configured local real-tenant endpoint failed with `OperationalError`; no safely identified live tenant connection is available. The synthetic tenant's 17 blocked people, unresolved identities and OQ-28 responsibilities remain activation blockers. No unsupported responsibility was mapped to Owner and no real-tenant mapping was applied.
3. The five browser journeys do not exercise every delayed job, download or stream revocation end to end. The backend denial matrix now covers those boundaries and online till replay/dataset delivery, but further pending resource traces may reveal coverage gaps. SO-09 retains full offline cutover.

The earlier [foundation evidence](so-03-foundation-evidence.md) is a historical inventory snapshot. The [interrupted implementation review](so-03-interrupted-implementation-review.md) records each former finding's current disposition. Neither later workflow retirement nor SO-03 acceptance is claimed here.
