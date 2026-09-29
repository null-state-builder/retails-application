// The sidebar, as one manifest (issue #87).
//
// KDPS navigates by *operation* — what actually happens to goods and money —
// not by the architecture's own layers. The old groups ("Documents", "Ledgers",
// "Controls", "Intelligence", "Edges & Admin") were design vocabulary no store
// person in Deoghar thinks in; the thirteen sections below are the words KDPS
// uses. Vocabulary is shared across roles: a role sees a *subset* of these
// sections, never a differently-named regrouping — so the store and the
// warehouse on the phone to each other both say "Transfer".
//
// This file is the single source of truth for navigation. Derived from it:
//   · the sidebar          (AppShell, intersected with the server's sections)
//   · the route guards     (auth/routeAccess — URL → the screen that owns it)
//   · the routes           (routes.tsx: planned screens are generated from it)
//   · the planned pages    (pages/plannedPages — what each unbuilt screen promises)
//   · the legacy redirects (every pre-#87 URL, below)
//
// Which sections a person actually gets is *not* decided here — the server
// sends it (SIDEBAR RBAC contract, #85), and `section` codes below match the
// server's codes exactly. This manifest only says what a section contains.
import {
  BarChart3,
  Boxes,
  CalendarClock,
  ClipboardCheck,
  ClipboardList,
  Contact,
  Handshake,
  LayoutDashboard,
  PackagePlus,
  Settings,
  ShoppingCart,
  Tag,
  Truck,
  Undo2,
  Users,
  Wallet,
  Warehouse,
} from "lucide-react";
import { PT_WORK_ACTIONS } from "../lib/ptWork";
import type { LucideIcon } from "lucide-react";

// The capability ladder, mirroring `accounts/sections.py`. Ordinal: a higher
// rung includes the powers of the lower ones.
export const CAPABILITY_ORDER = ["none", "view", "operate", "approve", "manage"] as const;
export type Capability = (typeof CAPABILITY_ORDER)[number];

/** Does the capability `held` on a section reach at least `minimum`? Fail-closed:
 *  anything unrecognised (or absent) counts as `none`. */
export function meetsCapability(held: string | undefined, minimum: Capability): boolean {
  const rank = CAPABILITY_ORDER.indexOf((held ?? "none") as Capability);
  return rank >= 0 && rank >= CAPABILITY_ORDER.indexOf(minimum);
}

/** Does this user reach at least `minimum` on `section`? The client's mirror of
 *  `accounts.permissions.user_can`, break-glass branch included: a superuser
 *  resolves to `manage` everywhere server-side, so a screen must not hide what
 *  the API would let them do.
 *
 *  One home for the rule, because it is easy to write four times and forget the
 *  superuser branch in one of them. Typed structurally rather than against
 *  `User`, so the navigation manifest keeps no dependency on the auth module. */
export function userCan(
  user: { is_superuser?: boolean; capabilities?: Record<string, string> } | null | undefined,
  section: string,
  minimum: Capability,
): boolean {
  if (!user) return false;
  if (user.is_superuser) return true;
  return meetsCapability(user.capabilities?.[section], minimum);
}

/** No store feature is on: what a caller passes before the session has loaded. */
export const NO_STORE_FEATURES: ReadonlySet<string> = new Set();

/** The store features on where this person is working now (ST-OPS-6).
 *
 *  `storeFeatures` is the session's `{feature key: [site id, ...]}` - every
 *  feature on at one or more of the person's sites. Working in one unit (the
 *  top-bar switcher), a feature counts only if it is on at that unit; in the
 *  all-units view it counts if it is on anywhere they work. Fail-closed: no
 *  payload is no features. */
export function storeFeaturesOn(
  storeFeatures: Record<string, string[]> | null | undefined,
  activeStoreId: number | string | null | undefined,
): ReadonlySet<string> {
  const on = new Set<string>();
  for (const [key, sites] of Object.entries(storeFeatures ?? {})) {
    const here = activeStoreId == null ? sites.length > 0 : sites.includes(String(activeStoreId));
    if (here) on.add(key);
  }
  return on;
}

/** Is `item`'s store feature (if it has one) on where the person works? */
export function storeFeatureOpen(item: NavItem, featuresOn: ReadonlySet<string>): boolean {
  return !item.storeFeature || featuresOn.has(item.storeFeature);
}

// Payroll is the one gate the ladder cannot express: Accounts must see it on
// `hrms: view` ("payroll inputs") while a store person must not, and the store
// person sits *higher* on the ladder at `hrms: operate` ("own attendance").
// An ordinal threshold can't separate them, so this stays an explicit role list.
export const PAYROLL_ROLES = ["owner", "it_admin", "accounts"];

// Brand terms carry each brand's margin (ticket 23): Setup reaches the warehouse,
// the data steward and Admin too, so the ladder cannot keep margins from them.
// Mirrors the server's `masters.brand_term_readers`.
export const BRAND_TERM_READERS = ["owner", "brand_manager", "accounts"];

export interface NavItem {
  label: string;
  /** The one canonical URL for this screen. There is exactly one per screen. */
  to: string;
  /** Finer gate than the section: only these role codes see the item. Use only
   *  where the capability ladder genuinely cannot express the rule (Payroll) —
   *  otherwise prefer `minCapability`, which reads the same server-sent data the
   *  API gates on and so cannot drift from it. */
  roles?: string[];
  /** Finer gate than the section: the rung the caller must hold *on this item's
   *  section* to see it. Mirrors a backend permission tighter than the section
   *  itself — e.g. the ledgers are `money: manage`, the rung only Owner and
   *  Accounts hold, so "Expenses only" roles keep the section but not the books. */
  minCapability?: Capability;
  /** Gate for a URL strictly *under* this item's own path, when it differs from
   *  `minCapability` — a document a caller may still open even though the
   *  list/create screen that owns its URL prefix now needs a higher rung. PT
   *  making rose to `approve` (#119) while reading a PT already made stays at
   *  whatever rung holding the section at all requires — reading and making are
   *  different rights, and only making moved. Undefined ⇒ a child shares the
   *  item's own `minCapability`, the older and still-default behaviour (a
   *  booking document, for instance, has no gate of its own to differ). */
  childMinCapability?: Capability;
  /** Not built yet — routed to the planned page, which says what will live here.
   *  The promise itself is in `pages/plannedPages.ts` (#89), keyed by this
   *  item's path: navigation says what a section contains, that manifest says
   *  what we have told the client it will do. */
  planned?: true;
  /** Reachable and routed, but not a menu item — an action reached from inside
   *  another screen. V-flip is the case: a rare ownership correction that lives
   *  as a button on Stock, not as a line in the sidebar. */
  action?: true;
  /** In the manifest, routed and gated exactly as before - but never drawn as a
   *  sidebar menu line. A strip or a fold may still name it as a tab, and a
   *  bookmark or a deep link still opens it for whoever the gate allows.
   *
   *  Two different reasons use it, and both are "this screen is not a sidebar
   *  destination", not "this screen is gone":
   *   · Price Book, which the store and warehouse operations PRD §11 hides for
   *     this increment while keeping its records and its screen;
   *   · EOSS Planning, which the Aug 2026 consolidation moved inside Promotions
   *     and which is now that row's own tab rather than a line of its own.
   *
   *  Unlike `action`, a hidden entry survives the access filter: the strip that
   *  names it has to be able to find it, and finding it is how the tab keeps the
   *  entry's own gate. `applyLayout` is where it stops being a menu line, which
   *  is the one place the sidebar's rows are built. */
  hidden?: true;
  /** A menu entry into *another* section's screen. It owns no route and no
   *  access rule — the section that hosts the screen keeps both, so one screen
   *  still has exactly one URL and one gate. */
  deepLink?: true;
  /** A goods-v1 screen, gated by the session's effective action grants
   *  (`SessionDTO.actions`, design §4.2; GSA-T02) and by nothing else. Holding
   *  any one of these draws the line and opens its URL; the legacy section
   *  list, capability ladder and role code play no part, in either direction -
   *  a legacy section never opens a goods screen, and a person whose legacy
   *  payload is empty still gets the goods lines their grants allow, under the
   *  same section words everybody else uses. Site, SBU, brand and field scope
   *  stay the server's: a line is a way in, never authority, and every read or
   *  command behind it is refused where the grant does not reach. */
  goodsActions?: string[];
  /** A legacy line that *also* opens for these goods-v1 grants. It keeps its
   *  legacy gate for whoever holds its section, and is drawn and opened for a
   *  person who holds none of it but holds one of these grants. Home's Action
   *  Needed and Alerts are the case (23 Sep 2026): each carries a goods block
   *  (exceptions, goods notifications) that a goods-v1-only person reached on
   *  its own screen before the two were folded in, and must still reach. The
   *  screen itself draws only the blocks the person holds. */
  orGoodsActions?: string[];
  /** A store-operations feature switched per store (ST-OPS-6): the line is
   *  drawn, and its URL opens, only while this feature is on where the person
   *  is working (`storeFeaturesOn`). It applies on top of every other gate and
   *  to everybody, break-glass included, because the server refuses an off
   *  feature to everybody too (`masters.store_features.require_feature`). */
  storeFeature?: string;
  /** A screen filed under this section whose *data* another section's gate
   *  guards: the line is drawn, and its URL opens, only for a person who also
   *  holds `minCapability` on `section`. Store operations ticket 10 moved the
   *  Day Summary and the Discount Report under Reports (PRD §17); their APIs
   *  still answer to `money: view` and `offers_price: view`, and a Reports line
   *  that walked somebody without those into a refusal would be the sidebar
   *  lying about the API (#85). Break-glass passes, as everywhere. */
  dataGate?: { section: string; minCapability: Capability };
}

/** Does this person also hold the section a moved screen's data answers to?
 *  `capabilities` is the session's `{section code: rung}`. No gate is open. */
export function dataGateOpen(
  item: NavItem,
  capabilities: Record<string, string> | undefined,
  isSuperuser: boolean,
): boolean {
  if (!item.dataGate || isSuperuser) return true;
  return meetsCapability(capabilities?.[item.dataGate.section], item.dataGate.minCapability);
}

export interface NavSectionDef {
  /** Section code from the server's RBAC contract (#85). */
  code: string;
  /** Fallback label; the server's label for the section wins when present. */
  label: string;
  icon: LucideIcon;
  /** CSS `--layer-*` token suffix (index.css). Sections share the nine tokens. */
  layer: string;
  items: NavItem[];
  /** A heading the server never sends, drawn from another section's grant.
   *  Brands (store operations PRD §5.1, ticket 23) is the one case: the RBAC
   *  sheet has no Brands row, and adding one is access-control work, so the
   *  heading answers to whatever the person holds on `setup` - where brand
   *  terms were always Setup's to guard. It can only ever show what that grant
   *  already opens: no Setup, no Brands. */
  grantedBy?: string;
}

/** The server section whose grant opens `code`: itself, or a heading's
 *  `grantedBy`. The route guard and the sidebar both ask this. */
export function sectionGrant(code: string): string {
  return SECTIONS.find((s) => s.code === code)?.grantedBy ?? code;
}

