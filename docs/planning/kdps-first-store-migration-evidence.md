# First-store migration rehearsal evidence

Recorded 30 September 2026. Command:
`backend/.venv/bin/python scripts/first-store-migration-proof.py`.

The helper first verifies the existing owned proof container against the host
endpoint. It then copies backend and Python verification sources into
`.local/first-store-migration-source-94b18fa39efb`, excluding credentials,
virtual environments, runtime data and the raw SOH workbook. Each child verifies
the frozen source hash and emits its database identity **before** migrations.
The original checkout and all databases/evidence are preserved; nothing is reset,
dropped or reverse-migrated.

| Identity | Current verified value |
| --- | --- |
| Project / container | `kdps-proof-ff7f4a176291` / `kdps-proof-ff7f4a176291-database-1` |
| Host / container port | `127.0.0.1:55433` / `5432` |
| PostgreSQL / user | `17.11` (`170011`) / `kdps_proof` |
| Matching host/container system identifier | `7690899938284695588` |
| Clean disposable sibling | `kdps_rehearsal_migrations_c1ce8e337934` |
| Upgrade disposable sibling | `kdps_rehearsal_migrations_1aa73cab2fc1` |
| Frozen Python source SHA-256 | `cc6d4cd9fa1d43b3ef4ab7e267139f2c269e7554298402ae5675f7b770df7fcd` |
| Rehearsal helper SHA-256 | `60009032cad5fb8e9d25b03f560e7e34d69d62241fac6b73f50109afa17528bb` |

Both cases passed. Clean install migrated to all current leaf nodes; repeating
`migrate` left no pending operations. Upgrade began at
`accounts.0021_role_field_access_roleassignment` and its dependencies, created
explicit fictional historical Tenant/HumanIdentity/Role/User source rows, then
applied all current migrations twice. Exact source IDs and the deliberately
custom inactive role's empty section/field policy were preserved.

A preview followed by a changed historical source scope was refused as
`STALE_MIGRATION_PLAN` without assignments. Restored fixture input produced one
linked target assignment; repeat produced zero new / one unchanged. Revoking
that target and applying again neither recreated nor reactivated it, and the
custom policy stayed inactive. The legacy-field checker exception names only
this disposable fixture's two `User.scope_type` assignments; it permits no
runtime grant or real-tenant migration.

Earlier migration rehearsals on siblings
`kdps_rehearsal_migrations_1423bc33a9fb` and
`kdps_rehearsal_migrations_5d3b3cf6d334` remain historical evidence. The frozen
copy run above supersedes them for current rehearsal-source evidence.

This proves schema install/upgrade repeatability and conservative linked
assignment replay for the named fixture. It does **not** prove every real
legacy RoleGrant/period/exclusion, historical financial document, brand-binding
migration, interrupted operational cutover or independently reviewed rollback.
The full baseline's migration/identity tests provide separate focused cases;
actual real source/target reconciliation and attributable sign-off remain
mandatory.

Rollback must append independently reviewed compensating assignment/policy or
mapping changes, invalidate affected sessions and preserve original approvals,
revocations, later independent edits, identifiers and business postings. It
must never restore a legacy permission source, delete evidence or reverse
business history. The fixture's direct target revocation is expressly a replay
test, **not a demonstrated operational rollback**. A real cutover cannot be
accepted until this compensating protocol has its own current evidence and
separately approved inputs.
