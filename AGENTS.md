# Consolidation working agreement

The store-operations roadmap delivers one supported implementation per workflow.
Before changing a business workflow, read the SO-03 consolidation inventory and
register in `docs/planning/`. Use its owner, target, dependencies and retirement
condition to define the change boundary. Do not infer a target from a `goods-v1`
name or from a screen being hidden.

For a normal delivery item, inspect its mapped dependency slice and newly
changed edges. Reserve manual repository-wide investigation for the SO-03
foundation and SO-15/SO-21 milestone checks; CI still runs the inventory drift
check on every change. The map contains static leads; before a workflow cutover,
trace each affected handler through its actual service, data read/write and
authorization/projection path.

- Put new business capabilities in the chosen target. Change a temporary legacy
  path only for essential safety, integrity or continuity while its named
  consumer still needs it.
- Keep one business writer after cutover. Preserve historical identifiers,
  documents, financial totals, identity links and approval evidence; do not
  duplicate postings or silently fall back to an old permission source.
- Trace every affected channel: UI/API, jobs, search, reports, exports, files,
  print and events. Add an inventory record and an owner when a new surface or
  dependency is found. A safety blocker must be resolved before accepting the
  affected release.
- Run `backend/.venv/bin/python scripts/check_so03_consolidation.py --check`
  after changing routes, commands, models, screens or API consumers. Use
  `--write` to refresh the reviewed inventory and register, then review the
  classification and generated diff before committing.
- Attach migration reconciliation, targeted behaviour and denial tests, and
  baseline results to the exact revision tested. A previous green run does not
  validate later changes. A hidden menu or redirect does not retire code.

See `CONTRIBUTING.md` for the workflow retirement checklist. Product decisions
and the user's instructions take precedence over this repository guidance.