export const SECTIONS: NavSectionDef[] = [
  {
    code: "home",
    label: "Today",
    icon: LayoutDashboard,
    layer: "home",
    items: [
      { label: "Dashboard", to: "/" },
      // Home is split by one question, "do I have to do something?" (Anand,
      // 23 Sep 2026). Action Needed is yes: the approvals inbox beside the
      // goods exceptions, each block keeping its own gate. Everyone has the
      // approvals inbox, so the line itself is ungated. Alerts is no: the
      // deadline alerts and the goods notification feed, read and dismissed.
      {
        label: "Action Needed",
        to: "/action-needed",
        orGoodsActions: ["exception.view", "exception.manage"],
      },
      { label: "Alerts", to: "/alerts", orGoodsActions: ["exception.view", "exception.manage"] },
      // Store operations ticket 49 (ST-OPS-4): today's task checklist, full width.
      // A store reaches it from its Dashboard card, the "Checklist items missed"
      // row and the checklist-missed alert (it is not one of Dashboard's tabs);
      // head office from this line. Only where store checklists are switched on.
      { label: "Today's Checklist", to: "/checklist", storeFeature: "store-checklists" },
    ],
  },
  {
    code: "sell",
    label: "Sell",
    icon: ShoppingCart,
    layer: "store",
    items: [
      // The counter itself (#181). `operate` for the same reason Till & Sync
      // carries it: this screen bills, and every endpoint behind it is gated at
      // `sell: operate`.
      { label: "Billing", to: "/sell", minCapability: "operate" },
      // The day's bills, this counter's and head office's as one list (OPS-08,
      // store and warehouse operations PRD §9.2). `operate`, not the `view` the
      // customer search carries: it opens the counter's own database to show the
      // bills that have not synced yet, which is a till's copy rather than a
      // report about selling.
      { label: "Bills", to: "/sell/bills", minCapability: "operate" },
      // Finding an old bill and printing it again (#185). `view`, not `operate`:
      // `GET /api/sell/sales` is gated at `sell: view` and this screen cannot
      // write, so an owner or an accountant reaching a customer's bill is the
      // matrix working rather than a hole in it.
      // Find-bill remains a guarded route reached from the counter (F3), not a
      // second public Sell destination.
      { label: "Customers", to: "/sell/customers", action: true, minCapability: "view" },
      // The counter's own state: what it holds offline, and what it still owes
      // head office (#180). Listed after the screens a cashier uses all day,
      // because it is the page somebody opens when something looks wrong.
      //
      // `operate`, not the section's own rung: both endpoints behind this page
      // are gated at `sell: operate` (`sell/permissions.py`), because the
      // dataset is the working copy a counter bills from rather than a report
      // about selling. Offering the entry at `view` would walk an owner or an
      // accountant into a 403.
      { label: "Till & Sync", to: "/sell/till", minCapability: "operate" },
      // Ticket 41 (ST-MNY-2): the day-close count by note and coin. `operate`
      // like Till & Sync: its endpoints are a till login's, for its own store.
      {
        label: "Cash Count",
        to: "/sell/cash-count",
        minCapability: "operate",
        storeFeature: "cash-count",
      },
      // Ticket 19 (ST-POS-4): the gift vouchers this store sold. `operate` like
      // Cash Count: its endpoints are a till login's, for its own store. Only
      // where gift vouchers are on.
      {
        label: "Gift Vouchers",
        to: "/sell/gift-vouchers",
        minCapability: "operate",
        storeFeature: "gift-vouchers",
      },
      // The card lives at the Setup address because it is a head-office dial,
      // but it is published under Sell: its API is `sell:manage`, and a user
      // who holds that permission need not hold Setup at all.
      { label: "Counter Settings", to: "/setup/settings", minCapability: "manage" },
    ],
  },
  {
    // Store operations PRD §5.1 (ticket 16): the Customers menu - the customer
    // list, each customer's page and the rights screen. The RBAC sheet has no
    // Customers row, so the heading answers to the Sell grant (`grantedBy`), the
    // grant the counter's customer work always sat under: no Sell, no Customers.
    // Reading is `sell: view`; acting on a customer's request is `sell: operate`,
    // which the page asks the server about. Only where customer rights are on.
    code: "customers",
    label: "Customers",
    icon: Contact,
    layer: "store",
    grantedBy: "sell",
    items: [
      {
        label: "Customer List",
        to: "/customers",
        storeFeature: "customer-rights",
        minCapability: "view",
      },
    ],
  },
  {
    // Store operations PRD §5.1 (ticket 20): Customer Orders. Reservations first;
    // special orders, alterations and home delivery join it with their own
    // tickets. Drawn from the Sell grant (`grantedBy`) at `operate`, because
    // every endpoint behind it is `sell: operate`, and each line only where its
    // own feature is on.
    code: "customer_orders",
    label: "Customer Orders",
    icon: CalendarClock,
    layer: "store",
    grantedBy: "sell",
    items: [
      {
        label: "Reservations",
        to: "/orders/reservations",
        storeFeature: "customer-reservation",
        minCapability: "operate",
      },
      // Ticket 21 (ST-ORD-2): special orders, only where they are on.
      {
        label: "Special orders",
        to: "/orders/special",
        storeFeature: "special-orders",
        minCapability: "operate",
      },
      // Ticket 22: alteration job cards, only where alterations are on.
      {
        label: "Alterations",
        to: "/orders/alterations",
        storeFeature: "alterations",
        minCapability: "operate",
      },
    ],
  },
  {
    code: "booking",
    label: "Booking",
    icon: ClipboardList,
    layer: "documents",
    items: [
      // One Bookings screen over both booking engines (store and warehouse
      // operations PRD §12). A store's goods go through the booking engine of
      // the stock system it runs on; the list, the booking page and the form
      // are the same for both, so there is one line here and no "vendor" or
      // "older" second one. It opens for whoever could open either screen
      // before: the legacy `booking: view` section, or a goods-v1 booking read.
      {
        label: "Bookings",
        to: "/booking",
        orGoodsActions: ["booking.manage", "receive.arrival", "pt.prepare", "pt.view"],
      },
      // The form, reached from the list's own button, not from a menu line.
      // Whoever can make a booking in either engine: legacy `booking: operate`
      // (a store holds only `view`, #130), or the goods-v1 `booking.manage`.
      {
        label: "New Booking",
        to: "/booking/new",
        minCapability: "operate",
        orGoodsActions: ["booking.manage"],
        action: true,
      },
      // Store operations ticket 39 (ST-BUY-1): buying budgets at cost. For the
      // Owner who sets them (`booking: approve`, which Admin's manage also
      // reaches) and the buyer who books against them (goods-v1
      // `booking.manage`). Every one of those logins carries the goods `cost`
      // field the API answers to; a store or warehouse login reaches neither.
      {
        label: "Open-to-Buy",
        to: "/booking/open-to-buy",
        minCapability: "approve",
        orGoodsActions: ["booking.manage"],
        storeFeature: "open-to-buy",
      },
    ],
  },
  {
    code: "receive_goods",
    label: "Receive Goods",
    icon: PackagePlus,
    layer: "store",
    items: [
      // The legacy Receive and PT Files lines went with legacy receiving
      // (OPS-18, PRD §5.5): goods-v1 is the one receiving system.
      //
      // The receiving inbox (OPS-04, store and warehouse operations PRD §5.1):
      // one list per site of vendor deliveries and incoming transfers, each
      // with the step it waits on, and one guided workflow behind it. Two
      // lines, one screen: Pending is the work, History is what is finished.
      //
      // Since OPS-17 the separate Arrivals and counting, Goods receipts, Accept
      // goods, Prepare PT and PT approvals lines are gone, with their routes
      // (PRD §12, "Exception for Receive Goods"): their panels are the
      // workflow's steps, Goods arrived on Pending starts a delivery, and PT
      // Work holds the preparing, the approving and the mapping rules.
      {
        label: "Pending",
        to: "/goods/receive",
        goodsActions: ["receive.arrival", "pt.prepare", "pt.view", "stock.view", "stock.accept"],
      },
      {
        label: "History",
        to: "/goods/receive/history",
        goodsActions: ["receive.arrival", "pt.prepare", "pt.view", "stock.view", "stock.accept"],
      },
      // PT Work (OPS-17): To prepare, To approve and Mapping rules, each tab
      // drawn only for somebody the server would answer (`lib/ptWork.ts`). A
      // receipt PT's reissue (E131) is the preparer's own authority, so the
      // preparing grants reach the approvals tab too, as they always did.
      {
        label: "PT Work",
        to: "/goods/pt-work",
        goodsActions: [...PT_WORK_ACTIONS],
      },
      // Goods-v1 labels and print jobs (ticket 11): a real scannable Code 128
      // for an official PT line, so it can be printed before its goods are
      // accepted. Also a delivery's Labels step.
      {
        label: "Labels",
        to: "/goods/labels",
        goodsActions: ["label.print"],
      },
      // Goods-v1 opening stock (ticket 10): the manifest, its variances and the
      // opening PT. Its grants are the ones its own list read answers to
      // (`ptmapper/goods_manifest_views.py` READ_ACTIONS).
      {
        label: "Opening stock",
        to: "/goods/opening",
        goodsActions: [
          "pt.prepare.opening",
          "opening.manifest.approve",
          "opening.variance.approve",
          "pt.approve.opening",
        ],
      },
    ],
  },
  {
    code: "transfer",
    label: "Transfers",
    icon: Truck,
    layer: "outbound",
    items: [
      // The goods-v1 transfer lifecycle (OPS-06, store and warehouse operations
      // PRD §7 and §12): two lines, Transfers and Requests. *New transfer*,
      // *Dispatch* and *Receive* are actions on the record and *In transit* is a
      // filter on the list, so none of them is a line here.
      //
      // The legacy lines below keep their URLs and their gates. They are simply
      // not this section's strip any more; the Distribution grid in particular
      // is deferred by the PRD with its code and records kept.
      {
        label: "Transfers",
        to: "/goods/transfers",
        goodsActions: ["transfer.allocate", "transfer.move", "stock.accept"],
      },
      {
        label: "Requests",
        to: "/goods/transfers/requests",
        goodsActions: ["transfer.allocate"],
      },
      { label: "Stock Transfers", to: "/transfer" },
      { label: "Send Stock", to: "/transfer/new" },
      // Bulk-splits one arrived warehouse batch into a draft per destination
      // store — the grid Ops Head fills in once instead of raising the same
      // transfer by hand, store by store (#229's stub, rebuilt on the existing
      // engine rather than a posting module of its own).
      { label: "Distribution", to: "/transfer/distribution" },
      { label: "Stock Request", to: "/transfer/requests" },
      { label: "In-Transit", to: "/transfer/in-transit" },
    ],
  },
  {
    code: "stock_count",
    label: "Stock Count",
    icon: ClipboardCheck,
    layer: "controls",
    items: [
      { label: "Count Sessions", to: "/stock-count" },
      // Store operations ticket 35 (ST-INV-3): when each store's blind count is
      // due; drawn only where the scheduled-counts switch is on.
      { label: "Count Schedule", to: "/stock-count/schedule", storeFeature: "scheduled-counts" },
      // Goods-v1 counts at non-trading sites (ticket 17): start a count (which
      // freezes the site), count blind, and end it with no posting when it
      // matches the book. A goods-v1 line, for the reason given on "Vendor
      // bookings"; reviewers are listed too, since the variance is theirs.
      {
        label: "Goods counts",
        to: "/goods/counts",
        goodsActions: ["count.run", "count.review"],
      },
      // Corrections live where they are caused: a count is what produces them.
      // Both are writes gated on `stock_count: operate` server-side, so the
      // link must not open for a role the API will refuse (#94).
      { label: "Adjustments", to: "/stock-count/adjustments", minCapability: "operate" },
      { label: "Write-offs", to: "/stock-count/writeoffs", minCapability: "operate" },
    ],
  },
  {
    code: "return_to_brand",
    label: "Return to Brand",
    icon: Undo2,
    layer: "outbound",
    items: [
      { label: "Returns", to: "/return-to-brand" },
      { label: "New Return", to: "/return-to-brand/new" },
      // Quarantine is built — it is a tab on Stock, reached here by deep link.
      { label: "Damage / Quarantine", to: "/stock?view=quarantine", deepLink: true },
    ],
  },
  {
    code: "stock",
    label: "Stock",
    icon: Boxes,
    layer: "ledgers",
    items: [
      { label: "Stock on Hand", to: "/stock" },
      // Goods-v1 stock (ticket 07): physical, valued, unvalued, ATS/transfer,
      // holds, reservations and reasons, now or as of a past time — beside
      // the legacy Stock on Hand above, which speaks the pre-goods-v1 shape.
      // A goods-v1 line, for the reason given on "Vendor bookings" in Booking.
      {
        label: "Goods stock",
        to: "/goods/stock",
        goodsActions: ["stock.view", "stock.accept"],
      },
      // Goods-v1 movements (ticket 12): bin moves, holds and releases at one
      // site. A goods-v1 line, for the reason given on "Vendor bookings".
      // Approvers are listed too: a release they decided is read here.
      {
        label: "Goods movements",
        to: "/goods/movements",
        goodsActions: ["movement.draft", "movement.approve"],
      },
      // The counter's question, not the back office's: "who has this in L?"
      // (#175). It reads every store deliberately — a registered scoping
      // exception — and carries quantities only, so it sits at the same `view`
      // rung a store person already holds on this section.
      { label: "Search Across Stores", to: "/stock/search" },
      { label: "Movement History", to: "/stock/history" },
      // Store operations ticket 12 (ST-CMP-3): the pieces here with no HSN, for
      // fixing; drawn only where the hsn-on-every-item switch is on.
      { label: "Items with no HSN", to: "/stock/missing-hsn", storeFeature: "hsn-on-every-item" },
      // Store operations ticket 33 (ST-INV-2): stock aged against its season;
      // drawn only where the season-ageing switch is on.
      { label: "Stock Ageing", to: "/stock/ageing", storeFeature: "season-ageing" },
      // Store operations ticket 32 (ST-INV-1): style-colours missing too many core
      // sizes; drawn only where the broken-size switch is on.
      { label: "Broken Sizes", to: "/stock/broken-sizes", storeFeature: "broken-size" },
      // Store operations ticket 34 (ST-TRF-1): transfers suggested to fill broken
      // sizes; drawn only where the size-balancing switch is on.
      { label: "Size Balancing", to: "/stock/size-balancing", storeFeature: "size-balancing" },
      // Not a menu item — an ownership action reached from Stock on Hand.
      // Relabelling who owns stock is `stock: manage` on the server (#94).
      { label: "V-Flip", to: "/stock/vflips", action: true, minCapability: "manage" },
    ],
  },
  {
    code: "money",
    label: "Money",
    icon: Wallet,
    layer: "controls",
    // The sheet gives store and warehouse "Expenses only (create)" while
    // Accounts and Owner get the whole section. Capability can't say that — both
    // hold `operate` — so the books themselves carry the finance gate they
    // already have on the server, and Money collapses to one Expenses line for
    // everyone else.
    items: [
      // The Day Summary moved under Reports (store operations ticket 10, PRD §17),
      // keeping the `money: view` gate its API answers to.
      { label: "Payments", to: "/money/payments", planned: true, minCapability: "manage" },
      // The store × month rupee target the Dashboard is measured against (#171).
      // It is a master, so Setup is where a reader might look for it - but the
      // gate it answers to is `money: manage`, and a nav item's gate *is* its
      // section's (routeAccess derives one from the other). Filed under Setup it
      // would show the Data Steward a screen the API refuses them, which is the
      // sidebar-lies-about-the-API failure #85 exists to prevent. Reading it
      // needs only the Money section, so the item carries no `minCapability`:
      // a store person sees their own store's number and the cells are text.
      { label: "Store Targets", to: "/money/store-targets" },
      { label: "Vendor Ledger", to: "/money/vendor", minCapability: "manage" },
      { label: "Cash", to: "/money/cash", minCapability: "manage" },
      // Same day, split into money in vs out per account instead of one
      // running balance (#254) — the daily read Accounts asked for directly,
      // reading the same `CashLedgerEntry` rows "Cash" already lists.
      { label: "Daily Cash", to: "/money/daily-cash", minCapability: "manage" },
      // The partner-billing dial (Rule 12): filed under Money, not Setup, for
      // the same reason Store Targets is — the gate deciding the filing is
      // `money`, which Owner and Accounts already hold, and putting it behind
      // Setup's `sell: manage` rung (as it briefly was) walled Owner out of a
      // screen the API itself lets them read and write.
      { label: "Partner Billing", to: "/money/partner-billing" },
      { label: "Partner Dues", to: "/money/partner-dues" },
      // The B2B bills head office still owes an e-invoice reference (#187).
      //
      // Filed under Money rather than Sell, and that is the gate deciding the
      // filing rather than the other way round. Raising an IRN is a statutory
      // duty of the people who file the returns; `sell: operate` is the *store*,
      // which would put it on a cashier's sidebar, and `sell: manage` is the IT
      // administrator alone, who holds no money on the ratified sheet. The
      // endpoint behind it is gated at `money: manage` for the same reason, so
      // the sidebar and the API agree (#85).
      { label: "IRN Queue", to: "/money/irn-queue", minCapability: "manage" },
      // Store operations ticket 38 (ST-REC-3): debit notes drafted for receiving
      // shortages. Accounts reviews and issues them, the Owner approves them in
      // the approvals inbox; both hold `money: manage`, which the API answers to.
      {
        label: "Debit Notes",
        to: "/money/debit-notes",
        minCapability: "manage",
        storeFeature: "debit-note-draft",
      },
      // Store operations ticket 42 (ST-MNY-3): the store's petty cash box. The
      // store records spends and top-ups ("Expenses only (create)" is `money:
      // operate`); head office sets the float and custodian at `manage`.
      {
        label: "Petty Cash",
        to: "/money/petty-cash",
        minCapability: "operate",
        storeFeature: "petty-cash",
      },
      // Store operations ticket 28 (ST-MNY-4): what is owed, paid and due to each
      // outright brand or vendor. Accounts records, the Owner reads; both hold
      // `money: manage`, which the API answers to. Never a store role.
      {
        label: "Payables",
        to: "/money/payables",
        minCapability: "manage",
        storeFeature: "brand-payables",
      },
      { label: "Bank Reconciliation", to: "/money/bank", minCapability: "manage" },
      { label: "Collections", to: "/money/collections", planned: true, minCapability: "manage" },
      { label: "Expenses", to: "/money/expenses", planned: true },
      { label: "Tally", to: "/money/tally", planned: true, minCapability: "manage" },
    ],
  },
  {
    // Store operations PRD §5.1 (ticket 23): a Brands menu. Terms first; Brand
    // Reports, Claims and Margin Share join it with their own tickets. Drawn
    // from the Setup grant (`grantedBy`), so a store person, who holds no Setup,
    // never sees it, and Terms only for its readers; the server refuses the rest
    // too. Only where brand terms are on.
    code: "brands",
    label: "Brands",
    icon: Handshake,
    layer: "master",
    grantedBy: "setup",
    items: [
      {
        label: "Terms",
        to: "/brands/terms",
        storeFeature: "brand-terms",
        roles: BRAND_TERM_READERS,
      },
      // Store operations ST-BRD-4 (ticket 27): the monthly margin share statement
      // per brand, labelled an estimate. A brand's margin is the commercial side
      // of its terms, so its API needs `money: manage` too: Accounts and Owner
      // only; drawn only where the margin-share switch is on.
      {
        label: "Margin Share",
        to: "/brands/margin-share",
        storeFeature: "margin-share",
        dataGate: { section: "money", minCapability: "manage" },
      },
      // Store operations ST-BRD-3 (ticket 26): the brand-funded share of the
      // month's discounts claimed from each brand and settled by its credit
      // note. Money work: its API needs `money: manage` (Accounts and Owner),
      // never a store role; drawn only where the brand-discount-claims switch is on.
      {
        label: "Claims",
        to: "/brands/claims",
        storeFeature: "brand-discount-claims",
        dataGate: { section: "money", minCapability: "manage" },
      },
      // Store operations ST-BRD-2 (ticket 29): each brand's Sale and SOH report in
      // its own saved layout, made monthly and on demand. The reports' API is
      // `reports: view` over the person's own stores; drawn only where the
      // brand-reports switch is on. Accounts saves the layouts.
      {
        label: "Reports",
        to: "/brands/reports",
        storeFeature: "brand-reports",
        dataGate: { section: "reports", minCapability: "view" },
      },
      // Store operations ST-BRD-5 (ticket 24): SOR stock aged from the brand's
      // dispatch date, to settle before the brand must invoice it. Which stock is
      // SOR is a brand term, so its API needs `money: manage` (Accounts and
      // Owner); drawn only where the sor-ageing switch is on.
      {
        label: "SOR Ageing",
        to: "/brands/sor-ageing",
        storeFeature: "sor-ageing",
        dataGate: { section: "money", minCapability: "manage" },
      },
    ],
  },
  {
    code: "offers_price",
    label: "Offers & Price",
    icon: Tag,
    layer: "intelligence",
    // Consolidated from five entries to three "pillars" (Aug 2026 redesign):
    // authoring a rule and running the EOSS markdown ladder both just make more
    // rows in the same `Offer` table, so "New Offer" and "EOSS Planning" moved
    // inside Promotions as its own in-page tabs/CTA rather than sidebar heads of
    // their own — `deepLink`-style reachability, without a fold's shared URL.
    // Their routes (`/offers/new`, `/offers/eoss`) are unchanged; only the
    // sidebar shrank.
    //
    // OPS-10 (store and warehouse operations PRD §11) adds Running Offers at the
    // front and hides Price Book. Both are navigation changes and nothing else:
    // Price Book keeps its screen, its route and its records, and offer
    // authoring stays with the roles the RBAC v1 grid already permits.
    //
    // EOSS Planning gains an entry of its own here for the first time, and
    // gains it `hidden`: the Offers row is a strip now, a strip's tabs are the
    // section's own entries, and EOSS had none to be a tab of. Hidden keeps the
    // Aug 2026 consolidation exactly as it was - no sidebar line - while giving
    // the row the tab the PRD asks for.
    items: [
      { label: "Running Offers", to: "/offers/running" },
      { label: "Price Book", to: "/offers/price-list", hidden: true },
      { label: "Promotions", to: "/offers" },
      { label: "EOSS Planning", to: "/offers/eoss", hidden: true },
      // Discount Reports moved under Reports (store operations ticket 10, PRD §17),
      // keeping the `offers_price: view` gate its API answers to.
    ],
  },
  {
    code: "hrms",
    label: "People",
    icon: Users,
    layer: "edges",
    items: [
      { label: "Attendance", to: "/staff/attendance", planned: true },
      // Store operations ST-HR-1 (ticket 07): one staff list per store - who is
      // assigned, who is active, who sells at the till. Everyone with HRMS reads
      // their own stores' lists; drawn only where the staff-list switch is on.
      { label: "Staff List", to: "/staff/list", storeFeature: "staff-list" },
      // A cashier holds HRMS for their *own* attendance ("Own attendance
      // (derived)"), so employee records and salary stay off their menu. A store
      // *manager* holds `hrms: manage` for their own store — the sketch makes
      // managing the store's members their job — so Member Details appears for
      // them and not for the cashier, from the same server-sent capability.
      {
        label: "Member Details",
        to: "/staff/members",
        minCapability: "manage",
        planned: true,
      },
      { label: "Payroll", to: "/staff/payroll", roles: PAYROLL_ROLES, planned: true },
    ],
  },
  {
    code: "reports",
    label: "Reports",
    icon: BarChart3,
    layer: "intelligence",
    items: [
      // Store operations ST-RPT-1 (ticket 10): the Sales report, read from the
      // reporting copy kept apart from billing. Everyone holding Reports reads
      // their own stores' figures; drawn only where the sales-report switch is on.
      // The server keeps cost and margin from anyone without `money: manage`.
      { label: "Sales Reports", to: "/reports/sales", storeFeature: "sales-report" },
      // Store operations ST-RPT-5 (ticket 47): the GST report, prepared for
      // Accounts to file. Its API also needs `money: manage` (the IRN queue's
      // gate), so only Accounts and Owner are drawn the line; drawn only where
      // the gst-reports switch is on.
      {
        label: "GST Reports",
        to: "/reports/gst",
        storeFeature: "gst-reports",
        dataGate: { section: "money", minCapability: "manage" },
      },
      // Store operations ST-CMP-7 (ticket 14): gift stock and the input tax
      // credit to reverse on it, for Accounts. Read like the Sales report; the
      // server sends cost and credit only to `money: manage`. Drawn only where
      // the gift-stock-itc switch is on (gated: CA sign-off at a real store).
      { label: "Gift Stock", to: "/reports/gift-stock", storeFeature: "gift-stock-itc" },
      // Store operations ST-OFR-2 (ticket 25): who funds the discounts, the brand
      // or KDPS, per offer, brand and store. The commercial side of brand terms,
      // so its API needs `money: manage` too: Accounts and Owner only, never a
      // store role; drawn only where the discount-funding-split switch is on.
      {
        label: "Discount Funding",
        to: "/reports/discount-funding",
        storeFeature: "discount-funding-split",
        dataGate: { section: "money", minCapability: "manage" },
      },
      // Store operations ST-INV-4 (ticket 44): pieces and cost lost to shrinkage
      // as a share of sales. Everyone holding Reports reads their own stores';
      // the server sends cost only with `money: manage`, so store roles see
      // pieces. Drawn only where the shrinkage-report switch is on.
      { label: "Shrinkage", to: "/reports/shrinkage", storeFeature: "shrinkage-report" },
      // Store operations ST-RPT-2 (ticket 43): sell-through, weeks of cover, GMROI
      // and dead stock. Everyone holding Reports reads their own stores'; the
      // server sends cost, margin and GMROI only with `money: manage`, so store
      // roles see pieces. Drawn only where the inventory-report switch is on.
      { label: "Inventory", to: "/reports/inventory", storeFeature: "inventory-report" },
      // Store operations ST-RPT-3 (ticket 45): per brand and store, sales,
      // returns, sell-through and stock age. Everyone holding Reports reads their
      // own stores'; the server sends margin, commission and GMROI (estimates until
      // OQ-50) only with `money: manage`, so store roles never see them. Drawn
      // only where the brand-performance-report switch is on.
      {
        label: "Brand Performance",
        to: "/reports/brand-performance",
        storeFeature: "brand-performance-report",
      },
      // Store operations ST-RPT-6 (ticket 48): the counter's exceptions per
      // store and staff member. Everyone holding Reports reads their own
      // stores'; no cost is in it. Drawn only where the exceptions-report
      // switch is on.
      { label: "Exceptions", to: "/reports/exceptions", storeFeature: "exceptions-report" },
      // Store operations ST-RPT-4 (ticket 46): each salesperson's results from
      // split shares. Everyone holding Reports opens it; the server sends a
      // salesperson their own row and a manager the team, and no cost. Drawn
      // only where the staff-performance-report switch is on.
      {
        label: "Staff Performance",
        to: "/reports/staff",
        storeFeature: "staff-performance-report",
      },
      // PRD §17: the day summary, discount report and store dashboard move under
      // Reports. Each screen is unchanged and keeps the gate its API answers to.
      // What the counter took, by tender, and what the day left open (#188):
      // `money: view`, which both store seats hold ("Expenses only (create)").
      {
        label: "Day Summary",
        to: "/reports/day-summary",
        dataGate: { section: "money", minCapability: "view" },
      },
      // What the chain gave away and what it forgot to give (D11 §5, §8).
      {
        label: "Discount Reports",
        to: "/reports/discounts",
        dataGate: { section: "offers_price", minCapability: "view" },
      },
      // One store's Home (#174), reachable from Reports too. Its API answers to
      // `home: view`, which every role holds; a network person picks a store.
      { label: "Store Dashboard", to: "/reports/store-dashboard" },
      { label: "Stock Reports", to: "/reports/stock", planned: true },
      { label: "Profit", to: "/reports/profit", planned: true },
      { label: "Daily Summary", to: "/reports/daily", planned: true },
      { label: "Report Maker", to: "/reports/maker", planned: true },
    ],
  },
  {
    code: "setup",
    label: "Setup",
    icon: Settings,
    layer: "master",
    items: [
      { label: "Products", to: "/setup/products", planned: true },
      // Goods-v1 organisation setup (ticket 02): legal entities, registrations,
      // sites, SBUs, location trees, and a site's readiness/closure — separate
      // from the legacy "Stores" screen below, which speaks the pre-goods-v1
      // store shape (no site type, permitted operations or readiness at all).
      {
        label: "Organisation",
        to: "/setup/organisation",
        goodsActions: [
          "org.entity.manage",
          "org.site.manage",
          "org.tenant.manage",
          "org.location.manage",
          "org.location.store.manage",
          "org.location.warehouse_bin.manage",
          "org.site.lifecycle.run",
          "org.site.lifecycle.approve",
        ],
      },
      // Goods-v1 staff and access (ticket 03): people separate from logins,
      // assignments, roles/grants and privileged-change review.
      {
        label: "People and access",
        to: "/setup/people-access",
        goodsActions: ["staff.manage", "staff.retire", "access.manage", "access.review"],
      },
      // Goods-v1 products and parties (ticket 04): styles, SKUs, aliases,
      // source mappings, vendors, brands, seasons and sub-brands, and the
      // barcode lookup that resolves a shared code without merging anything.
      {
        label: "Products and parties",
        to: "/setup/products-parties",
        goodsActions: [
          "product.master.manage",
          "product.master.propose",
          "crosswalk.manage",
          "crosswalk.propose",
          "identity.resolve",
          "vendor.manage",
          "master.retire",
        ],
      },
      // Goods-v1 governed configuration (ticket 04): profile versions, rates,
      // tax rules, reasons, series and label settings, drafted and approved.
      {
        label: "Configuration",
        to: "/setup/configuration",
        goodsActions: ["config.draft", "config.approve"],
      },
      // Goods-v1 monitoring, durable exports and command lookup (ticket 18):
      // the queue, the evidence anchors, the numbering ceilings and the
      // verifier, plus a scoped export of the numbers.
      {
        label: "Monitoring and exports",
        to: "/setup/monitoring",
        goodsActions: ["ops.health.view", "export.run", "audit.view"],
      },
      { label: "Stores", to: "/setup/stores" },
      { label: "Brands", to: "/setup/brands" },
      // The vendor master had no screen at all: vendors could only be created
      // through the API, and never corrected. Bookings, GRNs and every payable
      // hang off this row, so it belongs beside Brands (one vendor, many brands).
      { label: "Vendors", to: "/setup/vendors" },
      { label: "Seasons", to: "/setup/seasons" },
      { label: "GSTINs", to: "/setup/gstins" },
      { label: "Users & Roles", to: "/setup/users", minCapability: "manage" },
      // The access matrix as its own screen (#173): Users & Roles edits one role
      // at a time, this compares all nine at once and is where the money floors
      // are visible. Same rung - reading who may do what is Setup's top rung.
      { label: "Access", to: "/setup/access", minCapability: "manage" },
      // Store operations feature switches per store (ST-OPS-6). Everyone holding
      // Setup reads it; only Admin changes a switch, which the server enforces.
      { label: "Feature Switches", to: "/setup/feature-switches" },
      // The demo probe's screen: drawn only while the demo probe feature is on
      // where the person works, and the probe exists only in development.
      { label: "Feature check (demo)", to: "/setup/feature-check", storeFeature: "demo-probe" },
      // Store operations ST-OPS-3 (ticket 02): the read-only audit log. Opens for
      // the existing `audit.view` grant only, and only while the audit log is
      // switched on where the person works; the server enforces both.
      {
        label: "Audit Log",
        to: "/setup/audit",
        goodsActions: ["audit.view"],
        storeFeature: "audit-log",
      },
      // Store operations §6 (ticket 03): versioned tax settings. Everyone holding
      // Setup reads it (Accounts included); only Admin saves a version, which the
      // server enforces. Drawn only where the tax-settings switch is on.
      { label: "Tax Settings", to: "/setup/tax-settings", storeFeature: "tax-settings" },
      // Store operations ST-OPS-4 (ticket 49): the task checklist templates. Everyone
      // holding Setup reads it; only Admin sets one, which the server enforces.
      // Drawn only where store checklists are switched on.
      { label: "Task Checklists", to: "/setup/checklists", storeFeature: "store-checklists" },
      // Store operations ST-CMP-6 (ticket 15): the consent questions' wording.
      // Everyone holding Setup reads it; only Admin saves a new version, which
      // the server enforces. Drawn only where customer consent is switched on.
      {
        label: "Consent Wording",
        to: "/setup/consent-wording",
        storeFeature: "customer-consent",
      },
      // Store operations ST-CMP-5 (ticket 04): site prefixes and the number series.
      // Everyone holding Setup reads it; only Admin changes it, which the server
      // enforces. Drawn only where the document-series switch is on.
      {
        label: "Document Numbering",
        to: "/setup/document-numbering",
        storeFeature: "document-series",
      },
      // Store operations section 29 (ticket 07): the old salesperson rows and the
      // staff records they were matched to. Everyone holding Setup reads it; only
      // Admin resolves an unmatched row, which the server enforces. Not behind a
      // switch: it finishes moving existing records, which is never new work.
      { label: "Salesperson Matches", to: "/setup/salesperson-matches" },
    ],
  },
];

