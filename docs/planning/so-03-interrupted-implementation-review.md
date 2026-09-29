# SO-03 implementation review

## Continuation disposition — 30 September 2026

The working tree remains preserved and uncommitted. SO-03 remains open. [Access evidence](so-03-access-evidence.md) records the final verification commands and content fingerprint; preceding green runs do not validate later runtime edits.

| Finding or slice | Reviewed disposition |
| --- | --- |
| Logout completion race | The final browser run exposed a cancelled fire-and-forget logout and a restored session on immediate reload. Logout now waits for server acknowledgement; the UI reports failures and permits retry. Browser regressions delay acknowledgement and fail the first request. |
| Alert cursor commit guard | `AlertSeenView.post` resolves the request’s AccessContext and guards its caller-only write. Session expiry or assignment withdrawal during the write rolls the cursor back; tests require a live session and preserve foreign cursors. |
| Implicit request section stamp and text context | Shared scope helpers now require explicit authority parameters and stable context IDs. No display name grants brand access. |
| Specialist granting role lists | Thirteen specialist gates use versioned named role steps with scoped `AccessContext` decisions. Initial defaults are offered for explicit upgrade, not re-granted on every seed/read. Approval role snapshots remain separately blocked under SO03-B02. |
| New actions on existing policies | Missing actions are disabled. Browser failure exposed the proof tenant’s old stored policy; proof setup now explicitly versions its section baseline and Owner staff step through canonical endpoints. This is disposable fixture setup, not production migration acceptance. |
| Stable brand source conflicts | Source fingerprints include trusted relationship identity; reviewed batches refuse foreign, stale and conflicting established links. Immutable ledger evidence is preserved, and bindings are append-only. |
| Printed-sale identity | Known tenant stock/canonical goods IDs flow to the sale. Unresolved historical sources retain their completed bill without acquiring a guessed brand. Focused tests verify foreign stock cannot establish the link. |
| Alert inbox/history | Same-assignment scope, stable brand and field checks run before titles/object IDs serialize. Whole-site conditions require all-brand authority; duplicate labels and crossed tuples do not grant access. |
| Alert job | Queries and sync stay within the bound tenant, validate hits before writes, carry known brand IDs and retain established links. A whole-site snapshot cannot be mapped to one brand to widen visibility. |
| Scheduled assignment browser test | Replaced mocked payload proof with real backend edit/removal and stable ID/start assertions. Final rerun status is in access evidence. |

The manifest now distinguishes assignment-backed adapters from the former name-authority finding. It still records pending resource traces; converting an adapter is not proof for all its callers. SO03-B01 tracks remaining identity propagation/reconciliation, SO03-B02 approval responsibility/version rechecks, and SO03-B03 session field-union consumers. Most active entry-point access contracts remain pending. No real-tenant reconciliation or later workflow retirement is accepted.

## Historical disposition — 29 September 2026

The following snapshot is retained for history. Its test counts, inventory counts and “current” claims apply to that earlier content only.

# SO-03 interrupted implementation review

**Updated:** 29 September 2026. This review now records the disposition of the interrupted access patch in the preserved, uncommitted working tree. The final check results and content fingerprint are in [SO-03 access evidence](so-03-access-evidence.md). SO-03 remains open.

