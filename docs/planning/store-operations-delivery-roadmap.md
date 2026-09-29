# Store Operations Delivery Roadmap

**Project:** RetailsOps — KDPS first deployment\
**Created:** 29 September 2026\
**Purpose:** A discussion-by-discussion delivery checklist based on the decisions made in this conversation.\
**Active item:** SO-03 — Unified access and consolidation plan (Implementing; current verification and remaining authority/migration gates are recorded in access evidence, 30 September 2026).

## 1. The delivery order we agreed

1. **Run the application locally.** Completed for startup and login.
2. **Complete the store-operations PRD.** This is the immediate product scope, including the shared foundations needed to operate a store.
3. **Pilot it in one store.** Train people, use real reconciled data, observe daily operations and resolve issues until the store operates reliably.
4. **Complete the remaining main PRD.** Extend the same application with the remaining ERP capabilities.

The first product milestone is completion of the store-operations scope. It is **not limited to P1**. The P1–P4 sequence inside that PRD remains the delivery sequence within this milestone. A narrower pilot scope would require an explicit product-owner decision; this roadmap does not silently defer P3 or P4 until after the pilot.

The [main PRD](../prd.md) continues to govern shared architecture, data integrity, security, business rules and release obligations. The [store-operations PRD](../store-operations-prd.md) supplies the immediate feature scope. This roadmap sequences work; it does not replace either PRD, approve tax treatment, close policy gates or redefine acceptance.

**One application, one supported implementation per workflow.** Consolidation starts with the foundations and continues with every completed workflow. By store-operations acceptance, superseded implementations, duplicate screens, compatibility bridges and obsolete demonstration records must be retired. Required business history must be preserved. A permanent legacy/new split is not the intended product.

## 2. Where we stand now

| Area | Current evidence |
| --- | --- |
| Local application | Running; the user confirms they can log in and navigate screens. |
| Runtime | Django backend, React/Vite frontend, background worker and isolated PostgreSQL 17.11 database were started locally. |
| Startup checks | Fresh-database migrations, foundation seed, Django system check, frontend build and Owner login/dashboard requests passed during setup. |
| Dependency baseline | Reconstructed manifests and committed lockfiles install into empty environments without lockfile changes; exact versions and Linux limits are in the [SO-02 evidence](so-02-engineering-baseline-evidence.md). |
| TypeScript | The initial strict check reported **192 errors**; SO-02 repaired them without relaxing the prescribed options. The current strict check passes. |
| Data | Local setup uses synthetic accounts and masters. It is not a real-store opening or migration. |
| P1 implementation | Broad source coverage exists; individual workflows still require requirement-level verification. |
| P2–P4 implementation | Partial or missing work remains; policy and provider dependencies remain open. |
| Acceptance | Startup and screen access do not establish stock, billing, cash, recovery or store-operation acceptance. |
| SO-03 access consolidation | The preserved, uncommitted working tree has isolated backend/frontend/API/browser and inventory baseline evidence. Staff retirement, online sale replay, delayed delivery, tenant-wide policy reads, whole-site ageing and arrival counter assignment received further access checks. Pending resource traces, incomplete identity propagation, approval policy consolidation, session field-union consumers, real-tenant reconciliation and OQ-28 decisions keep SO-03 open. See the [access evidence](so-03-access-evidence.md) and [implementation review](so-03-interrupted-implementation-review.md). |

Local setup and restart instructions are in the [README](../../README.md). Existing implementation assessments are provisional until the relevant delivery item is examined and tested.

## 3. How to use this document

Discuss the items in order, referring to their stable IDs: **“Let's discuss SO-07.”** The table is the delivery queue; the item descriptions identify the outcome, questions and completion evidence for that conversation.

For each item:

1. Inspect the current implementation and map the relevant PRD requirements.
2. Walk through the operator's journey, roles, data, exceptions and offline behaviour.
3. Resolve the decisions needed for that item and record the agreed implementation work.
4. Implement, consolidate and verify the workflow.
5. Record what passed, what remains gated and the evidence before updating its status.

This document does not authorise implementing every queued item immediately. Each detailed conversation will establish that item's concrete work. Routine implementation details can then be handled within the agreed scope.

The [SO-01 requirement register](store-operations-requirement-register.md) is the requirement-level map for subsequent discussions. Its SO-01 work phases are documentation steps; the store PRD's P1–P4 phases remain the product delivery sequence.

Use these statuses: **Queued → Discussing → Implementing → Verifying → Accepted**. Use **Waiting on decision** where necessary, naming the decision and affected behaviour. “Implemented, awaiting activation” may be recorded separately, but is not “Accepted for real-store use.”