/** Every pre-#87 URL → its new home, longest prefix first. A path under an old
 *  prefix keeps its tail: `/documents/bookings/12` → `/booking/12`. */
const LEGACY_PREFIXES: [from: string, to: string][] = [
  ["/sell/returns", "/sell?mode=return"],
  ["/documents/bookings", "/booking"],
  // The goods-v1 booking screen, folded into the one Bookings screen. Its
  // `?booking=<id>` (or `new`) rides along and the list opens that booking.
  ["/goods/bookings", "/booking"],
  ["/documents/transfers", "/transfer"],
  // The old "Returns" stub covered both halves; customer returns are Sell's.
  ["/documents/returns", "/sell?mode=return"],
  ["/documents/sales", "/sell"],
  ["/documents/payments", "/money/payments"],
  ["/outbound/transfers", "/transfer"],
  ["/outbound/rtvs", "/return-to-brand"],
  ["/outbound/adjustments", "/stock-count/adjustments"],
  ["/outbound/writeoffs", "/stock-count/writeoffs"],
  ["/outbound/vflips", "/stock/vflips"],
  ["/ledgers/stock-on-hand", "/stock"],
  ["/ledgers/stock", "/stock/history"],
  ["/ledgers/vendor", "/money/vendor"],
  ["/ledgers/cash", "/money/cash"],
  ["/masters/stores", "/setup/stores"],
  ["/masters/brands", "/setup/brands"],
  ["/masters/vendors", "/setup/vendors"],
  ["/masters/seasons", "/setup/seasons"],
  ["/masters/gstins", "/setup/gstins"],
  ["/masters/users", "/setup/users"],
  ["/store/sell", "/sell"],
  ["/store/count", "/stock-count"],
  ["/store/transfer", "/transfer"],
  ["/controls/exceptions", "/alerts"],
  ["/controls/approvals", "/action-needed?show=approvals"],
  // Home's four lines became three (23 Sep 2026): the approvals inbox and the
  // goods exceptions are both on Action Needed now.
  ["/approvals", "/action-needed?show=approvals"],
  ["/goods/exceptions", "/action-needed?show=exceptions"],
  // The separate goods receiving screens OPS-17 took out (PRD §12, "Exception
  // for Receive Goods"). None is a screen any more: an old bookmark lands where
  // that work now lives - the inbox, whose deliveries walk those steps, or PT
  // Work, which keeps a `?pt=`/`?grn=` so the grid still opens on the same PT.
  ["/goods/arrivals", "/goods/receive"],
  ["/goods/receipts", "/goods/receive"],
  ["/goods/accept", "/goods/receive"],
  ["/goods/pt/prepare", "/goods/pt-work"],
  ["/goods/pt/approvals", "/goods/pt-work?tab=approve"],
  ["/controls/audit", "/setup/audit"],
  // Reconciliation is parked until Money is designed; bank rec is its nearest
  // named home, so the old link lands somewhere honest rather than nowhere.
  ["/controls/recon", "/money/bank"],
  ["/intel/dashboards", "/reports/sales"],
  // Store operations ticket 10 (PRD §17): two report screens moved under Reports.
  ["/money/day-summary", "/reports/day-summary"],
  ["/offers/discounts", "/reports/discounts"],
  ["/intel/profitability", "/reports/profit"],
  ["/intel/dead-stock", "/reports/stock"],
  ["/intel/forecast", "/reports/stock"],
  ["/intel/reports", "/reports/maker"],
  ["/edges/rbac", "/setup/users"],
  ["/edges/tally", "/money/tally"],
  ["/edges/integrations", "/setup/settings"],
  ["/edges/pos", "/setup/settings"],
  ["/edges/config", "/setup/settings"],
  // The standalone /sell/returns route was merged into the counter in return
  // mode: /sell?mode=return. Any saved link or bookmark still arrives correctly.
  ["/sell/returns", "/sell?mode=return"],
].sort((a, b) => b[0].length - a[0].length) as [string, string][];

