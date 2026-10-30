# KDPS design language

**Status: superseded for visuals by Design System v2 (accepted 30 Oct 2026).**

The source of truth for how KDPS looks is the Design System v2 artifact:
<https://claude.ai/artifact/JqxBxwgM5VP8xfx95SPY2v> (tokens, type scale, density
modes and one page per component), with the screen boards at
<https://claude.ai/artifact/UoKEechRvH9MHkjEx2k7kM>. The rollout, its decisions
and what each phase changed are in `docs/planning/design-system-v2-rollout.md`.

In the code:

- Tokens live in `frontend/src/index.css` (light and dark), density in
  `data-density` (Counter, Desk, Compact). `yarn check:design-tokens` fails on any
  off-scale size, heavy weight, colour, radius or shadow literal, and runs in
  `npm run verify`.
- Shared component styles are in `frontend/src/theme/components.css`, with a React
  wrapper per component in `frontend/src/components/` (`Section`, `Status`, `Note`,
  `KpiStrip`, `SegmentedControl`, `FilterChip`, `Switch`, `SettingRow`,
  `SplitView`, `PageHeader`, `EmptyState`/`ErrorState`/`Loading`). Each file starts
  with its contract.
- The rules that decide most screens: red `btn-cta` only commits money or stock;
  rust only marks where you are; places are links and actions are buttons; page
  parts are Sections on paper, not cards; states are a dot and a word.

The interaction and content principles below still apply unchanged.

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
