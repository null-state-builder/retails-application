# Layout, grids and space use for data-heavy desktop web apps (1280–2560px)

Scope: KDPS store-operations React app (cashiers and managers, mouse, laptops 1280–1440 and monitors 1920–2560). Current system: left sidebar (64px rail during billing), top bar, page padding 24/32/40, cards with 20px padding and 12px radius, split view (list + 360–400px aside, drawer below 1100px), controls 36px (desk) / 44px (counter).

Research note: about 25 searches and fetches. Several primary docs (m3.material.io, polaris-react.shopify.com, atlassian.design grid, Salesforce blog) could not be fetched: they returned 404 or 403, closed the socket, or returned an empty page. Where a number comes from a secondary source, it is marked as such.

## Max-width vs fluid: how major products and design systems handle 1440 → 2560px

### Takeaway
Design systems agree on a split. Reading and form content (prose, settings, single-record forms) is capped at roughly 660–1280px. Collection and workspace content (tables, boards, canvases, list–detail) fills the width. Each system supplies a "full width" escape hatch for that second kind. Carbon caps its grid at the 1584px "Max" breakpoint. GitHub/Primer defaults to an xlarge container (1280px per secondary sources). Polaris has default, narrow and full-width page variants, and full width is `max-width: none`.

### Cited Findings
- **Carbon 2x grid breakpoints:** Small 320px (4 cols, margin 0), Medium 672px (8 cols, 16px margin), Large 1056px (16 cols, 16px margin), X-Large 1312px (16 cols, 16px margin), Max 1584px (16 cols, 24px margin). Column count stays at 16 from Large upward. The total gutter is 32px (16px padding on each side of a box). Carbon defines no container max width; the 1584 "Max" breakpoint is the top step. — [Carbon 2x Grid overview](https://carbondesignsystem.com/elements/2x-grid/overview/)
- **Carbon fluid vs fixed vs hybrid grids:** fluid grids (fixed column count, columns scale) suit "editorial content, dashboards, images, video, and data visualizations", where scaling each item matters more than showing more items. Fixed grids, where column count grows with width, suit cases where "users want to see more items". Hybrid grids fix one dimension. A gutterless grid is suggested for closely related content. — [Carbon 2x Grid overview](https://carbondesignsystem.com/elements/2x-grid/overview/)
- **Polaris Page (Shopify Admin):** three variants. `fullWidth` sets `max-width: none`. `narrowWidth` uses `max-width: var(--pg-layout-width-primary-max)`. The default applies a `page-layout` mixin. — [Polaris Page.module.css on GitHub](https://github.com/Shopify/polaris/blob/main/polaris-react/src/components/Page/Page.module.css)
- **Polaris pixel values (secondary source):** a Shopify app stylesheet that overrides Polaris sets the default page at 998px (62.375rem) and narrow at 662px (41.375rem). These values could not be confirmed against official Polaris docs. — [Shopify CDN theme-extension.css (third-party)](https://cdn.shopify.com/extensions/e929dae6-783d-4517-b55b-4b1bf87a4cfe/highchem-app-7/assets/theme-extension.css)
- **GitHub Primer PageLayout:** `containerWidth` can be `full`, `medium`, `large` or `xlarge` (default `xlarge`). The pane is `small`/`medium`/`large` or a custom `{min,max,default}`, and can be `resizable` with a default `minWidth` of 256. Padding modes are `none`, `condensed` and `normal`. Below 768px the pane can become fullscreen. The page gives no pixel values for the keywords. — [Primer PageLayout](https://primer.style/product/components/page-layout/)
- **Primer pixel values (secondary sources):** breakpoints sm 544 / md 768 / lg 1012 / xl 1280. Container max-widths are .container-md 768px, .container-lg 1012px and .container-xl 1280px. — [GeeksforGeeks on Primer breakpoints](https://geeksforgeeks.org/primer-css-breakpoint-variables); [Syracuse design system containers](https://designsystem.syr.edu/documentation/utilities/containers)
- **Notion:** pages have a default width that leaves "plenty of white space on the right and left sides", plus a per-page "Full width" toggle. There is no account-wide default, so each page must be switched by hand (or created from a template). — [Super.so on Notion full width](https://super.so/blog/notion-full-width); [templatesfornotion](https://templatesfornotion.com/post/notion-full-width-by-default)
- **Gmail:** the density setting acts as a floor. Gmail will "automatically resize to a higher density on smaller windows, but it will never be less dense than you requested". Gmail is otherwise a fluid list app. — [BetterCloud on Gmail density](https://www.bettercloud.com/monitor/the-academy/how-to-change-the-text-density-and-spacing-in-gmail/)

### Inferences
- The products the owner admires (Linear, Jira, Gmail, Figma, Vercel's deployment lists) fill the window with their collection views. They cap only document-like or settings pages. A KDPS rule that follows this: **collections fill, records and forms cap.** Stock on hand, sales lists, bookings, returns, customers and reports should be fluid. Settings, a single customer edit form and the GRN header form should sit in about 720–960px columns. On big monitors those columns should get a second column (related info), not wider fields.
- Polaris (998 default / 662 narrow, unconfirmed) and Primer (1280 xl) show the common cap values. A 1280px cap for "page" content and ~660–720px for single-column forms are defensible, well-trodden numbers.
- Carbon keeps 16 columns from 1056 upward, so grid columns get wider rather than more numerous. Its own guidance says that is right for dashboards, but for "see more items" (product tiles, size grids) the grid should add columns instead. KDPS should use auto-fill grids (`repeat(auto-fill, minmax(Npx, 1fr))`) for tile-like content.

### Gaps
- Linear, Stripe Dashboard, Vercel, Jira and Figma do not publish layout max-width values. I found no primary source for their numbers and did not measure them, so none are given. Observing them (DevTools at 1440/1920/2560) would close this gap.
- I could not fetch official Polaris page docs (socket errors), so the 998/662 values remain secondary.
- I could not fetch the Atlassian grid page (empty content), so Atlassian breakpoints and max-widths are not covered.

## Multi-pane patterns and window-size classes

### Takeaway
Material 3 explicitly reserves its "large" (1200–1599dp) and "extra-large" (≥1600dp, "desktop displays") classes for multi-pane layouts. Fluent puts the top classes at 1366–1919 and ≥1920. Ant Design puts them at 1200, 1600 and 1920 (xxxl, added in v6.3). Across these systems the desktop thresholds converge on about **1200, about 1600 and about 1920**, which are natural points to add a second and third pane.

### Cited Findings
- **Material 3 / Android width classes:** Compact <600dp, Medium 600–839, Expanded 840–1199, **Large 1200–1599** ("large tablet displays"), **Extra-large ≥1600** ("desktop displays"). Large and extra-large "were added to target desktop and connected displays". Classes are for "high-level layout decisions, such as whether to use a specific canonical layout", not device checks. — [Android Developers: use window size classes](https://developer.android.com/develop/ui/compose/layouts/adaptive/use-window-size-classes)
- **M3 guidance (via search summary):** large and extra-large "offer multiple pane layouts starting at a screen width of 1200dp". — [Android/M3 window size classes (search summary)](https://developer.android.google.cn/develop/ui/compose/layouts/adaptive/window-size-classes?authuser=0)
- **Fluent 2 size classes:** small 320–479, medium 480–639, large 640–1023, x-large 1024–1365, **xx-large 1366–1919**, **xxx-large ≥1920**. Fluent suggests a 12-column grid (halves, thirds, fourths, sixths) with gutters in multiples of 4px that may change per breakpoint. It gives no pane guidance and no max widths. — [Fluent 2 Layout](https://fluent2.microsoft.design/layout)
- **Ant Design breakpoints:** xs <576, sm ≥576, md ≥768, lg ≥992, **xl ≥1200**, **xxl ≥1600**, **xxxl ≥1920 (since 6.3.0)**. — [Ant Design Grid](https://ant.design/components/grid)
- **Carbon:** Large 1056, X-Large 1312, Max 1584. — [Carbon 2x Grid](https://carbondesignsystem.com/elements/2x-grid/overview/)
- **Primer:** the side pane is resizable (min 256px default), and its width persists via `widthStorageKey`. Pane sizes are small/medium/large. — [Primer PageLayout](https://primer.style/product/components/page-layout/)
- **NN/g on editing within wide tables:** a "nonmodal side panel lets users edit a record while still viewing the table". Modal popups hide adjacent records, which users often reference. Accordions clutter the table. — [NN/g Data Tables](https://www.nngroup.com/articles/data-tables/)

### Inferences
Suggested pane ladder for KDPS. This is derived from the classes above and the current sidebar (240px expanded / 64px rail) and 360–400px aside; it is not a published rule.

| Viewport | Class (M3 / Fluent) | Layout |
|---|---|---|
| <1100 | Expanded / x-large | Sidebar can rail; record opens in drawer (current behavior) |
| 1100–1439 (laptops) | Large / xx-large | Sidebar + list + 360px docked aside; sidebar rails during billing |
| 1440–1919 | Large / xx-large | Sidebar + list + 400–440px aside; list gains secondary columns |
| ≥1920 | Extra-large / xxx-large | Third pane allowed: filters/facets rail (240–280px) on the left of the list, or an inspector/activity pane next to the record; tables show all optional columns |
| ≥2400 | — | Cap the aside (~480px) and give remaining width to the list; or allow two records side-by-side (compare) |

- The current 1100px drawer threshold sits between M3 Expanded (840) and Large (1200). That is reasonable, but aligning the docked aside to about 1200 would match three systems (M3, Ant xl, Fluent near 1024/1366).
- A resizable aside with a persisted width (as in Primer) is the cheapest way to let a manager on a 2560 monitor use space as they wish.

### Gaps
- I could not retrieve the m3.material.io canonical-layout pages (404) with recommended pane widths (for example, the M3 list-detail fixed 360dp first pane). The 360dp list-pane figure is from memory and not cited here.
- I found no published research on at what width users benefit from a third pane.

## Density modes (compact/comfortable)

### Takeaway
Gmail (Default/Comfortable/Compact), Salesforce Lightning (Comfy/Compact, with Cozy in the Aura docs), Carbon (five data-table row sizes from 24px to 64px) and Ant Design (table sizes large/middle/small plus a compact theme algorithm) all ship density options. Salesforce claims Compact gives "30% increase in information density" over Comfy. The change is mainly vertical spacing and label placement, not font size. I found no published usage-rate data.

### Cited Findings
- **Carbon data table row heights:** xs 24px, sm 32px, md 40px, **lg 48px**, xl 64px (only for two-line rows). Header row matches the row size. Cells have 16px left/right padding and a minimum 16px between columns. Toolbars are 24/32/48px. Pagination height matches the rows. — [Carbon Data table style](https://carbondesignsystem.com/components/data-table/style/)
- **Salesforce Lightning:** Comfy (default) puts labels on top of fields with more space between elements. Compact puts labels beside fields with tighter spacing. Cozy (Aura docs) is like compact with more line spacing. Admins set the org default (Setup > User Interface > Density), and users can switch in their profile menu. Compact is said to give a "30% increase in information density compared to the Comfy view" (Winter '19 announcement, via search summary; the blog itself returned 403). — [Salesforce: Changing the Display Density](https://developer.salesforce.com/docs/atlas.en-us.lightning.meta/lightning/data_service_display_density.htm); [Sigcorp: Comfy vs Compact](https://www.sigcorp.com/insights/salesforce-display-density-comfy-vs-compact/); [Salesforce Winter '19 density blog](https://developer.salesforce.com/blogs/2018/08/new-density-settings-for-the-lightning-experience-ui-in-winter-19)
- **Gmail:** three modes. Default adds space between messages and shows attachment chips. Comfortable adds space but shows attachments only as an icon. Compact removes the extra space. Sources disagree on which mode is the default, because labels changed across redesigns. — [GRCC support desk](https://supportdesk.grcc.edu/TDClient/53/Portal/KB/ArticleDet?ID=1065); [BetterCloud](https://www.bettercloud.com/monitor/the-academy/how-to-change-the-text-density-and-spacing-in-gmail/)
- **Ant Design table:** middle size uses 12px vertical / 8px horizontal cell padding. Small uses 8px. This comes from compiled CSS in third-party sites; I did not confirm the exact row heights. — [Apache ShenYu dashboard index.less](https://apache.googlesource.com/shenyu-dashboard/+/refs/tags/2.4.2/src/index.less)
- **Primer** offers `padding: none | condensed | normal` on PageLayout, a layout-level density switch. — [Primer PageLayout](https://primer.style/product/components/page-layout/)
- **Density as a concept:** Matt Ström defines UI density as "the value a user gets from the interface divided by the time and space the interface occupies". He separates visual, information (Tufte's data-ink), design, temporal (response speed: <100ms feels simultaneous) and value density. He argues that judging density only by how crowded a screen looks is a mistake. — [Matt Ström, "UI Density"](https://mattstromawn.com/writing/ui-density/)

### Inferences
- KDPS already has two densities by context (36px desk / 44px counter controls). A third "compact" table density, with rows at about 32px (Carbon sm) against a default of 40px (Carbon md), would match the systems above for managers reviewing stock and reports. The counter keeps 44px targets.
- Salesforce's model of an admin-set default plus a per-user override fits KDPS roles: the counter defaults to comfortable and back-office defaults to compact.
- Gmail's "density as a minimum, auto-denser on small windows" rule is a good pattern for 1280px laptops.
- Ström's temporal-density point matters as much as pixels. Instant list/aside switching (no spinners under 100ms) will make KDPS feel "dense" more than shrinking padding will.

### Gaps
- I found no published usage data on what share of Gmail, Jira or Salesforce users switch density. Jira and Atlassian density options were not verified (the Atlassian docs could not be fetched).
- I could not verify Ant Design's exact table row heights or its compact-algorithm values.

## Data tables on wide screens

### Takeaway
Use the extra width for more columns, not wider ones. Keep a human-readable identifier column frozen, keep headers sticky, and give users column show/hide that does not rely on drag-and-drop. Edit in a non-modal side panel so the table stays visible. Use row sizes of 32–48px, with 16px horizontal cell padding as the common baseline (Carbon).

### Cited Findings
- Freeze header rows and header columns when the table is larger than the screen, with a subtle shadow on frozen areas to keep spatial orientation. A frozen first column helps users track which record they're looking at while they scroll horizontally. — [NN/g Data Tables](https://www.nngroup.com/articles/data-tables/)
- Light borders, zebra striping and hover row-highlight help users keep their place across wide rows. — [NN/g Data Tables](https://www.nngroup.com/articles/data-tables/)
- Hiding and reordering columns should be low-effort and must not depend on drag-and-drop alone. Show the current state, such as how many columns are hidden. — [NN/g Data Tables](https://www.nngroup.com/articles/data-tables/)
- Order default columns by importance and keep related columns adjacent. The first column should be a human-readable identifier, not an auto-generated ID. Reduce the need to compare distant columns. — [NN/g Data Tables](https://www.nngroup.com/articles/data-tables/)
- For editing, a non-modal side panel beats modals (which hide neighboring records) and accordions (which clutter). In-place editing works only for narrow tables. — [NN/g Data Tables](https://www.nngroup.com/articles/data-tables/)
- **Carbon:** row sizes are 24/32/40/48/64, the 64px size only for two-line rows. Cells have 16px left/right padding and a minimum 16px between columns. Tables need 3+ columns. Column widths "vary by content". — [Carbon Data table style](https://carbondesignsystem.com/components/data-table/style/)

### Inferences
- **What to put in the extra width (KDPS):** on 1920+ screens, show optional columns that are hidden by default on laptops: MRP vs selling price, margin, last-sold date, stock at other stores, size-wise stock as inline mini-columns (S/M/L/XL), ageing, and supplier. Use column priorities (P1 always shown, P2 shown at ≥1440, P3 shown at ≥1920) plus a "Columns (3 hidden)" menu as NN/g recommends.
- Give numeric columns (₹ amounts, qty) fixed widths, right-alignment and tabular figures. Give the text column (item name) `minmax(200px, 1fr)` so it absorbs spare width up to a cap. Past that cap, add columns rather than stretching the name. (General practice; I did not source a specific number.)
- Put the record aside in the extra width instead of opening a modal. This is exactly NN/g's non-modal side-panel recommendation and KDPS's current split view.

### Gaps
- NN/g's table article gives no row-height or column-width numbers.
- I found no source quantifying the ideal maximum width of a text column in tables.

## Dashboards/KPI layouts that scale

### Takeaway
Grid choice is 12 columns (Fluent), 16 (Carbon) or 24 (Ant Design). Dashboards should be single-screen, at-a-glance views (NN/g). Group with proximity, borders and shape, not colour alone. Use bars/lines/bullet charts, not gauges, donuts or treemaps. Too many KPI cards dilutes attention ("card soup").

### Cited Findings
- **Grid column counts:** Ant Design 24 columns, with recommended gutters of `16 + 8n` px and responsive gutters such as `{xs:8, sm:16, md:24, lg:32}`. Carbon has 16 columns at ≥1056 with a 32px total gutter. Fluent suggests 12 columns with gutters in multiples of 4. — [Ant Design Grid](https://ant.design/components/grid); [Carbon 2x Grid](https://carbondesignsystem.com/elements/2x-grid/overview/); [Fluent 2 Layout](https://fluent2.microsoft.design/layout)
- **Carbon:** fluid grids (columns scale) are the recommended choice for dashboards and data visualizations. — [Carbon 2x Grid](https://carbondesignsystem.com/elements/2x-grid/overview/)
- **NN/g:** dashboards are single-page views to "absorb key information at a glance". Their "goal is not to facilitate exploration". Operational dashboards (time-sensitive, live data) differ from analytical ones (daily data, decisions); both need a single-screen layout. Use bar and line charts, use bullet charts instead of radial gauges (gauges "use space heavily"), and avoid pie/donut (except for large disparities), treemaps and 3D. Group with shape, borders and proximity; use colour only as a secondary cue. — [NN/g Dashboards: preattentive](https://www.nngroup.com/articles/dashboards-preattentive/)
- Colourful elements compete with each other, "resulting in dilution of importance of everything". — [Geckoboard](https://www.geckoboard.com/blog/design-approach-the-question-of-colour/)
- Too many KPI cards is a common mistake; focus on decision metrics. This is a vendor blog. — [UX Magic: dashboard UI design](https://uxmagic.ai/blog/dashboard-ui-design)
- Tufte's data-ink ratio (via Ström): aim near 1 by adding useful data or removing non-data ink. — [Matt Ström, "UI Density"](https://mattstromawn.com/writing/ui-density/)

### Inferences
- **Avoiding card soup in KDPS:** card borders, 20px padding and 12px radius on every tile are non-data ink that spends space and makes screens look like generic templates. Alternatives:
  - Put KPIs as **one unboxed KPI strip** (label above number, hairline dividers between, no card chrome).
  - Make **sections** (heading + content, separated by space or a rule) the default container.
  - Reserve cards for things that are individually actionable or movable.
  - This follows NN/g's "proximity and borders for grouping" and the data-ink principle.
- On 1920+ screens, a store dashboard should lay KPI strip + chart + exceptions list (low stock, pending returns, unsynced bills) side by side in 12-column spans such as 8+4 or 6+3+3. It should not stretch two charts to 1200px each. Charts gain little from extra width past about 800–900px; more panels gain more. (Inference; no source gives a number.)
- Use the till/billing screen as the "operational dashboard" (live, glanceable) and the reports screens as "analytical" ones. Each needs different density.

### Gaps
- I found no rigorous study comparing card-based and unboxed dashboard layouts on scanning speed.
- I found no source giving recommended KPI-tile counts per row by viewport width.

## Whitespace and information density evidence for expert/repeat users

### Takeaway
I found no NN/g article that directly quantifies density against scanning efficiency for experts, so this remains a gap. The available material argues that expert, repeat users handle and prefer higher density. It treats whitespace as a tool for grouping, not a default, and treats perceived density as partly a function of speed and arrangement.

### Cited Findings
- Experienced users "recognise domain patterns and automatically chunk information, enabling them to process higher density interfaces that would overwhelm novices" (pattern library, not peer-reviewed). — [Pattern Playground: density](https://pattern-playground.onrender.com/patterns/density)
- Density guidance in a design system: experts generally prefer higher density, and the right level depends on task complexity and frequency of use. — [BASIS design system: density](https://design.basis.com/foundations/density)
- "Dense layouts are better in many applications such as email and bank transactions", as a counterpoint to the decade-long "room to breathe" mantra. — [Envy Labs: When More is More](https://envylabs.com/insights/interface-information-density-best-practices)
- Whitespace still signals grouping, and for most UIs generous whitespace is usually better than too little. — [UX Collective: whitespace in UI design](https://uxdesign.cc/whitespace-in-ui-design-44e332c8e4a)
- Perceived density is unreliable: the same 500 dots look denser when arranged in neat rows. Temporal density (speed) is part of density, and actions under 100ms feel simultaneous. — [Matt Ström, "UI Density"](https://mattstromawn.com/writing/ui-density/)
- A 2005 mailing-list post reports non-information-bearing whitespace as inversely related to task success. The source is thin and dated. — [SIGIA-L archive](https://asist-archive.ischool.illinois.edu/sigia-l/2005-August/014198.html)

### Inferences
- For cashiers (repeat users, speed-critical) and managers (analytical), the evidence favours denser lists and tables, with whitespace used to separate groups rather than pad every element. KDPS's page padding of 40px at large sizes, and card padding of 20px nested inside it, likely double-pads. Fixed-plus-modest padding (for example 24px outer, sections not cards) would recover about 5–8% width at 1440 with no loss of grouping. (Inference.)
- "Looks generic" is largely a design-density issue (uniform cards and uniform spacing) rather than a pixel-count issue. Varying grouping (strips, rules, columns of differing weight) reads as more intentional.

### Gaps
- I did not retrieve the NN/g articles on whitespace, density or "dense vs. sparse" (search did not surface them), so I have no NN/g quotes on this question.
- I found no controlled study with numbers on row height against scanning speed for expert users in business software.