/** Every address the manifest has moved away from. The route table reads it to
 *  spot the one that a dynamic route would otherwise claim (see routes.tsx),
 *  so which paths are legacy is stated once, here. */
export const LEGACY_PATHS: string[] = LEGACY_PREFIXES.map(([from]) => from);

/** Does `pathname` sit at or under `prefix`? (`/stock` ≠ `/stock-count`.) */
export function underPrefix(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(prefix + "/");
}

/** One pathname shape for everything that keys on a path. Lowercase, because
 *  React Router matches paths case-insensitively and a bookmarked
 *  `/Ledgers/Vendor` must behave like `/ledgers/vendor`; trailing slashes
 *  dropped, because `/money/vendor/` is the same screen as `/money/vendor`. */
export function normalizePath(pathname: string): string {
  return pathname.toLowerCase().replace(/\/+$/, "") || "/";
}

/** The new home of an old URL, or null if `pathname` is not a legacy path. */
export function resolveLegacyPath(pathname: string): string | null {
  const normalized = normalizePath(pathname);
  for (const [from, to] of LEGACY_PREFIXES) {
    if (!underPrefix(normalized, from)) continue;
    // A destination carrying state in its query is an exact screen move, not a
    // prefix move. Appending a child path after `?mode=return` would manufacture
    // a malformed address such as `/sell?mode=return/anything`.
    if (to.includes("?")) return normalized === from ? to : null;
    return to + normalized.slice(from.length);
  }
  return null;
}