Acceptance checks, audit, permissions, feature controls and safe migration apply throughout. They are not postponed until the final testing item. Policy discussions may be brought forward when they block an earlier dependency.

## 4. Delivery queue

| Order | ID | Item | Stage | Status |
| --- | --- | --- | --- | --- |
| 00 | SO-00 | Local startup and login | Foundation | Completed — limited to startup/login |
| 01 | SO-01 | Requirement coverage and decision reconciliation | Foundation | Accepted — documented coverage and decisions, 29 September 2026 |
| 02 | SO-02 | Reproducible build and engineering baseline | Foundation | Accepted, 29 September 2026 — [Mac, Linux and hosted CI evidence](so-02-engineering-baseline-evidence.md) |
| 03 | SO-03 | Unified access and consolidation plan | Foundation | Implementing — authority coverage and real-tenant migration remain blocked; [evidence](so-03-access-evidence.md) |
| 04 | SO-04 | Organisation, product masters and opening-stock workflow | Foundation / P1 | Queued |
| 05 | SO-05 | Staff, assignments and manager authority | P1 | Queued |
| 06 | SO-06 | Brand terms, tax configuration and offers | P1 | Queued |
| 07 | SO-07 | Booking, receiving and PT to sellable stock | P1 | Queued |
| 08 | SO-08 | Inventory, transfers, counts and stock exceptions | P1 | Queued |
| 09 | SO-09 | Counter billing, exchanges and offline operation | P1 | Queued |
| 10 | SO-10 | Customer records, consent and rights | P1 | Queued |
| 11 | SO-11 | Reservations, special orders and alterations | P1 | Queued |
| 12 | SO-12 | Cash close, petty cash and outright payables | P1 | Queued |
| 13 | SO-13 | Buying support and brand reporting/claims | P1 | Queued |
| 14 | SO-14 | Reports, approvals, alerts, audit and daily checklists | P1 | Queued |
| 15 | SO-15 | P1 integration and consolidation checkpoint | P1 | Queued |
| 16 | SO-16 | Manual IRN, payments, settlements and GSTR-2B | P2 | Queued |
| 17 | SO-17 | Manual customer messaging and back-in-stock | P2 | Queued |
| 18 | SO-18 | Attendance, rosters, incentives and payroll export | P2 | Queued |
| 19 | SO-19 | Connect and verify external providers | P3 | Queued |
| 20 | SO-20 | Complete policy-dependent store features | P4 | Queued |
| 21 | SO-21 | Full store-operations acceptance and release proofs | Release | Queued |
| 22 | SO-22 | Prepare the real pilot store | Pilot | Queued |
| 23 | SO-23 | Operate and stabilise the one-store pilot | Pilot | Queued |
| 24 | SO-24 | Plan the remaining main-PRD delivery | Full ERP | Queued |

## 5. Discussion items

### SO-00 — Local startup and login

- **Outcome achieved:** The application runs locally and the user can sign in and inspect screens.
- **Evidence:** Local PostgreSQL 17.11, successful migrations and foundation seed, backend check, frontend build and browser login/dashboard verification; user confirmation in this conversation.
- **Boundary:** No claim that every screen or store workflow works end to end. The remaining engineering and product work begins below.

### SO-01 — Requirement coverage and decision reconciliation

- **Outcome:** A complete store-requirement register with one delivery owner item for every `ST-` requirement, plus the supporting main-PRD `R-` requirements.
- **Discuss:** For each requirement, what exists, what is partial, what is missing, what is gated and what evidence will establish completion. Confirm provisional phase assignments in store PRD §22.3.
- **Resolve:** Store PRD §34 tensions: offline attendance, online-only billing restrictions, debit-note authority, cash boundaries, accounting build/activation gates and marketing for minors. Also reconcile online credit-note redemption with the main PRD's initial exchange/payment policy, trading-store count policy (OQ-57), and the differing report performance targets.
- **Known corrections:** OQ-54 is answered in the current main PRD; real opening readiness still depends on OQ-29. Current B2B acceptance/printing must be aligned to the pending-IRN requirement. The permission bridge conflicts with the required single permission system.
- **Done when:** Every store requirement and applicable foundation is mapped; contradictions have recorded resolutions or specific named blockers. No gated rule is treated as approved by this roadmap.

**SO-01 work phases and handoff checks** (separate from product P1–P4):

