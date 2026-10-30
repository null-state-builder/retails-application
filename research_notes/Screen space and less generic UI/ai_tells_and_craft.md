# AI-generated UI tells and what crafted operational software does instead

Scope note: these notes cover named tells (visual and copy) and the counter-moves used by crafted products, written for the KDPS design system: IBM Plex Sans, warm paper background, navy primary, rust accent, red CTA, 12px-radius hairline cards, pastel pill chips, KPI tiles, sentence-case labels. Source quality varies a lot. The strongest primary sources are Anthropic's published `frontend-design` skill text, Linear's own redesign posts, Vercel's Web Interface Guidelines and Matt Ström-Awn's density essay. Most "AI slop" listicles are blogs or vendor content, so treat them as practitioner opinion rather than research.

## 1. What do designers and critics name as the tells of AI-generated / shadcn / vibe-coded UI?

### Takeaway
There are two layers of tells. The first-order "slop" layer is purple/indigo gradients, Inter-only type, three identical rounded cards, a centred hero, emoji for icons and the same fade-up on everything. The second-order "tasteful default" layer is what a more careful model produces, and it fits KDPS closely: a warm cream background with a terracotta/clay accent, a broadsheet look with hairline rules, the SaaS card kit with one radius and one soft shadow everywhere, and template chrome (all-caps eyebrows, "A · B · C" meta strings, monospace labels, "→" on buttons). Anthropic's own guidance says this calibration list describes where generated design "clusters". The common root problem is uniformity and choices nobody made, not any single element.

### Cited Findings

