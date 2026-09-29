// What each unbuilt screen honestly promises (issue #89).
//
// KDPS management has a build-status report (21 July 2026, "Build Status Report
// — Module-wise"). A demo of the sidebar and that report must tell one story, so
// the copy below quotes the report's own module names and promises rather than
// paraphrasing them, and places each screen where the report places it. Where the
// stores wrote a requirement down in their own words (the 25 July Store Ops
// notes), those words are used instead of ours.
//
// The rule for this file: a planned page may not flatter. If a screen is waiting
// on hardware, on a decision nobody has taken, or on data that will not exist
// until billing is live, it says so in `notes`. A placeholder that reads as
// "almost ready" is the exact failure #89 exists to prevent.
//
// Paired with the section manifest: every nav item marked `planned` must have an
// entry here, and nothing else may (asserted in plannedPages.test.ts).

/** When this screen arrives, in the report's own vocabulary. Three of the four
 *  are its sequence positions (next → then → later); `unscheduled` is its third
 *  column, "Planned" — the module may already be live or in test, but this part
 *  of it has not been started, so there is no position to quote. */
export type Stage = "next" | "then" | "later" | "unscheduled";

export interface ReportModule {
  /** The module's name in the client report, quoted exactly. */
  name: string;
  stage: Stage;
}

/** How each stage is worded to a reader. Plain words, no dates — the report
 *  deliberately gives no delivery dates and neither does the product. */
export const STAGE_LABEL: Record<Stage, string> = {
  next: "Next to be built",
  then: "After billing",
  later: "Later",
  unscheduled: "Planned",
};

/** Chip tone per stage, matching the report's own badge colours. Bare tones,
 *  rendered as `chip-${tone}` — one chip class per tone. */
export const STAGE_TONE: Record<Stage, string> = {
  next: "blue",
  then: "blue",
  later: "navy",
  unscheduled: "navy",
};

const GOODS_RECEIPT: ReportModule = { name: "Goods Receipt", stage: "unscheduled" };
const TRANSFERS: ReportModule = { name: "Transfers & Returns", stage: "unscheduled" };
const POS: ReportModule = { name: "POS & Billing", stage: "next" };
const ACCOUNTS: ReportModule = { name: "Accounts & Payments", stage: "then" };
const INVENTORY: ReportModule = { name: "Inventory Management", stage: "unscheduled" };
const ADMINISTRATION: ReportModule = { name: "Administration", stage: "unscheduled" };
const PROMOTIONS: ReportModule = { name: "Promotions & EOSS", stage: "later" };
const HRMS: ReportModule = { name: "HRMS & Payroll", stage: "later" };
const ANALYTICS: ReportModule = { name: "Reports & Analytics", stage: "later" };

/**
 * The client report's own module strip, whole.
 *
 * Listed rather than derived from `PLANNED_PAGES`, because a module does not
 * leave the report when its last screen is built: POS & Billing is on the strip
 * and has nothing planned under it since #184 landed the last of Sell's four
 * screens. Deriving the strip from what is unbuilt would quietly drop a module
 * from the client's report the moment we finished it, which is the opposite of
 * what finishing it should do.
 */
export const REPORT_MODULES: ReportModule[] = [
  GOODS_RECEIPT,
  TRANSFERS,
  POS,
  ACCOUNTS,
  INVENTORY,
  ADMINISTRATION,
  PROMOTIONS,
  HRMS,
  ANALYTICS,
];

export interface PlannedScreen {
  /** One line: what the screen is for, in the words its user would use. */
  summary: string;
  /** What will live here. The report's promises where the report makes one. */
  contains: string[];
  /** The honest caveats — what this waits on, and where the ask came from.
   *  Anything a client would be annoyed to discover later belongs here. */
  notes?: string[];
  /** The module of the client report this screen belongs to. */
  module: ReportModule;
}