| Phase | Subtasks | Acceptance |
| --- | --- | --- |
| 1. Source baseline | Index all `ST-`/`R-` IDs and sources; record document hierarchy and §22.3 placements. | 65 unique `ST-` and 166 unique `R-` IDs; placements trace to source and are confirmed without implying activation. |
| 2. Assess and assign | Inspect implementation/tests; assign one accountable item to each `ST-`; classify every `R-`, including split portions; define completion proof. | Every `ST-` has one owner, honest implementation/verification status and concrete proof; every `R-` has a disposition. |
| 3. Reconcile | Record §34 and cross-PRD resolutions, known code gaps and named gates. | Each tension has authoritative wording or a blocker naming decision owner, affected behaviour and delivery item. |
| 4. Publish and audit | Publish the register; align both PRDs and this roadmap; validate IDs, owners, links and status claims. | Documents agree, gaps have later SO handoffs, and unsupported verified/approved claims are absent. |

The dated decisions, gate ledger, implementation pointers and completion checks are in the [register](store-operations-requirement-register.md). SO-01 acceptance records a delivery map, not completion of the workflows it maps.

**Discussion record — 29 September 2026:** SO-01 is accepted as the planning foundation. Source inspection found implementation leads and gaps; no requirement-level runtime acceptance was performed here. The register has 65 store rows, each with one accountable delivery item, implementation/verification/activation states and a concrete future completion check. It classifies all 166 main requirements as 89 store, 63 split and 14 SO-24, retaining the later portion of every split requirement. The store PRD §22.3 placements and §34 tensions, plus store credit, OQ-57, report timing, OQ-54/OQ-29, pending IRN and the single permission system, now have dated authoritative wording in both PRDs. All named CA, privacy, migration, commercial, accounting, provider and release gates remain open where specified.

