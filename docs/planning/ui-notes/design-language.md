# Proposed design language for the screen review

**Status: reference proposal for the review, not a newly approved redesign.**

KDPS already has an approved visual direction in `frontend/src/index.css`: the
**Warm** design language. The screen review should use that as its starting point
and decide where existing screens follow it consistently. The goal is one
recognisable KDPS interface, not another palette or a new component style.

## Visual foundation

- Use the existing semantic tokens for paper, surface, inner surface, hairlines,
  text, brand colours, status colours, radii and shadows. Do not introduce page
  specific colour literals.
- Preserve the warm paper / surface layering and its warm-charcoal dark theme.
  Every semantic choice must work in both themes.
- Use the system font already defined in the shared stylesheet. Reserve the
  monospace token for identifiers, numbers and other compact machine-like values.
- Prefer the existing card, field, button and table styles. Keep one primary
  action visually clear; use secondary controls with the established treatments.
- Treat spacing, input height, headings, page width and card padding as shared
  design-system decisions. Compare existing screens before setting final values;
  the Signup layout alone does not establish a universal measurement.

## Interaction and content

- Give each screen one clear title and a short statement of its task. Keep
  instructions beside the decision or control they explain; move rare detail into
  a disclosure or help link.
- Make progress, selection and completion visible through text and shape as well
  as colour. Use the same treatment for loading, empty, denied, error and success
  states across screens.
- Keep forms keyboard operable with visible focus, associated labels, useful
  inline errors and a clear route to the first invalid field.
- At narrow widths, stack fields in a sensible reading order. Put wide data in
  deliberate internal scroll areas; prevent whole-page horizontal overflow.
- Keep navigation and visual status as orientation aids. They must not imply
  permissions, approvals, stock readiness or workflow completion that the server
  has not confirmed.
- Use concrete operator language and one name for each workflow throughout
  navigation, page titles, actions and messages.
- Keep a visible way back to the previous step, and bring back details already
  entered. Recorded from the signup confirmation review on 1 October 2026:
  going back is navigation, so it does not ask for a sign-in or a password.
  An identity check belongs on the action that saves, confirms or revises.
  Apply this when those screens are reviewed. It does not by itself approve a
  shared navigation change.

## Review questions for the complete screen tour

- Which visual patterns already work best in Today and Sell, and which are
  repeated inconsistently elsewhere?
- Which shared controls should be standardised, and which workflows need a
  genuinely different interaction?
- Does the Warm palette maintain readable contrast and clear statuses in both
  themes and at every screen size?
- Which areas are information-heavy enough to need progressive disclosure or a
  clearer separation between daily work and review/history?

Answer these after comparing the screens. Do not lock new spacing values, shared
components or navigation structure from Signup alone.
