# Retail POS and back-office/ERP screen layouts on desktop and large monitors

Research date: 2026-10-29. Scope: billing/checkout layouts, back-office list/detail screens, retail dashboards, POS usability evidence, Indian retail specifics. About 20 search/fetch calls. Several vendor help centres (Ginesys Confluence, Medium/UX Journal) were login-gated or returned 403, so some vendors are covered only thinly; see Gaps under each question.

## 1. Billing/checkout layouts in major POS products (cart vs product grid, totals, tenders, keyboard/scanner, 1920px use)

### Takeaway
The current direction across vendors is: the cart stays visible for the whole sale (including payment), payment/numpad/customer/staff attribution open in a side panel rather than modal pop-ups, and the product picker shrinks or disappears once the cashier is past item entry. Mainstream cloud POS products (Shopify, Square, Lightspeed X) are still tablet-first, and merchants complain that on a PC screen big tiles waste space and shrink the cart. Keyboard-first behaviour (Alt-key focus jumps, F-keys, Enter-to-pay-exact) appears mainly in older desktop POS (Lightspeed R-Series) and in Indian or ERPNext add-on POS products.

### Cited Findings
**Shopify POS**
- Shopify POS 11.0 introduced "a cleaner, more readable cart layout" that makes items, prices and totals easier to scan. The cart now stays on screen through checkout, so staff keep context from item selection through payment. The More actions menu holds only cart-wide actions, and multi-select lets staff edit several lines at once on larger orders. — [Shopify changelog: POS 11.0](https://changelog.shopify.com/posts/shopify-pos-version-11-0)
- In POS 11.0, larger numpads for cash and split payments appear in a left side panel instead of a pop-up. Customer and Staff attribution also open in that left side panel. — [Shopify changelog: POS 11.0](https://changelog.shopify.com/posts/shopify-pos-version-11-0)
- POS 10 added a dynamic cart header showing whether staff are working on a new order or a draft, and more line items fit on screen. — [Shopify changelog: POS 10.0](https://changelog.shopify.com/posts/shopify-pos-version-10-0) (summarised via search result)
- Tablet split-screen Home puts product and customer search alongside the cart, which is always visible next to search results. — [Shopify changelog: split screen home layout for tablet](https://changelog.shopify.com/posts/shopify-pos-split-screen-home-layout-for-tablet) (search summary)
- A Feb 2023 merchant request says large buttons "take up over half the screen at check out". Their carts can hold up to 35 items, so they want a bigger cart view, preferably on a PC screen, plus user-level settings for button size and layout. No Shopify response is shown. Related threads continue through 2024–2026: "reduce size of tiles on POS (or enable computer monitor use)" (Jan 2024), "Latest POS update… slows checkout process" (Nov 2024), "Please make product tiles/buttons smaller" (Jan 2026), "POS / POS Pro Checkout Screen Customization" (Oct 2026). — [Shopify Community](https://community.shopify.com/t/feature-request-larger-check-out-screen-smaller-buttons/194474)
- A community thread reports that Shopify POS officially runs only on iOS/Android, with no desktop app announced as of 2024. — [Shopify Community: POS on desktop](https://community.shopify.com/t/shopify-pos-on-desktop/5563/20) (search summary)

**Lightspeed Retail R-Series (desktop browser register, keyboard-first)**
- Register shortcuts: Alt+I focuses the Add Item box (scan or search), Alt+C focuses customer search, Alt+D goes to payments, Alt+R starts a refund, Alt+A layaway, Alt+O special order, Alt+W/K work orders, and Alt+S returns to the sale tab. On the payment screen, Alt+C focuses the Cash amount box, Alt+S finishes the sale and goes to the receipt, and Alt+B goes back to add more items. — [Lightspeed R-Series: Keyboard shortcuts](https://retail-support.lightspeedhq.com/hc/en-us/articles/228839547-Keyboard-shortcuts)

**Lightspeed Retail X-Series (formerly Vend)**
- The X-Series sell screen uses "Quick Keys": configurable tiles for adding products, with label, colour and optional image. Layouts are set per register and synced across devices. There is no hard limit, but on iPad more than 25 tiles requires swiping. Lightspeed advises keeping the set concise, because a large number of keys makes the Quick Key page scroll and can slow the sell screen. — [Lightspeed X-Series help: Quick Keys](https://x-series-support.lightspeedhq.com/hc/en-us/articles/25533825756443-How-many-keys-can-I-have-within-a-Quick-Key-layout)
- Vend is the predecessor of Lightspeed X-Series. — [Wikipedia: Vend (software)](https://en.wikipedia.org/wiki/Vend_(software)) (listed in search; not fetched)

**ERPNext / Frappe POS**
- In the 2021 ERPNext POS redesign, the old static item selector took about 60% of the screen even during checkout and payment. The new selector shrinks while item details are edited and hides on the payment screen. The numpad moved to the right-hand side for touch. Payment moved from a dialog into its own section, with the goal of "quick transactions with almost no dialog or popups". Popups were replaced with alerts, and rate/qty changes give visual feedback. Design goals included "Optimize for both keyboard and touch screens" and "Automatically focus on next required fields". Early feedback said "there are too many buttons on the screen", but the designer kept the actions visible so they stay within reach. — [Frappe blog, 31 May 2021](https://frappe.io/blog/accounting/point-of-sale-erpnext-makeover)
- Third-party ERPNext POS products add keyboard-first features. POS Awesome (v15/v16) supports keyboard navigation of product results, cart rows, drafts and payments, with Alt+3 for item search and Alt+PageUp for payments. POS Next adds a payment numpad with quick amounts, physical keyboard support, and Enter on an empty keypad to pay exact cash. — [POS-Awesome-V15](https://github.com/defendicon/POS-Awesome-V15); [POSNext releases](https://github.com/BrainWise-DEV/POSNext/releases); [POSNext PR #427](https://github.com/BrainWise-DEV/POSNext/pull/427)
- A forum request for POS Awesome asks for Alt+F (item search), Alt+C (customer), Alt+S (salesperson) and Alt+H (hold invoice). This shows that salesperson-by-keyboard is a recurring request in Indian/ERPNext retail use. — [Frappe forum](https://discuss.frappe.io/t/add-shortcut-focus-keys-on-pos-awesome-app-for-erpnext/111586)

**Odoo POS**
- Odoo docs: click products to add them to the cart. Each cart line is edited with Qty / % Disc / Price buttons on a keypad. Payment → choose method → enter amount → Validate, and cash is the default if no method is chosen. A Customer button is on the main screen and on the payment screen. Cash In/Out and Close Session sit in an upper-right menu. "New Order" starts the next sale. The docs do not describe proportions in text, only in a screenshot. — [Odoo 18 docs: Point of Sale](https://www.odoo.com/documentation/18.0/applications/sales/point_of_sale.html)

**Gofrugal (Indian retail)**
- Marketing copy promotes a single billing screen: "Manage complete billing from cash bill to delivery bill and all billing feature in a single screen". The screen shows item details and stock availability, item lookup by name, brand or code, split tender (cash, card, wallets, UPI, loyalty, coupons, prepaid cards), and bill-level and item-level discounts in % or ₹. — [Gofrugal billing software](https://www.gofrugal.com/retail/features/billing-software-for-retail-shop.html)
- The salesman can be picked during billing by name, code, or scanning the salesman's barcode, and commission can be configured per sale. — [Gofrugal GST billing](https://www.gofrugal.com/retail/billing-software/) (search summary)
- One Gofrugal help article uses F2 to proceed in the sales bill flow. A full F-key map was not found. — [Gofrugal guide: sales bill](https://community.gofrugal.com/portal/en/kb/articles/sales-bill-6-8-2020) (search summary)

**Comparator (non-target): WCPOS (WooCommerce POS)** uses a two-column desktop layout with the product panel on the left and the cart on the right. — [WCPOS docs](https://docs.wcpos.com/pos/) (search summary)

### Inferences
- For KDPS on a 1920px monitor with a scanner, a product-tile grid is the wrong primary element. Apparel is scanned by barcode, and Lightspeed itself warns that big tile sets slow the screen. Merchants complain that Shopify's tablet tiles waste PC screen space and shrink the cart. A cart-dominant layout fits better: bill lines take most of the width, and a fixed right rail holds customer, salesperson, totals and tenders. This follows the Shopify 11 and ERPNext decisions: the cart stays visible, payment goes in a panel rather than a modal, and the item picker hides at payment.
- Lightspeed R-Series's Alt-key focus jumps are a proven, low-cost keyboard model for a web register. Possible mappings are scan box, customer, salesperson, payment, cash amount, finish, and back to sale. POS Next's "Enter on empty = pay exact cash" is a concrete speed trick that fits KDPS's cash-heavy counter.
- Salesperson as a first-class, scannable field (Gofrugal's salesman barcode, the ERPNext Alt+S request) is an Indian-retail norm. It argues for a salesperson control in the always-visible rail, not inside a dialog.

### Gaps
- No primary-source description found for the desktop/browser register layout of Square for Retail, Toast (restaurant-oriented), Zoho Inventory/POS, Petpooja (restaurant-oriented), Marg ERP, or Ginesys POS. The Ginesys Confluence "How To Create a POS Bill" page needs a login.
- No vendor states a pixel or point font size for the "To pay" total, or exact cart/grid width ratios. The only quantified proportion is ERPNext's "about 60%" for the old item selector.
- Lightspeed X-Series keyboard shortcuts were not found; the Alt shortcuts are R-Series only.

## 2. Back-office list/detail screens (Shopify Admin, Odoo, ERPNext, Lightspeed, Zoho)

### Takeaway
Shopify's published admin guidance is the only concrete, citable rule set found. It uses two page widths: `base`, which allows an aside column, and `large`, for dashboards and index pages with many columns. It also prescribes an Index table for large datasets (multi-column, bulk actions, click through to a full detail page), a 4px spacing grid, and one density per page. Odoo's form view puts the chatter in a right-side panel on wide screens and below the form on narrow ones, and a cottage industry of add-on modules exists to resize, hide or reposition it. That suggests wide-screen space use is an open pain point there.

### Cited Findings
- Shopify app/admin layout: the default page width is `base`, and the `aside` slot only displays at that width. Use `large` for dashboard-style pages and for resource index pages with many data columns. Dashboards use 2–3 equal columns in a grid. Two-column layouts suit content-dense pages and editors. Settings pages use a narrow left column (title/description) and a wider right column (form). — [Shopify.dev: Layout](https://shopify.dev/docs/apps/design/layout)
- Spacing and density: build on a 4px grid, use looser spacing for low-density and tighter for high-density layouts, and "don't change density within a single page". Put most content in cards/sections, with at most one primary action per container. Use a simple table for small summaries and the Index table pattern when there is a lot of data. Row actions go in the last cell as tertiary or icon buttons or a single menu button, and tables should have no primary buttons. — [Shopify.dev: Layout](https://shopify.dev/docs/apps/design/layout)
- The Polaris IndexTable presents resources as multiple columns, lets staff act on one or more rows (bulk actions), navigates to the resource's detail page, and supports sorting, filtering and pagination. — [Polaris IndexTable](https://polaris-react.shopify.com/components/tables/index-table)
- Polaris column widths are set per cell, and an older issue asked for percentage-based widths. — [polaris-react issue #832](https://github.com/Shopify/polaris-react/issues/832)
- Odoo chatter position depends on screen width: it goes to the right on high-resolution screens and below the form otherwise. — [Odoo forum](https://www.odoo.com/forum/help-1/how-to-hide-or-re-position-the-chatter-pane-170829) (search summary)
- Several third-party Odoo 16–19 modules exist only to hide, resize or reposition the chatter or make forms full-width ("expands form views to the full screen width and moves the chatter section to the bottom"). — [fullwidth_form_and_chatter (17.0)](https://apps.odoo.com/apps/modules/17.0/fullwidth_form_and_chatter); [chatter_toggle (19.0)](https://apps.odoo.com/apps/modules/19.0/chatter_toggle); [Form Full Screen (18.0)](https://apps.odoo.com/apps/modules/18.0/form_full_screen)
- The OCA Web Responsive module makes list-view headers and footers sticky. — [Odoo Mastery: Web Responsive review](https://odoomastery.com/blog/modules-review-6/overview-of-the-web-responsive-module-33) (search summary)
- Lightspeed reviews: one Capterra reviewer says the back office "is not easy to navigate", another blames clutter on many features left over from older versions, and complaints focus more on reporting depth and paid add-ons than on layout. G2 rating is 4.0/5 from 293 reviews; Capterra 4/5 from 975. — [Capterra: Lightspeed Retail reviews](https://www.capterra.com/p/120491/Lightspeed-Retail/reviews/); [G2: Lightspeed Retail pros and cons](https://www.g2.com/products/lightspeed-retail/reviews?qs=pros-and-cons) (search summaries)

### Inferences
- For KDPS list screens (Bills, Goods received, Transfers, Stock counts, Customers) on 21–27" monitors, Shopify's guidance points to a full ("large") width table with dense rows. Use a single density per page, put row actions in the last cell, and avoid primary buttons inside tables. Primary actions belong in the page header.
- The Odoo chatter-module ecosystem suggests a right-hand side panel for a selected record is useful on wide screens, but users want control over it (collapse or full-width). For KDPS, a list + right detail pane (master–detail) on ≥1600px that falls back to a full page on laptops would follow this pattern. That is inferred from the Odoo responsive behaviour, not a documented best practice.

### Gaps
- No primary source found for native Odoo 17/18 list density settings, ERPNext list view column limits, Zoho Inventory list/detail layout, or Lightspeed back-office table design. No vendor gives a recommended column count.
- Shopify's "large" width in pixels is not stated on the page fetched.

## 3. Retail dashboards (daily sales, staff performance, stock): KPI layouts and what users praise or criticise

### Takeaway
Shopify POS's on-device Analytics is a good, simple model for a store dashboard. It has three KPI tiles (net sales, AOV, items per order), each compared with the same weekday last week, then ranked top-3 lists (products, product types, staff) and net payments by tender type, with no charts. Staff performance is a separate sortable table (staff, items sold, net sales). User criticism of retail POS reporting (Lightspeed) centres on shallow reports and paid add-ons rather than on chart styling.

### Cited Findings
- Shopify POS Daily Sales report: "compares today's sales to the sales made the same day last week". KPI tiles at the top show Net sales, Average order value and Items per order, and each tile opens a detail view. Below them are top 3 product variants by net sales (units, total), top 3 product types, top 3 staff at register (orders processed, total sales), and net payments by payment type with a total. Date ranges are Today, Yesterday, WTD, MTD, QTD and YTD. — [Shopify Help: Analytics on POS](https://help.shopify.com/en/manual/sell-in-person/shopify-pos/analytics-on-pos)
- Shopify "Total sales by staff" lists staff credited with line items at checkout (name, items sold, net sales), with date filters and sorting. It requires the "View analytics for device's location" permission and POS Pro. — [Shopify Help: Analytics on POS](https://help.shopify.com/en/manual/sell-in-person/shopify-pos/analytics-on-pos)
- Staff credit goes to the person attributed to each line item, even if another employee processed the transaction. — [Shopify blog: store performance dashboard](https://www.shopify.com/blog/store-performance-dashboard) (search summary)
- A vendor-blog layout heuristic arranges rows by question. Row 1 asks "was the period good?" (net sales vs plan, transactions, gross margin); later rows cover execution and loyalty (sales per labour hour, shrink, retention). It advises line charts for trends, bars for category comparisons, and a single scorecard number for the headline KPI. — [KORONA POS: Retail KPI dashboard](https://koronapos.com/blog/retail-kpi-dashboard/) (vendor blog, search summary)
- Lightspeed reviewers: built-in reports "lack a bit when it comes to deep diving", and advanced reporting costs extra and is "poorly designed and buggy". One reviewer wishes reporting and visualisation came with the product. — [Capterra: Lightspeed Retail reviews](https://www.capterra.com/p/120491/Lightspeed-Retail/reviews/); [G2](https://www.g2.com/products/lightspeed-retail/reviews?qs=pros-and-cons) (search summaries)

### Inferences
- KDPS's Reports/dashboard could copy the Shopify structure: a KPI row with a same-weekday-last-week comparison, ranked short lists for salesperson, top styles/categories and tender mix, and a link through to a full sortable table. That uses a wide screen better than large decorative charts. Salesperson ranking matters more for an Indian apparel store with commission than it does in Shopify's model.

### Gaps
- No Square Dashboard or Lightspeed/Odoo dashboard layout specifics found. No Reddit threads on cluttered or wasted-space screens were retrieved within the call budget.

## 4. Published usability research on POS screen design (speed, errors, ergonomics, font size, target sizes)

### Takeaway
I found no peer-reviewed, cashier-specific study tying font size or layout to checkout speed or error rates. The available evidence is vendor field claims (cashiers sit at least 80 cm from the screen and tap at least twice as fast as ordinary users) and general accessibility standards for target size. Treat any specific "POS font size" number as a design judgement, not research.

### Cited Findings
- A POS vendor's on-site research (UX Journal/Medium, "16 UX factors of the point of sale system") reports that cashiers usually sit at least 80 cm from the screen, tap at least twice as fast as typical users, and that one error can multiply time per customer by up to 10×. These are company claims, not peer-reviewed, and the article returned HTTP 403, so the claims were seen only via search summary. — [Medium/UX Journal: POS Design Guide Part 1](https://medium.com/uxjournal/pos-ux-design-part-one-the-16-ux-factors-in-point-of-sale-b94661936eea)
- A USPTO patent background estimates "Not on File" item-code errors at 10,000–15,000 per day at a large retailer, costing about $300–400K per retailer. This is an unverified patent claim. — [USPTO patent 12046059](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/12046059) (search summary)
- WCAG 2.2 SC 2.5.8 sets the minimum pointer target at 24×24 CSS px, and for important controls advises aiming for the stricter SC 2.5.5 (Target Size Enhanced). — [W3C: Understanding SC 2.5.8](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)
- ERPNext's redesign kept many visible buttons despite user feedback, reasoning that POS users need actions within reach. It also removed dialogs and popups to speed transactions. — [Frappe blog](https://frappe.io/blog/accounting/point-of-sale-erpnext-makeover)
- An e-commerce (not POS) checkout usability study with 35 participants raised SUS from 69.57 to 96.64 after a redesign that included larger fonts and clearer hierarchy. It concerns online shoppers, not cashiers. — [ResearchGate](https://www.researchgate.net/publication/403520773_A_Usability_Evaluation_of_an_E-Commerce_Checkout_System_Using_SUS_and_Usability_Testing) (search summary)

### Inferences
- If cashiers sit at about 80 cm (vendor claim), the "To pay" figure and the last-scanned line need to be legible at arm's length. The amount should be several times body size, and the last-scanned line should be highlighted. The exact size is a design call; no study gives one.
- For mouse plus scanner (not touch), 24px is the floor for secondary controls, and the larger WCAG 2.5.5 size suits primary actions such as Save & print and tender buttons. Big touch tiles are not needed, which frees space for more bill lines.
- Removing modals (Shopify 11, ERPNext) and keeping focus in the scan box are the most consistently cited speed levers.

### Gaps
- No controlled study found on cashier error rates versus layout, font size at arm's length, or mouse versus touch speed at POS. Academic search terms such as "cashier human factors" or "point-of-sale keying errors" were not explored within the budget.

## 5. Indian retail specifics: GST invoice fields, ₹ formatting, salesperson commission, how Indian POS products present them

### Takeaway
Indian POS products switch invoice format according to the buyer. Walk-in customers get a simple POS bill, and entering a GSTIN switches to a full GST tax invoice. B2C invoices under ₹50,000 need buyer name and address only on request. Salesperson attribution at billing (by name, code or barcode) with per-sale commission is a standard feature.

### Cited Findings
- Gofrugal switches to a GST-compliant tax invoice format with all mandatory fields when a customer GSTIN is entered at billing, and uses the standard POS bill format for walk-ins. It calculates the GST split and prints it on the invoice. — [Gofrugal GST billing software](https://www.gofrugal.com/retail/billing-software/) (search summary)
- Mandatory tax-invoice fields include supplier and buyer GSTIN (B2B), a unique invoice number, HSN codes, item-wise CGST/SGST or IGST, and place of supply. — [Gofrugal: billing vs invoicing](https://www.gofrugal.com/blog/billing-vs-invoice/) (search summary)
- Rule 46 (B2C): when the value is below ₹50,000, buyer name and address are required only if the recipient asks for them. Above ₹50,000 for an unregistered buyer, the invoice needs name, address, delivery address, and state name and code. B2C invoices need no buyer GSTIN. Every invoice needs supplier name, address and GSTIN, a unique serial of no more than 16 characters, the date, and HSN/SAC, description, taxable value, rate and tax amount. Place of supply applies to inter-state supplies. Field counts vary by source (16, 17 or 21), so check the CGST Rules text. — [ClearTax: GST invoice details](https://cleartax.in/s/gst-invoice-details); [Tally: GST invoice format](https://tallysolutions.com/gst/gst-invoice-format-mandatory-fields-rules-and-examples-for-businesses/); [GimBooks Rule 46 checklist](https://www.gimbooks.com/blog/gst-invoice-mandatory-fields-rule-46-checklist/) (search summaries)
- Ginesys: offline (in-store) purchases are charged CGST+SGST regardless of the customer's state of registration, but its GST FAQ also discusses a tagged customer's state code differing from the store's. Ginesys sources disagree on how the inter-state case is handled. — [Ginesys GST FAQ](https://ginesys.atlassian.net/wiki/spaces/PUB/pages/106233934/GST+FAQ) (search summary)
- Ginesys has a bulk salesperson-to-POS-site assignment import (Admin > Utilities > Data Import), so salespeople are scoped per store. — [Ginesys release note 12.15.9](https://ginesys.atlassian.net/wiki/spaces/PUB/pages/4035084416/Release+Note+12.15.9+-+2.15.7) (search summary)
- Ginesys POS has no separate service-invoice screen. Services such as alterations are billed as a non-inventory item with a service-type HSN. — [Ginesys: service bill at POS](https://ginesys.atlassian.net/wiki/spaces/PUB/pages/3539402757/How+To+Make+Service+Bill+at+POS+end) (search summary)
- Gofrugal: salesman selected at billing by name, code or barcode scan, with per-sale commission. — [Gofrugal GST billing](https://www.gofrugal.com/retail/billing-software/) (search summary)

### Inferences
- The KDPS billing screen does not need GST detail on the primary surface for B2C sales under ₹50,000. A collapsed "Tax invoice / GSTIN" affordance that expands buyer fields when a GSTIN or a ≥₹50,000 total is detected would match Gofrugal's behaviour and save rail space.
- Make salesperson a scannable or typeable field next to the scan box or in the totals rail. Indian products treat it as a per-bill or per-line attribute that feeds commission.
- Apparel stores bill alterations as service items (Ginesys pattern), so the bill-line component should handle non-stock service lines with a service-type HSN.

### Gaps
- ₹ formatting conventions (lakh/crore grouping, e.g. ₹1,23,456.00; `Intl.NumberFormat('en-IN')`) were not verified against a fetched source in this session.
- Petpooja and Marg billing-screen layouts, and Ginesys and Gofrugal F-key maps, were not found. Their help centres are gated or the search results did not cover them.
- No source shows how Indian POS products visually present salesperson commission on screen, beyond the existence of the feature.
