# Desktop redesign audit and consistency plan

**Date:** 29 October 2026. **Branch:** `desktop-redesign` at `4e24869`. **State:** proposal for approval. No app code is changed by this document.

**Target:** the Warm-palette tokens in `frontend/src/index.css`, the shared patterns already shipped (`.split-view`, `.filter-chip`, `.seg`, `.kdps-table`, `.stat-grid`, `.page-crumb`, `PageHeader`), and the 20 artboards of the [desktop mockup](https://claude.ai/artifact/1QwJaaRbvfy4Vcs3Qbd6hD).

**How the audit was done**

- **Pages opened:** every route in `frontend/src/routes.tsx` and every menu entry in `navConfig.ts` `SECTIONS`, in the running app. A probe script recorded each page's header, filters, tables, buttons, overflow, contrast and states, and every page was also looked at in a screenshot.
- **Roles:**
  - Store manager (Debanjan) first.
  - Owner (K D Singh) for full coverage.
  - Cashier 1 and Warehouse (Tanmay) for the pages that change by role.
- **Viewports:** 1440×900 is the main size. 1280×800 and 1920×1080 were checked on the dashboard, a report and a list page, and dark mode was spot-checked.
- **Data:** the rehearsal tenant has most store features switched off, so Customers, Orders, Gift vouchers, Brands, Sales report and others refused every role. To open them without touching the rehearsal data, the audit ran against a copy of that database:
  - `kdps_rehearsal_audit`, a Postgres template copy, with all 50 store features on for both stores.
  - Started through a git-ignored launch entry `kdps-api-audit`.
  - The rehearsal database was not modified, and the rehearsal API is running again on :8000.
- **Not reachable:** these detail screens need a record, and the tenant has none. Change password was opened through a temporary-password session on the audit copy, which was then reset. The scan overlay turned out to be reachable only from legacy writers that goods-v1 sites refuse (see its row). These detail screens still have no record to open:
  - a bill, a booking, a delivery, a transfer, a count, a return, a customer, and a debit note.
  - They are marked *unverified*, and the plan treats them through the layout rule rather than a page-specific finding.
- **Screenshots:** kept outside the repo, in the session's tool-results folder.

Status meanings: **matches** = consistent with the redesign; **minor** = small fixes inside the existing layout; **re-layout** = the page's structure changes; **merging** = folds into another page (§6); **roadmap / off menu / hidden** = leaves the menu (§6); **retiring** = SO-03 names another target, so only menu and safety changes apply.

---

## 1. Consistency rulebook

### Standard page layouts

| Code | Layout | Use it for | Shape at ≥1100px | Below 1100px |
| --- | --- | --- | --- | --- |
| **L** | List + detail | Any list whose row opens a record (bills, transfers, deliveries, returns, offers, brands, people, ledgers) | Header → filter bar (chips / seg / one search) → table on the left, record on the right in `.split-view-aside` (minmax(380px, 40%)). The URL carries `?open=<id>` so the record can be linked and the back button works | The aside becomes a right-hand drawer over the list |
| **L-expand** | List, rows expand | Staged work where the next step matters more than reading the record (transfers) | Stage strip that doubles as the filter → table with a "Next" action column; a row expands in place to show its parts and the one next action | Same |
| **L-facet** | List with facet panel | Large browse-by-attribute lists (stock on hand) | KPI row → view tabs → a 240px left filter panel (checkbox facets with counts) beside the table; a row opens its detail in a drawer | Facets become a filter drawer |
| **F** | Form / wizard | Creating one document (booking, arrival, return, reservation, offer) | Header with a back crumb → numbered steps as sentence-case `h3` section cards in a 2- or 3-column field grid → a sticky footer with the single primary action on the right. A side summary column (totals, checks, what happens next) when the form has lines | One column, footer stays sticky |
| **D** | Dashboard / report | Home, store dashboard, daily cash, every report | Header (actions: date range, export) → KPI row (`.stat-grid`, auto-fit minmax(210px)) → filter seg → content grid of cards or one wide table with a totals row. A "Not included" note sits above the numbers it qualifies | Cards stack |
| **W** | Workspace (scan / till / count) | Billing, till, cash count, count session, labels, scan screen | Full-height work area with no page lead. Left: the thing being worked on (scan target, lines). Right: a fixed 360px panel with the totals and the one confirm button | Same, panel below |
| **S** | Settings | Setup pages with several kinds of record | Left sub-nav (200px) → content panel, which is itself an **L** list with split detail | Sub-nav becomes a select |

### Component rules every page follows

1. **Header:** `PageHeader` on every page, standalone screens excepted. It contains:
   - the crumb (section / screen);
   - an `h1` that names the screen and never changes with role;
   - an optional count line (`N records · scope`);
   - at most one lead sentence;
   - actions on the right.

   No uppercase eyebrows anywhere. A section title inside a page is a sentence-case `h3`, in ink, never rust.
2. **One primary action per page.** `.btn-cta` (red) is used only for that page's main action. `.btn-primary` (navy) is used for a form's submit when the page already has a CTA. Everything else is `.btn`. A disabled CTA uses the neutral disabled style, never a pink wash.
3. **Filters:**
   - A choice between 2–6 views or scopes uses `.seg`.
   - A status filter uses `.filter-chip` with counts.
   - Free text uses one `SearchBox`.
   - A select is used only for long lists (store, brand, season), and is never the first control when the top-bar switcher already sets the store.
   - Filters sit on one bar, with search last.
4. **Pickers:** one `Combobox` (type to filter, choose from the list). The current two-control `PickerField` (a search box above a select) is retired everywhere.
5. **Tables:** `.kdps-table` everywhere, with:
   - sentence-case headers;
   - right-aligned tabular numbers;
   - a sticky header inside a bounded scroll area;
   - `₹` in headers, never "(Rs)";
   - a totals row where sums mean something;
   - paging or "Show more" past 200 rows.

   A table wider than its card shows its key columns frozen. A row opens its record in the split view; per-row "Edit" or "Open" buttons are not used.
6. **Numbers:**
   - Indian digit grouping everywhere (15,734; ₹1,57,55,304).
   - Tile values in the sans face with tabular numerals, never monospace.
   - Currency in tiles rounded to the rupee, or compact (₹1.58 Cr) with the exact value in the tooltip.
   - **An unknown value shows "—" with a reason, never 0.** A value still loading shows a skeleton, never "Unavailable".
7. **States:** one shared component for each of these.

   | State | What the user sees |
   | --- | --- |
   | Loading | Skeleton rows |
   | Empty | One line saying why it is empty, plus the action that fills it |
   | Error | Plain-language message and a Retry button. The raw server text goes to the console, never the page |
   | No access | One page with the crumb, what the page is for, who can open it, and Back |
   | Needs a store | An inline store picker in the header, instead of a warning or a red error |

8. **Colour:**
   - Rust as text is always `--rust-ink`.
   - Status uses the green / amber / red / blue / grey chip pairs.
   - No hex outside `index.css` tokens. That includes `var(--x, #hex)` fallbacks and `KdpsLogo.tsx`; the logo needs a dark-mode variant.
9. **Space:** content uses the full width of `main` (`.page-pad`, 20px 28px 40px). No page renders without the pad. Prose stays at most 72ch wide; tables, forms and grids do not have that limit.
10. **Navigation:**
    - The sidebar is the only place that lists screens.
    - In-page tabs exist only for views of the same records (e.g. Running / Planned / Ended). A tab strip must not repeat the sidebar.
    - A planned (not built) screen is not in the menu; it is listed on `/roadmap`.
    - Billing collapses the menu to the 64px icon rail.
11. **From the mockup's Foundations board:**
    - Table rows are 38px.
    - A KPI strip, or stage counts, doubles as the filter.
    - An applied filter chip is solid; an available one is dashed.
    - A button shows its keyboard key where it has one (Save & print · F9).
    - A note that says what happens next sits beside the button that does it.
    - Every status chip carries a word, so colour is never the only signal.
    - IDs and barcodes are set in IBM Plex Mono 12.5; everything else uses the system font.

---

## 2. What the audit found, across pages

| # | Finding | Where (examples) | Fix lives in |
| --- | --- | --- | --- |
| 1 | **Unknown shown as 0.** These numbers come out as 0 or ₹0 when the real value isn't known:<br>• Exceptions and Inventory reports print 0 and ₹0 on every row under a banner that says "the reporting copy has not been built yet, so nothing is shown".<br>• Movement History shows 0 entries, 0 units and ₹0 while Stock shows 15,734 pcs.<br>• Store Targets says "committed across 2 stores: ₹0" when no target is set.<br>• Legacy In-transit shows ₹0 tiles. | `/reports/exceptions`, `/reports/inventory`, `/stock/history`, `/money/store-targets`, `/transfer/in-transit` | Batch 0b |
| 2 | **Fields 100–160px tall.** The global `.form-grid .input { flex: 1 1 160px }` (Shared.css) and `SearchBox`'s `flex: 1 1 260px` become heights inside a column `.field` | Reservations, Special orders, Opening stock (Site), Labels (PT id), New booking and Goods arrived (vendor / brand / season / booking pickers) | Batch 0 |
| 3 | **Duplicate screens for one job:**<br>• two ways to create a transfer (Send Stock vs New transfer);<br>• two stock-request screens;<br>• two count systems;<br>• three stock views (Stock, Goods stock, Damage & Quarantine with identical filters);<br>• two bill searches (Bills vs Sell › Customers);<br>• master lists twice: Stores vs Organisation › Sites, and Brands / Vendors / Seasons vs Products and parties. | Transfers, Stock Count, Stock, Sell, Setup | Batch 1 (menu), SO-03 owners (code) |
| 4 | **No list opens a split detail** except Bills, PT Work, Organisation and People and access. Action Needed opens a 460px overlay that hides the list. Products and parties replaces the list with the record. Stores and Vendor Ledger edit in a form above the table. | Most **L** pages | Batch 2 |
| 5 | **Two navigation models.**<br>• The store persona (cashier) gets collapsed sections plus an in-page tab strip.<br>• Other roles get sidebar sub-items, and Receive Goods, Offers, Inventory and Transfers also show a tab strip that repeats the sidebar.<br>• Owner and Warehouse see Billing, Till and Cash Count, which only explain that the login is not a counter.<br>• The store manager's menu puts Money and Reports above Sell. | Shell | Batch 1 |
| 6 | **Uppercase rust eyebrows are still used 187 times across 59 files:** "STEP 1 · LOCATIONS", "START A COUNT", "BOOKS HEALTH", "OPERATIONS HEALTH", "NOTHING RUNNING", "OPTIONAL · THE VENDOR'S DOCUMENT" | Forms, dashboards, empty states | Batch 0 |
| 7 | **KPI tiles:**<br>• monospace numbers without grouping ("15734 pcs");<br>• ₹1,57,55,304.17 overflows its tile;<br>• "Unavailable" while still loading;<br>• six ₹0 tiles in a row on an empty ledger. | Home, Stock, Vendor Ledger, Cash, Movement History | Batch 0 |
| 8 | **Five different "needs a store" and "no access" treatments:**<br>• amber note (Checklist);<br>• red error plus a "Loading…" that never ends (Day Summary);<br>• red error (Cash Count);<br>• inline picker (Petty Cash, SOR Ageing — the right one);<br>• "NOT FOUND … (ADR-0003)" (Goods arrived, as Owner);<br>• "ACCESS / No access" page without a crumb. | Many | Batch 0 |
| 9 | **More than one red CTA per page.**<br>• Sell › Customers: the three mode buttons are styled as CTAs, plus Search.<br>• Opening stock: two CTAs.<br>• Monitoring: two CTAs.<br>• Gift vouchers: the status filter is styled as a navy primary. | — | Batch 0 / page |
| 10 | **Raw internals on screen:**<br>• Counter Settings prints "The sell_policy configuration has no effective sell_policy configuration…" twice;<br>• PT Work lists manifest UUIDs;<br>• Products and parties lists hash style codes;<br>• Action Needed shows "Change cf9f77d9". | — | Batch 0 (errors), page |
| 11 | **Long, unpaged pages:**<br>• Audit Log renders every entry (a 143,000px page);<br>• Products and parties is 5,500px;<br>• Transfer Requests lists every stock line by category only ("SHIRT · 1"), about 3,000px. | — | Batch 2/3 |
| 12 | **Wide report tables scroll sideways:**<br>• Exceptions (2,573px) and Inventory (1,862px) at 1440;<br>• Store Targets overflows by 43px;<br>• Monitoring wraps every cell at 1280. | Reports, Money, Setup | Batch 4 |
| 13 | **Slow first paint (local, 4–12 s):** Stock on hand, Goods stock, Missing HSN, Stock Ageing, Receive Pending. Not a layout problem, but a skeleton is needed so the page doesn't read as empty or "Unavailable". | Stock | Batch 0 (states) |
| 14 | **Dark mode** is sound for tokens and tables. The KDPS logo (hex navy in `KdpsLogo.tsx`) almost disappears on the dark top bar. `Combobox.css` keeps stale hex fallbacks and uses `--rust` as text. | Shell | Batch 0 |
| 15 | **Viewports:**<br>• 1280: no horizontal page overflow anywhere probed.<br>• 1920: content fills the width, but the lone search box on Transfers drifts far right of its filters, and dashboard cards keep 1440 proportions.<br>• Pages with only a small form (Bills, Labels, Distribution, Count Schedule) leave about 60% of the screen empty. | — | Batch 2–5 |

Smaller defects noticed on the way:

- `/stock/vflips` titles itself "Stock on Hand" for the store manager.
- `/transfer/requests` is titled "Earlier transfer records" for the store manager and "Stock Request" for the Owner.
- Customers shows "No customer matches" before anything is searched.
- Signup still says "Set up your company and first store" when signup is closed.
- `/goods/labels` renders without `.page-pad`, against the sidebar edge.
- EOSS Planning is hidden in the menu but shown as a tab.
- The Movement History empty state tells the user to "Post a PT file from Patna (PT Mapper → Push into system)", an instruction that no longer exists.

### Mockup artboard → live page

Each artboard was viewed rendered at 1440×900, and the dashboard also in dark mode. The published version (1793234526-1e38) is byte-identical to the source used, and was served locally with the artifact's own runtime. Each artboard is the target for the page named here; the page table compares against it.

| Artboard | Live page | What the artboard sets |
| --- | --- | --- |
| Today · Dashboard (`Main`) | `/` | KPI row; Action needed table; Today's checklist; Sales by hour; Top brands; Latest bills; Stock at a glance; Start a task. No placeholder cards. |
| Today · Action Needed | `/action-needed` | Tabs All / Approvals / Goods exceptions; list with the record in a split, Approve / Send back / Reject in the aside |
| Sell · Billing counter | `/sell` | Line table (Item, Barcode, MRP, Offer, Qty, Net) with tender panel; mode seg |
| Sell · Bills | `/sell/bills` | Count line "46 today · ₹…"; table with Sync column; bill in the split |
| Customers · Profile | `/customers`, `/customers/:id` | List + profile split; profile tabs Bills / Open orders / Alterations / Returns; saved sizes |
| Customer Orders · Alterations | `/orders/*` | **One page** with tabs Reservations / Special orders / Alterations; alterations as a 4-stage board (Received → Ready for pickup) |
| Booking · Bookings | `/booking`, `/booking/open-to-buy` | Open-to-buy strip on top; tabs Open / Waiting for the Owner / Done; booking with lines in the split |
| Receive Goods · Pending | `/goods/receive` (+ delivery) | KPI row; tabs Pending / PT work / Done today (views of one inbox); delivery in the split with a step bar and invoice-vs-count table |
| Transfers | `/goods/transfers` | Stage strip (Draft, Waiting approval, Packed, On the road, Receiving, Difference) that doubles as the filter; Way seg; one table with a "Next" action column; **a row expands in place** to show its cartons and "Continue receiving". No split. |
| Stock Count · Session | `/goods/counts/:id` | Zones list, counted-in-zone table, session and counters panel (split) |
| Stock · Stock on hand | `/inventory` + `/goods/stock` | KPI row (units, value at cost, sold this week, broken sizes, older than 180 days, quarantined); tabs By barcode / style / brand / store / **Quarantine** on one page; **a left filter panel** (Brand, Season, Category, Stock health checkboxes with counts) beside a 38px-row table. No split. |
| Money · Daily Cash | `/money/daily-cash` (+ `/sell/cash-count`) | **One day-close page**: a day seg (27 Oct / 28 Oct / Today); a cash-in-the-drawer strip ending in the difference; takings by tender against settlement; closing count by note; cash movements with who approved them |
| Reports · Sales | `/reports/sales` (template for all reports) | KPI row; Net sales by day chart; By brand and By salesperson tables; Export as the one primary |
| Return to Brand · Returns | `/return-to-brand` | Allowance strip by brand; tabs All / In progress / Credit note owed; return with steps in the split |
| Brands · Terms | `/brands/terms` | KPI row; terms table with search and season chip; proposed-change diff in the split |
| Offers & Price · Promotions | `/offers` (+ `/offers/eoss`) | Tabs Running / Planned / End-of-season sale / Ended; offer + price check in the split; EOSS table below |
| People · Attendance | `/staff/attendance` (planned) | Tabs Attendance / Staff list / Member details; week grid; leave requests aside |
| Setup · People and access | `/setup/people-access` | People table with roles and sites, till, last sign-in; person in the split with **a section × capability matrix** (None / View / Operate / Approve / Manage); Setup pages are sidebar items, not an in-page sub-nav |
| Sign in | `/login` | Two-panel sign-in; "This counter" panel (till, connection, bills held offline); Keep me signed in; Forgot it; demo chips marked dev-only; Light / Dark / System switch |
| Foundations | — | Type scale and density (240px menu, 56px top bar, 38px rows, auto-fit KPI tiles); page anatomy (header → summary → tabs and filters → list + detail); one shape per component |
| Topbar, Sidebar | Shell | Search (Ctrl K), sync, working unit, alerts, mail, profile; sidebar section order Today → Sell → Customers → Customer Orders → Booking → Receive Goods → Transfers → Stock Count → Return to Brand → Stock → Money → Brands → Offers & Price → People → Reports → Setup, with only the open section expanded and live counts beside items |

What the mockup adds to the rulebook:

- **Tabs that are views of one record set are right** (Receive Pending / PT work / Done today; Stock By barcode … Quarantine). What goes is a strip that repeats the sidebar's *separate screens*.
- **Two list pages in the mockup don't use a split:**
  - Transfers: rows expand in place, because a transfer is worked through its stages.
  - Stock on hand: uses a left filter panel, because it is browsed by facet.

  The rulebook adds these as L-layout variants **L-expand** and **L-facet**.
- **The mockup's menu still lists the legacy items:** Send Stock, Distribution, In-Transit, Count Schedule, Goods counts, Adjustments and Write-offs. It predates this SO-03 check. §4 proposes removing them, which is Q4.

### Status tally

The page table has 116 rows. Together they cover every route in `routes.tsx`, the standalone routes in `App.tsx` and the shared screens; some rows group a list with its `/new` and `/:id` routes. Counted row by row:

| Status | Rows |
| --- | --- |
| matches | 7 |
| minor | 48 |
| re-layout | 36 |
| merging | 4 |
| roadmap / off menu / hidden | 5 |
| retiring | 16 |

With the §6 decisions applied, no row is still waiting on a decision. **Merging** rows fold into the named page. **Retiring** rows cover 27 routes plus the scan overlay (C01, C02, C07, C08, the test-run sync, and temporary navigation).

---

## 3. Every page

Layout codes as in §1, plus **P** = planned placeholder, **A** = sign-in / standalone, **Pr** = print. Effort: **S** ≤ half a day, **M** 1–2 days, **L** 3+ days. *(u)* = state with data not reachable in the tenant.

### Today

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/` Dashboard | D | re-layout | • "Collections · coming soon" and "Morning brief · AI preview" placeholder cards take about 40% of the page.<br>• Owner, manager and warehouse see "Network at a glance" admin counts (legal entities, GSTINs).<br>• Monospace, ungrouped numbers.<br>• Uneven grid at 1440.<br>• The mockup `Main` replaces the placeholders with Sales by hour, Top brands, Latest bills, Stock at a glance and an Action needed table. | M |
| `/action-needed` | L | re-layout | • The overlay drawer hides the list; the mockup uses a split.<br>• Approvals and exceptions are stacked cards with two buttons per row; they should be one table grouped by topic.<br>• The Site select sits among the chips. | M |
| `/alerts` | L | minor | • Three 0 tiles take a row even when the page is empty.<br>• Unread/All pills should be a `.seg`. | S |
| `/checklist` | L | minor | • A login without a store gets only an amber note; it needs the inline store picker. | S |
| `/mail` | A | minor | • A one-line "not set up" message; use the empty-state component. | S |
| `/roadmap` | L | minor | • Hidden page. It becomes the home of every planned menu item. | S |

### Sell

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/sell` Billing | W | minor | • Close to mockup `Billing`.<br>• The empty scan area takes about half the screen; show held bills and the last 5 bills there.<br>• Non-counter logins see a one-line note. | M |
| `/sell/bills` | L | minor | • Split view is built (u).<br>• Native date inputs.<br>• "Back to billing" repeats the tab strip. | S |
| `/sell/customers` (bill search) | L | merging → Bills (Q1a) | • Duplicates Bills, server-side.<br>• Its mode switch is three red CTAs plus a red Search.<br>• The label "Customers" collides with the Customers section.<br>• Proposed to become the "All stores" scope of Bills (Q1). | M |
| `/sell/till` | W | matches | • Dense, clear status panels.<br>• Non-counter logins see a one-line note. | S |
| `/sell/cash-count` | W | minor | • The two-column expected/counted layout is good.<br>• A login without a store gets a red error instead of the store picker. | S |
| `/sell/gift-vouchers` | L | minor | • The status filter is styled as a navy primary; should be a `.seg`.<br>• Search and Find are stacked.<br>• No split detail. | S |
| `/setup/settings` Counter settings | S | minor | • Raw sell_policy error printed twice.<br>• Listed under Sell with a `/setup` address. | S |
| `/sell/display` | W | matches | • Standalone second window; a plain message when opened directly is fine. | — |

### Customers and Customer Orders

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/customers` | L | re-layout | • Search-only page.<br>• "No customer matches" shows before any search.<br>• The result should open the profile in a split (mockup `Customers`). | M |
| `/customers/:id` | L (aside) | re-layout (u) | • Becomes the split aside; mockup profile. | M |
| `/orders/reservations` | L + F | re-layout | • 160px-tall inputs.<br>• The new-reservation form sits above the list; the list should lead, with "New reservation" opening the aside. | M |
| `/orders/special` | L + F | re-layout | • Same bug, and nine stacked tall fields. | M |
| `/orders/alterations` | L | re-layout | • Mockup `CustomerOrders` draws alterations as a 4-stage board, on one Customer orders page with tabs; today it is two stacked search cards. | M |

### Booking

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/booking` | L | minor | • Matches the list pattern.<br>• Opening a booking leaves the page; it should open the split (mockup `Booking`). | M |
| `/booking/new` | F | re-layout | • The vendor, brand and season pickers render as 110px empty boxes above separate selects.<br>• Uppercase eyebrows. | M |
| `/booking/:id` | L (aside) | re-layout (u) | • Becomes the split aside. | M |
| `/booking/open-to-buy` (+ `/:id`) | L + F | minor | • The budget form sits above the list.<br>• The mockup shows an open-to-buy strip on Bookings. | S |

### Receive Goods

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/goods/receive` Pending | L | re-layout | • The tab strip repeats the sidebar.<br>• Mockup `ReceiveGoods` has KPIs and a split with delivery steps.<br>• Slow first paint. | M |
| `/goods/receive/new` Goods arrived | F | re-layout | • Picker boxes 110px tall.<br>• Uneven column heights.<br>• Owner sees "NOT FOUND … (ADR-0003)" as the denial. | M |
| `/goods/receive/:kind/:id` delivery | F (steps) | re-layout (u) | • Should open in the Pending split with the step bar from the mockup. | L |
| `/goods/receive/history` | L | minor | • Add a split. | S |
| `/goods/pt-work` | L | minor | • The split exists, but the left list is 200px and wraps raw manifest UUIDs. | S |
| `/goods/labels` | W | re-layout | • No page pad.<br>• PT id input 100px tall.<br>• About 70% of the page empty. | S |
| `/goods/opening` | F | re-layout, moves to Setup (Q6) | • Site select 100px tall.<br>• Two red CTAs.<br>• Unstyled table header.<br>• A first-store task, not daily receiving (Q6). | M |
| `/goods/stock-sync` Legacy sync | F | retiring (test-run only) | • Empty table with no empty line.<br>• Keep as is and move it out of the daily menu (Q6). | — |

### Transfers

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/goods/transfers` | L-expand | re-layout | • Has the Way seg and stage chips.<br>• Mockup `Transfers` turns the chips into a stage strip with counts, adds a "Next" action column, and expands rows in place to show cartons.<br>• At 1920 the search drifts away from the filters. | M |
| `/goods/transfers/requests` | F | re-layout | • Every stock line listed by category only ("SHIRT · 1"), about 3,000px.<br>• Needs search by style or barcode, sizes as columns, and a basket summary on the right. | M |
| `/goods/transfers/:id` | F (stages) | re-layout (u) | • Keeps its own page, as the mockup does (the L-layout exception); use the step bar and the W panel for the stage's one action. | M |
| `/goods/transfers/:id/shipments/:dispatchId/document` | Pr | matches | • Print layout; exempt from the screen rules. | — |
| `/transfer` Earlier transfer records | L | retiring → `/goods/transfers` (C07) | • Menu only. | — |
| `/transfer/new` Send Stock | F | retiring → `/goods/transfers` "New transfer" | • A second way to create a transfer.<br>• Uppercase steps. | — |
| `/transfer/:id`, `/transfer/:id/pt` | L / Pr | retiring → C07 | — | — |
| `/transfer/distribution` | F | retiring → C07 | • Needs a target decision for "split a delivery across stores" (Q4). | — |
| `/transfer/requests` (+ `/new`, `/:id`) | L / F | retiring → `/goods/transfers/requests` | • Its title changes with role. | — |
| `/transfer/in-transit` | D | retiring → the "In transit" chip on `/goods/transfers` | • ₹0 tiles from the legacy source. | — |

### Stock Count

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/goods/counts` | L | re-layout | • List only.<br>• Mockup `StockCount` (session with live progress and variances) belongs on this target. | M |
| `/goods/counts/:id` | W | re-layout (u) | • The count session. | L |
| `/stock-count`, `/stock-count/:id` | L / W | retiring → `/goods/counts` (C08) | • A second count system.<br>• "START A COUNT" eyebrow.<br>• On a goods-v1 site, "Open a count" fails with a raw "legacy stocktake mutation … disabled" message, yet the menu still offers it. That supports Q4. | — |
| `/stock-count/adjustments` (+ `/:id`) | L | retiring → C08 correction journey | — | — |
| `/stock-count/writeoffs` (+ `/new`, `/:id`) | L / F | retiring → C08 correction journey | — | — |
| `/stock-count/schedule` | F + L | minor, becomes a Stock counts tab (Q5) | • SO-03 lists it under C08 without a named target (Q5).<br>• Form and list are fine; disabled CTA is pink. | S |

### Return to Brand

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/return-to-brand` | L | re-layout | • Mockup `ReturnToBrand` has a return-allowance strip and a split with steps; today it is a list only.<br>• The Defective/Season-end seg is drawn as large buttons. | M |
| `/return-to-brand/new` | F | re-layout | • Legacy step form with uppercase steps.<br>• Roles that may only mark damage get a full page of explanation; that should be a no-access state with a link to Damage and Quarantine. | M |
| `/return-to-brand/:id` | L (aside) | re-layout (u) | — | M |
| `/inventory?tab=damage-history` (Earlier damage records) | L | retiring (temporary nav) → `/goods/stock` Quarantine | — | — |

### Stock

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/inventory` Stock (`/stock` and `/stock/search` redirect here) | L-facet | re-layout | • The tab strip folds three sections together.<br>• Tiles in monospace without grouping; ₹ value overflows its tile.<br>• "Unavailable" while loading.<br>• Rows capped at 2,000.<br>• 6–12 s first paint.<br>• Mockup `Stock`: a KPI row, view tabs that include Quarantine, and a left facet panel with counts (C06 projection). | L |
| `/inventory?tab=damage` | L | merging → Stock on hand (Q3) | • Same filters and data as `/goods/stock` Quarantine; merge (Q3). | S |
| `/goods/stock` | L | re-layout | • Four selects plus a datetime field.<br>• Titled "Stock", the same as `/inventory`.<br>• Becomes the views (On hand / Availability / In transit / Quarantine) of one Stock page. | M |
| `/goods/stock/origins/:id` | L (aside) | re-layout (u) | • Origin journey in the aside. | S |
| `/goods/movements` | L + F | minor | • Three paragraphs of explanation above the work. | S |
| `/stock/history` Movement History | L | merging → Movements (Q3) | • Legacy ledger shows 0 / ₹0 while stock exists.<br>• Out-of-date empty-state instruction.<br>• Projection or merge into Movements (Q3). | M |
| `/stock/missing-hsn` | L | minor | • Slow; needs the skeleton state. | S |
| `/stock/ageing` | D | minor | • Slow; needs the skeleton state. | S |
| `/stock/broken-sizes` | L | minor | • Rules form inline is fine.<br>• Add a split for a flagged style. | S |
| `/stock/size-balancing` | L | minor | • Add a split. | S |
| `/stock/vflips` (+ `/new`, `/:id`) | L / F | retiring → C08 correction journey | • Titled "Stock on Hand" for the store manager. | — |

### Money

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/money/daily-cash` | D | re-layout | • Mockup `Money`.<br>• Date input spans the full width.<br>• Coloured ₹0 cells. | M |
| `/money/cash` | L | re-layout | • "Total cash on hand ₹0" with no opening balance recorded (verify it is known).<br>• Inline form with placeholder-only fields.<br>• "← Vendor Ledger" link button. | M |
| `/money/vendor` | L | re-layout | • Six ₹0 tiles.<br>• Three stacked empty cards.<br>• Placeholder-only inline form.<br>• Red and plain header buttons.<br>• "Cash Ledger →" link button. | M |
| `/money/petty-cash` (+ `/:id`) | L | minor | • Its inline store picker is the model for the others.<br>• Add a split. | S |
| `/money/payables` | L | minor | • Add a split. | S |
| `/money/debit-notes` (+ `/:id`) | L | minor | • The "Notes / Show" label is misaligned.<br>• Add a split. | S |
| `/money/irn-queue` | L | minor | • Small pill tabs should be a `.seg`.<br>• Add a split. | S |
| `/money/bank` | L + F | minor | • Fine; use the shared upload control. | S |
| `/money/partner-dues` | L | minor | — | S |
| `/money/partner-billing` | S | minor, moves to Setup (Q6) | • A single setting; move to Setup › Configuration (Q6). | S |
| `/money/store-targets` | F (grid) | minor | • "₹0 committed" when nothing is set.<br>• 43px overflow.<br>• FY select spans the full width. | S |
| `/money/payments`, `/money/collections`, `/money/expenses`, `/money/tally` | P | roadmap, off the menu (Q2) | • Planned; leave the menu (Q2). | S |

### Brands

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/brands/terms` | L | re-layout | • A bullet list of 39 "no model for AW-26" lines.<br>• Mockup `Brands` is a table with KPIs and a proposal diff in the split. | L |
| `/brands/margin-share` | D | minor | • Fits the report template. | S |
| `/brands/claims` (+ `/:id`) | L | minor | • Month title and picker are misaligned.<br>• Add a split. | S |
| `/brands/reports` | D | minor | — | S |
| `/brands/sor-ageing` | D | minor | — | S |

### Offers & Price

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/offers` Promotions | L | re-layout | • Mockup `Offers`: tabs by state, split with the offer and a price check, plus an EOSS table.<br>• Today a list only. | M |
| `/offers/running` | L | minor | • The tab strip repeats the menu.<br>• Uppercase rust "SITE" label.<br>• Uppercase empty state. | S |
| `/offers/new`, `/offers/:id` | F | minor | • Good structure.<br>• Uppercase section labels. | S |
| `/offers/eoss` | L | minor | • Hidden in the menu but shown as a tab.<br>• Becomes a tab of Promotions, as in the mockup. | S |
| `/offers/price-list` Price Book | L | hidden, not redesigned (Q7) | • C03 marks the route unresolved until a distinct purpose is approved (Q7).<br>• Uppercase labels. | S |

### People

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/staff/list` | L | minor | • Add a split per person.<br>• Mockup `People` puts it as a tab beside Attendance. | S |
| `/staff/attendance`, `/staff/members`, `/staff/payroll` | P | roadmap, off the menu (Q2) | • Planned (C18). The mockup draws Attendance for later. Leave the menu (Q2). | — |

### Reports

All reports share one template that works: a filter card, a `.seg` view switch, a "Not included" note, a table, then "How these figures are worked out". The fixes are to that template.

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/reports/sales` | D | re-layout | • Mockup `Reports` adds a KPI row and a trend chart.<br>• Store is a select. | M |
| `/reports/exceptions` | D | re-layout | • 0 / ₹0 rows while "nothing is shown".<br>• 2,573px table.<br>• "(Rs)" headers. | M |
| `/reports/inventory` | D | re-layout | • 0s beside "—" while the reporting copy is missing.<br>• 1,862px table. | M |
| `/reports/gst`, `/reports/gift-stock`, `/reports/discount-funding`, `/reports/shrinkage`, `/reports/brand-performance` | D | minor | • "(Rs)" headers.<br>• Store select. | S each |
| `/reports/staff` | D | minor | • A "Monthly targets" editor sits inside the report; move it to Store targets / People. | S |
| `/reports/day-summary` | D | minor | • Red "pick a store" plus a never-ending "Loading…". | S |
| `/reports/discounts` | D | minor | • Uppercase rust tile labels.<br>• Check that the ₹0 tiles are real zeros. | S |
| `/reports/store-dashboard` | D | merging → Home (Q8) | • Overlaps the Home dashboard for one store (Q8). | S |
| `/reports/stock`, `/reports/profit`, `/reports/daily`, `/reports/maker` | P | roadmap, off the menu (Q2) | • Planned; leave the menu (Q2). | — |

### Setup

| Route | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/setup/organisation` | S | matches | • Sub-nav plus table; add a split for a site. | S |
| `/setup/people-access` | S | re-layout | • Mockup `Setup`: the person in a split with a section × capability matrix (None / View / Operate / Approve / Manage) and their sites.<br>• Today the page has a left sub-nav (People, Roles, Workflow thresholds…) and a list only. | M |
| `/setup/products-parties` | S | re-layout | • Opening a style replaces the list.<br>• 5,500px of hash style codes.<br>• Open/Retire on every row. | M |
| `/setup/configuration` | S | matches | — | S |
| `/setup/products` Product lists | S | minor | — | S |
| `/setup/first-store` | F | minor | • Content narrower than the page (924px). | S |
| `/setup/monitoring` | D | minor | • Cramped 8-column table with every cell wrapping.<br>• Two red CTAs.<br>• Uppercase eyebrows. | S |
| `/setup/audit` | L | re-layout | • Renders every entry (143,000px).<br>• Needs paging, a sticky header and a split for before/after. | M |
| `/setup/feature-switches` | L | minor | • Long descriptions in table cells; move them to the split. | S |
| `/setup/tax-settings`, `/setup/checklists`, `/setup/consent-wording`, `/setup/document-numbering` | S / F | minor | • Uppercase labels where present. | S each |
| `/setup/stores`, `/setup/gstins` | L | retiring → views in `/setup/organisation` (C01) | • Inline edit with placeholder-only fields. | — |
| `/setup/brands`, `/setup/vendors`, `/setup/seasons` | L | retiring → views in `/setup/products-parties` (C01) | • "Brands" also collides with the Brands section. | — |
| `/setup/salesperson-matches` | L | retiring (C02 migration workbench) | — | — |
| `/setup/feature-check` | A | off the menu (Q2) | • A demo probe listed in the menu (Q2). | — |

### Sign-in, standalone and shared states

| Route / screen | Layout | Status | Main problems | Effort |
| --- | --- | --- | --- | --- |
| `/login` | A | minor | • Mockup `Login` adds a "This counter" panel (till, connection, bills held offline), Keep me signed in, Forgot it, and a theme switch.<br>• Demo chips need a dev-only label.<br>• The card is small at 1440. | S |
| `/signup` | A | minor | • Closed-signup state keeps the "Set up your company" title. | S |
| `/change-password` | A | minor | • Opened through a temporary-password session on the audit copy.<br>• Same two-panel layout as Login.<br>• Uppercase rust eyebrow "REPLACE YOUR PASSWORD".<br>• The mismatch error shows inline (good).<br>• Small card at 1440. | S |
| No-access page (shared) | — | minor | • Uppercase "ACCESS", no crumb.<br>• Replace with the shared no-access state. | S |
| Planned page (shared) | P | matches | — | — |
| Scan screen overlay (`ScanScreen`) | W | retiring with its callers | • Used only by the legacy count (`StockCount.tsx`), legacy transfers (`OutboundTransfers.tsx`) and legacy returns (`OutboundRTV.tsx`).<br>• Tried on the audit copy: "Open a count" refuses with "Legacy stocktake mutation uses the legacy stock contract and is disabled for this goods-v1 site", so the overlay cannot open on a goods-v1 store.<br>• If a goods-v1 flow needs scanning later, build it on the W layout with the shell kept. | — |
| Legacy redirect (`*`) | — | matches | — | — |

---

## 4. Proposed navigation and placement

Sidebar order follows how often a store person needs each section. Planned items leave the menu for `/roadmap`. Legacy routes stay reachable by bookmark until SO-07 and SO-08 retire them, because a hidden menu item does not retire code.

| Section | Items after the change | What moves, merges or goes |
| --- | --- | --- |
| **Today** | Dashboard · Action needed · Alerts · Today's checklist | — |
| **Sell** | Billing · Bills · Till & sync · Cash count · Gift vouchers | • Sell › Customers (bill search) merges into Bills as an "All stores" scope (Q1).<br>• Counter settings moves to Setup.<br>• Counter screens are hidden from logins that are not a counter. |
| **Customers** | Customers · Reservations · Special orders · Alterations | • Customer Orders folds into Customers (Q1). |
| **Receive Goods** | Pending · History · PT work · Labels | • Opening stock moves to Setup › First store setup.<br>• Legacy sync shows only on test-run tenants, under Setup (Q6).<br>• Goods arrived is the Pending page's CTA, not a menu item. |
| **Transfers** | Transfers · Requests | • Send Stock, Distribution, Stock Request, In-Transit and Earlier transfer records leave the menu.<br>• In transit is a chip on Transfers.<br>• "Split a delivery across stores" becomes an action on Transfers once C07 has a target for it (Q4). |
| **Stock** | Stock on hand · Movements · Stock counts (with Count schedule as a tab) · Ageing · Broken sizes · Size balancing · Items with no HSN | • Goods stock and Damage & Quarantine become views of Stock on hand (one page, `.seg` On hand / Availability / In transit / Quarantine).<br>• Movement History merges into Movements (Q3).<br>• The Stock Count section folds in as Stock counts (`/goods/counts`); its legacy items leave the menu (Q5).<br>• V-flip leaves the menu (C08). |
| **Return to Brand** | Returns | • New return is the page's CTA.<br>• Earlier damage records leaves the menu (temporary nav). |
| **Booking** | Bookings · Open-to-buy | • New booking is the page's CTA. |
| **Offers & Price** | Promotions · Running offers | • EOSS planning becomes a tab of Promotions, as in the mockup. |
| **Money** | Daily cash · Cash · Petty cash · Vendor ledger · Payables · Debit notes · IRN queue · Bank reconciliation · Partner dues · Store targets | • Partner billing (one setting) moves to Setup › Configuration.<br>• Payments, Collections, Expenses and Tally go to the roadmap. |
| **Brands** | Terms · Margin share · Claims · Reports · SOR ageing | — |
| **Reports** | Sales · Day summary · GST · Inventory · Brand performance · Staff performance · Shrinkage · Exceptions · Discounts · Discount funding · Gift stock | • Store dashboard becomes the Home dashboard's one-store view (Q8).<br>• Stock reports, Profit, Daily summary and Report maker go to the roadmap.<br>• The monthly-targets editor moves from Staff performance to Store targets. |
| **People** | Staff list | • Attendance, Member details and Payroll go to the roadmap until built (the mockup keeps Attendance as the next screen). |
| **Setup** | Organisation · People and access · Products and parties · Product lists · Configuration · Counter settings · Tax settings · Document numbering · Consent wording · Task checklists · Feature switches · Audit log · Monitoring and exports · First store setup | • Stores and GSTINs become Organisation views.<br>• Brands, Vendors and Seasons become Products and parties views (C01).<br>• Salesperson matches stays until C02 retires it.<br>• Feature check (demo) leaves the menu. |

**One navigation model for every role:**

- The sidebar always lists screens.
- In-page tab strips are removed where they repeat the sidebar: the Receive Goods strip, the Offers strip, the Inventory strip, the Transfers strip and the store persona's Sell strip.
- For the store persona, the sidebar opens with only its sections expanded (Q9).

---

## 5. Order of work

Each batch is one reviewable change, verified in the browser at 1440 / 1280 / 1920 and in dark mode. Each batch also runs:

- `npm test`, `tsc` and prettier;
- `scripts/check_so03_consolidation.py --check`, in any batch that touches routes, navigation or screens.

### Batch 0: shared components (S–M each, about 4–5 days)

1. Fix the flex-basis-as-height bug at the source:
   - Scope `.form-grid` sizing to row layouts only.
   - Make `.field > *` never inherit `flex-basis`.
   - Give `SearchBox` a row-only basis.

   This fixes Reservations, Special orders, Opening stock, Labels, New booking and Goods arrived.
2. Replace `PickerField` (search box + select) with `Combobox` everywhere.
3. Add a `SectionTitle` (sentence-case `h3`) and remove every uppercase eyebrow (187 uses in 59 files).
4. Rework the `StatTile` / `.stat-*` styles:
   - sans tabular numerals and Indian grouping;
   - compact currency with the exact value in a tooltip;
   - `—` with a reason for unknown;
   - a skeleton while loading.
5. Add shared state components: `LoadingRows`, `EmptyState`, `ErrorState` (maps server codes to plain words; raw text goes to the console), `NoAccess`, and `StoreGate` (inline store picker).
6. Buttons:
   - a lint check for at most one `.btn-cta` per page;
   - a neutral disabled CTA style;
   - mode switches become `.seg`.
7. Tables:
   - sticky header on `.kdps-table` everywhere;
   - `₹` in headers;
   - frozen first column for wide tables;
   - a `Pager` / "Show more" component.
8. Colour and layout clean-up:
   - a dark-mode variant of `KdpsLogo`;
   - remove the `Combobox.css` hex fallbacks;
   - `.page-pad` on Labels.

### Batch 0b: unknown never shown as zero (S each, about 1 day)

Fix these before any visual work, because each one is wrong today, not just untidy:

- **Reports:** Exceptions and Inventory show "—" when the reporting copy is missing.
- **Movement History:** shows "not tracked here" instead of 0 / ₹0 for stores on the goods journal.
- **Store Targets:** shows "not set" instead of "₹0 committed".
- **Owner dashboard:** confirm that "Owed to brands" and the Cash total are known zeros, not missing values.

### Batch 1: navigation and placement (M, about 2 days)

- Apply the §4 menu.
- Remove the tab strips that repeat the sidebar.
- Hide counter screens from non-counter logins.
- Move the planned items to the roadmap.
- Fix the role-dependent titles (`/transfer/requests`, `/stock/vflips`).
- No route is deleted. Retiring routes stay as bookmarks, and SO-03 runs with `--check`.

### Batch 2: list + detail pages (highest traffic first; about 12–15 days)

Bills (plus the merged bill search) → Action needed → Receive Pending with the delivery in the split → Transfers (table with a Next column; detail stays its own staged page) → Stock on hand (merged views) → Customers with the profile → Bookings → Returns → Promotions → Gift vouchers → Vendor ledger and Cash → IRN queue, Debit notes, Payables, Petty cash → Brand terms → People and access, Products and parties, Organisation → Audit log → Alerts, Checklist, Staff list.

### Batch 3: forms and wizards (about 8–10 days)

Reservations → Special orders → Alterations board → Goods arrived → Transfer requests → New booking → New return → Open-to-buy → Offer author → Opening stock (in Setup, Q6).

### Batch 4: dashboards and reports (about 6–8 days)

Home dashboard → Daily cash → the report template (KPI row, wide-table handling) applied to all 11 reports → Store targets → Monitoring → the one-store view of Home (Q8).

### Batch 5: workspaces (about 6–8 days)

Billing (use the empty scan area) → Till & sync → Cash count → Stock count session on `/goods/counts/:id` (mockup) → Labels → the scan screen on desktop.

### Batch 6: sign-in and standalone (S, about 1 day)

Login → Signup closed state → Change password → No-access page.

---

## 6. Decisions

All ten questions were answered on 29 October 2026; each took the recommended option. The menu map in §4 and the work order in §5 already assume these answers.

| # | Question | Decision |
| --- | --- | --- |
| 1a | Sell › Customers bill search | **Merge into Bills** as a This counter / All stores scope. The menu item goes, and its address redirects to Bills. |
| 1b | Customer Orders section | **Fold into Customers:** Customers, Reservations, Special orders, Alterations. |
| 2 | Planned and demo menu items | **Move to the roadmap page.** The 11 planned items and Feature check (demo) leave the menu. |
| 3 | Stock views | **One Stock on hand page** with On hand / Availability / In transit / Quarantine views. Movement History folds into Movements. |
| 4 | Legacy Transfers, Stock Count and V-flip items | **Off the menu now.** Their addresses keep working as bookmarks until SO-07 and SO-08 retire the code. Distribution returns later as an action on Transfers. |
| 5 | Count Schedule | **Keep it as a tab of Stock counts** (`/goods/counts`) and redesign it there. |
| 6 | Opening stock, Legacy sync, Partner billing | **Move all three to Setup.**<br>• Opening stock → First store setup.<br>• Legacy sync → Setup, shown only on test-run stores.<br>• Partner billing → Configuration. |
| 7 | Price Book | **Leave it hidden** and out of the redesign until SO-06 decides. |
| 8 | Store dashboard | **Make it Home's one-store view.** The Reports item goes, and its address redirects to Home. |
| 9 | Cashier navigation | **Same sidebar for every role.** It opens with Today, Sell and Customers expanded. The store persona's tab strip goes. |
| 10 | Dashboard placeholders | **Remove "Collections" and "Morning brief" until they are real.** The mockup's real cards take their place. |

Still to settle before implementation starts: your approval of the plan as a whole.

---

## Appendix: environment left behind by the audit

- **`kdps_rehearsal_audit`:** a Postgres copy of the rehearsal database with all store features on. It is used only for this audit and can be dropped:

  ```bash
  dropdb -h 127.0.0.1 -p 55432 kdps_rehearsal_audit
  ```

- **`kdps-api-audit`:** an entry in the git-ignored `.claude/launch.json`. It serves that copy on :8000 when the rehearsal API is stopped.
- **Rehearsal API:** `kdps-api-wardrobe-rehearsal` is running again, and its database was not written to.
- **`kdps-mockup`:** a git-ignored launch entry that serves the downloaded mockup, with the artifact's own runtime, on :5199. This is how the artboards were viewed rendered. Its files are in the session scratchpad only.