/** Flat list of every item in the manifest, tagged with its owning section. */
export const NAV_ITEMS: (NavItem & { section: string })[] = SECTIONS.flatMap((s) =>
  s.items.map((i) => ({ ...i, section: s.code })),
);

/** The path part of an item's `to` (drops the `?view=quarantine` deep link). */
export function itemPath(item: NavItem): string {
  return item.to.split("?")[0];
}

/** The deepest eligible item whose path `pathname` sits at or under. Longest
 *  wins, so `/goods/receive/history` is History and not Pending, while
 *  `/booking/12` — a document with no line of its own — still resolves to
 *  Bookings. */
function deepestUnder(
  pathname: string,
  eligible: (item: NavItem) => boolean,
): (NavItem & { section: string }) | null {
  const normalized = normalizePath(pathname);
  let best: (NavItem & { section: string }) | null = null;
  for (const item of NAV_ITEMS) {
    if (!eligible(item)) continue;
    const path = itemPath(item);
    if (!underPrefix(normalized, path)) continue;
    if (!best || path.length > itemPath(best).length) best = item;
  }
  return best;
}

/** The one screen a URL belongs to — what the route guard gates on. A deep link
 *  owns no URL (the section hosting the screen keeps it), but an `action` screen
 *  does own one and carries its own gate, so it counts here. */
export function itemOwning(pathname: string): (NavItem & { section: string }) | null {
  return deepestUnder(pathname, (i) => !i.deepLink);
}

/** Does this menu line get the highlight at `pathname`? Exactly one line does.
 *
 *  Pass the item, not its path: "Damage / Quarantine" *is* `/stock` once its
 *  `?view=` is dropped, so comparing paths alone would light it alongside Stock
 *  on Hand in another section. Screens with no line of their own fall back to
 *  the nearest one that has — a V-flip keeps Stock on Hand lit, the same way a
 *  booking document keeps Bookings lit — so the sidebar never goes dark under
 *  you. Letting React Router answer this instead lit every ancestor too. */
export function isActiveItem(item: NavItem, pathname: string): boolean {
  if (item.deepLink || item.action || item.hidden) return false;
  // A hidden entry is not a menu line, so standing on it lights the nearest one
  // that is - exactly as an `action` screen and a document with no line of its
  // own already do. Without this, hiding Price Book would leave the sidebar dark
  // for anyone who deep-linked to it.
  const lit = deepestUnder(pathname, (i) => !i.deepLink && !i.action && !i.hidden);
  return !!lit && itemPath(item) === itemPath(lit);
}

/** Is this item on the menu for a caller holding `held` on the item's section?
 *  The sidebar gates the menu line and `routeAccess` gates the URL, reading two
 *  different halves of the same login payload — so the rule itself lives here
 *  once and the two cannot drift into hiding a link the URL still opens. */
export function itemVisible(
  item: NavItem,
  held: string | undefined,
  roleCode: string,
  isSuperuser: boolean,
  goodsActions: readonly string[] = [],
  featuresOn: ReadonlySet<string> = NO_STORE_FEATURES,
): boolean {
  if (!storeFeatureOpen(item, featuresOn)) return false;
  if (isSuperuser) return true;
  // A goods-v1 line answers to its own action grants alone (`NavItem.goodsActions`).
  if (item.goodsActions) return goodsItemVisible(item, goodsActions);
  // A legacy line a goods grant also opens (`NavItem.orGoodsActions`).
  if (orGoodsVisible(item, goodsActions)) return true;
  if (item.minCapability && !meetsCapability(held, item.minCapability)) return false;
  if (item.roles && !item.roles.includes(roleCode)) return false;
  return true;
}

/** Does this session hold one of the goods grants that also open a legacy line?
 *  Fail-closed like `goodsItemVisible`. */
export function orGoodsVisible(item: NavItem, goodsActions: readonly string[]): boolean {
  return (item.orGoodsActions ?? []).some((a) => goodsActions.includes(a));
}

/** Does this session hold one of a goods-v1 line's action grants? Fail-closed:
 *  no actions, or a line with none listed, is no. */
export function goodsItemVisible(item: NavItem, goodsActions: readonly string[]): boolean {
  return (item.goodsActions ?? []).some((a) => goodsActions.includes(a));
}

// ---------------------------------------------------------------------------
// Persona layouts (#96, reshaped by #170) - how one persona's sidebar is
// *arranged*.
//
// The sections above are shared vocabulary and the server decides which of them
// a person gets. Neither changes here. What changes is the order they are drawn
// in, and whether several of them are drawn as one *folded* page.
//
// D10 settled the store login's shape: ten flat sections, and no subsections in
// the sidebar, ever - anything that needs dividing divides inside the page, as
// tabs. So #96's grouping heading (a label with three section heads nested
// under it) is gone, and in its place is a fold: one link, one URL, and the
// folded sections' screens drawn on that page as tabs.
//
// Three rules this must not break, all of them tested below in navConfig.test:
//   · Arranging runs *after* access, over whatever the server's section list
//     leaves standing. It can only ever draw less, never more.
//   · A tab's gate is the gate of the menu entry it draws. Folding is
//     presentation, so a tab can never open a screen the sidebar would have
//     hidden - a role without count rights simply sees no Count & Adjust tab.
//   · Names never change, with two conscious exceptions Anand made for the
//     store persona alone (#84 as amended, #229): Home reads "Dashboard" and
//     HRMS reads "Attendance" there. Every other section keeps the word the
//     warehouse and HO use for it - a store person and the warehouse both
//     say "Transfer".
//
// A role with no layout gets the flat list, which is what keeps every other
// persona byte-identical to before this file grew this section.

/** One tab of a folded page. */
export interface FoldTab {
  /** The `?tab=` value. Stable, because it ends up in people's URLs. */
  slug: string;
  label: string;
  /** The manifest entry whose screen this tab draws, by its `to`. Naming the
   *  entry rather than restating a section code is what keeps the tab's gate
   *  and the screen's standalone URL in one place. */
  entry: string;
  /** A goods-v1 line of a folded section (GSA-T02), drawn as a tab that opens
   *  its own standalone screen rather than a panel on the folded page. Set only
   *  by `foldTabs`, never in a fold's own definition. */
  link?: true;
}

/** Several sections drawn as one link, their screens recomposed as tabs on one
 *  page. Unlike a section head it owns a URL - but it owns no section code and
 *  no capability, because every tab keeps the gate of the entry it draws. */
export interface NavFoldDef {
  heading: string;
  icon: LucideIcon;
  /** CSS `--layer-*` token suffix, as on a section. */
  layer: string;
  /** The one URL. Its tabs are `?tab=<slug>` under it. */
  to: string;
  /** Section codes this fold stands in for; their heads leave the sidebar. */
  sections: string[];
  tabs: FoldTab[];
}

/** One section drawn as a single sidebar link, its own screens recomposed as a
 *  horizontal tab row on those screens themselves (#227).
 *
 *  A fold merges several sections onto one new URL it owns. A strip owns no URL
 *  at all: its tabs are the section's existing canonical routes, and the tab row
 *  is plain navigation between them (grill decision 1). So no screen is edited
 *  to gain tabs, no route is added, and a bookmark keeps working - the strip is
 *  presentation laid over URLs that already exist.
 *
 *  It owns no capability either: every tab is a menu entry of `section`, and
 *  keeps that entry's gate. Folding a section can only ever subtract. */
export interface NavStripDef {
  /** The section code this row stands for. Its head leaves the sidebar. */
  section: string;
  /** This persona's name for the row. Absent ⇒ the section's own label. */
  label?: string;
  /** The entries drawn as tabs, by their `to`, in display order. */
  tabs: string[];
  /** Which of the section's goods-v1 lines this row appends after `tabs`, by
   *  their `to`, in display order. Absent ⇒ all of them, which is the older and
   *  still-default behaviour; `[]` ⇒ none, for a row that names every tab it
   *  wants in `tabs` itself, goods lines included, in the order it wants them.
   *
   *  Naming them became necessary when the receiving inbox landed (OPS-04): the
   *  store and warehouse operations PRD §12 gives the store Pending and History
   *  and the warehouse those plus PT work, labels and opening stock, and
   *  "append every goods line in the section" cannot tell the two apart. Like
   *  `tabs`, this can only ever subtract: a line named here that access already
   *  hid stays hidden, and a line left out keeps its own URL and its own gate. */
  goodsTabs?: string[];
}

