# Design System v2 rollout plan

How the KDPS Design Language v2 moves from the design artifacts into `frontend/`, cashier screens first.

- **What it rolls out:**
  - **Design system v2:** tokens and 47 component specs, each with a README that serves as its contract. [Design system artifact](https://claude.ai/artifact/JqxBxwgM5VP8xfx95SPY2v).
  - **The 80 cashier boards**, restyled to v2. [Cashier canvas artifact](https://claude.ai/artifact/UoKEechRvH9MHkjEx2k7kM).
  - **The research behind v2:** [`reports/Screen space and less generic UI.md`](../../reports/Screen%20space%20and%20less%20generic%20UI.md).
- **What it builds on:** [`desktop-redesign-audit.md`](desktop-redesign-audit.md).
  - Batches 0–2 are shipped (commits `f6984de` to `e4bbecd`).
  - Batches 3–6 have not started.
  - This plan changes how those remaining batches are built; it does not reopen the audit's decisions.
- **What it coordinates with:** [`app-loading-and-caching-plan.md`](app-loading-and-caching-plan.md). That plan owns lazy routes and the query cache; this plan owns how loading looks.

## Where the app is today (measured 30 Oct 2026)

| Area               | Today                                                                                                              | v2                                                                                                                                 |
| ------------------ | ------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| Stylesheets        | 48 CSS files, about 14,100 lines. The largest are `Billing.css` (2,767 lines) and `AppShell.css` (1,395).          | Same files, fewer literals                                                                                                         |
| Tokens             | Colours in `src/index.css` (`:root` and `html[data-theme="dark"]`); radius 12/8 and two shadows                    | v2 `tokens.css`: grounds moved toward neutral, `radius-control`/`box`/`overlay`, three shadows, size, layout and breakpoint tokens |
| Hard-coded colours | Low outside `index.css`: about 22 each in `Billing.css`, `AppShell.css` and `ScanScreen.css`                       | Tokens only                                                                                                                        |
| Typeface           | `system-ui`. IBM Plex Mono is loaded but used only on the counter (`theme/counter.css`).                           | IBM Plex Sans app-wide, Plex Mono for identifiers only                                                                             |
| Font sizes         | Values used: 12 (114), 13 (109), **12.5 (93)**, **13.5 (71)**, 14 (59), 15 (24), and 14 more sizes from 10.5 to 34 | 12, 13, 14, 16, 20, 24, 32, 44, 56 only                                                                                            |
| Font weights       | **700 (95)**, **650 (22)**, **800 (6)**, 600 (130), 500, 400                                                       | 400, 500, 600                                                                                                                      |
| Radii              | 999, 8, 10, 6, 12, 9, 14, 4 as literals                                                                            | 0 / 4 / 8 / 12 / round, as tokens                                                                                                  |
| Breakpoints        | 1100 (split), 1024, 900, 860, 768, 720 and others                                                                  | 1200 / 1600 / 1920 / 2400 for desktop; the phone sizes stay                                                                        |
| Page dividers      | `.card` (7), `.stat-card` (27), `StatTile` (3)                                                                     | Section (unboxed), KpiStrip, `.box` only for tables, record pane and bill                                                          |
| Billing            | Already a bill with a right rail (`PaymentPanel.tsx`, `RailFoot.tsx`); keys F2/F3/F4/F9/Esc                        | Same structure; the rail is reordered and uses Counter density                                                                     |

## How the rollout works

1. **Tokens before screens.**
   - Most colours already come from variables, so swapping the token values restyles about 70% of the app in one reviewable change.
   - Old token names stay as aliases for one release, so nothing breaks while the screens move over.
2. **A mechanical sweep, then judgement.**
   - A codemod rewrites the literal sizes, weights and radii.
   - People review the files where layout or meaning changes.
3. **Shared components next.** Section, KpiStrip, Status, Table, SplitView pane, PageHeader, states. Each screen then adopts them rather than restyling itself.
4. **Screens through the remaining audit batches.** Batches 3–6 are built straight in v2. Pages shipped in batches 0–2 get a short adoption pass.
5. **Every change is checked the same way.** See "Checks on every change" below.

## Phases

### Phase 0: prepare (½ day) — done 30 Oct 2026 (`6629b2b`)

- Commit or park the uncommitted `src/pages/sell/Till.css` and `Till.tsx` changes that are on the branch now, so v2 starts from a clean tree.
- Add `browser/visual-baseline.spec.ts`.
  - It screenshots the cashier screens at 1280, 1440 and 1920, in light and dark: Home, Billing, Bills, Till & sync, Cash count, Day close, Customers, Receive, Transfers, Stock on hand, Stock counts, one report.
  - Run it on the demo cashier login against the rehearsal database.
  - It is the before picture for every later phase.
- Add `scripts/check-design-tokens.mjs`, which reports:
  - half-pixel font sizes;
  - weights 650, 700 and 800;
  - hex or `rgb()` outside `index.css`;
  - radius and shadow literals.

  Start it in report-only mode; it becomes a CI failure at Phase 6.

### Phase 1: foundation (1–2 days, one PR)

- **Tokens:** replace the colour blocks in `src/index.css` with the v2 values from the design system's `tokens.css`, in light and dark.
  - Add the spacing, size, layout and radius tokens.
  - Keep `--radius` and `--radius-inner` as aliases, pointing to `--radius-box` and `--radius-control`.
  - Point `--shadow-rest` at `none` and `--shadow-hover` at `--shadow-float`.
- **Fonts:**
  - Add `@fontsource/ibm-plex-sans` (400, 500, 600, latin) and self-host it next to Plex Mono in `theme/fonts.css`.
  - Set the app font stack to Plex Sans.
  - Move the Plex Mono rules out of `theme/counter.css` so identifiers use mono everywhere and amounts never do.
  - Verify in devtools that Plex Sans gives equal-width digits with `font-variant-numeric: tabular-nums`. The research left this unconfirmed.
- **Base type:** body 14/20, letter-spacing 0.16px, `b, strong { font-weight: 600 }`.
- **Browser chrome:** update `META_LIGHT` and `META_DARK` in `theme/theme.ts` and the inline script in `index.html` to the new paper colours (`#f7f6f3`, `#1d1c1a`).
- **Density:**
  - Write `data-density` on `<html>` from the role at sign-in: `store_person` → `counter`, everyone else → `desk`. Compact is opt-in, as the design system's Density spec says.
  - A person's override is read from `kdps-density` in the browser for now (`theme/density.ts`); storing it with their profile needs an API change and is a follow-up.
  - Add the density blocks from the design system's `bundle.css`.
  - Add a `src/theme/layout.ts` that exports the breakpoints for code that needs them (pane docking, column priority).
- **Button sizes follow density** (decision 7): Desk 13px/600 on 36px, Counter 14px/600 on 44px, Compact 13px/600 on 32px, through a `--btn-text` token beside `--control`. Commit buttons (Take payment, Save & print, Post goods) stay 44px in every density. Till & Sync's page-only 13px override is deleted.
- **Links** (decision 6): `a` and `.link` are underlined in `--navy`, with a 2px underline offset, everywhere.

**Result:** every screen in the new colours, typeface and base size, with nothing restructured yet.

### Phase 2: literal sweep (2 days, one PR per area)

**Done 30 Oct 2026.** `frontend/scripts/codemods/type-v2.mjs` rewrote 485 declarations in 42 stylesheets (areas: shell, components, sell, pages). It decided 93 of them from the selector, marking each one "judged" (`--judged` lists them): sizes 15–19px became 14 or 16 for text and headings, and 20 for amounts; 7–10px corners became `radius-control` for controls and `radius-box` for containers. Then, by hand:

- **Shadows:** the 11 shadows became `--shadow-float`, `--pin-shadow`, or the new `--focus-halo`, `--halo-ok` and `--halo-error`. Input focus is rust, like `--focus-ring`.
- **Colours:** white text became `--on-fill`, hex fallbacks inside `var()` were dropped, and the scan flashes use `--green-bg` and `--red-bg`.
- **Inline styles:** inline sizes, weights and radii in 14 components moved onto the scale.

`check-design-tokens.mjs` now reports 4 findings, all on purpose: label paper and print rules (`GoodsLabels.css`, `TagPrint.css`, the print block in `AppShell.css`) and the text on the login's navy panel.

A codemod (`scripts/codemods/type-v2.mjs`) with a dry-run diff, applied per area: shell, sell, goods, stock, reports, setup.

| From                                | To                                           | Note                                                   |
| ----------------------------------- | -------------------------------------------- | ------------------------------------------------------ |
| 10.5, 11, 11.5, 12.5px              | 12px                                         | nothing smaller than 12                                |
| 13.5px                              | 14px                                         | 13px only in compact table cells                       |
| 15px                                | 14px (text), 16px (headings, counter inputs) | the person reviewing a file picks one                  |
| 17, 19px                            | 16px or 20px                                 | headings go to 16; money the cashier checks goes to 20 |
| 22, 23, 26px                        | 24px                                         | metrics                                                |
| 30, 34px                            | 32px                                         | lead figure                                            |
| weight 650, 700, 800                | 600                                          |                                                        |
| radius 6, 8, 9, 10 on controls      | `var(--radius-control)`                      |                                                        |
| radius 10, 12 on containers         | `var(--radius-box)`                          |                                                        |
| radius 14                           | `var(--radius-overlay)`                      |                                                        |
| shadows on in-flow cards and asides | none                                         |                                                        |

Each area's PR shows its before and after screenshots.

### Phase 3: shared components (3–4 days)

Map each design-system component onto what exists, extending a component rather than adding a parallel one.

| v2 component                                      | App today                                                    | Change                                                                                                                                                                                                                                     |
| ------------------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Section                                           | `.card`, `.section-card`, `.stat-card` used as page dividers | new `Section` (`heading` + optional link, unboxed); `.box` kept for tables, record pane and bill                                                                                                                                           |
| KpiStrip                                          | `StatTile`, `.stat-grid`                                     | one lead figure with a same-weekday sentence, then an unboxed strip                                                                                                                                                                        |
| Status (Chip)                                     | tone chips across `index.css` and `Shared.css`               | `.status` dot + word; filled `.chip` only for amber/red/blue states that need action                                                                                                                                                       |
| Table                                             | `.kdps-table`, `.data-table`, `.ledger-table`                | row height from density, 16px cell padding, `.pin-col`, `.p2` from 1600 and `.p3` from 1920, a "Columns (n hidden)" menu, a single last-cell action menu                                                                                   |
| SplitView pane                                    | `.split` + `.aside`, breakpoint 1100                         | `.pane`, border-left only, docks from 1200. 360px default, resizable 320–440, width stored per person. 420px from 1600. The sidebar folds to the rail while a record is open below 1600.                                                   |
| PageHeader                                        | `components/PageHeader.tsx`                                  | title 20/600 navy; a lead sentence stating where things stand; a hairline under it                                                                                                                                                         |
| EmptyState, ErrorState                            | `components/States.tsx`                                      | left-aligned, a heading, one line, the action                                                                                                                                                                                              |
| Note                                              | `.note` variants                                             | 3px left bar, ink text                                                                                                                                                                                                                     |
| Buttons                                           | `.btn-*`                                                     | radius 4. `btn-cta` only for the one action that commits money or stock; delete and void become `btn-danger` (audit the 78 files using `btn-cta`).                                                                                         |
| SegmentedControl, FilterChip, SearchBox, Combobox | existing                                                     | v2 styles; no API change                                                                                                                                                                                                                   |
| Switch                                            | `.till-switch` (Till), checkboxes and label-flipping buttons | one `Switch` for settings that save on flip, with "On" / "Off" written beside it (decision 8). First users: Feature Switches (with a third state, "On, held off"), a product's no-discount setting, broken-size rules, Till's scan sounds. |
| SettingRow                                        | `TaskRow` in `Till.tsx`; hand-built rows in settings pages   | title and sentence on the left, the control in a right column at least 140px wide, an inline form opening below (decision 9)                                                                                                               |
| View filters                                      | "Show retired", "Only mine" and similar checkboxes           | `FilterChip`s in the list toolbar. Where the filter swaps the whole list (Organisation's "Show retired"), a two-option `SegmentedControl`: Active / Retired (decision 10).                                                                 |

Write each component's contract from its design-system README into a short comment at the top of the component file.

**Done 30 Oct 2026.** The v2 component styles are in `frontend/src/theme/components.css` (loaded after `index.css`), and each component has a React wrapper in `frontend/src/components/` with its contract at the top: `Section`/`Box`, `KpiStrip`/`LeadFigure`, `Status`, `Note`, `SegmentedControl`, `FilterChip`/`FilterChips`, `Switch`, `SettingRow`/`Settings`, `SplitView`/`Pane` (width 320–440, kept per person). `PageHeader` draws `.page-head` with a hairline; `EmptyState`/`ErrorState` are left-aligned with a heading. Adopted so far:

- **Switch:** Till's scan sounds, Feature Switches (with "On, held off"), a product's No discounts, broken-size rules.
- **SettingRow:** every Till & Sync task and setting (`TaskRow` is now a thin wrapper).
- **SegmentedControl:** all `.seg` view switches (five reports, Transfers, Stock on hand, Return to Brand), the theme picker, Alerts' Unread/All, Outbound transfer kind, Billing's Sale/Return and customer search-by, credit due-days, Cash count's deposit/handover, Customers' search-by. `bill-mode`, `theme-seg`, `an-seg`, `mode-toggle` and the `plus-due` buttons are gone. Product lists' upload/lists/history strip switches panels, so it stays a tab strip.
- **View filters:** Organisation's three "Show retired" buttons became Active / Retired; vendors' "Show retired", Alerts' "Only mine", Stock sync's "Only rows still to review" and SOH import's "Include zero stock" became FilterChips.
- **Buttons:** of 213 `btn-cta`, 49 stay red (they commit money or stock on that press), 3 became `btn-danger` (red outline: disconnect, confirm retirement, cancel a booking) and 161 became `btn-primary` (navy), including drafts, Issue GRN, Record the invoice and submit-for-approval (decided 30 Oct).
- **By CSS, without touching markup:** `table.data` rows follow density with `.p2`/`.p3` column priority and `.pin-col`; `.chip` is the 24px square-cornered v2 chip; `.ok-note`/`.warn-note`/`.ai-note`/`.till-alert` draw as v2 Notes; `.stat-grid` draws as an unboxed KPI strip with no icons; `.h1` is 20/28.

Screens move from `.card` page dividers to `Section`, and from tone chips to `Status`, in Phase 5 as each is rebuilt.

### Phase 4: shell and motion (2 days)

- **Shell sizes:** top bar 52px on `paper`, sidebar 232px with quieter section headers, rail items 44×40.
- **The shell never remounts.** `TillProvider` is applied per screen in `routes.tsx` (line ~147) and `auth/ProtectedRoute.tsx`. Move it so moving between Sell screens keeps the shell and the till session mounted. This was the main cause of the jerky screen changes.
- **Page enter:** opacity only over 100ms, no rise. Sections never fade up and cards never lift on hover. Drawers, modals and toasts follow the design system's Motion section.
- **Loading states:** skeletons appear only after 300ms and are held at least 400ms. The previous screen stays visible while the next loads.
  - This depends on the loading plan's Phase 1 (lazy routes and cached data). If that ships first, this phase only styles it.
  - If not, this phase adds the delay and the minimum hold to the existing `LoadingRows`.

**Done 30 Oct 2026.**

- **Shell:** top bar 52px on `paper`, sidebar 232px. Section headers are 13/500 muted (ink 600 for the one you are in) and section icons are muted; the screen you are on is `surface` with rust text and a 2px rust bar instead of a rust wash. Rail items are 44×40.
- **The shell never remounts:** `ProtectedRoute` keeps one `TillProvider` above `AppShell` and switches it on only in the counter room (`TillProvider enabled`). Billing, Bills, Till & Sync and Cash count all declare `room: "counter"`, so moving between them keeps the shell and the running counter, and moving to any other screen keeps the shell. `withTill` is gone.
- **Motion:** a new screen fades in over 100ms, opacity only (`.content > *`); Home's tiles no longer lift on hover. Reduced motion turns both off.
- **Loading:** the app-loading plan has not shipped, so this phase adds `Loading` (skeleton lines, nothing shown for the first 300ms) and `useDelayedLoading` (300ms delay, 400ms minimum hold) in `components/States.tsx`. The goods screens' shared `listState` uses `Loading`; other screens adopt it in Phase 5.

### Phase 5: cashier screens (folds into audit batches 3–6, about 12–15 days)

In order of cashier traffic. Each screen is one PR, built to its board on the canvas.

1. **Billing** (Batch 5):
   - Counter density.
   - The rail in the v2 order: customer, salesperson, a collapsed Tax invoice (GSTIN) row that opens at ₹50,000 or when a GSTIN is entered, totals in 20px, To pay at 44px, then the commit button.
   - The last-scanned line gets a rust bar.
   - Payment stays in the rail; check that `UpiCharge` (QR) is the only overlay.
2. **Bills, Till & sync, Cash count, Day close:** adoption pass for batch 0–2 pages, plus Batch 5 for Cash count.
3. **Home** (Batch 4):
   - The greeting heading gives way to "Today, Thu 29 Oct" and a sentence on the store's position.
   - KpiStrip with net sales as the lead.
   - "Needs you" and ranked top-3 lists for salespeople, brands and tenders.
4. **Customers, Reservations, Special orders, Alterations** (Batch 3).
5. **Receive, Goods arrived, Transfers, Transfer requests** (Batch 3).
6. **Stock on hand, Stock counts, Count schedule, Labels** (Batches 3 and 5).
7. **Reports:**
   - The report template from Batch 4 becomes KpiStrip plus one wide table with column priorities.
   - Charts use navy plus a warm grey; rust appears in no chart.
8. **Sign-in and standalone** (Batch 6).

**Done 30 Oct 2026**, as one commit rather than a PR per screen. Presentation only: no API, route, permission or writer changed, and every testid stayed. Where a board shows data the API does not carry, that part was left out rather than fetched:

- **Billing:** rail order is customer, Sold by (new `billing/SoldBy.tsx`, moved out of the bill bar), a GSTIN row that opens itself at ₹50,000 net or when a GSTIN is entered, the tenders, then To pay at 44px above Save & print. Rust bar on the last-scanned line and the tender being typed into. Every `.bill-alert` and banner is a `Note`; `UpiCharge` is the only payment overlay. Totals stay under the lines (in the rail they push the tenders off a 1366×768 screen).
- **Bills:** `SplitView` with the selected bill in a `Pane`: number, Reprint (navy), Exchange, lines, tenders and a receipt preview built from what the list already holds. Synced / Not yet synced are on-screen `FilterChips`. **Till & sync** and **Cash count:** Sections, notes and status words; Cash count shows expected, counted and difference side by side. **Day close** has no screen yet (`DailyCash` is money in/out), so its board waits.
- **Home:** "Today, Fri 30 Oct" with a sentence built from loaded data, net sales as the `LeadFigure`, a `KpiStrip`, "Needs you" as links, tenders ranked. Salesperson and brand rankings and the same-weekday comparison are not in the dashboard data, so they are left out. The store checklist is a `Section`.
- **Customers, Reservations, Special orders, Alterations, Find a bill:** one wide table each with `.p2`/`.p3` columns, `Status` for state, `FilterChips` with counts, the open record in a side pane (kept in `?open=`). The "New …" forms open from a header button.
- **Receive, Goods arrived, Transfers, Requests:** wide tables with column priorities, the stage tiles are `FilterChips`, Goods arrived has a numbered stepper, cards became Sections and Boxes.
- **Stock, Counts, Count schedule, Labels:** the stock view switch is a `SegmentedControl` (it only changes `?view=`), totals are a `KpiStrip`, tables use `table.data`, states are `Status`.
- **Reports:** Sales report is a lead figure plus strip, one wide table and a navy bar chart by day; Day summary has a tender table with navy and warm-grey share bars and an exceptions table. **Login** and **Change password** share one layout (`SignInFrame`); the demo chips are unchanged.
- **Shared:** `.dl` (description list), `.section > p` and `.section > .btn` spacing, and `a.seg-btn` joined `components.css`; `goodsScreen`'s `Feedback` and the PIN refusal render `Note`. Dead rules (`.transfers-stage*`, `.stock-limitation`, `.bill-alert`, `--hero-navy`) are gone.

Not seen on screen with data, because the demo cashier gets "No access" or the rehearsal tenant has no rows: Sales report, Customers, the order screens, transfer detail and the receive stepper. Their tests pass; they need a look with a manager login on seeded data.

### Phase 6: other roles and clean-up (3–4 days)

- Manager, warehouse and head-office pages adopt the same components (Desk or Compact density).
- Remove the token aliases and dead CSS.
- Turn `check-design-tokens.mjs` into a CI failure.
- Update `docs/planning/ui-notes/design-language.md` to point at the design system artifact as the source.

## Checks on every change

- **Code checks:** `npm run typecheck`, `npm run lint`, `npm run format:check`, `npm test`.
- **Browser tests:**
  - `npm run test:browser`. These target only a few classes (`.operations-page`, `.net-num`, `.spark-col`, `.form-grid`, `.content`); keep those names or update the tests in the same PR.
  - The visual baseline at 1280, 1440 and 1920, light and dark, with the diff attached to the PR.
- **Contrast:** recheck any new colour pair against the table in the design system README (WCAG AA; 3:1 for control edges and focus).
- **Consolidation:** `backend/.venv/bin/python scripts/check_so03_consolidation.py --check` in every PR that touches screens, routes or navigation (AGENTS.md).
  - This rollout changes presentation only: no API, writer, route or permission changes.
  - If a screen PR finds a workflow it would have to change, that goes through the SO-03 inventory first.
- **Printing is out of scope:** bills and credit notes on 80 mm stay as they are.
  - The global `@media print` rules in `TagPrint.css` and `GoodsLabels.css` that set a 50 mm page for every print are a separate fix.
  - That fix should land before Phase 5 touches Bills.

## Effort

| Phase                   | Days                                                                                         |
| ----------------------- | -------------------------------------------------------------------------------------------- |
| 0 Prepare               | 0.5                                                                                          |
| 1 Foundation            | 1–2                                                                                          |
| 2 Literal sweep         | 2                                                                                            |
| 3 Shared components     | 3–4                                                                                          |
| 4 Shell and motion      | 2                                                                                            |
| 5 Cashier screens       | 12–15 (this replaces the remaining audit batches for cashier pages; it does not add to them) |
| 6 Other roles, clean-up | 3–4                                                                                          |
| **Total**               | **about 24–30 working days**                                                                 |

Phases 1–2 can ship as one visible release, so the whole app changes look at once. Phases 3–6 ship screen by screen behind no flag, because each screen is complete when it lands.

## Decisions (accepted 30 Oct 2026)

| #   | Question                                                                                                     | Decision                                                                                                                  |
| --- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| 1   | Cashier density outside the counter workspaces (Bills, Customers, Stock)                                     | **Counter everywhere for `store_person`.** One setting is simpler to train, and the person can switch to Desk in Profile. |
| 2   | Billing shortcuts. The research suggests Alt keys (Lightspeed style); the app and staff use F2/F3/F4/F9/Esc. | **Keep the F-keys.** They are one press, already taught, and printed on the boards. Update the TillLayout spec to match.  |
| 3   | Red as the commit colour                                                                                     | **Keep `cta` red for the one action that commits money or stock.** Delete and void move to red outline and text.          |
| 4   | Rust                                                                                                         | **Keep rust's one job:** where you are (active nav, focus, current step, last scan). It leaves charts and decoration.     |
| 5   | Ship Phases 1–2 as one release or area by area                                                               | **One release.** A half-switched app looks broken; the token aliases keep the risk low.                                   |
| 6   | Buttons or links                                                                                             | **Actions are buttons, places are links.** Every link looks the same: underlined, in the link colour.                     |
| 7   | Button size                                                                                                  | **By density:** Desk 13/600 on 36px, Counter 14/600 on 44px, Compact 13/600 on 32px. Commit buttons stay 44px.            |
| 8   | On/off settings                                                                                              | **`Switch`** with "On" / "Off" beside it, saved the moment it is flipped. Two named options use a `SegmentedControl`.     |
| 9   | Rows of settings with a control on the right                                                                 | **`SettingRow`** shared component, control column at least 140px. Table rows and status-band rows keep their own layout.  |
| 10  | View filters                                                                                                 | **Filter chips** in the list toolbar; a filter that swaps the whole list is a two-option `SegmentedControl`.              |

Decisions 6–10 came from the Till & Sync design session; the 22 two-option controls it found, built six ways today, all move to `SegmentedControl` in Phase 3.