export const PLANNED_PAGES: Record<string, PlannedScreen> = {
  // ---- Sell ---------------------------------------------------------------
  // Nothing. Every screen under Sell is built - Billing (#181), Return &
  // Exchange (#184), Customers (#185) and Till & Sync (#180) - and a promise on
  // this list for a screen somebody can already open would be a promise nobody
  // can reach, which `plannedPages.test.ts` says out loud.
  //
  // The stores' own wording from the 25 July note about Customers, "re-print
  // only, no editing", is what shipped: there is no writer behind that screen to
  // edit with.

  // ---- Receive Goods ------------------------------------------------------
  // Nothing. "Upload Bill" was a promise the built screen already keeps: the
  // brand's invoice goes up inside "New receipt", where it is attached to the
  // receipt and read for the line match. #228 deleted the entry rather than
  // leave a menu line whose own page said "go and do it over there".

  // ---- Transfer -----------------------------------------------------------
  // Nothing. Distribution's stub was deleted in #229; the real screen shipped
  // later as "Distribution grid" (bulk-splits an arrived batch into per-store
  // transfer drafts) rather than a promise sitting empty in the sidebar.

  // ---- Money --------------------------------------------------------------
  "/money/payments": {
    summary: "Paying a brand, with the checks done before the money leaves.",
    contains: [
      "Vendor payments with approval workflow",
      "3-way matching before paying — invoice against goods receipt against PT",
      "What is due, what is overdue, what is on hold",
    ],
    notes: [
      "The vendor ledger and cash book are already live, so what we owe is visible today. The paying itself comes with this module.",
    ],
    module: ACCOUNTS,
  },
  "/money/collections": {
    summary: "Money collected at the stores against money actually banked.",
    contains: [
      "Collected against banked, store by store",
      "Short deposits and late deposits visible per store",
    ],
    module: ACCOUNTS,
  },
  "/money/expenses": {
    summary: "Day-to-day spending at a store or the warehouse.",
    contains: [
      "Store and warehouse expense entries",
      "Coded so each expense lands in the right head",
      "A store person can enter an expense without being able to see the books",
    ],
    module: ACCOUNTS,
  },
  "/money/tally": {
    summary: "What has reached Tally, and what is still waiting.",
    contains: [
      "Tally integration — what has gone across and what is pending",
      "Anything that failed, with the reason, so it can be sent again",
    ],
    notes: [
      "Tally stays the statutory book of record. This system feeds Tally; it does not replace it.",
    ],
    module: ACCOUNTS,
  },

  // ---- Offers & Price -----------------------------------------------------

  // ---- Staff --------------------------------------------------------------
  "/staff/attendance": {
    summary: "The store's daily attendance screen — mark in, mark out, see the month.",
    contains: [
      "Mark check-in and check-out at the store",
      "See your own leaves and delays",
      "Send the day's attendance",
    ],
    notes: [
      "Posting attendance needs a biometric device at the store. No device is integrated yet, and hardware like this has lead time — until one is chosen and connected, attendance cannot be marked from this screen.",
      "Attendance and payroll were deferred by decision, to be revisited before go-live. The stores have now asked for attendance twice — 30 June and 25 July — so whether to pull a thin attendance-only slice forward is an open call for KDPS.",
    ],
    module: HRMS,
  },
  "/staff/members": {
    summary:
      "The store's own people — its staff. A store manager keeps their own store's members here.",
    contains: [
      "Add and remove members",
      "Contact details and bank details",
      "Each member's monthly target against their achievement",
      "Growth and de-growth against last month",
      "The store's members with their photos",
    ],
    notes: [
      "“Member” means a member of staff. It was settled on 25 July 2026 that Member Details is a staff scorecard, not a customer loyalty scheme. Loyalty is not part of this screen and stays out of scope; the POS owns the customer.",
      "Because a member record holds bank details, this screen belongs to the Attendance & Payroll module, which is deferred by decision until before go-live — it is not around the corner. Staff bank details are personal data and the screen will be built with that duty, not before it.",
      "Targets and achievement also need every bill to record who sold it. That is not decided yet and not built, so member-wise numbers cannot be shown until it is.",
      "A cashier does not see this screen. Keeping the store's people is the manager's job.",
    ],
    module: HRMS,
  },
  "/staff/payroll": {
    summary: "Salary and incentive inputs for the people the stores employ.",
    contains: [
      "Payroll inputs",
      "Sales incentive calculation",
      "Employee records and documents",
      "Employee self-service",
    ],
    notes: [
      "Payroll needs attendance first, so it comes after attendance — the last part of the deferred Attendance & Payroll module, not the first.",
    ],
    module: HRMS,
  },

  // ---- Reports ------------------------------------------------------------
  "/reports/stock": {
    summary: "What is lying where, how old it is, and what is not moving.",
    contains: [
      "Stock report",
      "Stock ageing",
      "Dead-stock alerts",
      "Return-deadline tracking for SOR brands",
      "Demand forecasting and auto-replenishment",
    ],
    notes: [
      "Stock position can be read from the ledger the system already keeps today — Stock on Hand and Movement History are live in the Stock section. Ageing, dead stock and deadline tracking are the parts still to be built.",
    ],
    module: INVENTORY,
  },
  "/reports/profit": {
    summary: "What each brand and each store actually earned.",
    contains: [
      "Brand-wise and store-wise profit and loss",
      "Cost taken from the PT or invoice at stock-in, revenue from the bill at sale — derived, never typed in by hand",
    ],
    notes: [
      "Cost is already captured on every goods receipt. The revenue half arrives with billing, so profit becomes real then.",
    ],
    module: ANALYTICS,
  },
  "/reports/daily": {
    summary: "The day's business on one page, sent to the phone.",
    contains: [
      "Daily business summary on WhatsApp",
      "Per person, so each role gets the numbers that are theirs",
    ],
    notes: ["The summary is mostly sale numbers, so it waits on billing."],
    module: ANALYTICS,
  },
  "/reports/maker": {
    summary: "Build the report you want, and keep the format.",
    contains: [
      "MIS dashboards",
      "Pick the measures and the columns, save the format, run it again next month",
      "The client's own sale and stock report formats as ready-made starting points",
    ],
    module: ANALYTICS,
  },

  // ---- Setup --------------------------------------------------------------
  "/setup/products": {
    summary: "The item master — every style, in every size and colour.",
    contains: [
      "Style, size, colour, barcode and HSN",
      "Barcodes as scan aliases, with stock counted under them",
      "Season and collection tags on every item",
    ],
    notes: [
      "Items already enter the system through brand PT files and PT making, which create them as goods arrive. This screen is where they will be managed directly instead.",
    ],
    module: ADMINISTRATION,
  },
};