/** One row of a persona's sidebar: a section code, a fold, or a strip. */
export type LayoutRow = string | NavFoldDef | NavStripDef;

/** Is this layout row a strip? A fold names several `sections`; a strip names
 *  the one `section` it is. The two predicates are each other's complement over
 *  the non-string rows, and every place that has to tell the shapes apart calls
 *  one of them rather than re-writing the test. */
export function isStripRow(row: LayoutRow): row is NavStripDef {
  return typeof row !== "string" && "section" in row;
}

/** Is this layout row a fold? */
export function isFoldRow(row: LayoutRow): row is NavFoldDef {
  return typeof row !== "string" && !isStripRow(row);
}

/** The rows, top to bottom. A section held but named nowhere here is still
 *  drawn - appended after these rows - so retuning somebody's access can never
 *  silently lose them a section. */
export type PersonaLayout = LayoutRow[];

// Inventory (D10 §1): Stock, Stock Count and Return to Brand fold into one
// page. Four tabs over three section codes - Damage & Quarantine and Return to
// Brand are both Return to Brand's, which is where a store's "mark damage only"
// right already lives.
export const INVENTORY_FOLD: NavFoldDef = {
  heading: "Stock",
  icon: Warehouse,
  layer: "store",
  to: "/inventory",
  sections: ["stock", "stock_count", "return_to_brand"],
  tabs: [
    { slug: "stock", label: "Stock on Hand", entry: "/stock" },
    // The store persona's only way to this screen until the Dashboard's
    // quick-actions row lands (#174): the fold *is* their Stock section, so a
    // tab left off it is unreachable for exactly the people it was built for.
    { slug: "search", label: "Search Across Stores", entry: "/stock/search" },
    // Ticket 12: drawn only where the hsn-on-every-item switch is on (its entry's gate).
    { slug: "hsn", label: "Items with no HSN", entry: "/stock/missing-hsn" },
    // Ticket 33: drawn only where the season-ageing switch is on (its entry's gate).
    { slug: "ageing", label: "Stock Ageing", entry: "/stock/ageing" },
    // Ticket 32: drawn only where the broken-size switch is on (its entry's gate).
    { slug: "broken", label: "Broken Sizes", entry: "/stock/broken-sizes" },
    // Ticket 34: drawn only where the size-balancing switch is on (its entry's gate).
    { slug: "balance", label: "Size Balancing", entry: "/stock/size-balancing" },
    { slug: "damage", label: "Damage & Quarantine", entry: "/stock?view=quarantine" },
    { slug: "count", label: "Count & Adjust", entry: "/stock-count" },
    // Ticket 35: drawn only where the scheduled-counts switch is on (its entry's gate).
    { slug: "schedule", label: "Count Schedule", entry: "/stock-count/schedule" },
    { slug: "returns", label: "Return to Brand", entry: "/return-to-brand" },
  ],
};

// Sell's published pair after the counter absorbed Return & Exchange and
// Customers (#267). The hidden routes above retain their guard while slice 8
// moves the real flow; they are deliberately not navigation tabs.
export const SELL_STRIP: NavStripDef = {
  section: "sell",
  // Bills sits between the counter and the counter's own state, because that is
  // the order a person moves through them: ring it up, look it up, then ask why
  // it has not gone in (OPS-08).
  tabs: ["/sell", "/sell/bills", "/sell/till", "/sell/cash-count", "/sell/gift-vouchers"],
};

// Receive Goods as the store sees it (store and warehouse operations PRD §12):
// the inbox's two tabs and nothing else. The legacy Receive line left the row in
// OPS-17 and the manifest in OPS-18, with its screen.
export const RECEIVE_INBOX_STRIP: NavStripDef = {
  section: "receive_goods",
  tabs: ["/goods/receive", "/goods/receive/history"],
  goodsTabs: [],
};

// The same row at the warehouse, plus the three things only the warehouse does
// (PRD §5.1, §12): PT Work (To prepare, To approve, Mapping rules), Labels and
// Opening Stock. Five tabs.
export const WAREHOUSE_RECEIVE_STRIP: NavStripDef = {
  section: "receive_goods",
  tabs: [
    "/goods/receive",
    "/goods/receive/history",
    "/goods/pt-work",
    "/goods/labels",
    "/goods/opening",
  ],
  goodsTabs: [],
};

// Stock as the store and warehouse operations PRD §12 names it: one row with
// Stock and Movements, quantity by location and by availability, from the
// goods-v1 records. The three legacy lines follow at the end: they keep their
// URLs and their own gates, and nothing that was reachable stops being
// reachable - the PRD's "read-only history behind the tabs, not a competing
// view".
//
// The warehouse's row only. The store keeps D10's ten rows, where the Stock
// section is one of the three the Inventory fold speaks for; its goods lines
// are that fold's own links, so both screens stay reachable there.
//
// `goodsTabs: []` because both goods lines are named in `tabs` themselves, in
// the order the PRD puts them.
export const STOCK_STRIP: NavStripDef = {
  section: "stock",
  tabs: [
    "/goods/stock",
    "/goods/movements",
    "/stock",
    "/stock/search",
    "/stock/history",
    "/stock/missing-hsn",
    "/stock/ageing",
    "/stock/broken-sizes",
    "/stock/size-balancing",
  ],
  goodsTabs: [],
};

// Transfer as the store and warehouse operations PRD §12 names it: two tabs,
// Transfers and Requests, and the same row at both personas. *New transfer*,
// *Dispatch* and *Receive* are actions on the transfer record and *In transit*
// is a filter on the list, so none of them is a tab (PRD §7).
//
// Send Stock, Stock Request, In-Transit and the Distribution grid leave the
// row: In transit is a filter now, sending is an action on the record, and the
// grid is deferred by the PRD with its code and records kept. All four stay in
// the manifest with their URLs and their own gates, exactly as they were.
//
// The legacy Stock Transfers screen stays at the end of the row: a person with
// no goods grants would otherwise be left with an empty Transfer row, and
// nothing that was reachable may stop being reachable.
//
// `goodsTabs: []` because both goods lines are named in `tabs` themselves, in
// the order the PRD puts them.
export const TRANSFER_STRIP: NavStripDef = {
  section: "transfer",
  tabs: ["/goods/transfers", "/goods/transfers/requests", "/transfer"],
  goodsTabs: [],
};

// Booking, one destination per persona (store and warehouse operations PRD
// §12): the one Bookings screen, which lists both booking engines' bookings as
// the same cards. What a person may *do* there is the screen's own question -
// each booking draws only the actions its engine grants this person - so the
// store's read-only list and the warehouse's workspace are the same screen
// under different grants. *New Booking* is that screen's button, not a tab.
export const BOOKING_STRIP: NavStripDef = {
  section: "booking",
  tabs: ["/booking"],
  goodsTabs: [],
};

// Offers & Price as the store and warehouse operations PRD §11 names it:
// Running Offers first, then the permitted read views. It replaces the
// hand-coded `PromotionsTabs`, which drew its own two-tab row inside two screens
// - a second tab mechanism beside this one, with its own idea of who may see
// what. A strip cannot show a screen the sidebar would have hidden, which is the
// property the hand-coded row never had.
//
// Price Book is not a tab and not a line: PRD §11 hides it for this increment.
// Its screen, its route and its records are untouched, and a deep link still
// opens it for whoever holds the section.
export const OFFERS_STRIP: NavStripDef = {
  section: "offers_price",
  tabs: ["/offers/running", "/offers", "/offers/eoss"],
};

/** Strips every persona is drawn, whatever their layout.
 *
 *  The general rule is that only a persona whose *sidebar* strips a section gets
 *  a tab row on its screens, so an expanded sidebar is never copied onto the
 *  page. Offers & Price is the exception, and it was already the exception
 *  before this file knew the word: EOSS Planning and the authoring screens have
 *  no sidebar line at all, so the row is the only navigation between them, and
 *  `PromotionsTabs` drew one for everybody. This replaces that component rather
 *  than leaving a second mechanism beside it.
 *
 *  It only ever adds a tab row. Which sections a no-layout persona's sidebar
 *  draws, and how, is untouched. */
export const UNIVERSAL_STRIPS: NavStripDef[] = [OFFERS_STRIP];

// The store's own screen, as D10 decided it on 30 July 2026: ten sections, in
// this order, one row each, and no subsections ever - anything a section needs
// to divide does it inside the page, as tabs (#229). Sell and Inventory proved
// the two mechanisms first (#227, #170); this slice turns the rest into strips.
// Home becomes "Dashboard" - the store dashboard that carries approvals and
// alerts as cards (#174) - and HRMS becomes "Attendance", both consciously
// renamed for the store persona alone (see the amendment above). Approvals and
// Alerts stay in the manifest as Home's own items; they are simply not one of
// Dashboard's tabs, so they stay reachable by URL and from the bell, never from
// this row.
const STORE_LAYOUT: PersonaLayout = [
  { section: "home", label: "Today", tabs: ["/"] },
  SELL_STRIP,
  // Store operations ticket 16 (PRD §5.1): Customers, its own row after Sell,
  // drawn only where customer rights are on.
  { section: "customers", tabs: ["/customers"] },
  // Store operations ticket 20 (PRD §5.1): Customer Orders, drawn only where
  // customer reservations or special orders (ticket 21) are on.
  { section: "customer_orders", tabs: ["/orders/reservations", "/orders/special"] },
  // Inventory still speaks for the Stock section here, so the store keeps D10's
  // ten rows and reaches Goods stock and Goods movements as this fold's own
  // goods links. The Stock strip below is the warehouse's row alone.
  INVENTORY_FOLD,
  // The store's Receive Goods row is the inbox, and only the inbox (store and
  // warehouse operations PRD §12): Pending and History, no PT work, no labels,
  // no opening stock. The legacy Receive screen and every separate goods screen
  // keep their URLs and their gates - they are simply not this row's tabs, and
  // the workflow behind the inbox is where a store person meets them.
  RECEIVE_INBOX_STRIP,
  TRANSFER_STRIP,
  // One booking destination (OPS-10, PRD §12): the goods workspace, read-only
  // for a store person because `booking.manage` is what draws its actions.
  BOOKING_STRIP,
  // The Day Summary moved under Reports (ticket 10, PRD §17).
  // Petty Cash (ticket 42) is drawn only where its switch is on.
  { section: "money", tabs: ["/money/store-targets", "/money/petty-cash", "/money/expenses"] },
  OFFERS_STRIP,
  {
    section: "reports",
    // Ticket 10 (PRD §17): the Sales report, then the day summary, discount
    // report and store dashboard that moved here, then the planned reports.
    tabs: [
      "/reports/sales",
      // Ticket 46: a salesperson's own results, drawn only where its switch is on.
      "/reports/staff",
      "/reports/day-summary",
      "/reports/discounts",
      "/reports/store-dashboard",
      "/reports/stock",
      "/reports/profit",
      "/reports/daily",
      "/reports/maker",
    ],
  },
  // Member Details and Payroll are not tabs here - a store manager's extra
  // `hrms: manage` rung would otherwise add Member Details, and D10 §10 keeps
  // this row to the one built screen (Attendance is a placeholder; the grill
  // rules the manager loses nothing real today).
  // Store operations ticket 07: the store's own staff list joins it, drawn only
  // where the staff-list switch is on.
  { section: "hrms", label: "People", tabs: ["/staff/attendance", "/staff/list"] },
];

