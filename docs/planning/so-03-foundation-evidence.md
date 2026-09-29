# SO-03 consolidation foundation evidence

**Recorded:** 29 September 2026, 13:15 UTC. **State:** inventory and drift-check foundation delivered in an uncommitted shared working tree. This record does not accept the interrupted SO-03 access implementation or any later workflow retirement.

**Revision identity:** `HEAD b9e1e259d9b8696507f13123b4e8b7b4a7a4c783`; SHA-256 of the sorted path/content stream of 1,279 tracked and non-ignored untracked paths, excluding this evidence file: `79efae24dc1013f03ba64325dca30724394a01e0eefe14910bba73c95a0a994c`. The stream includes each relative path followed by its file bytes (or a deletion marker), with NUL separators. The working tree contains earlier unfinished SO-03 application changes; they were preserved, not reset or accepted by this checkpoint.

## Coverage and owner decisions

The [machine inventory](so-03-consolidation-inventory.json) and generated [register](so-03-consolidation-register.md) and [dependency map](so-03-dependency-map.md) classify **2,910 discovered surfaces** with one primary owner, lifecycle, caller/dependency leads, data ownership, authorization boundary, migration requirements, replacement, acceptance evidence and removal condition. Discovery includes 791 mounted HTTP method routes, 11 configured middleware, 36 management commands, 17 scheduled jobs, 3 outbox handlers, 4 signal receivers, 315 migrations, 291 models, 485 maintained backend modules, 32 operational/configuration files, 140 frontend routes, 2 nested views, 120 navigation entries, 50 bookmarks, 554 API consumers (including 7 direct browser download links), 10 print sinks, 1 display window and 48 till/PWA client modules. The inventory has 2,899 exact classification rules across A00/P00 and C01–C20.

Lifecycle totals are 1,676 canonical, 475 temporary supported legacy, 315 historical read-only, 70 retirement candidates and **374 unresolved with named gates**. Of the unresolved records, 201 are optional Django admin methods; 80 are API consumers whose runtime target still needs tracing, and 48 are till/PWA modules awaiting SO-09 offline cutover proof. Sixty-seven temporary API methods have no statically named frontend consumer and carry a separate consumer-trace gate. The protected-boundary ledger freezes 94 existing legacy-authority source/reference edges; CI rejects growth or reintroduction of retired paths/routes/modules.

The map is static evidence: a route's read/write candidates come from its source file or callback module and can include another handler's operations. Before a workflow retires, its owner must verify the exact handler → service → data read/write → authorization and field-projection chain, as required by [CONTRIBUTING.md](../../CONTRIBUTING.md). An absent static caller does not establish that code is unused. Each C01–C20 retirement requires its own exact endpoint/payload, history, reconciliation and removal proof; SO-15 and SO-21 remain milestone checks.

## Checks on this working tree

| Check | Result and limit |
| --- | --- |
| `backend/.venv/bin/python scripts/check_so03_consolidation.py --check` | Passed: 2,910 surfaces match the reviewed snapshot and generated files; protected-boundary guard passed. |
| Focused scanner, map, checker and boundary unit tests | 20 passed. New routes/commands/screens, owner gaps, admin gates, client storage modules, callback chains, alias imports and retired components have focused checks. |
| Ruff, Ruff format and mypy on the ten new inventory scripts/tests | Passed; mypy checked the five implementation scripts. |
| `git diff --check` | Passed. |
| SO-02 frontend baseline | TypeScript, ESLint (0 errors, 18 warnings), Prettier and 15 Vitest tests passed. |
| SO-02 browser baseline on the isolated proof database | Build/assets and all 4 Playwright journeys passed. The final prepare step found no pending migrations; the proof schema includes migration 0022 applied in the earlier isolated run. |
| SO-02 backend baseline | Django system and migration-drift checks passed, then Ruff stopped on the unused `Refusal` import in `backend/outbound/views.py:38` from the interrupted access patch. The baseline did not reach its later stages. Separate backend mypy passed 547 files and both import contracts passed (908 files, 5,603 dependencies). |
| Generated API contract | Failed only on two stock endpoint description lines concerning shared season-end and tenant category rules; the generated client has not been changed at this foundation checkpoint. |
| Focused current-tree SO-03 backend migration/admin/denial/search tests | 34 passed, 1 failed: tenant-owned stock barcode `FIND-SKU-A` was absent from scoped search results. |

The [interrupted implementation review](so-03-interrupted-implementation-review.md) records the scheduled-assignment editor mismatch, migration and history gates, still-mounted legacy `RoleGrant` writer, optional-admin risk, contract drift and search failure. Those application fixes remain SO-03 integration work. The legacy `ProfileWizard` is PT/SKU identity configuration under `/setup/configuration?view=wizard`, not an access-grant editor; its corrected ownership is in the inventory.

## Later SO-03 integration status

The counts and check results above describe the 13:15 UTC foundation snapshot, not the later access patch. The generated inventory has continued to change after the SBU retirement POST was unmounted; current totals belong to the manifest and dated access evidence. The dated [interrupted implementation review](so-03-interrupted-implementation-review.md) and [SO-03 access evidence](so-03-access-evidence.md) record the subsequent implementation checks, newly found legacy approval callback, migration exclusions and remaining gates. The foundation checkpoint remains a prerequisite inventory, not SO-03 acceptance.
