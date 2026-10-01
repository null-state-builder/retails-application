# KDPS UI and UX notes

This folder records screen-by-screen observations before a coordinated UI pass.
Each screen gets its own Markdown note. Keep observations separate from proposed
changes and distinguish visual defects, workflow friction, accessibility needs,
and product decisions. Do not implement from these notes until the screen review
is complete and the combined change list has been agreed.

## Screen review register

| Screen | Status | Notes | High | Medium | Low | Open decisions |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Signup | Re-inspected 1 Oct 2026 at about 725×994, dark theme, Edit setup with blank fields | 15 | 4 | 9 | 2 | 2 |
| Signup — Review setup | Prior populated-summary note stands; Review was not opened this pass | 1 | 0 | 1 | 0 | 0 |
| Signup — Confirmation | Contract reviewed; live tab was already on Edit setup, so this view was not opened | 2 | 1 | 1 | 0 | 1 |

**Current backlog: 18 candidate changes across 3 screens.** Priorities and counts
are provisional until all screens have been reviewed. A note can be consolidated,
reclassified, or closed after later screen comparisons. This is an audit register,
not implementation approval.

## Review order

1. Record screen purpose, route, role and the important states.
2. Note what already works and should remain consistent.
3. Write each candidate change with a priority and a reason.
4. Record unresolved product choices rather than choosing on behalf of the owner.
5. Compare the screen against [the proposed design language](design-language.md).
6. At the end of the screen tour, group duplicate issues and publish one coordinated
   implementation list with totals by priority and dependency.

## Screen notes

- [Signup](signup.md)
- [Signup — Review setup](signup-review.md)
- [Signup — Confirmation](signup-confirmation.md)
- [Design language](design-language.md)