// The warehouse's own arrangement: the rows the store and warehouse operations
// PRD §12 names for this persona, each one sidebar entry - Home, Booking,
// Receive Goods (the inbox plus this site's PT, label and opening-stock work),
// Transfer, Stock, Offers & Price. Sell is absent because the RBAC v1 sheet
// gives this role none of it, so the server never sends the section.
//
// Money, Reports and Attendance keep "existing presentation" (PRD §12), which is
// what the section rows below are: whatever the manifest lists, drawn the way
// every non-persona login draws them. Sections the PRD does not name at all -
// Stock Count, Return to Brand, Setup - are not listed here and are appended
// after these rows by `applyLayout`, in the server's order, exactly as before.
const WAREHOUSE_LAYOUT: PersonaLayout = [
  "home",
  BOOKING_STRIP,
  WAREHOUSE_RECEIVE_STRIP,
  TRANSFER_STRIP,
  STOCK_STRIP,
  OFFERS_STRIP,
  "money",
  "reports",
  "hrms",
];

/** Role code → its sidebar arrangement. Absent ⇒ the flat list. The one store
 *  role (`store_person`, RBAC v1) draws the store's screen; what it may do on it
 *  is what the server grants, and the layout does not need to know about that. */
export const PERSONA_LAYOUTS: Record<string, PersonaLayout> = {
  store_person: STORE_LAYOUT,
  warehouse: WAREHOUSE_LAYOUT,
};

// Everyone else - Owner, Accounts, Brand Manager, Admin - reads the store operations
// PRD §5.1 menu: its fourteen rows in its order, each section still expanded as a
// plain heading. Only Stock differs from the flat list: Stock, Stock Count and
// Return to Brand are one row, the same fold the store has. A section the person
// holds that is named nowhere here is still appended, in the server's order
// (`applyLayout`), so retuned access never loses a section.
export const DEFAULT_LAYOUT: PersonaLayout = [
  "home",
  "sell",
  "customers",
  "customer_orders",
  "receive_goods",
  "transfer",
  INVENTORY_FOLD,
  "booking",
  "money",
  "brands",
  "offers_price",
  "reports",
  "hrms",
  "setup",
];

/** The arrangement a role's sidebar is drawn in: its own persona's, else the PRD's. */
export function layoutFor(roleCode: string): PersonaLayout {
  return PERSONA_LAYOUTS[roleCode] ?? DEFAULT_LAYOUT;
}

/** Every fold any persona is drawn, once. */
const FOLDS: NavFoldDef[] = [
  ...new Set([...Object.values(PERSONA_LAYOUTS).flat(), ...DEFAULT_LAYOUT].filter(isFoldRow)),
];

/** A fold's key, in the same namespace as a section code - which is why it is
 *  prefixed rather than being the bare heading. */
export function foldKey(fold: NavFoldDef): string {
  return `fold:${fold.to}`;
}

/** The folded page a URL *is*, or null. A fold owns its URL outright: no menu
 *  entry points at `/inventory`, so `itemOwning` cannot answer for it and the
 *  route guard would default-allow it.
 *
 *  Exact, not by prefix: a fold's gate is "any one of my tabs", which is the
 *  loosest gate in the manifest. A screen routed under `/inventory/...` later
 *  must carry its own, not inherit this one by sitting beneath it. */
export function foldOwning(pathname: string): NavFoldDef | null {
  const normalized = normalizePath(pathname);
  return FOLDS.find((f) => normalized === f.to) ?? null;
}

/** A section the signed-in user actually gets, with its visible items. The
 *  server decides *which* sections (#85); the manifest says what is in one; an
 *  item gate can still hide an individual line. */
export interface VisibleSection {
  def: NavSectionDef;
  label: string;
  items: NavItem[];
}

const SECTION_DEFS = new Map(SECTIONS.map((s) => [s.code, s]));

/** The sections this person gets, with their visible items - the one access
 *  filter, and the only authority on what may be drawn. Everything below it
 *  (arranging, grouping, collapsing) consumes its output and can only subtract.
 *
 *  Typed structurally rather than against `User`, so the manifest keeps no
 *  dependency on the auth module. */
export function visibleSections(
  user: {
    role?: { code?: string } | null;
    is_superuser: boolean;
    sections?: { code: string; label?: string; capability: string }[];
  },
  goodsActions: readonly string[] = [],
  featuresOn: ReadonlySet<string> = NO_STORE_FEATURES,
): VisibleSection[] {
  const roleCode = user.role?.code ?? "";
  const out: VisibleSection[] = [];
  const drawn = new Set<string>();
  const held = Object.fromEntries((user.sections ?? []).map((g) => [g.code, g.capability]));
  // Server order, not manifest order - the payload is the authority on both
  // which sections and in what order. Fail-closed: no payload ⇒ no sidebar.
  for (const granted of user.sections ?? []) {
    const def = SECTION_DEFS.get(granted.code);
    if (!def) continue; // a section the server knows and this build doesn't
    drawn.add(def.code);
    // Item gates are finer than the section: the rung held on *this* section
    // (`minCapability`) or, where the ladder can't express it, a role list. The
    // break-glass superuser passes both. A goods-v1 line answers to its action
    // grants instead (`itemVisible`).
    const items = def.items.filter(
      (i) =>
        !i.action &&
        itemVisible(i, granted.capability, roleCode, user.is_superuser, goodsActions, featuresOn) &&
        dataGateOpen(i, held, user.is_superuser),
    );
    // Every item gated away ⇒ nothing to navigate to; don't show an empty head.
    if (items.length) out.push({ def, label: granted.label || def.label, items });
    // A heading the server never sends, opened by this section's grant
    // (`NavSectionDef.grantedBy`), drawn right after it on the same gates.
    for (const derived of SECTIONS.filter((s) => s.grantedBy === granted.code)) {
      drawn.add(derived.code);
      const derivedItems = derived.items.filter(
        (i) =>
          !i.action &&
          itemVisible(i, granted.capability, roleCode, user.is_superuser, goodsActions, featuresOn) &&
          dataGateOpen(i, held, user.is_superuser),
      );
      if (derivedItems.length) out.push({ def: derived, label: derived.label, items: derivedItems });
    }
  }
  // Goods-v1 lines in a section the server did not send (GSA-T02). A
  // goods-v1-only person's legacy payload is empty, so without this their
  // grants would reach screens no menu shows. Only the goods lines are drawn:
  // the section's legacy lines stay behind the legacy grant they always
  // needed. Appended after the server's sections, in manifest order.
  for (const def of SECTIONS) {
    if (drawn.has(def.code)) continue;
    // A legacy line a goods grant also opens counts here on that grant alone -
    // never on its legacy gate, which this person does not hold.
    const items = def.items.filter(
      (i) =>
        !i.action &&
        storeFeatureOpen(i, featuresOn) &&
        (i.goodsActions
          ? itemVisible(i, undefined, roleCode, user.is_superuser, goodsActions, featuresOn)
          : !!i.orGoodsActions && (user.is_superuser || orGoodsVisible(i, goodsActions))),
    );
    if (items.length) out.push({ def, label: def.label, items });
  }
  return out;
}

/** A row of the rendered sidebar. */
export type NavRow =
  | { kind: "section"; key: string; section: VisibleSection }
  | { kind: "fold"; key: string; fold: NavFoldDef; tabs: FoldTab[] }
  | {
      kind: "strip";
      key: string;
      strip: NavStripDef;
      /** The section the strip stands for - its icon, layer and fallback label.
       *  Resolved here because a row only exists where the section does. */
      def: NavSectionDef;
      label: string;
      /** Never empty: a strip with no visible tab is not drawn at all. */
      tabs: NavItem[];
    };

/** A strip's key, in the same namespace as a section code and a fold's key. */
export function stripKey(strip: NavStripDef): string {
  return `strip:${strip.section}`;
}

/** The tabs of `fold` this person may see, in the fold's own order.
 *
 *  Reads the *output* of the access filter, so a tab exists only where the menu
 *  entry behind it survived that filter: folding four screens onto one page can
 *  never show one of them to somebody the sidebar would have refused it to.
 *
 *  A deep-link entry needs both halves. "Damage / Quarantine" is Return to
 *  Brand's line onto Stock's screen, and the two carry different gates - the
 *  line answers to Return to Brand, the URL to Stock - so a tab drawing it must
 *  clear the pair, exactly as clicking the line and landing on the screen does. */
export function foldTabs(fold: NavFoldDef, sections: VisibleSection[]): FoldTab[] {
  const passed = new Set(sections.flatMap((s) => s.items.map((i) => i.to)));
  const panels = fold.tabs.filter((t) => {
    if (!passed.has(t.entry)) return false;
    const host = itemOwning(t.entry.split("?")[0]);
    return !host || passed.has(host.to);
  });
  // The goods-v1 lines of the folded sections that access left standing
  // (GSA-T02). The fold speaks for their sections, so without these a folded
  // persona's grants would reach screens their sidebar never shows. Each opens
  // its own screen: the folded page draws no goods panel.
  const listed = new Set(fold.tabs.map((t) => t.entry));
  const links: FoldTab[] = sections
    .filter((s) => fold.sections.includes(s.def.code))
    .flatMap((s) => s.items)
    .filter((i) => i.goodsActions && !listed.has(i.to))
    .map((i) => ({
      slug: `goods-${itemPath(i).split("/").pop()}`,
      label: i.label,
      entry: i.to,
      link: true as const,
    }));
  return [...panels, ...links];
}

/** The tabs of `fold` one signed-in person may see. The guard and the page both
 *  ask this, so what the URL opens and what the page draws cannot disagree. */
export function foldTabsFor(
  fold: NavFoldDef,
  user: Parameters<typeof visibleSections>[0] | null | undefined,
  goodsActions: readonly string[] = [],
  featuresOn: ReadonlySet<string> = NO_STORE_FEATURES,
): FoldTab[] {
  return user ? foldTabs(fold, visibleSections(user, goodsActions, featuresOn)) : [];
}

/** The tabs of `strip` this person may see, in the strip's own order.
 *
 *  Reads the *output* of the access filter, exactly as `foldTabs` does: a tab
 *  exists only where the menu entry behind it survived that filter, so a strip
 *  can never show a screen the sidebar would have hidden. An entry the section
 *  no longer carries at all simply has no tab. */
export function stripTabs(strip: NavStripDef, sections: VisibleSection[]): NavItem[] {
  const section = sections.find((s) => s.def.code === strip.section);
  if (!section) return [];
  const byPath = new Map(section.items.map((i) => [i.to, i]));
  const listed = strip.tabs.flatMap((to) => {
    const item = byPath.get(to);
    return item ? [item] : [];
  });
  // The section's goods-v1 lines follow the strip's own tabs (GSA-T02): they
  // are the section's canonical routes too, so a stripped persona reaches them
  // from the same row rather than losing them with the section's head. A strip
  // that names `goodsTabs` gets those, in that order, and nothing else.
  const goods = strip.goodsTabs
    ? strip.goodsTabs.flatMap((to) => {
        const item = byPath.get(to);
        return item?.goodsActions && !strip.tabs.includes(to) ? [item] : [];
      })
    : section.items.filter((i) => i.goodsActions && !strip.tabs.includes(i.to));
  return [...listed, ...goods];
}

/** This persona's name for a strip row: its own override, else whatever the
 *  server called the section, else the manifest's label. */
export function stripLabel(strip: NavStripDef, sections: VisibleSection[]): string {
  const section = sections.find((s) => s.def.code === strip.section);
  return strip.label ?? section?.label ?? SECTION_DEFS.get(strip.section)?.label ?? strip.section;
}

