# Typography and type scales for dense operational desktop software

Scope: retail back office, POS and admin UIs on 1280–1440px laptops and 1920–2560px monitors. Baseline for comparison is the current KDPS system: IBM Plex Sans and Plex Mono, body 14/21, table cells 13.5, labels 13, captions 12.5, page title 24/700, KPI value 23/700, money-xl 44.

Research note: I read primary design-system pages for Carbon, Fluent 2, Primer and Atlassian. Polaris pages failed to load (socket errors and a redirect), so Polaris values come from a search snippet of its token reference. I found no first-party Linear or Stripe numbers.

## 1. What base sizes, scales and line heights do respected dense products use?

### Takeaway
Every published product design system I checked uses **14px body with a 20px line height (about 1.43)**. Secondary, label and caption text is **12px on 16px**. Shopify Polaris is the densest at **13px body on 20px**. The steps are few and mostly whole pixels on a 4px line-height grid: 12, 14, 16, 20, 24, 28, 32. Page titles in product UI are **20–28px**. Headings are separated by weight (semibold or bold) at least as much as by size. KDPS's 14/21 body is in line with the others. Its 12.5/13/13.5/14 cluster is not: no reference system has four sizes within 1.5px of each other.

### Cited Findings
**IBM Carbon, productive set (for product UI, Plex Sans)**
- Productive set base is 14px. Expressive (web pages) base is 16px. Product pages "have a higher density of information housed inside containers" — [Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/)
- label-01, helper-text-01, legal-01 and code-01: 12/16, weight 400, tracking 0.32px — [Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/)
- body-compact-01: 14/18. body-01: 14/20. Both weight 400, tracking 0.16px — [Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/)
- heading-compact-01: 14/18, weight 600. heading-01: 14/20, weight 600. These are section headings at body size, separated only by weight — [Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/)
- heading-03: 20/28, weight 400. heading-04: 28/36, weight 400. heading-05: 32/40, weight 400. heading-06: 42/50, weight 300. heading-07: 54/64, weight 300. Large headings get *lighter*, not bolder — [Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/)
- Expressive set: body-02 16/24, body-compact-02 16/22, label-02 14/18 — [Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/)
- Carbon's scale formula is "Xn = Xn-1 + {INT[(n-2)/4] + 1} * 2" starting at 12px. The steps are 12, 14, 16, 18, 20, 24, 28, 32, 36, 42, 48, 54, 60, 68, 76, 84, 92. Increments grow by 2px every four steps instead of following a fixed ratio — [Carbon typography overview](https://carbondesignsystem.com/elements/typography/overview/)

