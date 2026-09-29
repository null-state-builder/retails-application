# Workflow consolidation checklist

For each store-operations delivery item:

1. Find its target and current dependencies in the [SO-03 consolidation register](docs/planning/so-03-consolidation-register.md) and [generated dependency map](docs/planning/so-03-dependency-map.md). Confirm the API, screen, job, service and data boundary before editing. Inspect that workflow slice and new edges rather than repeating a manual whole-repository search. For each affected entry point, review the exact handler → service → model/read-or-write → authorization/projection chain; the generated static candidates are leads, not execution proof.
2. Give every new or newly discovered entry point one primary owner and a lifecycle state in the [machine inventory](docs/planning/so-03-consolidation-inventory.json). Record its replacement and retirement condition if it is temporary legacy code.
3. Move all readers and writers of the affected business state to the selected target. Check search, reports, files, exports, print, events and queued work as well as the main screen.
4. Rehearse data migration with repeatable dry runs and reconcile identifiers, historical links, document numbers, approval evidence and financial or stock totals. End with one business writer. A rollback plan must preserve transactions created after cutover.
5. List retained endpoints, changed payloads, removed mutations and supported redirects in the workflow cutover record. Remove obsolete mutations, callers and screens only after the replacement and historical reads pass. Add retired modules, paths and API prefixes to `protected_boundaries` in the machine inventory so CI blocks their return. Keep any required history read-only and test bookmark redirects separately from APIs.
6. Run `backend/.venv/bin/python scripts/check_so03_consolidation.py --check`, focused allowed and denied journeys, the applicable SO-02 baseline checks and the generated API contract. Record results against the exact revision tested.

If the inventory exposes a safety gap, fix or disable the affected path before
acceptance. If it exposes another duplicate, assign its retirement item first;
expand the current item only when its safe delivery depends on that work.