/** The strip whose section owns this URL for this persona, or null.
 *
 *  Per persona, because a strip is an arrangement: an owner whose sidebar still
 *  expands Sell gets no strip and so no redundant second copy of their own menu.
 *  Asked of `itemOwning`'s *section*, not its tab list (#229): a screen the
 *  strip lists no tab for - `/transfer/new`, `/goods/receive/new`, `/approvals`
 *  under the Home strip - still belongs to a section this persona's sidebar
 *  strips, so the row still lights. Whether a *tab row* is worth drawing there
 *  is `sectionTabsFor`'s question, not this one. */
export function stripOwning(pathname: string, roleCode: string): NavStripDef | null {
  // Asked of `itemOwning` rather than walked again here: which screen a URL
  // belongs to is already the manifest's one rule, and a second walk beside it
  // is a second answer waiting to disagree with the route guard.
  const owner = itemOwning(pathname);
  if (!owner) return null;
  const strips = layoutFor(roleCode).filter(isStripRow);
  return strips.find((s) => s.section === owner.section) ?? null;
}

/** The strip whose *tab row* this URL gets, or null.
 *
 *  This persona's own strip, else a universal one (`UNIVERSAL_STRIPS`). Kept
 *  apart from `stripOwning` because the two answer different questions:
 *  `stripOwning` says how this persona's *sidebar* is arranged, which is what
 *  decides which row to light and unfold, and a universal strip changes nothing
 *  there. */
export function tabStripOwning(pathname: string, roleCode: string): NavStripDef | null {
  const own = stripOwning(pathname, roleCode);
  if (own) return own;
  const owner = itemOwning(pathname);
  if (!owner) return null;
  return UNIVERSAL_STRIPS.find((s) => s.section === owner.section) ?? null;
}

/** The tab lit at `pathname`: the one whose path is the deepest prefix of it, so
 *  a child URL keeps its parent tab lit (`/sell/returns/12` lights Return &
 *  Exchange, a bill under `/sell/9` lights Billing). Null when this person holds
 *  no tab covering where they are standing. */
export function activeStripTab(tabs: NavItem[], pathname: string): NavItem | null {
  const normalized = normalizePath(pathname);
  let best: NavItem | null = null;
  for (const tab of tabs) {
    const path = itemPath(tab);
    if (!underPrefix(normalized, path)) continue;
    if (!best || path.length > itemPath(best).length) best = tab;
  }
  return best;
}

/** The tab row a stripped section puts on its own screens, or null for no row.
 *
 *  The whole decision in one place, because the shell only draws it: which strip
 *  owns this URL for this persona, which of its tabs access left standing, which
 *  one is lit, and what the header calls the row. Null - draw nothing - covers
 *  the three cases the grill named: a persona whose sidebar still expands the
 *  section, a strip access has cut to a single tab (a strip of one is not a
 *  choice), and a URL none of the surviving tabs covers.
 *
 *  Asked of `tabStripOwning`, so a universal strip (Offers & Price) reaches a
 *  persona with no layout of its own - which is what `PromotionsTabs` did before
 *  this row replaced it. */
export interface SectionTabs {
  /** The eyebrow: the strip's row name. */
  crumb: string;
  /** The lit tab's name, which is this screen's title. */
  title: string;
  tabs: NavItem[];
  active: NavItem;
}

export function sectionTabsFor(
  pathname: string,
  user: Parameters<typeof visibleSections>[0] | null | undefined,
  goodsActions: readonly string[] = [],
  featuresOn: ReadonlySet<string> = NO_STORE_FEATURES,
): SectionTabs | null {
  if (!user) return null;
  const strip = tabStripOwning(pathname, user.role?.code ?? "");
  if (!strip) return null;
  // A hidden screen the row does not list is a screen of its own, reached by a
  // deep link. Lighting the nearest tab would title Price Book "Promotions" - a
  // header that names a different screen from the one being read. A URL with no
  // entry at all (an offer document, `/transfer/new`) is a different case and
  // still lights its parent tab, which is the rule every strip has kept.
  const owner = itemOwning(pathname);
  if (owner?.hidden && !strip.tabs.includes(owner.to)) return null;
  const sections = visibleSections(user, goodsActions, featuresOn);
  const tabs = stripTabs(strip, sections);
  if (tabs.length < 2) return null;
  const active = activeStripTab(tabs, pathname);
  if (!active) return null;
  return { crumb: stripLabel(strip, sections), title: active.label, tabs, active };
}

/** The tab `slug` names, or the first one this person can see. An unknown slug,
 *  or one whose tab this person is not shown, falls back rather than leaving
 *  them on a page with nothing on it. */
export function resolveFoldTab(tabs: FoldTab[], slug: string | null): FoldTab | null {
  const panels = tabs.filter((t) => !t.link);
  return panels.find((t) => t.slug === slug) ?? panels[0] ?? null;
}

/** Arrange the sections this person holds into their persona's rows.
 *
 *  Takes the *output* of the access filter and only ever reorders, folds or
 *  drops what is already in it - so no arrangement can put a section in front
 *  of somebody the server did not send it to. */
export function applyLayout(sections: VisibleSection[], roleCode: string): NavRow[] {
  const layout = layoutFor(roleCode);

  const byCode = new Map(sections.map((s) => [s.def.code, s]));
  const rows: NavRow[] = [];
  const spokenFor = new Set<string>();
  for (const row of layout) {
    if (typeof row === "string") {
      spokenFor.add(row);
      const s = byCode.get(row);
      const drawn = s && menuOnly(s);
      if (drawn?.items.length) rows.push({ kind: "section", key: row, section: drawn });
      continue;
    }
    if (isStripRow(row)) {
      // A strip speaks for its section whether or not any tab survives: the
      // head is gone from this persona's sidebar either way.
      spokenFor.add(row.section);
      const section = byCode.get(row.section);
      const tabs = stripTabs(row, sections);
      // No tab this person can see ⇒ nowhere for the row to land; don't draw it.
      if (section && tabs.length) {
        rows.push({
          kind: "strip",
          key: stripKey(row),
          strip: row,
          def: section.def,
          label: stripLabel(row, sections),
          tabs,
        });
      }
      continue;
    }
    // A fold speaks for its sections whether or not it draws a tab for each of
    // them: the heads are gone from this persona's sidebar either way, and the
    // page is where those screens now live.
    for (const code of row.sections) spokenFor.add(code);
    const tabs = foldTabs(row, sections);
    // No panel this person can see ⇒ nothing on the page; don't draw the link.
    if (tabs.some((t) => !t.link)) {
      rows.push({ kind: "fold", key: foldKey(row), fold: row, tabs });
      continue;
    }
    // Only goods-v1 lines survive (GSA-T02): with no page to hang them on, they
    // are drawn under their own sections rather than silently lost.
    for (const code of row.sections) {
      const s = byCode.get(code);
      const goods = s?.items.filter((i) => i.goodsActions) ?? [];
      if (s && goods.length) {
        rows.push({ kind: "section", key: code, section: { ...s, items: goods } });
      }
    }
  }

  // Held, but named nowhere in the layout - an admin can retune access, so this
  // is a real case and not a defect. Appended in the server's order rather than
  // dropped: a sidebar that quietly loses a section somebody was just granted is
  // worse than one whose last row is in an unexpected place.
  for (const s of sections) {
    if (spokenFor.has(s.def.code)) continue;
    const drawn = menuOnly(s);
    if (!drawn.items.length) continue;
    rows.push({ kind: "section", key: s.def.code, section: drawn });
  }
  return rows;
}

/** The same section with its `hidden` entries left off - what a sidebar draws.
 *
 *  The one place a hidden entry stops being a menu line, and deliberately *after*
 *  the access filter rather than inside it: a strip or a fold names its tabs out
 *  of the filter's output, so hiding an entry there would take the tab away with
 *  the line. Every sidebar row is built here, so there is nowhere else for a
 *  hidden entry to leak back onto the menu. */
function menuOnly(section: VisibleSection): VisibleSection {
  const items = section.items.filter((i) => !i.hidden);
  return items.length === section.items.length ? section : { ...section, items };
}

/** The whole sidebar for one signed-in person: what they may see, arranged the
 *  way their persona reads it. One call, so the two steps can only happen in
 *  that order - access first, arrangement second. */
export function sidebarRows(
  user: {
    role?: { code?: string } | null;
    is_superuser: boolean;
    sections?: { code: string; label?: string; capability: string }[];
  },
  goodsActions: readonly string[] = [],
  featuresOn: ReadonlySet<string> = NO_STORE_FEATURES,
): NavRow[] {
  return applyLayout(visibleSections(user, goodsActions, featuresOn), user.role?.code ?? "");
}

/** Where a signed-in person with no Home of their own lands: the first screen
 *  their sidebar draws, or null when it draws none. A goods-v1-only login has no
 *  legacy Home section, so `/` would otherwise open "No access" in front of the
 *  very destinations their grants allow. */
export function firstDestination(rows: NavRow[]): string | null {
  const row = rows[0];
  if (!row) return null;
  if (row.kind === "fold") return row.fold.to;
  if (row.kind === "strip") return row.tabs[0]?.to ?? null;
  return row.section.items[0]?.to ?? null;
}

/** The test handle for a menu line, as `nav-<section>-<screen>`. A deep link
 *  keeps its `?view=`, because its path alone is the host section's own screen:
 *  drawn under Stock, "Damage / Quarantine" would otherwise answer to the same
 *  handle as "Stock on Hand". */
export function testId(sectionCode: string, item: NavItem): string {
  const tail = itemPath(item).split("/").pop() || "home";
  const view = item.to.split("?view=")[1];
  return `nav-${sectionCode}-${tail}${view ? `-${view}` : ""}`;
}

/** Every section drawn as its own head in `rows`. A fold draws no section head
 *  - its sections are tabs on one page - so it contributes none. */
export function sectionsIn(rows: NavRow[]): VisibleSection[] {
  return rows.flatMap((r) => (r.kind === "section" ? [r.section] : []));
}

/** Does `row` draw as a single line - a link straight to a screen, rather than
 *  a head with several items under it? True for a fold and a strip, whose
 *  whole point is to be one line, and for a section access has cut to exactly
 *  one visible item. This is the same question the sidebar's own
 *  `renderSection` already answers to decide whether a section draws as
 *  `oneLineRow` or as a toggle over `.nav-items` (#96) - and, since #230, the
 *  question the icon rail asks to decide "navigate on click" versus "open a
 *  flyout". One predicate, so the two can never draw a different answer for
 *  the same row. */
export function isOneLineRow(row: NavRow): boolean {
  return row.kind !== "section" || row.section.items.length === 1;
}

/** Is the folded page where this person is standing? True on the fold's own URL
 *  and on any standalone screen it folds - so a store person who lands on
 *  `/stock` from the global search still sees which row they are on. */
export function isActiveFold(fold: NavFoldDef, pathname: string): boolean {
  if (underPrefix(normalizePath(pathname), fold.to)) return true;
  const owner = itemOwning(pathname);
  return !!owner && fold.sections.includes(owner.section);
}

/** The key of the sidebar row this person's screen is drawn under - a section
 *  code, or a fold's key. What the sidebar has to unfold to show where you are;
 *  a folded page draws as one link and so has nothing to unfold. */
export function headingOwning(pathname: string, roleCode: string): string | null {
  const layout = layoutFor(roleCode);
  const folds = layout.filter(isFoldRow);
  const here = folds.find((f) => isActiveFold(f, pathname));
  if (here) return foldKey(here);
  const strip = stripOwning(pathname, roleCode);
  if (strip) return stripKey(strip);
  for (const section of SECTIONS) {
    if (section.items.some((i) => isActiveItem(i, pathname))) return section.code;
  }
  return null;
}