**Checks actually run:** automated source/register ID equality (65/166), exact 65-owner comparison, title and section comparison, row-width checks, local Markdown-link resolution and explicit unverified/activation-hold checks passed. These check documentation integrity only. The first workflow handoffs are SO-03 for permission consolidation, SO-04 for the stale OQ-54 opening block, SO-08 for count freeze, SO-09 for active online store credit and online refusal safety, SO-16 for pending-before-IRN issue, SO-18 for photo-only offline attendance, and SO-21 for release proofs. The [register's gate ledger](store-operations-requirement-register.md#named-decision-and-release-gates) names each remaining decision and its affected item.

### SO-02 — Reproducible build and engineering baseline

- **Outcome:** A repeatable local setup and a trustworthy verification path for subsequent delivery.
- **Discuss:** Reconstructed dependency compatibility; strict TypeScript failures; absent original test tooling; missing app icons/PWA assets; generated API-contract checks; clean setup and restart behaviour.
- **Work:** Fix the baseline without weakening the prescribed checks. Establish the relevant backend, frontend and browser test commands and requirement-linked acceptance evidence. Verify dependency installs from committed lockfiles.
- **Done when:** Clean setup, build, required type checks and baseline checks pass; startup instructions are usable; later items have a repeatable way to demonstrate success and failure cases.
- **Engineering verification:** The [SO-02 evidence record](so-02-engineering-baseline-evidence.md) records the committed code, clean disposable-checkout setup and two complete Mac verification passes, negative probes, a complete manual Linux verification pass and a [successful hosted GitHub Actions run](https://github.com/null-state-builder/retails-application/actions/runs/36547922163). Engineering baseline acceptance is recorded on 29 September 2026. Later SO items still own workflow, supported-device and release proofs.

### SO-03 — Unified access and consolidation plan

- **Outcome:** One permission authority and one target implementation for every workflow.
- **Discuss:** The six initial roles, per-user site/brand scope, step rules, sensitive-field access and separate-person approvals. Identify which existing models, APIs, services and screens will remain and how their data moves.
- **Work:** Unify authentication/session payload and authorisation checks across UI, APIs, search, exports, files and events. Migrate assignments safely. Start removing superseded access bridges once replacement coverage is proven.
- **Done when:** Access decisions have one source of truth, denial tests pass, and every remaining duplicate has an explicit replacement and retirement item. This item starts consolidation; SO-15 and SO-21 verify completion across the delivered workflows.
- **Sources:** Main PRD §4.3 and Appendix B; ST-OPS-5.

**Prerequisite consolidation checkpoint — inventory before further feature work:** Maintain one machine-readable [inventory](so-03-consolidation-inventory.json), its generated [register](so-03-consolidation-register.md) and [dependency map](so-03-dependency-map.md). Classify every discovered route, screen, command, registered job, service/model family, configuration and migration with one accountable owner, target and retirement condition. Trace writers and downstream readers across authorization, data and delivery channels; mark dynamic edges and unverified ownership as explicit gates. Review the interrupted SO-03 patch against this map and correct stale evidence claims. CI must reject inventory drift, new dependencies on retired components and reintroduced legacy permission sources. Complete this checkpoint before resuming access feature implementation; it does not change a public business contract or declare the unfinished access patch accepted.

**Checkpoint record — 29 September 2026:** The inventory, generated map/register, CI guard and [foundation evidence](so-03-foundation-evidence.md) are present in the working tree. The machine manifest and generated register record current discovery totals and individual reference dispositions. The [later access evidence](so-03-access-evidence.md) records the latest isolated checks and the remaining active-path, real-tenant and offline gates. SO-03 remains **Implementing**.

**Workflow retirement rule:** SO-04–SO-14 and SO-16–SO-20 each confirm their mapped boundary, rehearse data migration, move readers and writers to one target, cut over without permanent dual writes, remove obsolete mutations and attach revision-specific evidence. Necessary legacy paths receive bounded safety, integrity and continuity support with an owner and retirement gate. SO-15 checks P1 retirement and SO-21 checks the complete store milestone.

**Decision and work record — 29 September 2026:** The six Appendix B roles are initial configurable defaults. Each effective-dated role assignment carries its own explicit site/brand scope; all-sites/all-brands includes future members, while selected membership remains fixed. Sensitive-field defaults, additive step rules, separate-person approvals, stepped-up access changes, affected-session invalidation and distinct-person review within one working day are agreed. The [SO-03 consolidation register](so-03-consolidation-register.md) maps mounted API and screen families to targets, data movement and retirement owners; the PRD edits record the agreed rules. The scheduled-assignment interface, migration 0022, stock search, protected outbound projection, SBU route removal, optional admin guard, legacy approval callback and selected-brand transfer read scope have been addressed. Staff retirement now ends unified assignments; online sale replay, delayed exports/files/events, till dataset delivery, special-order transfer, tenant-wide settings reads, whole-site ageing and arrival counter assignment have further authority checks. The [access evidence](so-03-access-evidence.md) records current isolated test counts and the read-only proof-tenant report with 17 blocked people. The [implementation review](so-03-interrupted-implementation-review.md) names remaining access gaps, including remaining stable identity propagation, approval responsibility outside unified workflow policy and global field-visibility consumers. Brand-text readers now fail closed without a proven tenant-owned ID; this does not reconcile historical records. Real-tenant reconciliation and affected OQ-28 decisions remain activation gates; later workflow duplicates retain their SO-04–SO-20 owners and SO-15/SO-21 checkpoints.

### SO-04 — Organisation, product masters and opening-stock workflow

- **Outcome:** A store can be configured with valid identities and receive a governed opening balance.
- **Discuss:** Legal entities, GSTINs, sites, store prefixes, bins, brands/vendors, seasons, SKU/barcode identity, HSNs, canonical profiles and approved source files. Define opening reconciliation and rejected-row handling.
- **Work:** Replace the obsolete blanket OQ-54 opening block with the current approved readiness controls, including OQ-29. Preserve true season where known and audited unknown history where justified. Rehearse migration with representative data.
- **Done when:** Synthetic rehearsal reconciles quantities and values to its source; duplicates and invalid rows are handled visibly; approvals and history are preserved. Actual pilot data is loaded under SO-22.
- **Sources:** Main PRD master-data/opening requirements and §14; ST-CMP-3, ST-CMP-5, ST-OPS-5.

### SO-05 — Staff, assignments and manager authority

- **Outcome:** Every sale and approval can be attributed to the correct person at the correct store.
- **Discuss:** Active staff, effective assignments, salesperson identity versus till login, split attribution, individual manager PINs and Store Manager responsibilities.
- **Work:** Complete staff migration without rewriting historical sales attribution. Retire the separate salesperson register. Preserve each manager's accountability; plan the Store Manager role required before multi-store rollout.
- **Done when:** Staff scope and historical attribution survive assignment changes; staff selection and split attribution work at the till; unauthorised or shared override paths are refused.
- **Sources:** ST-HR-1, ST-POS-2; store PRD §29 and Q7.

### SO-06 — Brand terms, tax configuration and offers

- **Outcome:** Versioned, approved rules drive pricing, tax and commercial treatment consistently.
- **Discuss:** Unknown versus confirmed brand models; outright/SOR/consignment/concession terms; HSN and tax versions; discount allocation; bank offers; brand-funded discounts; gift-stock treatment; promotion-services agreements.
- **Work:** Align pure calculations on the till and server, preserve rule versions, and verify independently approved golden cases. Confirm offer return, funding split and simulation against the required historical windows as sales data becomes available.
- **Done when:** Calculations and rule versions reconcile; unknown commercial terms remain explicit; affected activation stays gated until the required sign-offs are recorded.
- **Sources:** ST-CMP-1, ST-CMP-2, ST-CMP-3, ST-CMP-7; ST-BRD-1, ST-BRD-6; ST-OFR-1, ST-OFR-2, ST-OFR-3; store PRD §32.

### SO-07 — Booking, receiving and PT to sellable stock

- **Outcome:** Goods move from source documents and physical receipt into truthful, traceable sellable stock.
- **Discuss:** Booking, arrival, counting, GRN, discrepancies, PT preparation/mapping, approval, labels and separate physical acceptance. Include partial deliveries, late documents, wrong items and human review of supported extraction.
- **Work:** Verify three-way matching and shortage debit-note drafts. Distinguish operational documents from accounting posting that still needs OQ-47 resolution.
- **Done when:** The full journey reconciles source/count/PT totals and values; quarantined or unaccepted goods cannot be sold; interruption and retries do not duplicate stock or numbers; superseded receiving code is retired.
- **Sources:** Main PRD receiving/PT requirements; ST-REC-1, ST-REC-3.

### SO-08 — Inventory, transfers, counts and stock exceptions

- **Outcome:** Store and warehouse stock remain explainable through movements and corrections.
- **Discuss:** Available-to-sell stock, holds, reservations, bins, damage, vendor returns, write-offs/disposal, transfer request/approval/dispatch/receipt, shortages/excesses and return-to-source. Apply the SO-01 decision for OQ-57: freeze store sales during a trading-store count after confirming online that till queues have synced and finalisation has stopped; resume after closure.
- **Work:** Finish broken-size and season-aware ageing alerts, scheduled counts, shrinkage, size-balancing suggestions and the correct transfer documents by GSTIN relationship.
- **Done when:** Movements preserve quantities, frozen values and evidence; discrepancies have owned resolution paths; counting obeys approved trading policy; one supported stock/transfer/count route remains.
- **Sources:** ST-INV-1, ST-INV-2, ST-INV-3, ST-INV-4; ST-TRF-1, ST-TRF-2; supporting main-PRD inventory/transfer requirements.

### SO-09 — Counter billing, exchanges and offline operation

- **Outcome:** The cashier can complete the supported sale and exchange journeys quickly and reliably.
- **Discuss:** Scanning, offers, salesperson splits, tender recording, receipts/reprints, manager overrides, customer display, gift vouchers, original-bill exchanges, return eligibility, document series and online-only actions. Active online store credit is in the complete store milestone; settle its issue/redemption lifecycle and OQ-45 safeguards before activation.
- **Work:** Verify one registered till per store, offline authority/quantity protection, local bill persistence, automatic sync, duplicate handling, till replacement and financial-year transitions. Build the B2B handoff boundary for SO-16; do not leave early invoice printing active where pending IRN is required.
- **Done when:** Ordinary supported B2C billing survives connection loss and retries; prohibited actions fail clearly; till/server calculations agree; numbers are not reused. B2B acceptance is completed in SO-16.
- **Sources:** ST-CMP-2, ST-CMP-5; ST-POS-2, ST-POS-4, ST-POS-5, ST-POS-6; main PRD offline/POS contract. Multi-till-per-store policy remains a separate OQ-30 decision.

### SO-10 — Customer records, consent and rights

- **Outcome:** Customer data can be used and corrected under the approved privacy rules.
- **Discuss:** Optional phone number, separate e-bill/marketing consent, customer confirmation, purchase history, saved sizes, merge, rights requests, retention and minors' marketing exclusion.
- **Work:** Keep historical sale evidence intact through customer corrections and merges. Confirm the proposed retention policy before activation. Supply the consent foundation for SO-17 and SO-19.
- **Done when:** Staff can complete authorised journeys; merges preserve attribution; unauthorised disclosure and unconsented marketing are prevented; retention and rights actions are auditable.
- **Sources:** ST-CMP-6; ST-CUS-1, ST-CUS-2.

### SO-11 — Reservations, special orders and alterations

- **Outcome:** Stock holds, customer advances and physical custody remain accountable until closure.
- **Discuss:** Reservation expiry, sale-period duration, receipts, pickup, cancellation/refund/forfeiture, special-order collection, alteration charges and custody.
- **Work:** Verify transitions between held stock, sale, cancellation and expiry; prevent duplicate advance use. Resolve CA wording and custody dependencies before activation.
- **Done when:** Every quantity and advance has a traceable outcome; retries are safe; online-only boundaries are enforced; paid and free alterations follow the approved rules.
- **Sources:** ST-ORD-1, ST-ORD-2, ST-ORD-3; OQ-49 and relevant CA sign-offs.

### SO-12 — Cash close, petty cash and outright payables

- **Outcome:** The store can account for physical cash and permitted routine spending, and view supported brand obligations.
- **Discuss:** Note/coin count, custody and handover, advances, top-ups, recoveries, internal transfers, variance ownership, petty-cash heads/approval and outright payable rules.
- **Work:** Resolve OQ-03/OQ-08; reconcile expected cash without double-counting petty spending or advances. Keep operational payable information distinct from unapproved accounting treatment.
- **Done when:** A full day's cash reconciles to its source transactions and count; variances and approvals are owned and auditable; outright obligations follow confirmed terms.
- **Sources:** ST-MNY-2, ST-MNY-3, ST-MNY-4 (outright). General Payments, Collections, Expenses and Tally remain main-PRD work except for explicitly required store dependencies.

### SO-13 — Buying support and brand reporting/claims

- **Outcome:** Store stock and sales support buying decisions and commercial follow-up.
- **Discuss:** Open-to-buy inputs, same-season size curves, SOR ageing, brand-specific Sale/SOH layouts, funded-discount claims, credit-note settlement evidence and margin-share estimates.
- **Work:** Verify against operational source records; preserve missing-input warnings; label estimates until OQ-50 permits final profitability claims. SOR payable policy itself is SO-20.
- **Done when:** Plans, reports and claims reproduce their inputs; required export layouts are verified; ageing uses the approved dates; no missing commercial value is guessed.
- **Sources:** ST-BUY-1, ST-BUY-2; ST-BRD-2, ST-BRD-3, ST-BRD-4, ST-BRD-5.

### SO-14 — Reports, approvals, alerts, audit and daily checklists

- **Outcome:** Each role can monitor and close its daily responsibilities with trustworthy information.
- **Discuss:** Sales, stock, brands, staff, GST and exceptions; source completeness/freshness; report exports; approval ownership; alert routing/escalation; before/after audit; store checklists and feature controls.
- **Work:** Reconcile reports to accepted source workflows. Resolve OQ-15 where conversion is required, OQ-18 for routing and OQ-50 for final profitability. Verify revocation and sensitive-field protection across downloads and background jobs.
- **Done when:** Metrics reconcile, missing data is visible, queues lead to permitted actions, writes are traceable and feature rollback preserves valid records. Required report performance is demonstrated under the reconciled PRD target.
- **Sources:** ST-RPT-1 through ST-RPT-6; ST-OPS-1, ST-OPS-2, ST-OPS-3, ST-OPS-4, ST-OPS-6.

### SO-15 — P1 integration and consolidation checkpoint

- **Outcome:** All P1 workflows operate together on one implementation.
- **Walkthrough:** Booking → arrival/count → GRN → PT approval → physical acceptance → transfer → store receipt → sale → exchange → cash close → reports.
- **Work:** Exercise discrepancies, refusals, replay and connection loss. Verify the migration of required records; remove superseded screens, routes, services, models/bridges and obsolete demo fixtures once replacement coverage is proven. Synthetic test fixtures remain legitimate where explicitly needed.
- **Consolidation handoff:** Close the P1 rows C01–C15 in the [SO-03 register](so-03-consolidation-register.md), or record each unmet row's owner and blocker with reconciled history and denial evidence. The shared access authority itself must already be proved by SO-03.
- **Done when:** P1 coverage and acceptance evidence are complete; no P1 workflow depends on an old/new implementation split; remaining gates are explicitly recorded. P1 completion is an internal checkpoint, not the agreed final store milestone or permission to pilot early.
- **Sources:** ST-OPS-5; store PRD §24 and §25.

### SO-16 — Manual IRN, payments, settlements and GSTR-2B

- **Outcome:** Provider-dependent financial store workflows function using the required P2 manual/file modes.
- **Discuss:** Pending B2B bills/credit notes, manual IRN and acknowledgement capture, countdown alerts, manually recorded UPI/card payments, confirmation status, settlement CSV layouts, bank matching and GSTR-2B JSON reconciliation.
- **Work:** Replace the existing post-sale IRN queue behaviour with the approved pending lifecycle. Applicable invoices print and goods release only after IRN entry. Keep manually recorded payments distinct from provider-confirmed results. Resolve OQ-24 sample-layout requirements.
- **Done when:** Valid samples, invalid files, duplicates, unmatched rows and corrections have tested outcomes; pending invoices cannot be prematurely issued; retry does not duplicate payment or reconciliation effects.
- **Sources:** ST-CMP-4; ST-POS-1; ST-MNY-1; ST-REC-2.

### SO-17 — Manual customer messaging and back-in-stock

- **Outcome:** Messaging workflows are usable before provider connection.
- **Discuss:** E-bill queue, secure time-limited share links/QR, manual sending and recorded status, consent-filtered campaign exports, back-in-stock requests/expiry and staff tasks triggered by accepted stock.
- **Work:** Build manual workflows and provider test doubles; preserve honest delivery states and ensure messaging cannot stall ordinary billing.
- **Done when:** Consent and recipient scope are enforced, share links expire as required, request/task lifecycle works, and failed or repeated sends have explicit outcomes.
- **Sources:** ST-POS-3; ST-CUS-4, ST-CUS-5.

### SO-18 — Attendance, rosters, incentives and payroll export

- **Outcome:** Store people operations work through the approved payroll-export boundary.
- **Discuss:** Photo-only attendance without face recognition; store geofence; offline capture and original timestamps; photo retention; rosters, leave, overtime and corrections; incentive slabs; payroll export contract.
- **Work:** Resolve OQ-02/OQ-19 and relevant capture/privacy decisions. Preserve raw attendance evidence and versioned rules. Calculate incentive attribution including split sales and returns.
- **Done when:** Offline capture/retry, correction approvals, incentive calculations and payroll export reconcile to approved inputs. Full in-app payroll and payslips remain main-PRD scope.
- **Sources:** ST-HR-2, ST-HR-3, ST-HR-4, ST-HR-6.

### SO-19 — Connect and verify external providers

- **Outcome:** The accepted manual workflows gain their approved live integrations.
- **Discuss:** Payment gateway/card machine, WhatsApp BSP/SMS DLT and OTP consent, IRP/GSP and GSTR-2B API; accounts, costs, credentials, templates and operating responsibilities.
- **Work:** Connect providers through existing workflow interfaces; test callback authentication, replay, delayed responses, unknown outcomes, reconciliation and supported fallback. Switch integrations per store under approved controls.
- **Done when:** Applicable sandbox and controlled live verification passes, provider outcomes are truthful and reconciled, duplicate effects are prevented and credentials are handled correctly. Choosing a provider does not retroactively block completion of P2 manual modes.
- **Sources:** P3 portions of ST-POS-1, ST-POS-3, ST-CMP-4, ST-REC-2, ST-MNY-1, ST-CUS-4, ST-CUS-5 and ST-CMP-6 OTP consent.

### SO-20 — Complete policy-dependent store features

- **Outcome:** The remaining P4 store scope is delivered under explicit approved policies.
- **Discuss:** Loyalty points/tiers and accounting treatment; home-delivery custody/handover/returns; min/max replenishment; Shops & Establishments forms; SOR payable recognition and settlement.
- **Work:** Resolve the affected OQ-47, OQ-49, OQ-26 and replenishment/register requirements through the PRD decision process. Honour main-PRD implementation gates while those decisions remain open.
- **Done when:** The agreed P4 workflows and acceptance cases are implemented and verified, with approvals recorded for activation. If a decision remains open, identify the resulting milestone blocker; do not call the complete store scope finished by omission.
- **Sources:** ST-CUS-3; ST-ORD-4; ST-INV-5; ST-HR-5; ST-MNY-4 (SOR).

### SO-21 — Full store-operations acceptance and release proofs

- **Outcome:** Evidence supports the claim that the complete agreed store scope can be used in a real store.
- **Work:** Close the requirement register against store PRD §24 and applicable main-PRD acceptance scenarios. Run role/field/isolation tests, representative migrations, performance, accessibility, named scanner/printer/browser tests, offline durability and workload isolation.
- **Required proofs:** Main PRD §16.10 requires the 50,000-line PT under 30 seconds with safe interruption; 50-way stock/numbering concurrency; 200 offline bills surviving the specified failures; forced RLS and scoped disclosure checks; evidence tamper/stopped-stream detection; and point-in-time restore plus independently reconciled portable export.
- **Infrastructure:** Complete provider-enforced evidence retention, independent verification/audit-log transport, monitoring and recovery. The local filesystem evidence adapter does not establish production protection. Close affected OQ-33/OQ-34/OQ-37/OQ-38/OQ-40 release gates.
- **Done when:** Required evidence passes, applicable activation approvals are recorded, all duplicate implementations are retired and the release record identifies delivered requirements, actual checks and residual limitations. No unapproved scope omissions remain hidden.
- **Consolidation handoff:** Re-inventory the mounted routes, service/model readers and screen journeys against C01–C20 of the [SO-03 register](so-03-consolidation-register.md); prove no duplicate business write authority and identify any retention-only historical tables.

### SO-22 — Prepare the real pilot store

- **Outcome:** One named store is ready for controlled activation.
- **Discuss:** Pilot store and people; hosting/access; hardware/network; legal and commercial masters; opening stock; outstanding holds/transit/customer advances; numbering; cutover ownership; support and rollback.
- **Work:** Load and reconcile approved real sources, configure role/site access and features, rehearse cutover and recovery, train every role and provide a one-page counter guide for offline/manual situations. Confirm CA sign-offs, GSTIN/AATO facts and unknown brand models.
- **Done when:** Data reconciliation and readiness checklist are accepted, supported hardware works, authorised operators can perform their jobs, monitoring has owners and rollback preserves valid records. Demo records are excluded from the live operating dataset.
- **Sources:** Main PRD §14; store PRD §30 and §32.

### SO-23 — Operate and stabilise the one-store pilot

- **Outcome:** A real store demonstrates reliable daily operation.
- **Work:** Review success measures and exceptions daily for the first two weeks: billing speed, sync, tax mismatches, stock accuracy, cash, settlements, attendance, reports and user issues. Resolve defects and rerun affected acceptance checks.
- **Done when:** The recorded pilot exit criteria are met and the product owner accepts operational readiness. Two elapsed weeks alone do not constitute success.
- **Next decision:** Agree the expansion schedule and remaining main-PRD work. Multi-store rollout requires the Store Manager role and proceeds in controlled groups. Multiple stores does not mean multiple tills per store; OQ-30 remains separate where applicable.

### SO-24 — Plan the remaining main-PRD delivery

- **Outcome:** A verified remaining-scope backlog for the full ERP, built on the accepted store application.
- **Discuss:** Full accounting and period close, general payments/collections/expenses, Tally, statutory treatment, full payroll/payslips, partner/franchise lifecycle and settlements, site lifecycle, final profitability, and the broader analytics/intelligence catalogue, including remaining document extraction requirements.
- **Work:** Reconcile every remaining `R-` requirement against what store delivery already satisfies. Record policy dependencies and acceptance evidence before sequencing the next releases.
- **Done when:** The remaining full-product commitment has an agreed delivery plan. This planning item's completion does not mark the main PRD complete. Continue extending the same consolidated application.

## 6. Decisions and inputs to track from the beginning

These inputs are gathered during the relevant discussions; they are not all prerequisites for starting development.

| Input / decision | Primary discussion |
| --- | --- |
| Requirement hierarchy, contradictions and provisional phase assignments | SO-01 |
| Complete role/action/field/site/brand matrix (OQ-28) | SO-03 |
| Opening migration sources and cutover evidence (OQ-29; OQ-54 answered) | SO-04, SO-22 |
| Tax golden cases and all applicable CA sign-offs; numbering and entity facts | SO-06, SO-09, SO-16, SO-22 |
| Brand models/agreements and SOR commercial treatment (OQ-26) | SO-06, SO-13, SO-20 |
| Trading-store count sales freeze (OQ-57 resolved in SO-01); implementation and proof | SO-08 |
| Store-credit issue/redemption eligibility, OQ-45 and single-till boundaries | SO-09 |
| Customer retention, reservation wording and custody (OQ-49) | SO-10, SO-11, SO-20 |
| Expense heads and cash custody (OQ-03/OQ-08) | SO-12 |
| Metrics, footfall, final profitability and alert routing (OQ-15/OQ-43/OQ-50/OQ-18 as applicable) | SO-13, SO-14 |
| Bank/settlement examples (OQ-24) | SO-16 |
| Incentives and payroll inputs (OQ-02/OQ-19) | SO-18 |
| Provider selection, accounts, costs and unknown-outcome handling | SO-19 |
| Accounting implementation/activation boundary (OQ-47 and related gates) | SO-01 and each affected money feature |
| Hosting, evidence, capacity, export/recovery and numbering release decisions | SO-21, prepared earlier where needed |
| Pilot store, equipment, staff, training and support ownership | SO-22 |

## 7. Record to complete after each discussion

Copy this small record under the relevant item or link to its detailed working document:

```text
Item ID and title:
Discussion date:
Current status:
PRD requirement IDs / sections:
Verified current behaviour:
Agreed operator journey and exceptions:
Decisions resolved, with authoritative PRD record:
Remaining decisions and affected activation:
Implementation to retain:
Data migration / obsolete code to retire:
Concrete delivery tasks:
Acceptance cases and expected results:
Checks actually run and evidence:
Residual issues:
Accepted outcome / next item:
```

Maintain a separate distinction between **code exists**, **behaviour verified**, **approval received** and **activated for the pilot**. Update the roadmap from evidence as we move through the discussions.