**Anthropic's published calibration list (primary source: the `frontend-design` skill text, authors listed as Prithvi Rajasekaran and Alexander Bricken)**
- The skill says AI-generated design "right now clusters around some traits":
  1. a warm cream background (near #F4F1EA) with a high-contrast serif display and a terracotta or warm-clay accent (often near #D97757). The skill notes this is Anthropic's own Claude accent, so on a client brief it reads as a tell.
  2. a near-black background with one bright acid-green or vermilion accent.
  3. a broadsheet layout with hairline rules, zero border-radius and dense newspaper-like columns.
  4. the SaaS card kit: content chopped into identical rounded cards, one border-radius on everything regardless of hierarchy, the same soft grey shadow (rgba(0,0,0,.1)) under each, and gradient washes as decoration.
  5. template chrome that appears whatever the subject: tracked-out ALL-CAPS eyebrow labels, meta strings joined with middle dots, "WORD — fragment" labels, tinted near-black (#0B0B0B, #111) used as black, a monospace face for small data labels, and "→" appended to links and buttons.
  — [Anthropic frontend-design skill (claude-plugins-official, local copy of SKILL.md); community mirror](https://agenticskills.io/skills/frontend-design)
- The same skill says these traits "are defaults rather than choices, and they appear regardless of subject". It also says the brief's own words win when the brief asks for one of these looks. — [frontend-design skill](https://agenticskills.io/skills/frontend-design)
- It names these typographic tells: accenting one word in a headline, all-caps labels, and unnecessary labels above content. — [frontend-design skill](https://agenticskills.io/skills/frontend-design)
- It warns about numbered markers (01/02/03) used for content that is not a sequence. — [frontend-design skill](https://agenticskills.io/skills/frontend-design)
- On motion: "fade-and-slide-up entrances on each section and hover transitions on every card are the generic default and read as AI-generated." — [frontend-design skill](https://agenticskills.io/skills/frontend-design)
- On the hero: "a big number with a small label, supporting stats, and a gradient accent is the default treatment." That is the KPI-tile pattern. — [frontend-design skill](https://agenticskills.io/skills/frontend-design)
- An earlier version, as summarised by third parties, named overused fonts (Inter, Roboto, Arial), "purple gradients on white backgrounds" and "cookie-cutter" layouts. — [Medium summary by N. Porter](https://medium.com/@porter.nicholas/anthropic-skills-marketplace-the-anti-ai-slop-ui-design-skill-a572d0cfef4f)

**Community audit catalogue (avoid-ai-design, a 67-entry catalogue with a scanner)**
- It lists "second-order 'tasteful' defaults": cream + terracotta accent, near-black + acid green/vermilion, "broadsheet cosplay" hairline rules, SaaS card kit, tracked all-caps eyebrows, mono/all-caps meta labels, "A · B · C" strings, one accented headline word, decorative 01/02/03, fake window dots, an emerald fallback accent and mono chrome. — [avoid-ai-design (GitHub)](https://github.com/funboy322/avoid-ai-design)
- Typography: Inter everywhere. There is also a "tasteful free font" set (Space Grotesk, Geist, Instrument Serif, Fraunces). — [avoid-ai-design](https://github.com/funboy322/avoid-ai-design)
- Colour: purple/indigo gradient, gradient headline text, an untouched shadcn palette, "one hue pretending to be two", and muted text below WCAG AA. — [avoid-ai-design](https://github.com/funboy322/avoid-ai-design)
- Components: `rounded-2xl shadow-lg` on everything, reflexive glass/backdrop-blur, icons in rounded squares, a "worn Lucide icon set", the sparkle icon as the AI symbol, and placeholder avatars. — [avoid-ai-design](https://github.com/funboy322/avoid-ai-design)
- Motion: the same fade-up on every section, bounce easing, count-up stats, and animation that ignores reduced motion. — [avoid-ai-design](https://github.com/funboy322/avoid-ai-design)
- Its own caveat: a finding does not prove AI made the site, because "designers shipped rounded cards long before LLMs", and much of it is Tailwind and shadcn as installed. — [avoid-ai-design](https://github.com/funboy322/avoid-ai-design) (via search summary)

**Designer essays and blogs (2025–2026)**
- Kosta Canatselis (23 Jul 2026) names seven tells:
  1. "Inter paired with a system fallback and nothing else."
  2. A purple-to-blue gradient used in place of a colour system.
  3. Generic headlines ("Build the future of work").
  4. Uniform everything: identical padding, radius and card height.
  5. Only the happy path is designed.
  6. Absent or identical motion.
  7. Everything visible at once.

  He also flags a "left-edge coloured line with a curve on panels". — [Mania Design / HEY World](https://www.mania.design/blog/spot-the-slop-a-ui-designers-guide-to-fixing-ai-defaults/)
- 925Studios names Inter "when nobody chose it", the indigo-to-purple gradient (traced to Tailwind's indigo-500), three rounded cards in a row, a dark hero with a gradient button and soft-shadowed cards, thin-line interchangeable icons, weightless headlines ("Build faster. Ship smarter.") and the "Get Started" CTA. — [925Studios](https://www.925studios.co/blog/ai-slop-design-tells)
- Several blogs list a violet-to-indigo primary, three identical feature cards, Inter only, emoji in place of icons, a centred hero with one CTA, and nested cards inside cards. They explain the sameness as models returning "the median" of training data, much of which is Tailwind defaults since 2019. — [DEV: The Purple Gradient Problem](https://dev.to/james_anderson_h/the-purple-gradient-problem-why-ai-ui-all-looks-alike-and-how-to-fix-it-3j65); [vibecodekit](https://vibecodekit.dev/ai-slop-design)
- One post describes the "shadcn look": too many apps share the same buttons, cards, spacing and typography. — [search summary of shadcn-related posts; abduzeedo](https://abduzeedo.com/node/89243)
- A widely repeated anecdote says Adam Wathan apologised in Aug 2025 for making Tailwind UI buttons `bg-indigo-500`. It was found only in secondary blogs and is **unverified**. — [925Studios](https://www.925studios.co/blog/ai-slop-web-design-guide)

**Dashboard-specific tells**
- The "equal-3 KPI grid" (three identical cards, each with a label, a big number and a small % delta) is called a tell, because "operators never read three KPIs equally". — [ux-skill wiki, L. Junaidy](https://github.com/Laith0003/ux-skill/wiki/How-to-design-a-dashboard-with-Claude-Code)
- The same source flags every metric in its own card (16px radius, soft shadow, 24px padding) as "a telltale sign". It says real data mostly lives in flat tables or inline rows. — [ux-skill wiki](https://github.com/Laith0003/ux-skill/wiki/How-to-design-a-dashboard-with-Claude-Code)
- It also flags proportional numerals that don't align in columns, and gradient state pills that read "as marketing, not telemetry". — [ux-skill wiki](https://github.com/Laith0003/ux-skill/wiki/How-to-design-a-dashboard-with-Claude-Code)
- Counterpoint: a summary KPI row at the top is a legitimate "summary first, detail later" pattern. The critique is about equal weighting and decoration, not the row itself. — [search summary of KPI-card guides](https://gallery.fanruan.com/kpi-card-example)

### Inferences
- **KDPS fits several second-order tells at once.** A warm paper background with a rust accent is cluster #1 in Anthropic's list, minus the serif display. Hairline borders on everything lean toward cluster #3 and the card kit (#4). 12px-radius cards everywhere with KPI tiles is cluster #4 plus the "big number + small label" default. That probably explains why the owner says it "looks a lot AI" even though it avoids purple and Inter.
- The owner wants to keep the tokens. The highest-leverage changes are therefore about structure rather than palette:
  - stop boxing everything
  - vary radius and weight by hierarchy
  - stop using rust as a decorative accent and give it one job
  - kill pastel pill soup
  - replace the equal KPI row with a hierarchy (one lead figure plus a sentence or inline rows)
- IBM Plex Sans does not appear in any tell list I found (Inter, Roboto, Geist, Space Grotesk and similar do). On its own it is probably not a tell. Its neutral use is what reads as default.

### Gaps
- I found no primary designer essay that names "pastel pill status chips" or "lucide icon on every nav item" as tells in exactly those words. The closest are "worn Lucide icon set" and "icon in a rounded square" in avoid-ai-design, and "gradient state pill" in the ux-skill wiki. A 925Studios fetch returned extra unsourced "general guidance" on badges, icons and "Welcome back". I excluded it as unattributed.
- I could not reach a Hacker News or X thread with a primary critique of the "shadcn look". The search returned mostly vendor pages.
- I found no systematic or empirical study. Every tell list is practitioner opinion.

## 2. What do crafted operational products do instead?

### Takeaway
Crafted tools reduce chrome rather than decorate it:
- fewer, softer separators ("structure felt not seen")
- dimmer navigation so content wins
- fewer, smaller icons and no coloured icon backgrounds
- neutral palettes generated from a few inputs, with colour kept for meaning
- strict alignment of every label, icon and button
- tabular numerals for anything compared
- density measured as value per pixel and per second, not as emptiness

Distinctiveness comes from a few deliberate decisions applied consistently, not from adding ornament.

### Cited Findings

**Linear (primary sources)**
- In the 2024 redesign (Karri Saarinen, Mar 2024), themes moved to LCH and were generated from three inputs (base, accent, contrast) instead of 98 hand-set variables. Blue in chrome was deliberately limited for "a more neutral, timeless look". — [Linear: How we redesigned the Linear UI](https://linear.app/now/how-we-redesigned-the-linear-ui)
- Sidebar and tabs were condensed toward Apple-standard density. Labels, icons and buttons were aligned "vertically and horizontally", which is something users "feel after a few minutes" rather than notice. — [Linear](https://linear.app/now/how-we-redesigned-the-linear-ui)
- Linear uses Inter Display for headings and Inter for body text, so Inter itself is not the problem when it is a deliberate pairing. — [Linear](https://linear.app/now/how-we-redesigned-the-linear-ui)
- In the later refresh, the guiding principle is "Don't compete for attention you haven't earned". The sidebar is "a few notches dimmer" so content takes precedence. — [Linear: Behind the latest design refresh](https://linear.app/now/behind-the-latest-design-refresh)
- Separators had "quietly proliferated". Linear cut their number and softened their contrast so structure is "felt not seen". — [Linear refresh](https://linear.app/now/behind-the-latest-design-refresh)
- Icons were reduced and scaled down, and "colored team icon backgrounds" were removed as unnecessary. — [Linear refresh](https://linear.app/now/behind-the-latest-design-refresh)
- The palette moved from cool blue-grey to a warmer, less saturated grey. The team warned that "go too warm, though, and the interface risks looking muddy." Success was measured by most people not noticing. — [Linear refresh](https://linear.app/now/behind-the-latest-design-refresh)

**Vercel Web Interface Guidelines (primary)**
- Numbers that will be compared use `font-variant-numeric: tabular-nums` or a mono face. — [Vercel Web Interface Guidelines](https://vercel.com/design/guidelines)
- Every element aligns deliberately to a grid, baseline, edge or optical centre, with "±1px" optical adjustment. — [Vercel guidelines](https://vercel.com/design/guidelines)
- Nested radii: a child's radius is ≤ the parent's and concentric. Borders, shadows and text are tinted toward the background hue on non-neutral backgrounds. — [Vercel guidelines](https://vercel.com/design/guidelines)
- Status is never colour alone and always has a text label. Hover, active and focus states have more contrast than rest. — [Vercel guidelines](https://vercel.com/design/guidelines)
- Design the empty, sparse, dense and error states. — [Vercel guidelines](https://vercel.com/design/guidelines)
- Typographic details: the real ellipsis character (…), curly quotes, non-breaking spaces between numbers and units, and tidy rag. — [Vercel guidelines](https://vercel.com/design/guidelines)

**Density theory (Matt Ström-Awn, c. 2024)**
- "UI density is the value a user gets from the interface divided by the time and space the interface occupies." He separates visual, information, design and temporal density. — [Matt Ström-Awn: UI Density](https://mattstromawn.com/writing/ui-density)
- He adapts Tufte's "Every bit of ink on a graphic requires reason" to screens, while accepting that separators and signposts are necessary ink. — [UI Density](https://mattstromawn.com/writing/ui-density)
- Proximity (Gestalt) groups related items without boxes. A tight grid reads as related, and ample space separates groups. — [UI Density](https://mattstromawn.com/writing/ui-density)
- On temporal density: under 100ms feels instant, and animation there can make an app feel slower. Bloomberg Terminal is praised for near-instant loads. — [UI Density](https://mattstromawn.com/writing/ui-density)

**Bloomberg Terminal**
- Bloomberg treats "concealing complexity" as its UX approach: it keeps a continuous experience across thousands of functions and adjusts how growing data volumes are shown so users are not overwhelmed. — [Bloomberg Tech: How Bloomberg Terminal UX designers conceal complexity](https://www.bloomberg.com/company/stories/how-bloomberg-terminal-ux-designers-conceal-complexity)
- It is cited as the canonical example of visual density: sparklines, volume breakdowns, dense tables and headlines on one screen. — [UI Density](https://mattstromawn.com/writing/ui-density)

**Stripe Dashboard (secondary sources only)**
- Stripe is cited as a best-in-class example of data density, with compact tables and nested information. — [AdminLTE: 25 dashboard examples](https://adminlte.io/blog/25-best-dashboard-design-examples-for-2026-real-products-principles/)
- Stripe uses one licensed typeface (Söhne, Klim) plus Source Code Pro for code. The product surface is mostly tables and key-value pairs at 13–14px. — [fontalternatives: Stripe Söhne](https://fontalternatives.com/inspiration/stripe-sohne/)
- Tabular figures in money cells are claimed as Stripe's "quiet financial-data signal". This comes from a reverse-engineered or AI-generated guide and is **not documented by Stripe**. — [shadcn.io Stripe design reference](https://www.shadcn.io/design/stripe/raw)

**Anthropic's counter-moves (primary)**
- "Visual structure is information": outlines, borders, numbering, eyebrows and dividers should encode information, not decorate. — [frontend-design skill](https://agenticskills.io/skills/frontend-design)
- "Spend your boldness in one place. Let one element be the memorable thing, keep everything around it quiet and disciplined." — [frontend-design skill](https://agenticskills.io/skills/frontend-design)
- Use one or two families, and if two, make them clearly distinct. Use a deliberate type scale with intentional weights, widths and spacing, and treat type as an active design element. — [frontend-design skill](https://agenticskills.io/skills/frontend-design)
- Motion should answer the user's action (opening, expanding, confirming). Prefer one orchestrated moment to scattered effects. — [frontend-design skill](https://agenticskills.io/skills/frontend-design)
- "Uniform everything" is fixed with deliberate variation: the main element gets more size, proximity and weight, and supporting elements recede. — [Mania Design](https://www.mania.design/blog/spot-the-slop-a-ui-designers-guide-to-fixing-ai-defaults/)
- Colour by function: each colour signals an action, a warning or success. Rename tokens by purpose. — [Mania Design](https://www.mania.design/blog/spot-the-slop-a-ui-designers-guide-to-fixing-ai-defaults/); 4–6 colours with assigned roles — [avoid-ai-design](https://github.com/funboy322/avoid-ai-design)

### Inferences
These are concrete counter-moves for KDPS that keep its tokens, synthesised from the above:
- **Rules over boxes.** Replace bordered 12px cards for page sections with unboxed sections separated by spacing and one softened rule. Keep a real container only where an object is actually separate: a modal, a bill or receipt, a draggable item. Fewer, lower-contrast separators, following Linear.
- **Radius by hierarchy, not one value.** For example: 0–2px on table rows and inputs inside a surface, a larger radius only on floating layers. Nested radii should be concentric (Vercel).
- **Navy is the system colour, red means only "this commits money/stock" or destructive, rust gets one named job or goes.** The current palette has three strong hues (navy, rust, red). Give each one a single semantic role, Linear-style colour restraint.
- **Status as text plus a small mark, not pastel pills.** In dense tables, use a coloured dot or glyph with a plain word. Keep a filled chip only for the rare state that needs action.
- **Replace equal KPI tiles with a hierarchy.** Show one lead figure (e.g. today's net sales) large in tabular Plex. Secondary figures go inline in a quiet row or a sentence, with no box per metric.
- **Tabular numerals everywhere money or quantity is compared.** IBM Plex Sans supports `tnum`. Right-align amounts. Use "₹" and Indian digit grouping (1,23,456) consistently.
- **Dim the chrome.** Make the sidebar a few notches lower in contrast than content, with fewer icons, smaller icons and no tinted icon backgrounds.
- **Alignment audit.** Labels, icons and buttons should sit on shared vertical and horizontal lines (Linear's "feel after a few minutes").
- **Motion only in response to actions:** a scan being added to a bill, a sale being committed. No section fade-ups.

### Gaps
- No primary-source design write-ups were retrieved for Raycast, Things, Arc, Height, Superhuman, Retool's newer UI, Teenage Engineering or Apple Numbers/Finder in this pass. I hit the tool-call budget. Claims about them would come from general knowledge and are not cited here.
- Stripe has no first-party design post on dashboard tables or typography that I could find. The Stripe facts above are third-party.
- Rauno Freiberg, Paco Coursey, Emil Kowalski and Brian Lovin essays were not fetched. The Vercel Web Interface Guidelines are associated with that circle but carry no byline on the page.

## 3. How do brands make an internal tool feel "theirs"?

### Takeaway
The repeated advice is to derive the design direction from the business's own materials, artifacts and vocabulary rather than from a "look". For a retailer, the natural artifacts are the receipt or bill, tags and price tickets, the brand mark, and the language of the shop floor. Real identity work shows that one artifact (here, a receipt) can drive type choice, numerals and wordmark details across every touchpoint.

### Cited Findings
- Anthropic's skill says the subject's "industry, subject matter, materials, and vernacular are where distinctive visual choices come from". It advises opening with "the most characteristic thing in the subject's world". — [frontend-design skill](https://agenticskills.io/skills/frontend-design)
- avoid-ai-design's fix: "Derive the direction from the product's own materials and artifacts instead of a look". Write it into a DESIGN.md with a justification for each move. Its demo opens with the product's most characteristic artifact, a cohort table. — [avoid-ai-design](https://github.com/funboy322/avoid-ai-design)
- 925Studios suggests building a palette "from something true about the product", and icons that illustrate this product specifically. — [925Studios](https://www.925studios.co/blog/ai-slop-design-tells)
- Retail case (A.D.B. Lips Design Bureau for NEW ØRD3R, Nov 2020): the receipt was the starting point. They chose Favorit Mono because it "reminded us of those typical receipt typefaces, but in a very modern way". A sibling face with one custom "R" became the signature. The wordmark swaps "E" for "3", nodding to numbers on receipts and cycling specs. The palette is black and white so content and photos carry colour. — [The Brand Identity: NEW ØRD3R](https://the-brandidentity.com/feed/d-b-take-inspiration-receipts-retail-identity-cycling-store-new-ord3r/)
- Practical constraint: a POS logo must survive thermal printing, since "fine detail and soft gradients turn to mud on receipt paper". — [DevDiggers: brand the POS terminal](https://docs.devdiggers.com/multipos-point-of-sale-for-woocommerce/tutorials/how-to-brand-the-pos-terminal/)
- Consistency is part of identity: drift in font, colour, radius or nav from page to page is itself a tell. Build every page from one DESIGN.md "contract". — [avoid-ai-design](https://github.com/funboy322/avoid-ai-design)

### Inferences
These are KDPS-specific identity moves; they are proposals, not sourced practice:
- **Receipt and bill as the signature artifact.** The Till and any bill or return preview could render as an actual KDPS bill: a narrow column, a dotted or perforated rule, right-aligned tabular amounts, the store name and GSTIN header, totals in a heavier weight. Use it only there, so the motif carries meaning (Anthropic: "spend your boldness in one place").
- **Price-tag / swing-tag details** for product and SKU rows: size, colour and MRP set like a garment tag. Fashion retail has its own vocabulary.
- **Domain vocabulary:** bill, MRP, size run, alteration, exchange, GRN, stock transfer, store codes (e.g. the Ratu Road store). Use it instead of generic SaaS nouns ("items", "records", "entities").
- **Brand mark used sparingly:** the KDPS mark in the app chrome and on printed or exported documents, not a generic logo square.
- **Avoid a monospace receipt font as default chrome.** "Mono chrome" and mono data labels are themselves listed tells. If a receipt face is used, confine it to the bill artifact.

### Gaps
- No published case study was found of an internal store-operations or POS tool deliberately branded around its retailer's artifacts. The NEW ØRD3R example is a physical and print identity, not software.
- No sources were found on Indian retail-specific UI conventions (₹ formatting, lakh/crore grouping in dense tables) from a design perspective.

## 4. Copywriting tells of AI UI text, and how to write terse operational microcopy

### Takeaway
Copy tells are:
- weightless headlines ("Build faster. Ship smarter.", "Build the future of work")
- marketing verbs ("Elevate", "Seamless", "Powerful", "Unlock")
- generic CTAs ("Get Started", "Continue", "Submit")
- arrows welded to buttons
- labels above everything
- apologetic or vague errors

Crafted microcopy:
- names the exact action ("Save API Key", "Save changes")
- keeps one verb through the flow (Publish → "Published")
- uses numerals for counts
- tells users how to exit an error
- names things in the user's vocabulary, not the system's

### Cited Findings
- Stock openers and vocabulary: "Elevate your workflow", "Elevate", "Seamless", "Powerful". Generic CTAs ("Get Started"). Arrows welded to CTAs. — [avoid-ai-design](https://github.com/funboy322/avoid-ai-design)
- Weightless headlines: "Build faster. Ship smarter." — [925Studios](https://www.925studios.co/blog/ai-slop-design-tells); "Build the future of work" "means nothing". Rewrite in the founder's real voice until they'd say it aloud. — [Mania Design](https://www.mania.design/blog/spot-the-slop-a-ui-designers-guide-to-fixing-ai-defaults/)
- Anthropic's guidance:
  - "Words appear in a design for one reason: to make it easier to understand and use."
  - Name things by what users understand, not how the system is built ("a user manages notifications, not webhook config").
  - Describe rather than sell.
  - A CTA says exactly what happens: "Save changes," not "Submit."
  - An action keeps the same name through the flow: "Publish" → toast "Published."
  - "Errors don't apologize, and they are never vague."
  - "An empty screen is an invitation to act."
  - Plain verbs, sentence case, no filler. "Let each written element do exactly one job."

  — [frontend-design skill](https://agenticskills.io/skills/frontend-design)
- Template chrome in copy: "A · B · C" meta strings, "WORD — fragment" labels, "→" on buttons. — [frontend-design skill](https://agenticskills.io/skills/frontend-design)
- Vercel's rules:
  - active voice ("Install the CLI", not "The CLI will be installed")
  - "Use numerals for counts" ("8 deployments")
  - "Error messages guide the exit"
  - "Labels are clear & specific" ("Save API Key", not "Continue")
  - loading states and follow-up menu items end with "…"
  - "Prefer `&` over `and`"

  — [Vercel Web Interface Guidelines](https://vercel.com/design/guidelines)
- **Conflict on capitalisation:** Vercel uses Title Case for headings and buttons (Chicago) and sentence case only for marketing — [Vercel guidelines](https://vercel.com/design/guidelines). Anthropic's skill recommends sentence case — [frontend-design skill](https://agenticskills.io/skills/frontend-design). Both are legitimate. The point is to pick one and apply it consistently. KDPS's sentence case is not a tell in itself.
- Missing states: the happy path only. Write error messages in plain language and design empty and loading states on purpose. — [Mania Design](https://www.mania.design/blog/spot-the-slop-a-ui-designers-guide-to-fixing-ai-defaults/); [Vercel guidelines](https://vercel.com/design/guidelines)

### Inferences
These are KDPS microcopy rules derived from the above:
- Don't use greeting heroes ("Welcome back, Anand"). Lead with the state of the store: "Ratu Road · 14 bills today · ₹42,380" (or as a sentence, to avoid the middle-dot meta-string tell).
- Buttons name the business action and object: "Take payment", "Print bill", "Post GRN", "Save return". The result toast reuses the verb: "Bill printed", "GRN posted".
- Errors say what happened and how to exit, with no apology: "Barcode 8901… isn't in this store's stock. Check the size, or search by style code."
- Empty states direct the next action: "No bills yet today. Scan an item to start one."
- Use numerals and ₹ always, and Indian digit grouping.
- Remove labels and eyebrows that only restate the heading, and remove "→" from buttons.

### Gaps
- I could not retrieve a primary source that explicitly names "Welcome back" heroes or emoji in UI copy as an AI tell. It is widely assumed, but the only text I found was unattributed tool-generated guidance, so I excluded it.
- No primary Linear, Stripe or Superhuman content-design guidelines were fetched in this pass.
