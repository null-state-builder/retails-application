# Trade uniform boxes for panes and hierarchy

KDPS looks "AI-made" mainly because its tokens are applied uniformly, not because any single token is wrong. A warm paper background with a rust accent, 12px hairline cards around everything, one radius, pastel pills and a row of equal KPI tiles together match the "tasteful default" cluster that Anthropic's own design guidance says generated UI converges on. Large monitors and laptops expose the same problem in a second form: padding that grows with the viewport, cards nested inside padded pages, and five text sizes within 1.5px of each other spend space without adding information. The fix keeps IBM Plex, navy and the paper background, and changes structure. Collections fill the window while records and forms cap their width. Extra width becomes panes and columns, never bigger body text. Type moves to whole-pixel Carbon-style steps (12, 13, 14, 16, 20, 24, 32, 44) and gains hierarchy through weight and colour. Sections replace cards. Radius varies with hierarchy, status becomes a dot plus a word, and each colour gets one job. The till becomes a cart-dominant screen with a fixed payment rail, not a tile wall. Dashboards lead with one figure and a few ranked lists rather than equal tiles. One caveat applies throughout: every "AI tell" list is practitioner opinion and no controlled study exists. Many numeric thresholds below are therefore reasoned inferences from published design systems, and the report marks them as such.

**How to read this report.** A claim with a link is sourced from the research notes. Text marked **Inference** is reasoning that applies those sources to KDPS and is not a published rule. The rule tables at the end carry a *Basis* column with the same distinction.

## KDPS sits in the "tasteful default" cluster, not the purple-gradient one

The familiar first-order tells are purple gradients, Inter everywhere, three identical feature cards and fade-ups. KDPS avoids all of them, which is why its generic feel is harder to name. Anthropic's published `frontend-design` skill lists where generated design "clusters". First on its list is **a warm cream background (near #F4F1EA) with a terracotta or warm-clay accent (near #D97757)**. Its fourth cluster is the **"SaaS card kit"**: content chopped into identical rounded cards with **one border-radius on everything regardless of hierarchy** and the same soft shadow under each. Its fifth is template chrome: tracked all-caps eyebrows, meta strings joined with middle dots, monospace for small data labels, and "→" on buttons. The skill calls these "defaults rather than choices" that "appear regardless of subject" ([frontend-design skill](https://agenticskills.io/skills/frontend-design)). The same text names **"a big number with a small label, supporting stats"** as the default hero treatment, which is the KPI-tile pattern ([frontend-design skill](https://agenticskills.io/skills/frontend-design)). The community catalogue avoid-ai-design repeats the cream-plus-terracotta and card-kit entries. It adds "broadsheet cosplay" hairline rules and mono chrome, and it cautions that a finding does not prove AI authorship, because designers shipped rounded cards long before LLMs ([avoid-ai-design](https://github.com/funboy322/avoid-ai-design)). For dashboards, one practitioner guide calls the **equal three-KPI grid** a tell because "operators never read three KPIs equally". It also flags every metric in its own padded, rounded, shadowed card ([ux-skill wiki](https://github.com/Laith0003/ux-skill/wiki/How-to-design-a-dashboard-with-Claude-Code)).

**Inference:** KDPS overlaps cluster 1 (paper plus rust, minus the serif), cluster 4 (12px hairline cards everywhere) and the KPI default at the same time. That overlap, more than any colour value, explains the owner's "looks a lot AI" reaction. IBM Plex Sans appears on no tell list found; the lists name Inter, Roboto, Geist and Space Grotesk. Plex is not the problem, but using it neutrally at one weight is part of the sameness.