| Earlier finding | Current disposition |
| --- | --- |
| Scheduled assignment editor omitted `id` and `effective_from` | The canonical PUT sends both for existing rows. The backend edits future rows in place, revokes removed future rows and versions already-effective edits. Focused backend tests cover stable IDs/dates, revision conflicts, foreign IDs, password confirmation and session revocation. A browser journey checks the outgoing scheduled-row payload. |
| Migration 0022 and replay safety | The proof database was stepped from 0022 back to 0021 and forward to 0022; clean test databases also apply it. Apply now locks and rebuilds the source plan, refuses stale previews, preserves source-linked rows and revoked targets, and blocks unlinked existing target authority from receiving another legacy assignment. The proof tenant's two applications created zero rows and zero conflicts; they do **not** resolve its 17 blocked people. |
| Tenant-owned barcode absent from search | The all-scope predicate now remains explicitly unrestricted inside the tenant filter. Focused search and the backend baseline pass. Barcode metadata and quantities are sourced from tenant stock in the search handler. |
| Two generated stock descriptions differed | Reviewed against the actual endpoint behavior: shared legacy season end is blocked pending SO-04; tenant category rules require tenant-wide authority. The generated client was updated and the 486-path contract check passes. |
| Unused `Refusal` import and unused cost projection | The import is removed. Outbound nested reads, mutation responses and PT JSON/CSV/XLSX use request-bound protected-field projection; restricted writes are rejected. Tests cover nested cost, two-site cost coverage, a hidden CSV and write denial. |
| Mounted SBU retirement wrote `RoleGrant` | The POST route, screen action and legacy grant-writing helper were removed. Direct URL resolution fails. SBU reads/history remain; restoration is owned by SO-04 and must revoke unified assignments and invalidate sessions. |
| Optional Django admin | The mount requires both `DEBUG` and explicit `ENABLE_DJANGO_ADMIN=1`; production settings cannot enable it with the flag alone. A denial test verifies the production route list. |
| Legacy access approval callback | A pending pre-cutover `AccessChange` could still apply `User.role` or actor-policy writes through the shared approval handler. That callback now raises and rolls the approval transaction back; focused tests cover direct and hook invocation. Historical rows remain as evidence. |
| Selected-brand transfer PT returned 404 | The shared transfer read scope now checks every snapshot-brand line through complete site/brand assignments in SQL. Search uses the same resource query; the file path accepts a qualifying selected-brand Owner and denies a document containing an uncovered line. |
| Staff retirement appended legacy grant revocations | The mounted retirement command now locks current and future `RoleAssignment` rows, checks the actor against every complete assignment scope, ends current rows and revokes scheduled rows without changing IDs. It bumps the affected person's security epoch and records the assignment IDs in audit evidence. Focused denial tests cover a mixed-scope actor and the absence of new `RoleGrant` rows. |
| Online sale replay returned before store-scope check | The requested till store is now resolved through the current Store Person assignment before any idempotency lookup. The ordinary, concurrent and in-transaction replay branches compare the existing bill's store before returning its identifiers. A denial test covers all three branches. |
| Delayed output could outlive authority | Queued exports recheck scope and fields before producing and before artifact publication; create/detail responses now require access for file links. Evidence download rechecks after off-box read, event delivery refreshes on reconnect and within batches, and till datasets recheck the session and store after build. Focused tests cover revocation at these boundaries. |
| Special-order transfer lost its session guard | The transfer command now receives the request session, requires it in `_transfer_access` and carries the resulting `AccessContext` guard to commit. A denial test changes the security epoch before that guard runs. |
| Tenant-wide policy history used a section hint | Tax settings, consent wording and document series GET handlers now require an assignment with explicit tenant-wide setup view authority; local site authority cannot read tenant-wide history. |
| Whole-site reports accepted a selected-brand grant | Stock ageing and SOR ageing now require one assignment with all-brand authority at the selected site before rendering a whole-site report. A denial test covers selected-brand and all-brand assignments. |
| Arrival counter assignment used site reach as authority | `person_can_receive` now verifies the target identity's tenant and requires `receive.arrival` over the whole site/all brands before assigning a count session or handover. A denial test covers wrong tenant, wrong site and selected-brand scope. |

## Unresolved access gates

- The protected-boundary inventory records a disposition for each of 88 frozen reference edges: 59 assignment-backed adapters, 14 unmounted legacy references, 11 historical evidence references, three active legacy-authority paths and one disabled callback. Actual handler/resource/projection traces are verified for 48; **40 still need resource-level review**. The three active edges are alerts, search and legacy stock readers that call `masters.scoping.scope_by_store_and_brand`; outbound readers reach it too and are recorded as SO03-B01. Legacy rows lack a stable brand ID, and duplicate or renamed display names cannot establish assignment ownership. SO-03 cannot claim one safe authority on those paths yet.
- The isolated proof tenant still has 20 source-linked migration candidates and 17 blocked people: 15 unmapped-role findings, 10 unmapped-grant findings, one empty intersection, one platform identity and two platform-only findings. These are findings, not 29 separate people. The read-only local real-tenant connection probe failed; no real-tenant mappings were applied. Missing identities, ambiguous scopes and unsupported OQ-28 responsibilities remain activation blockers.
- Five browser journeys pass for login, scoped navigation, access editing and scheduled-row payloads. Backend denial tests cover delayed job/download and event revocation plus online till replay and dataset delivery. Browser coverage does not yet exercise those delayed paths end to end, and the 40 pending traces can reveal further field-projection work.

Later SO-04–SO-14 and SO-16–SO-20 workflow retirement rows remain planning targets. This review does not claim their implementation or acceptance.