**Microsoft Fluent 2 (web, Segoe UI)**
- Caption 2: 10/14. Caption 1: 12/16. Body 1: 14/20. Subtitle 2: 16/22 semibold. Subtitle 1: 20/26 semibold. Title 3: 24/32 semibold. Title 2: 28/36. Title 1: 32/40. Large Title: 40/52. Display: 68/92 — [Fluent 2 typography](https://fluent2.microsoft.design/typography)
- "Strong" and "Stronger" variants raise the weight of the same size (Semibold, then Bold) to add emphasis without adding sizes — [Fluent 2 typography](https://fluent2.microsoft.design/typography)
- Windows ramp: Caption 12/16, Body 14/20, Body Large 18/24, Subtitle 20/28, Title 28/36 — [Fluent 2 typography](https://fluent2.microsoft.design/typography)

**GitHub Primer**
- Display: 2.5rem (40px), line height 1.375, weight 500. Title large: 2rem (32px), 1.5, weight 600. Title medium: 1.25rem (20px), 1.625, weight 600. Title small: 1rem (16px), 1.5, weight 600. Subtitle: 20px, 1.625, weight 400 — [Primer typography primitives](https://primer.style/product/primitives/typography/)
- Body large: 16px / 1.5. Body medium: 14px / 1.5. Body small: 12px / 1.625. Caption: 12px / 1.25. Code block: 13px / 1.5 — [Primer typography primitives](https://primer.style/product/primitives/typography/)
- Line heights are unitless and align to a 4px grid. Sizes are in rem to support browser zoom. Primer says "use weight CSS variables" rather than arbitrary weights — [Primer typography foundations](https://primer.style/foundations/typography)

**Atlassian (Jira and Confluence)**
- heading.xxlarge: 32/36. xlarge: 28/32. large: 24/28. medium: 20/24. small: 16/20. xsmall: 14/20. xxsmall: 12/16. All bold — [Atlassian typography](https://atlassian.design/foundations/typography)
- body.large: 16/24. body (the default in components): 14/20. body.small: 12/16 — [Atlassian typography](https://atlassian.design/foundations/typography)
- Separate **metric** tokens for numbers: metric.large 28/32, metric.medium 24/28, metric.small 16/20, all bold. code: 12/20 — [Atlassian typography](https://atlassian.design/foundations/typography)
- Usage: XL and L for page titles, M for modals, S and XS for small components. Body S is for secondary content "used sparingly". Weights: Regular for paragraphs, Medium for most components and text beside icons, Bold sparingly — [Atlassian typography](https://atlassian.design/foundations/typography)

**Shopify Polaris (admin)**
- `--p-font-size-325` is 13px. The **body-md text token uses font-size-325 (13px)** with line-height-500, regular weight. The font-size scale runs from 11px to 40px — [Polaris text tokens](https://polaris-react.shopify.com/tokens/text), [Polaris font tokens](https://polaris-react.shopify.com/tokens/font) (from search snippets; the pages themselves failed to load)

**Linear**
- The 2024 UI redesign used Inter Display for headings "to add more expression to our headings while maintaining their readability," and regular Inter elsewhere. It aimed to "increase the hierarchy and density of navigation elements" while reducing "visual noise." Text and neutral icons were made darker in light mode. Theming went from 98 variables to 3: base, accent and contrast. The post gives no pixel sizes — [Linear: How we redesigned the Linear UI](https://linear.app/now/how-we-redesigned-the-linear-ui)
- Third-party reverse-engineered write-ups report Inter Variable, a 13px size for metadata, and unusual weights of 510 and 590. These are unofficial and disagree with each other — [skills.sh Linear UI skills](https://skills.sh/ihlamury/design-skills/linear-ui-skills), [refero style](https://styles.refero.design/style/a5cc9b0f-d274-458a-b990-d18482b70838)

**Stripe Dashboard**
- I found no first-party write-up. Third-party summaries say Stripe uses Söhne, light (300) weights with negative tracking at display sizes, and `tnum` tabular numerals for financial data. These mostly describe the marketing site and should be treated as unverified — [shadcn.io Stripe design](https://www.shadcn.io/design/stripe/raw), [leerob archive](https://archive.leerob.io/blog/how-stripe-designs-beautiful-websites)

### Inferences
- Consensus product ramp: 12/16 (label, caption), 14/20 (body), 16/20–24 (small heading or emphasised body), 20/24–28 (section or panel title), 24–28/28–36 (page title), 32–42 (display or large metric). A denser table variant would be 13/20, following Polaris.
- KDPS body at 14 matches the field. Its line height of 21 (1.5) is looser than the 20 (1.43) everyone uses for UI text. Primer's 1.5 is for body paragraphs. At 14/20, more rows fit on a laptop.
- The "AI-made" feel probably comes partly from **too many near-identical sizes** (12.5, 13, 13.5, 14), which read as noise rather than hierarchy. Fractional pixel sizes are another likely cause. Reference systems use a few whole-pixel steps and vary weight and colour. Suggested consolidation: 12, 13 (tables only), 14, 16, 20, 24 or 28, then numeric display sizes.
- KDPS page title at 24/700 is at the bold end. Fluent and Atlassian use 24 semibold or bold. Carbon uses 20–28 at regular weight. A 20–24 semibold title would read calmer and less like a template.

### Gaps
- Bloomberg Terminal: I did not find published token values. It is widely described as fixed-size and monospace-heavy, but I have no citable numbers.
- Polaris heading values (headingSm to heading3xl) and responsive heading variants could not be verified because the pages failed to load.
- Linear and Stripe have no first-party pixel values.
- Vercel Geist was not checked within the tool budget.
- Material Design 3 was not fetched. From memory, body-medium is 14/20 and label-large is 14/20 at weight 500, but this is unverified.

## 2. Fluid (clamp) type vs fixed steps on large screens

### Takeaway
For product UI, **Carbon explicitly keeps text fixed**. Productive styles stay the same size at every breakpoint, and fluid styles are reserved for marketing or editorial headings and "should not be used inside a container." Fluent 2, Atlassian and Primer publish no viewport-scaling guidance for UI text. They size in rem so the *user* or browser zoom controls scale, not the viewport. Extra width on a 2560px screen should go to more columns, panels and context, not bigger body text.

### Cited Findings
- Fixed styles "are not responsive... The type size remains constant regardless of break point." Fluid headings and display "change size at different breakpoints." Fluid styles: "Do not use these styles inside a container." Fluid headings are "primarily used in web pages, and are therefore part of the expressive set" — [Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/)
- Productive headings are fixed. Expressive headings are responsive. Expressive type "would be distracting in product" — [Carbon typography overview](https://carbondesignsystem.com/elements/typography/overview/)
- Atlassian uses rem "so users can resize text to their needs." The page has no responsive or large-screen rules — [Atlassian typography](https://atlassian.design/foundations/typography)
- Primer uses rem "to support browser zoom." The pages I could read had no narrow, regular or wide type variants — [Primer typography foundations](https://primer.style/foundations/typography), [Primer primitives](https://primer.style/product/primitives/typography/)
- Fluent 2's typography page has no guidance on large screens or responsive scaling — [Fluent 2 typography](https://fluent2.microsoft.design/typography)
- Linear tested navigation "from very condensed to more spacious configurations" instead of scaling type — [Linear redesign](https://linear.app/now/how-we-redesigned-the-linear-ui)

### Inferences
- Do not clamp body, table, label or control text to the viewport. A 27" 2560px monitor at about 109 ppi is physically similar to a 24" 1920px monitor at about 92 ppi once the OS scales. Viewport width says little about viewing distance or pixel size, so viewport-based text scaling solves the wrong problem.
- Where scaling is wanted, make it a **user or device density setting** that changes the root rem, for example compact at 13px base and comfortable at 14px. Do not drive it from `vw`.
- Fluid sizing is defensible only for a few display numerals in a non-container area, such as the tender amount on a till screen, and should have a clamp ceiling.
- On wide screens, "text taking better place" should mean: no artificial `max-width` on tables, more visible columns, persistent side panels and multi-column summary areas. It should not mean 16–18px body text.

### Gaps
- Carbon does not document behaviour at its max (1584px+) breakpoint.
- No source found gives large-monitor-specific type guidance from Fluent or Polaris.

## 3. Hierarchy without huge headings; sizes for big numbers

### Takeaway
Reference systems build hierarchy mostly with **weight steps at a constant size** (Carbon heading-01 is 14/600 next to body-01 at 14/400, and Fluent has Strong and Stronger), **colour tiers** (primary vs subdued neutral) and **a separate numeric style** (Atlassian metric tokens are bold, 16–28px). Big numbers for arm's-length or counter reading can be sized from viewing distance. A common rule is cap height ≈ 0.007 × distance, or a capital letter subtending 20–22 arcminutes (16–18 is the marginal floor).

### Cited Findings
- Carbon's section heading heading-01 is the same 14px as body, set at weight 600. Larger headings drop to 400 or 300 — [Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/)
- Carbon: SemiBold is "ideal for section headers, but should not be used for long text." Keep running text neutral. "Color is not for decoration" — [Carbon typography overview](https://carbondesignsystem.com/elements/typography/overview/)
- Fluent: primary colour raises prominence, and a lighter neutral subdues lower-hierarchy text. Use sentence case and avoid all caps. Large text for contrast purposes is above 18.5px bold or 24px regular (3:1). Standard text needs 4.5:1 — [Fluent 2 typography](https://fluent2.microsoft.design/typography)
- Primer: avoid colour as the *main* means of emphasis. Use semantic headings and don't reorder heading tags for visual effect — [Primer typography foundations](https://primer.style/foundations/typography)
- Atlassian: separate metric.large, medium and small tokens (28/32, 24/28, 16/20 bold) are reserved for emphasised numbers. Headings should introduce sections rather than "bold text or font-size changes alone" — [Atlassian typography](https://atlassian.design/foundations/typography)
- All caps: an MIT AgeLab glance study (Frutiger) found lowercase needed **26% more time** to read accurately than uppercase, with a bigger penalty at small sizes. Condensed widths needed 11.2% more time. The authors warn against all caps for longer passages — [NN/g, glanceable fonts](https://www.nngroup.com/articles/glanceable-fonts/)
- Viewing-distance sizing: character height subtending **20–22 arcminutes** "assures legibility." **16–18** "might be acceptable" depending on contrast — [KSU ergonomics chapter (secondary)](https://faculty.ksu.edu.sa/sites/default/files/4%20-%20Chapter%204%20-%20Vision%20-%20part%202%20%28AMS%20May01_24%29.pdf). The "007 rule" is character height ≈ 0.007 × viewing distance, attributed to HFES practice in a secondary source — [PropertyCasualty360](https://www.propertycasualty360.com/amp/2003/05/06/qa-007-still-a-secret/). A desktop requirement at 50cm and 20–22 arcmin, measured on capitals, is quoted in [Articulate community](https://community.articulate.com/discussions/rise-360/ergonomic-requirements) (unconfirmed as HFES 100 text).
- Tabular numerals (`tnum`) for financial figures are cited for Stripe by third parties — [shadcn.io Stripe design](https://www.shadcn.io/design/stripe/raw). Applying `font-variant-numeric: tabular-nums` forces tabular figures in any font that has them — [Flutter FontFeature docs](https://api.flutter.dev/flutter/dart-ui/FontFeature/FontFeature.tabularFigures.html)

### Inferences
These are my calculations. They assume about 0.27mm per CSS pixel on a 24" 1080p monitor at 100% scale and a Plex cap height of about 0.7em; I did not verify the cap-height figure.
- **Cashier reading own screen at about 60cm:** 20 arcmin ≈ 3.5mm cap ≈ 13 CSS px cap ≈ **18–20px font**. 14px body is fine for sustained reading. Totals and amounts the cashier must confirm should be about 20–24px semibold.
- **Customer reading the amount to pay across the counter at 1–1.5m:** 20 arcmin ≈ 5.8–8.7mm cap. The 007 rule gives 7–10.5mm, which is about 25–38 CSS px cap, or **36–56px font**. The current money-xl at 44 suits about 1.2m. Keep it, set it in tabular figures, and use weight 500–600 rather than 700, because heavy Plex at that size clogs. For 1.5m or more, or a customer-facing display, use about 56px.
- **Dashboard KPIs (read at the desk, about 60–70cm):** 24–32px is enough. Atlassian's metric tokens (24 and 28 bold) are a good anchor. The current KPI at 23/700 is effectively the same as the 24px title, so number and title compete. Either take KPIs to 28–32 at 600 with a 12–13px muted label above, or drop the page title to 20.
- Hierarchy recipe that avoids the "template" look: one size for content (14), one for dense data (13), one for meta (12, muted colour, not smaller). Headings use the same 14px at 600, or 16 or 20 at 600. Numbers use their own tabular, larger style. Section labels in small caps or uppercase at 12px with letter spacing are acceptable as short labels, given the all-caps glance advantage, but never for sentences.

### Gaps
- I could not access the HFES 100-2007 or ISO 9241-303 text directly. The arc-minute figures are from secondary sources.
- I found no published retail POS design-system guidance on tender-amount sizes.

## 4. IBM Plex specifics

### Takeaway
Carbon (the system Plex was built for) recommends **Light, Regular and SemiBold** for digital UI. SemiBold is for headers, not long text. Carbon's productive set uses 400 for body, 600 for small headings, 400 for 20–32px headings and 300 for 42px and up. It adds +0.16px tracking at 14px and +0.32px at 12px, so Plex is tracked *open* at small sizes. Plex Mono is used at 12/16 (code-01) and 14/20 (code-02). Bold (700) appears nowhere in Carbon's productive set.

### Cited Findings
- "We suggest using IBM Plex Light, Regular, and SemiBold for digital experiences." SemiBold is "ideal for section headers, but should not be used for long text." Italics are for emphasis only — [Carbon typography overview](https://carbondesignsystem.com/elements/typography/overview/)
- Tracking: 12px styles at 0.32px, 14px styles at 0.16px, 16px and above at 0. Display sizes of 60px and above use -0.64px — [Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/)
- Plex Mono tokens: code-01 12/16 at 0.32px, code-02 14/20 at 0.32px. All other styles use Plex Sans — [Carbon type sets](https://carbondesignsystem.com/elements/typography/type-sets/)
- Type tokens are "calibrated for IBM Plex" — [Carbon typography overview](https://carbondesignsystem.com/elements/typography/overview/)
- Plex is open source (OFL) with Sans, Serif, Mono and Sans Condensed, and is published on npm as @ibm/plex-sans — [IBM/plex GitHub](https://github.com/IBM/plex), [Fontsource](https://fontsource.org/fonts/ibm-plex-sans/about)

### Inferences
- KDPS uses 700 for titles and KPIs. Carbon's guidance implies 600 for emphasis and 400 or 300 for very large display numbers. Moving title and KPI weights to 600, and the 44px money figure to 500–600, would follow IBM's own usage and make big numerals look less blunt.
- KDPS's 13.5 and 12.5 sizes have no counterpart in Carbon's Plex scale (12, 14, 16, 18, 20, 24, 28, 32 ...). The scale is described as "calibrated for IBM Plex," so whole-pixel Carbon steps are the safer choice.
- If 13px is kept for tables, apply roughly +0.16–0.24px tracking, by analogy with Carbon's small-size tracking.
- Plex Sans Condensed is a candidate for dense table headers or narrow numeric columns on laptops. This is untested here.
- Mono for SKUs, barcodes and IDs at 12–13px is consistent with Carbon. For amounts, use Plex Sans with tabular figures rather than Mono: Mono has wider figures and an un-financial look.

### Gaps
- **Whether Plex Sans's default figures are tabular or proportional, and whether it ships `tnum` or `pnum`, could not be verified.** No Plex Sans font file was found locally to inspect: the repo has only @fontsource/ibm-plex-mono, and how Sans is loaded was not traced. Searches returned nothing specific. Recommendation regardless: set `font-variant-numeric: tabular-nums` on all money and quantity columns and check the digit widths in devtools.
- No IBM statement found on Plex optical behaviour at 11–13px or on x-height values.

## 5. Readability evidence: minimum sizes, measure, viewing distance

### Takeaway
Product systems do not use body text below 12px. 12 is reserved for labels, captions and helpers, and 14 is the working size. The 45–90 character measure rule applies to **prose**, so cap paragraphs, help text and notes at about 70–80ch. **Do not** cap tables, which should use the available width. Screens are read from farther away than print, which is why web body guidance is 15–25px for *reading*. Operational UIs deliberately sit below that, at 13–14, because they are scanned, not read.

### Cited Findings
- Line length: "Aim for an average line length of 45–90 characters, including spaces," or 2–3 alphabets per line — [Butterick, Line length](https://practicaltypography.com/line-length.html)
- Web body text: "15–25 pixels." Print: 10–12pt. Rationale: "we typically read screens from further away than we read printed material, so larger point sizes help compensate" — [Butterick, Point size](https://practicaltypography.com/point-size.html)
- Primer: keep lines "around 80 characters or less" and left-align — [Primer typography foundations](https://primer.style/foundations/typography)
- Fluent: left-align LTR text and don't right-align long blocks — [Fluent 2 typography](https://fluent2.microsoft.design/typography)
- Smallest sizes in use: Carbon 12px (label, helper, legal), Atlassian 12px (body.small, used "sparingly"; heading.xxsmall for fine print), Fluent 10px (Caption 2) but 12px as the standard caption, Polaris 11px (smallest token) with 13px body — [Carbon](https://carbondesignsystem.com/elements/typography/type-sets/), [Atlassian](https://atlassian.design/foundations/typography), [Fluent 2](https://fluent2.microsoft.design/typography), [Polaris font tokens](https://polaris-react.shopify.com/tokens/font)
- Desktop reading distance in ergonomic requirements is commonly taken as 50cm, with characters subtending 20–22 arcmin measured on capitals — [Articulate community (secondary)](https://community.articulate.com/discussions/rise-360/ergonomic-requirements); [KSU ergonomics (secondary)](https://faculty.ksu.edu.sa/sites/default/files/4%20-%20Chapter%204%20-%20Vision%20-%20part%202%20%28AMS%20May01_24%29.pdf)

### Inferences
- Floor: 12px for meta and labels only, in a muted but still 4.5:1 colour. Nothing smaller. With a mostly non-designer staff audience on store hardware, avoid Fluent's 10px and Polaris's 11px tiers.
- Tables: use 13 or 14px, tabular numerals, right-aligned numbers, and full width. On 1920–2560px, extra width should become more columns or a detail rail. On 1280–1440px, hide low-priority columns rather than shrink type.
- Prose areas (notes, help, descriptions, empty states, confirmation text) should be capped at `max-width: 70–80ch` even on wide screens. Fill the rest of the row with another panel rather than stretching paragraphs.
- Laptop vs monitor: at default OS scaling, a 14" laptop CSS pixel is physically about the same size as or slightly smaller than a 24" monitor's (with 125–150% scaling). The laptop is viewed nearer (about 40–50cm vs 60–70cm), so angular size is about equal. This supports **the same type tokens at both widths**, with layout changing rather than type.

### Gaps
- I found no rigorous study with a single "minimum font size for sustained work" number for UI. NN/g's guidance tested 3–4mm glance text in a car-UI context, not desktop data tables.
- No source quantified 24" vs 27" vs 14" viewing distances directly. The numbers above are standard ergonomics assumptions.