Crafted operational tools solve this by removing chrome rather than adding ornament. Linear's refresh cut its "quietly proliferated" separators and softened them until structure was **"felt not seen"**. It made the sidebar "a few notches dimmer" so content wins, shrank icons, and removed coloured icon backgrounds. It also warmed its greys while warning that **"go too warm… the interface risks looking muddy"** ([Linear refresh](https://linear.app/now/behind-the-latest-design-refresh)). An earlier Linear redesign generated whole themes from three inputs and limited blue in chrome for "a more neutral, timeless look" ([Linear redesign](https://linear.app/now/how-we-redesigned-the-linear-ui)). Vercel's guidelines add mechanical discipline: **nested radii are concentric and a child's is never larger than its parent's**, borders are tinted toward the background hue, **status is never colour alone**, and compared numbers use tabular figures ([Vercel Web Interface Guidelines](https://vercel.com/design/guidelines)). Anthropic's counter-principle is "visual structure is information": borders, eyebrows and dividers must encode something. It also says to "spend your boldness in one place" ([frontend-design skill](https://agenticskills.io/skills/frontend-design)).

Identity comes from the business, not from a look. Anthropic's skill says distinctive choices come from the subject's "industry, subject matter, materials, and vernacular" ([frontend-design skill](https://agenticskills.io/skills/frontend-design)). A real retail identity, NEW ØRD3R, built its whole system from the receipt, choosing a mono face because it recalled receipt type ([The Brand Identity](https://the-brandidentity.com/feed/d-b-take-inspiration-receipts-retail-identity-cycling-store-new-ord3r/)). A POS branding guide notes that marks must survive thermal printing, where soft detail "turns to mud" ([DevDiggers](https://docs.devdiggers.com/multipos-point-of-sale-for-woocommerce/tutorials/how-to-brand-the-pos-terminal/)). **Inference:** KDPS's signature artifact is the bill. Render the bill and the return preview as an actual KDPS bill: narrow column, perforated rule, store and GSTIN header, right-aligned tabular amounts. Confine that motif there so it carries meaning, and keep mono out of general chrome, since mono labels are themselves a listed tell. No case study was found of an internal POS deliberately branded around its retailer's artifacts, so this direction is untested.

## Five text sizes within 1.5px read as noise, not hierarchy

Every published product system checked converges on **14px body on a 20px line**: Carbon body-01 14/20, Fluent Body 1 14/20, Atlassian body 14/20 and Primer body-medium at 14 ([Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/); [Fluent 2](https://fluent2.microsoft.design/typography); [Atlassian](https://atlassian.design/foundations/typography); [Primer](https://primer.style/product/primitives/typography/)). Secondary text sits at **12/16** in Carbon, Fluent and Atlassian. Shopify Polaris is the densest, with **13px body** (from a search snippet of its token pages) ([Polaris text tokens](https://polaris-react.shopify.com/tokens/text)). Carbon's scale is the most relevant because it is **"calibrated for IBM Plex"**. It runs in whole pixels: 12, 14, 16, 18, 20, 24, 28, 32, 36, 42 ([Carbon typography overview](https://carbondesignsystem.com/elements/typography/overview/)). KDPS's 12.5 / 13 / 13.5 / 14 cluster has no counterpart in any of these systems. **Inference:** four sizes so close together cannot be told apart, so they add visual noise without adding hierarchy, and the fractional values are themselves a sign that nobody chose them. KDPS's 21px line height (1.5) is also looser than the 20px (1.43) every system uses for UI text. Primer reserves 1.5 for paragraphs, and moving to 20 fits more rows on a laptop.

Reference systems build hierarchy with weight and colour more than with size. Carbon's section heading heading-01 is **the same 14px as body, set at 600**. Its larger headings get *lighter* (20–32px at 400, 42px and above at 300). Carbon also recommends **Plex Light, Regular and SemiBold**, with Bold absent from its productive set ([Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/); [Carbon overview](https://carbondesignsystem.com/elements/typography/overview/)). Fluent raises emphasis by weight at a fixed size ("Strong", "Stronger") ([Fluent 2](https://fluent2.microsoft.design/typography)). Atlassian keeps separate **metric tokens for numbers: 28/32, 24/28 and 16/20 bold** ([Atlassian](https://atlassian.design/foundations/typography)). Carbon tracks Plex open at small sizes, **+0.32px at 12px and +0.16px at 14px** ([Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/)). **Inference:** KDPS's 24/700 title and 23/700 KPI are nearly the same style, so title and number compete. Drop the title to 20/600 and give numbers their own tabular ramp, and both read as intended. Weight 700 throughout is blunter than IBM's own use of Plex, and 600 is the house emphasis.

Do not scale text with the viewport. Carbon states that productive type "remains constant regardless of break point" and that fluid styles must not be used inside containers. Atlassian and Primer size in rem so that *users* control scale through zoom ([Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/); [Atlassian](https://atlassian.design/foundations/typography); [Primer foundations](https://primer.style/foundations/typography)). The 45–90 character measure applies to prose, not tables ([Butterick, line length](https://practicaltypography.com/line-length.html)), and Primer caps lines at about 80 characters ([Primer foundations](https://primer.style/foundations/typography)). **Inference:** a laptop is viewed closer than a 24–27" monitor, so a CSS pixel subtends roughly the same angle on both. The same type tokens should therefore serve 1280 and 2560. Layout changes with width; type changes only with a user-chosen density mode that sets the root size. Cap prose blocks (notes, help, confirmations, empty states) at 70–80ch, and never cap tables.

Large money figures are the one place to size by viewing distance. Ergonomics sources put legibility at characters subtending **20–22 arcminutes**, with 16–18 marginal ([KSU ergonomics, secondary](https://faculty.ksu.edu.sa/sites/default/files/4%20-%20Chapter%204%20-%20Vision%20-%20part%202%20%28AMS%20May01_24%29.pdf)). A rule of thumb is character height ≈ **0.007 × distance** ([PropertyCasualty360](https://www.propertycasualty360.com/amp/2003/05/06/qa-007-still-a-secret/)). A POS vendor claims cashiers sit at least **80cm** from the screen; that is not peer-reviewed and was seen only via a search summary ([UX Journal](https://medium.com/uxjournal/pos-ux-design-part-one-the-16-ux-factors-in-point-of-sale-b94661936eea)). **Inference, from the notes' calculation assuming about 0.27mm per CSS pixel and a Plex cap height of about 0.7em (unverified):** amounts the cashier confirms at about 60cm need 20–24px semibold. A customer reading across the counter at 1–1.5m needs 36–56px. The current 44px money-xl suits about 1.2m and should stay, but at weight 500–600, because heavy Plex clogs at display size. A customer-facing display needs about 56px. No POS-specific study of tender-amount size exists.

## Big monitors earn panes and columns, not wider cards or bigger text

The design systems agree on a split. **Reading and form content is capped; collection and workspace content fills the window.** Polaris's Page has a `fullWidth` variant that is literally `max-width: none` beside default and narrow variants ([Polaris Page.module.css](https://github.com/Shopify/polaris/blob/main/polaris-react/src/components/Page/Page.module.css)). A third-party stylesheet puts those variants at about 998px and 662px, unconfirmed against official docs ([Shopify CDN, third-party](https://cdn.shopify.com/extensions/e929dae6-783d-4517-b55b-4b1bf87a4cfe/highchem-app-7/assets/theme-extension.css)). Primer's PageLayout offers `full`, `medium`, `large` and `xlarge` containers ([Primer PageLayout](https://primer.style/product/components/page-layout/)), with xlarge reported as 1280px by secondary sources ([GeeksforGeeks](https://geeksforgeeks.org/primer-css-breakpoint-variables)). Shopify's admin guidance makes the split explicit. Use `base` width, which allows an aside, for record pages. Use `large` for dashboards and index pages with many columns. Settings pages use a narrow title column beside a wider form column ([Shopify.dev Layout](https://shopify.dev/docs/apps/design/layout)). Notion's per-page "Full width" toggle shows the same split from the user's side ([Super.so](https://super.so/blog/notion-full-width)).

Breakpoints cluster where multi-pane layouts begin. Material 3 reserves **Large (1200–1599)** and **Extra-large (≥1600, "desktop displays")** for multi-pane canonical layouts ([Android Developers](https://developer.android.com/develop/ui/compose/layouts/adaptive/use-window-size-classes)). Ant Design steps at **1200, 1600 and 1920** ([Ant Design Grid](https://ant.design/components/grid)). Fluent's top classes start at **1366 and 1920** ([Fluent 2 Layout](https://fluent2.microsoft.design/layout)). Carbon caps at **1584** with 16 columns from 1056 upward ([Carbon 2x Grid](https://carbondesignsystem.com/elements/2x-grid/overview/)). Primer lets the side pane be **resizable, with a default minimum of 256px and a persisted width** ([Primer PageLayout](https://primer.style/product/components/page-layout/)). NN/g recommends a **non-modal side panel** for editing a record while the table stays visible, because modals hide the neighbouring rows users reference ([NN/g Data Tables](https://www.nngroup.com/articles/data-tables/)). Odoo moves its chatter to the right on wide screens. A small ecosystem of add-ons exists only to resize, hide or reposition it, which suggests users want control over the side pane ([Odoo forum](https://www.odoo.com/forum/help-1/how-to-hide-or-re-position-the-chatter-pane-170829); [fullwidth_form_and_chatter](https://apps.odoo.com/apps/modules/17.0/fullwidth_form_and_chatter)). **Inference:** KDPS's 1100px drawer threshold should move to 1200, matching three systems. Its 360–400px aside should become resizable and persisted. Width beyond 1920 should buy a third pane, such as a facet rail or a record inspector, not wider cards.

Padding should stop growing with the viewport. **Inference (my arithmetic):** at 1440 with a 240px sidebar, content is 1200px wide. KDPS's 32px page padding plus 20px card padding consumes 104px of that (8.7%) before any data appears. A flat 24px page gutter with unboxed sections consumes 48px (4%). At 2560, the 40px tier only enlarges empty margins. Shopify asks for a **4px spacing grid** and warns **"don't change density within a single page"** ([Shopify.dev Layout](https://shopify.dev/docs/apps/design/layout)). Matt Ström-Awn defines density as **value divided by the time and space the interface occupies**. He notes that proximity groups items without boxes, and that responses under 100ms feel simultaneous, so speed is part of density ([UI Density](https://mattstromawn.com/writing/ui-density)). The only expert-density evidence is from pattern libraries and design systems, not controlled studies. It holds that repeat users chunk patterns and tolerate higher density ([Pattern Playground](https://pattern-playground.onrender.com/patterns/density); [BASIS](https://design.basis.com/foundations/density)). No NN/g or peer-reviewed study quantifying density against expert scanning speed was found.

Density modes are standard practice. Carbon ships data-table rows at **24/32/40/48/64px** with **16px horizontal cell padding** and at least 16px between columns ([Carbon Data table](https://carbondesignsystem.com/components/data-table/style/)). Salesforce Lightning offers Comfy and Compact. Compact moves labels beside fields and is claimed to give a **30% increase in information density**, with the figure seen via a search summary. Admins set the org default and users can override it ([Salesforce docs](https://developer.salesforce.com/docs/atlas.en-us.lightning.meta/lightning/data_service_display_density.htm); [Sigcorp](https://www.sigcorp.com/insights/salesforce-display-density-comfy-vs-compact/)). Gmail treats the chosen density as a floor: it goes denser on small windows but "never less dense than you requested" ([BetterCloud](https://www.bettercloud.com/monitor/the-academy/how-to-change-the-text-density-and-spacing-in-gmail/)). For tables, NN/g asks for a frozen human-readable first column and sticky headers. It wants column hide/show that does not depend on drag-and-drop and shows how many columns are hidden. It also suggests light borders, zebra striping or hover highlight across wide rows ([NN/g Data Tables](https://www.nngroup.com/articles/data-tables/)). Shopify puts row actions in the last cell and **no primary buttons in tables** ([Shopify.dev Layout](https://shopify.dev/docs/apps/design/layout)). Carbon advises adding columns, not widening them, when users "want to see more items" ([Carbon 2x Grid](https://carbondesignsystem.com/elements/2x-grid/overview/)). **Inference:** on 1920+ screens, KDPS stock and sales tables should reveal lower-priority columns rather than stretch the item name. Examples are MRP vs selling price, margin, last sold, stock at other stores, size-wise stock and ageing.

## The till should be a cart with a rail, not a tile wall

Billing is where KDPS should look least like a template and most like a shop. Vendors are converging on the same choices. In Shopify POS 11, the **cart stays on screen through checkout**, and cash/split numpads plus customer and staff attribution open in a **left side panel instead of a pop-up** ([Shopify changelog POS 11.0](https://changelog.shopify.com/posts/shopify-pos-version-11-0)). ERPNext's 2021 redesign found that the old item selector took **about 60% of the screen even during payment**. The new design shrinks the selector while lines are edited, hides it at payment, and aims for "quick transactions with almost no dialog or popups". It also auto-focuses the next required field and optimises for keyboard as well as touch ([Frappe blog](https://frappe.io/blog/accounting/point-of-sale-erpnext-makeover)). Tablet-first tiles fail on PCs. Shopify merchants with 35-item carts complain that large buttons "take up over half the screen at check out", and requests to shrink tiles run from 2023 into 2026 ([Shopify Community](https://community.shopify.com/t/feature-request-larger-check-out-screen-smaller-buttons/194474)). Lightspeed warns that large Quick Key sets scroll and slow the sell screen ([Lightspeed X-Series](https://x-series-support.lightspeedhq.com/hc/en-us/articles/25533825756443-How-many-keys-can-I-have-within-a-Quick-Key-layout)).

Keyboard and scanner speed come from focus management, not big targets. Lightspeed R-Series maps **Alt+I to the scan/search box, Alt+C to customer and Alt+D to payments**. On the payment screen, Alt+C focuses the cash amount, Alt+S finishes the sale and Alt+B goes back to items ([Lightspeed R-Series shortcuts](https://retail-support.lightspeedhq.com/hc/en-us/articles/228839547-Keyboard-shortcuts)). POS Next makes **Enter on an empty keypad pay the exact amount in cash** ([POSNext releases](https://github.com/BrainWise-DEV/POSNext/releases)). Indian retail treats the salesperson as a first-class billing field. Gofrugal selects the salesman by name, code or **barcode scan**, with per-sale commission ([Gofrugal](https://www.gofrugal.com/retail/billing-software/)), and ERPNext users ask for an Alt+S salesperson shortcut ([Frappe forum](https://discuss.frappe.io/t/add-shortcut-focus-keys-on-pos-awesome-app-for-erpnext/111586)). Gofrugal switches to a **full GST tax invoice only when a GSTIN is entered** and uses a simple bill for walk-ins ([Gofrugal](https://www.gofrugal.com/retail/billing-software/)). Under Rule 46, B2C invoices **below ₹50,000 need buyer name and address only on request** ([ClearTax](https://cleartax.in/s/gst-invoice-details)). Ginesys bills alterations as non-inventory service items with a service HSN ([Ginesys](https://ginesys.atlassian.net/wiki/spaces/PUB/pages/3539402757/How+To+Make+Service+Bill+at+POS+end)). WCAG 2.2 sets **24×24px** as the minimum pointer target and advises the larger enhanced size for important controls ([W3C SC 2.5.8](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)).

**Inference:** for a mouse-and-scanner apparel counter, the till should be a cart-dominant screen. Bill lines take the flexible width. A fixed right rail holds, top to bottom: customer, salesperson (scannable), a collapsed "Tax invoice / GSTIN" row that expands when a GSTIN is typed or the total reaches ₹50,000, totals, then tenders. Payment happens in the rail, never in a modal, and the cart stays visible. Focus returns to the scan box after every action. The last-scanned line is highlighted so the cashier can confirm it at arm's length. No vendor publishes cart-to-rail proportions or a "To pay" size; the only quantified figure is ERPNext's 60% anti-pattern. The widths below are therefore design judgement. This is also the one screen where KDPS should spend its boldness: the receipt-like bill preview and a large, calm, tabular To pay figure.

## Dashboards should lead with one number and ranked lists

Shopify POS's daily sales view is the best concrete model found. It shows three figures (net sales, average order value, items per order), each **compared with the same weekday last week**, and each opening a detail view. Below them it lists the **top 3 products, top 3 product types and top 3 staff**, and net payments by tender, over Today/Yesterday/WTD/MTD/QTD/YTD ranges. Staff sales sit in a separate sortable table ([Shopify Help: Analytics on POS](https://help.shopify.com/en/manual/sell-in-person/shopify-pos/analytics-on-pos)). NN/g frames dashboards as **single-screen, at-a-glance** views, "not to facilitate exploration". It favours bars and lines, bullet charts over gauges, and no donuts or treemaps. It groups with proximity, borders and shape, with colour secondary ([NN/g dashboards](https://www.nngroup.com/articles/dashboards-preattentive/)). Geckoboard warns that competing colourful elements dilute "importance of everything" ([Geckoboard](https://www.geckoboard.com/blog/design-approach-the-question-of-colour/)). Carbon recommends fluid grids for dashboards ([Carbon 2x Grid](https://carbondesignsystem.com/elements/2x-grid/overview/)). Lightspeed's users complain about shallow reports and paid add-ons, not chart styling ([Capterra](https://www.capterra.com/p/120491/Lightspeed-Retail/reviews/)).

**Inference:** KDPS should replace equal bordered KPI tiles with one lead figure, today's net sales, set large in tabular Plex with its same-weekday comparison written as a sentence. Two or three secondary figures follow in an unboxed strip separated by hairlines. Ranked short lists for salesperson, style/category and tender mix come next, each linking to a full table. Salesperson ranking matters more here than in Shopify's model because Indian apparel stores pay commission. At 1920+, lay this out on a 12-column grid as 8+4 or 6+3+3, with exception lists (low stock, pending returns, unsynced bills) beside the figures. Do not stretch two charts across 1,200px each. No source quantifies the best KPI count per row or the width past which charts stop gaining, so the ~900px chart cap below is judgement.

## Conclusion

The research changes the question. "Make it look less AI" and "use big screens better" are one problem: KDPS applies a single treatment everywhere, and that treatment is both generic and space-hungry. The same uniform card that reads as template chrome also double-pads a 1440 laptop and stretches emptily on a 2560 monitor. Because the cause is structural, the palette can mostly stay. Hierarchy has to become explicit in five places: type steps, radius, colour roles, pane allocation by width, and a single lead number per screen. The strongest evidence comes from design systems (Carbon, Fluent, Atlassian, Primer, Shopify) and from vendor changelogs. The weakest is the "AI tell" literature itself, which is opinion, and the absence of any cashier-specific study on type size or layout. The KDPS team should therefore validate the till and the compact table mode with real cashiers and managers rather than treat these numbers as settled.

Two decisions belong to the product owner rather than to research. The first is whether red stays as the CTA colour, which conflicts with red's universal meaning of destructive or error. The second is whether rust survives at all. The recommendation is to give each of navy, red and rust exactly one job. The warm paper is closest to the "cream and terracotta" tell, so it should shift slightly toward neutral (Linear's warning about "muddy" warmth applies) rather than be dropped.

## Rules for the KDPS design system

These are the concrete rules. *Basis* says whether a rule comes from a cited source or is an inference for KDPS.

### Type scale tokens

| Token | Now | Proposed size/line | Weight, tracking | Use | Basis |
|---|---|---|---|---|---|
| `text-meta` | caption 12.5 | **12/16** | 400, +0.32px, muted ink (≥4.5:1) | timestamps, helper, secondary cells | Sourced: Carbon label-01, Fluent Caption 1, Atlassian body.small all 12/16 |
| `text-label` | 13 | **12/16** | 500, +0.32px | field labels, column headers | Size sourced (Carbon); weight is inference (Atlassian uses Medium for components) |
| `text-table-compact` | 13.5 | **13/20** | 400, +0.16–0.24px | table cells in compact mode only | Sourced: Polaris body 13 (snippet); tracking is inference |
| `text-body` | 14/21 | **14/20** | 400, +0.16px | body, default table cells, controls | Sourced: Carbon, Fluent, Atlassian, Primer |
| `text-heading` | — | **16/24** | 600 | section and aside headings | Sourced range (Fluent Subtitle 2 16/22 semibold); choice is inference |
| `text-title` | 24/700 | **20/28** | 600 | page title | Inference; Carbon heading-03 20/28, Carbon uses no 700 |
| `metric` | KPI 23/700 | **24/28** | 600, tabular | secondary KPIs | Sourced anchor: Atlassian metric.medium 24/28 |
| `metric-lead` | — | **32/40** | 600, tabular | the one lead figure per dashboard | Inference; Carbon step 32/40 |
| `money-confirm` | — | **20–24/28** | 600, tabular | line totals and subtotal the cashier confirms | Inference from viewing-distance calculation |
| `money-xl` | 44 | **44/48** | 500–600, tabular | To pay | Inference: keep size, lighter weight per Carbon's use of Plex |
| `money-customer` | — | **56/64** | 500, tabular | customer-facing display | Inference (1.5m+ viewing) |
| `mono-id` | Plex Mono | **12/16 or 13/20** | 400, +0.32px | SKU, barcode, bill and GRN numbers only | Sourced: Carbon code-01 12/16; restriction to IDs is inference (mono chrome is a listed tell) |

Use whole pixels only and delete 12.5 and 13.5. Do not use weight 700 anywhere. Apply `font-variant-numeric: tabular-nums` to every money and quantity, with amounts in Plex Sans, not Mono (Vercel sourced; Plex Sans `tnum` support **unverified**, so check in devtools). Do not apply viewport-based `clamp()` to UI text; the only exception is `money-customer`, with a ceiling (Carbon sourced). Text never goes below 12px.

### Spacing and padding

| Rule | Basis |
|---|---|
| 4px base grid; spacing steps 4, 8, 12, 16, 24, 32, 48 | Sourced: Shopify 4px grid. Steps are inference |
| Page gutter **24px at every desktop width** (16 in compact mode). Retire the 32 and 40 tiers; extra width goes to content | Inference (padding growth wastes 2560 width; arithmetic above) |
| Page sections are unboxed: heading plus content, separated by 32px or one softened rule | Inference from Linear's "felt not seen" and the Anthropic/ux-skill card-kit critiques |
| Remaining containers use 16px padding (desk/compact) or 20px (counter); never a padded card inside a padded card | Inference |
| One density per page | Sourced: Shopify.dev |

### Grid and breakpoints

| Name | Range (CSS px) | Basis |
|---|---|---|
| `narrow` | < 1200 | Sourced convergence: M3 Large 1200, Ant xl 1200 |
| `laptop` | 1200–1599 | as above |
| `wide` | 1600–1919 | Sourced: M3 Extra-large 1600, Ant xxl 1600 |
| `xwide` | 1920–2399 | Sourced: Ant xxxl 1920, Fluent xxx-large 1920 |
| `ultra` | ≥ 2400 | Inference |

Use a 12-column fluid grid with a 24px gutter (16 in compact) for dashboards and record layouts (Sourced: Fluent 12 columns, Carbon fluid grids for dashboards). Use `repeat(auto-fill, minmax(N, 1fr))` for tile-like content so that wide screens show more items, not bigger ones (Sourced: Carbon fixed grid for "see more items").

### Cap versus fill

| Content | Width rule | Basis |
|---|---|---|
| Collections: stock on hand, bills, GRNs, transfers, returns, customers, bookings, reports | **Fill** (`max-width: none`) | Sourced: Polaris fullWidth, Shopify `large` for index pages |
| Single record or form (customer edit, GRN header) | **Cap at 720px single column**. At `wide`+, add a second column of related info (history, stock, audit), up to 1280 total | Inference from Polaris narrow/default and Primer xl 1280 (secondary values) |
| Settings | Narrow title/description column beside a form column, capped at 960 | Sourced pattern: Shopify settings layout. Number is inference |
| Prose (notes, help, empty states, confirmations) | `max-width: 72ch` | Sourced: Butterick 45–90, Primer ~80 |
| Dashboard charts | Single chart ≤ ~900px wide; add panels instead of widening | Inference |
| Table text column | `minmax(200px, 1fr)` capped ~480px; past the cap, reveal columns | Inference |

### Pane plan per width

| Width | Sidebar | Main | Record pane | Extra |
|---|---|---|---|---|
| `narrow` < 1200 | 64px rail | list full width | overlay drawer, 400px | — |
| `laptop` 1200–1599 | 240px; **rails to 64 when a record pane is open** | fluid list, min 560 | docked, **360px default, resizable 320–440, width persisted** | P1 columns only |
| `wide` 1600–1919 | 240px | fluid list | 400–440px | P1+P2 columns; forms gain second column |
| `xwide` 1920–2399 | 240px | fluid list | 440px | optional 240–280px facet/filter rail, or record inspector; P3 columns |
| `ultra` ≥ 2400 | 240px | fluid list | cap 480px | list keeps the rest, or two records side by side for compare |

Basis: thresholds are sourced from M3, Ant and Fluent. The non-modal side pane is NN/g. Resizable and persisted panes are Primer. Pixel widths and the auto-rail are inference. The arithmetic at 1280 is 1280 − 64 − 360 − 48 = **808px of list**, against 632px if the sidebar stays expanded.

### Density modes

| Mode | Default for | Controls | Table row | Table text | Labels | Basis |
|---|---|---|---|---|---|---|
| Counter | till role | 44px primary/tenders, 36px secondary | 48px | 14/20 | above fields | Row size sourced (Carbon lg). Role mapping inference |
| Desk | managers | 36px | 40px | 14/20 | above fields | Carbon md 40 sourced |
| Compact | back office (opt-in) | 32px | 32px | 13/20 | beside fields at `wide`+ | Carbon sm 32, Salesforce Compact labels-beside, sourced |

The role sets the default and the user can override it (Sourced: Salesforce). The chosen mode is a floor: below 1366px, step one notch denser automatically, never looser (Sourced pattern: Gmail; threshold is inference). The floor for any control is 24×24 (Sourced: WCAG 2.5.8).

### Tables

| Rule | Basis |
|---|---|
| Sticky header; frozen first column holding a human-readable identifier (style code + name, bill no.), with a subtle shadow when scrolled | Sourced: NN/g |
| Cell padding 16px horizontal (12 in compact); ≥16px between columns | Sourced: Carbon (compact value is inference) |
| Numbers right-aligned, tabular, fixed width; ₹ prefix; Indian grouping via `Intl.NumberFormat('en-IN')` (1,23,456) | Tabular and alignment sourced (Vercel). en-IN grouping is inference, not verified in the notes |
| Column priorities: P1 always, P2 at ≥1600, P3 at ≥1920; a "Columns (3 hidden)" menu that works without drag-and-drop | Hide/show sourced (NN/g). Priority tiers are inference |
| Row actions in the last cell as a tertiary or icon button or a single menu; no primary buttons inside tables; primary actions live in the page header | Sourced: Shopify.dev |
| Hover row highlight; light row rules or zebra, not both plus borders | Sourced: NN/g |
| Edit in the docked record pane, not a modal | Sourced: NN/g |
| Design empty, sparse, dense and error states | Sourced: Vercel |

### KPI and dashboard layout

| Rule | Basis |
|---|---|
| One lead figure (`metric-lead`, e.g. today's net sales) with its same-weekday-last-week comparison written as a sentence | Comparison sourced (Shopify POS). Single lead is inference from ux-skill's "operators never read three KPIs equally" |
| Secondary figures (AOV, items per bill, bills count) in one unboxed strip: label above number, hairline dividers, no card per metric | Inference from card-kit critiques and NN/g proximity grouping |
| Ranked top-3 to top-5 lists: salesperson, style/category, tender mix, each linking to a full sortable table | Sourced: Shopify POS analytics. Salesperson emphasis is inference |
| Bars and lines only; bullet charts for target vs actual; no gauges, donuts or treemaps | Sourced: NN/g |
| Single screen at the target width; at `xwide`, 12-column spans 8+4 or 6+3+3 with an exceptions list (low stock, pending returns, unsynced bills) | Single screen sourced (NN/g). Spans are inference |
| Colour only for exceptions, never to decorate tiles | Sourced: Geckoboard, Carbon "colour is not for decoration" |

### Billing screen layout

| Rule | Basis |
|---|---|
| Cart-dominant: bill lines fill the flexible width; no product-tile grid as the primary element | Sourced direction: Shopify 11, ERPNext, merchant complaints. Apparel/scanner fit is inference |
| Fixed right rail: **380px at `laptop`, 440px at `wide`+**, holding customer → salesperson (scannable) → collapsed "Tax invoice / GSTIN" → totals → tenders → Take payment | Rail contents sourced (Shopify panel, Gofrugal salesman scan). Widths are inference |
| Payment happens in the rail; cart stays visible; no payment modal; the item search hides or shrinks at payment | Sourced: Shopify 11, ERPNext |
| GSTIN block expands when a GSTIN is entered or the bill reaches ₹50,000 | Sourced behaviour (Gofrugal) and threshold (Rule 46 via ClearTax). Trigger design is inference |
| Focus returns to the scan box after every action; Alt-key jumps (scan, customer, salesperson, payment, cash amount, finish, back); Enter on an empty cash field pays exact | Sourced: Lightspeed R-Series, POS Next. KDPS key map is inference |
| Last-scanned line highlighted; line total and subtotal in `money-confirm`; To pay in `money-xl` | Inference from viewing distance |
| Line columns at `laptop`: #, item (style + name + size/colour), qty, MRP, discount, amount. At `xwide`, add per-line salesperson, stock left and discount reason | Inference |
| Service lines (alterations) as non-stock items with a service HSN | Sourced pattern: Ginesys |
| The bill preview is the one place for the receipt motif (perforated rule, store/GSTIN header); thermal-safe mark | Sourced inspiration (NEW ØRD3R, DevDiggers). Application is inference |
| Primary tender and Take payment at 44px; everything else 36px | WCAG sourced. Sizes are inference |

### Colour, chips, radius, chrome

| Rule | Basis |
|---|---|
| **Navy** = system colour: primary actions, selection, focus, links | Inference from Linear's colour restraint and Mania's "colour by function" |
| **Red** = destructive and error only. If the brand insists on red CTAs, reserve red fill for the single money-committing action per screen (Take payment, Post GRN) and show destructive actions as red text plus icon | Inference; a product decision |
| **Rust** gets one named job (brand mark and bill artifact) or is retired from UI chrome | Inference: rust on paper is Anthropic's cluster 1 |
| Shift the paper background a few steps toward neutral; tint borders and shadows toward the paper hue | Sourced: Linear "too warm… muddy", Vercel tinted borders. Amount is inference |
| Status = 6–8px dot or glyph + plain word; no pastel pills in tables. A filled chip only for a state that needs action (e.g. "Sync failed"); never gradient chips | Sourced: Vercel "never colour alone", ux-skill "gradient pill". Dot pattern is inference |
| Radius by hierarchy: **0** table rows and in-section dividers; **4** inputs, buttons, chips; **8** surviving containers, popovers, bill preview; **12** modals and drawers only; children concentric and never larger than parents | Concentric rule sourced (Vercel). Values are inference |
| No shadow on in-flow content; shadow only on floating layers | Inference from the "same soft shadow under each" tell |
| Sidebar a few notches lower in contrast than content; fewer and smaller icons; no tinted icon squares | Sourced: Linear refresh |
| Motion only in answer to actions (line added, bill committed); no section fade-ups or hover lift on cards; aim for <100ms list/pane switches | Sourced: Anthropic skill, Ström |
| Align labels, icons and buttons to shared lines; audit at 1280 and 1920 | Sourced: Linear, Vercel |

### Copy rules

| Rule | Basis |
|---|---|
| Buttons name the action and the object ("Take payment", "Print bill", "Post GRN", "Save return"); never "Submit", "Continue", "Get started" | Sourced: Anthropic skill, Vercel |
| The result reuses the verb ("Bill printed", "GRN posted") | Sourced: Anthropic skill ("Publish" → "Published") |
| Errors say what happened and how to get out, without apology: "Barcode 8901… isn't in this store's stock. Check the size, or search by style code." | Principle sourced (Anthropic, Vercel). Example is inference |
| Empty states invite the next action: "No bills yet today. Scan an item to start one." | Sourced principle (Anthropic) |
| No greeting hero; open with store state in a sentence ("Ratu Road has 14 bills today, ₹42,380 net") | Inference (the greeting-as-tell claim was unsourced) |
| No tracked all-caps eyebrows, no "A · B · C" meta strings, no "WORD — fragment" labels, no "→" on buttons, no 01/02/03 on non-sequences, no label that restates the heading | Sourced: Anthropic skill, avoid-ai-design |
| Use shop vocabulary (bill, MRP, size run, alteration, exchange, GRN, transfer), not "items", "records" or "entities" | Sourced principle (Anthropic "vernacular"). Word list is inference |
| Numerals for counts; ₹ always; real ellipsis "…" on loading and on menu items that open more; non-breaking space between number and unit | Sourced: Vercel |
| Sentence case everywhere, consistently (Vercel uses Title Case; either is fine if applied without exception) | Sourced conflict, resolved by consistency |
| No marketing verbs ("Seamless", "Powerful", "Elevate", "Unlock") | Sourced: avoid-ai-design |

**Open items to verify before freezing tokens:** whether IBM Plex Sans as loaded ships `tnum` figures. Measure Polaris and Primer pixel widths, which are secondary sources here. Confirm the en-IN grouping output in the target browsers. Run a short timed test of the till rail and the compact table mode with KDPS staff, because no published study covers cashier type sizes or layouts.
