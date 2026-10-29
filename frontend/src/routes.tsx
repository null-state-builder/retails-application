// The route table, as data (issue #87).
//
// One canonical URL per screen, named by the section that owns it. Kept as an
// array rather than inline JSX so the ranking that decides which screen a URL
// opens — `/transfer/new` is the new-transfer form, `/transfer/12` is transfer 12 — is
// something a test can assert rather than something we hope React Router got
// right. `id` is that assertion's handle; it never reaches the user.
import { isValidElement } from "react";
import type { ReactElement, ReactNode } from "react";
import { matchRoutes } from "react-router-dom";
import type { RouteObject } from "react-router-dom";

import { lazyPage } from "./lib/lazyPage";
import { withQuery } from "./lib/query";
import { PlannedPage } from "./pages/PlannedPage";
import { LegacyRedirect } from "./shell/LegacyRedirect";
import { LEGACY_PATHS, NAV_ITEMS, itemPath } from "./shell/navConfig";
import { TillProvider } from "./till/TillProvider";

// Every screen is its own chunk, fetched the first time it is opened (or when
// its sidebar link is hovered or focused: `preloadRoute` below).
const Home = lazyPage(() => import("./pages/Home"), "Home");
const BrandsPage = lazyPage(() => import("./pages/MasterPages"), "BrandsPage");
const GstinsPage = lazyPage(() => import("./pages/MasterPages"), "GstinsPage");
const SeasonsPage = lazyPage(() => import("./pages/MasterPages"), "SeasonsPage");
const StoreTargetsPage = lazyPage(() => import("./pages/MasterPages"), "StoreTargetsPage");
const StoresPage = lazyPage(() => import("./pages/MasterPages"), "StoresPage");
const VendorsPage = lazyPage(() => import("./pages/MasterPages"), "VendorsPage");
const OrganisationPage = lazyPage(() => import("./pages/Organisation"), "OrganisationPage");
const FirstStoreSetupPage = lazyPage(
  () => import("./pages/FirstStoreSetup"),
  "FirstStoreSetupPage",
);
const PeopleAccessPage = lazyPage(() => import("./pages/PeopleAccess"), "PeopleAccessPage");
const ProductsPartiesPage = lazyPage(
  () => import("./pages/ProductsParties"),
  "ProductsPartiesPage",
);
const ConfigurationPage = lazyPage(() => import("./pages/Configuration"), "ConfigurationPage");
const ProductListsPage = lazyPage(() => import("./pages/ProductLists"), "ProductListsPage");
const SellPolicySettingsPage = lazyPage(
  () => import("./pages/SellPolicySettings"),
  "SellPolicySettingsPage",
);
const AuditLogPage = lazyPage(() => import("./pages/AuditLog"), "AuditLogPage");
const SalesReportPage = lazyPage(() => import("./pages/SalesReport"), "SalesReportPage");
const GiftStockReportPage = lazyPage(
  () => import("./pages/GiftStockReport"),
  "GiftStockReportPage",
);
const DiscountFundingPage = lazyPage(
  () => import("./pages/DiscountFunding"),
  "DiscountFundingPage",
);
const DebitNotesPage = lazyPage(() => import("./pages/DebitNotes"), "DebitNotesPage");
const PettyCashPage = lazyPage(() => import("./pages/PettyCash"), "PettyCashPage");
const PayablesPage = lazyPage(() => import("./pages/Payables"), "PayablesPage");
const OpenToBuyPage = lazyPage(() => import("./pages/OpenToBuy"), "OpenToBuyPage");
const GstReportPage = lazyPage(() => import("./pages/GstReport"), "GstReportPage");
const ExceptionsReportPage = lazyPage(
  () => import("./pages/ExceptionsReport"),
  "ExceptionsReportPage",
);
const StaffPerformanceReportPage = lazyPage(
  () => import("./pages/StaffPerformanceReport"),
  "StaffPerformanceReportPage",
);
const ShrinkageReportPage = lazyPage(
  () => import("./pages/ShrinkageReport"),
  "ShrinkageReportPage",
);
const InventoryReportPage = lazyPage(
  () => import("./pages/InventoryReport"),
  "InventoryReportPage",
);
const BrandPerformanceReportPage = lazyPage(
  () => import("./pages/BrandPerformanceReport"),
  "BrandPerformanceReportPage",
);
const StoreDashboardPage = lazyPage(
  () => import("./pages/StoreDashboardPage"),
  "StoreDashboardPage",
);
const TaxSettingsPage = lazyPage(() => import("./pages/TaxSettings"), "TaxSettingsPage");
const ConsentWordingPage = lazyPage(() => import("./pages/ConsentWording"), "ConsentWordingPage");
const BrandTermsPage = lazyPage(() => import("./pages/BrandTerms"), "BrandTermsPage");
const MarginSharePage = lazyPage(() => import("./pages/MarginShare"), "MarginSharePage");
const BrandClaimsPage = lazyPage(() => import("./pages/BrandClaims"), "BrandClaimsPage");
const SorAgeingPage = lazyPage(() => import("./pages/SorAgeing"), "SorAgeingPage");
const BrandReportsPage = lazyPage(() => import("./pages/BrandReports"), "BrandReportsPage");
const CustomerListPage = lazyPage(
  () => import("./pages/customers/CustomerList"),
  "CustomerListPage",
);
const CustomerPage = lazyPage(() => import("./pages/customers/CustomerPage"), "CustomerPage");
const ReservationsPage = lazyPage(() => import("./pages/Reservations"), "ReservationsPage");
const SpecialOrdersPage = lazyPage(() => import("./pages/SpecialOrders"), "SpecialOrdersPage");
const GiftVouchersPage = lazyPage(() => import("./pages/GiftVouchers"), "GiftVouchersPage");
const AlterationsPage = lazyPage(() => import("./pages/Alterations"), "AlterationsPage");
const DocumentNumberingPage = lazyPage(
  () => import("./pages/DocumentNumbering"),
  "DocumentNumberingPage",
);
const SalespersonMatchesPage = lazyPage(
  () => import("./pages/SalespersonMatches"),
  "SalespersonMatchesPage",
);
const MissingHsnPage = lazyPage(() => import("./pages/MissingHsn"), "MissingHsnPage");
const StockAgeingPage = lazyPage(() => import("./pages/StockAgeing"), "StockAgeingPage");
const BrokenSizesPage = lazyPage(() => import("./pages/BrokenSizes"), "BrokenSizesPage");
const SizeBalancingPage = lazyPage(() => import("./pages/SizeBalancing"), "SizeBalancingPage");
const CountSchedulePage = lazyPage(() => import("./pages/CountSchedule"), "CountSchedulePage");
const StoreChecklistPage = lazyPage(() => import("./pages/StoreChecklist"), "StoreChecklistPage");
const TaskChecklistsPage = lazyPage(() => import("./pages/TaskChecklists"), "TaskChecklistsPage");
const StaffListPage = lazyPage(() => import("./pages/StaffList"), "StaffListPage");
const FeatureCheckPage = lazyPage(() => import("./pages/FeatureSwitches"), "FeatureCheckPage");
const FeatureSwitchesPage = lazyPage(
  () => import("./pages/FeatureSwitches"),
  "FeatureSwitchesPage",
);
const PartnerBillingPage = lazyPage(() => import("./pages/PartnerBilling"), "PartnerBillingPage");
const PartnerDuesPage = lazyPage(() => import("./pages/PartnerDues"), "PartnerDuesPage");
const DailyCashPage = lazyPage(() => import("./pages/DailyCash"), "DailyCashPage");
const BankReconciliationPage = lazyPage(
  () => import("./pages/BankReconciliation"),
  "BankReconciliationPage",
);
const BookingDetailPage = lazyPage(() => import("./pages/Bookings"), "BookingDetailPage");
const BookingNewPage = lazyPage(() => import("./pages/Bookings"), "BookingNewPage");
const BookingsPage = lazyPage(() => import("./pages/Bookings"), "BookingsPage");
// Goods-v1 receiving: the inbox, the delivery workflow and Goods arrived
// (OPS-04, OPS-17). The arrivals, receipts, acceptance and PT-preparation
// screens are no longer screens of their own; their panels are the steps.
const ReceiveDeliveryPage = lazyPage(
  () => import("./pages/ReceiveDelivery"),
  "ReceiveDeliveryPage",
);
const ReceiveNewPage = lazyPage(() => import("./pages/ReceiveDelivery"), "ReceiveNewPage");
// Aliased: the legacy outbound screens below already export a
// `TransferDetailPage` of their own, and both keep their routes (#303).
const GoodsTransferDetailPage = lazyPage(
  () => import("./pages/TransferDetail"),
  "TransferDetailPage",
);
const TransferDocumentPage = lazyPage(
  () => import("./pages/TransferDocumentPrint"),
  "TransferDocumentPage",
);
const GoodsTransferRequestsPage = lazyPage(
  () => import("./pages/TransferRequests"),
  "TransferRequestsPage",
);
const GoodsTransfersPage = lazyPage(() => import("./pages/Transfers"), "TransfersPage");
const ReceiveHistoryPage = lazyPage(() => import("./pages/ReceiveInbox"), "ReceiveHistoryPage");
const ReceiveInboxPage = lazyPage(() => import("./pages/ReceiveInbox"), "ReceiveInboxPage");
// PT Work (OPS-17): To prepare, To approve and Mapping rules.
const PtWorkPage = lazyPage(() => import("./pages/PtWork"), "PtWorkPage");
// Goods-v1 opening stock for synthetic tenants (ticket 10, GSA-T10).
const GoodsOpeningPage = lazyPage(() => import("./pages/GoodsOpening"), "GoodsOpeningPage");
const StockSyncPage = lazyPage(() => import("./pages/StockSync"), "StockSyncPage");
const GoodsOriginJourneyPage = lazyPage(
  () => import("./pages/GoodsStock"),
  "GoodsOriginJourneyPage",
);
const GoodsStockPage = lazyPage(() => import("./pages/GoodsStock"), "GoodsStockPage");
// Goods-v1 labels and print jobs (ticket 11), beside the legacy TagPrint modal
// (PriceList.tsx), which prints a different, unrelated floor price tag.
const GoodsLabelsPage = lazyPage(() => import("./pages/GoodsLabels"), "GoodsLabelsPage");
const GoodsMovementsPage = lazyPage(() => import("./pages/GoodsMovements"), "GoodsMovementsPage");
// Goods-v1 counts at non-trading sites (ticket 17).
const GoodsCountDetailPage = lazyPage(() => import("./pages/GoodsCounts"), "GoodsCountDetailPage");
const GoodsCountsPage = lazyPage(() => import("./pages/GoodsCounts"), "GoodsCountsPage");
// Goods-v1 monitoring, durable exports and command lookup (ticket 18).
const GoodsMonitoringPage = lazyPage(
  () => import("./pages/GoodsMonitoring"),
  "GoodsMonitoringPage",
);
const TransferListPage = lazyPage(() => import("./pages/OutboundTransfers"), "TransferListPage");
const TransferNewPage = lazyPage(() => import("./pages/OutboundTransfers"), "TransferNewPage");
const TransferDetailPage = lazyPage(
  () => import("./pages/OutboundTransfers"),
  "TransferDetailPage",
);
const TransferPtPage = lazyPage(() => import("./pages/OutboundTransfers"), "TransferPtPage");
const DistributionGridPage = lazyPage(
  () => import("./pages/DistributionGrid"),
  "DistributionGridPage",
);
const StockRequestListPage = lazyPage(
  () => import("./pages/OutboundStockRequests"),
  "StockRequestListPage",
);
const StockRequestNewPage = lazyPage(
  () => import("./pages/OutboundStockRequests"),
  "StockRequestNewPage",
);
const StockRequestDetailPage = lazyPage(
  () => import("./pages/OutboundStockRequests"),
  "StockRequestDetailPage",
);
const InTransitPage = lazyPage(() => import("./pages/OutboundInTransit"), "InTransitPage");
const RTVListPage = lazyPage(() => import("./pages/OutboundRTV"), "RTVListPage");
const RTVNewPage = lazyPage(() => import("./pages/OutboundRTV"), "RTVNewPage");
const RTVDetailPage = lazyPage(() => import("./pages/OutboundRTV"), "RTVDetailPage");
const AdjustmentListPage = lazyPage(
  () => import("./pages/OutboundAdjustments"),
  "AdjustmentListPage",
);
const AdjustmentDetailPage = lazyPage(
  () => import("./pages/OutboundAdjustments"),
  "AdjustmentDetailPage",
);
const StockCountListPage = lazyPage(() => import("./pages/StockCount"), "StockCountListPage");
const StockCountDetailPage = lazyPage(() => import("./pages/StockCount"), "StockCountDetailPage");
const WriteOffListPage = lazyPage(() => import("./pages/OutboundWriteoffs"), "WriteOffListPage");
const WriteOffNewPage = lazyPage(() => import("./pages/OutboundWriteoffs"), "WriteOffNewPage");
const WriteOffDetailPage = lazyPage(
  () => import("./pages/OutboundWriteoffs"),
  "WriteOffDetailPage",
);
const VFlipListPage = lazyPage(() => import("./pages/OutboundVflips"), "VFlipListPage");
const VFlipNewPage = lazyPage(() => import("./pages/OutboundVflips"), "VFlipNewPage");
const VFlipDetailPage = lazyPage(() => import("./pages/OutboundVflips"), "VFlipDetailPage");
const ActionNeededPage = lazyPage(() => import("./pages/ActionNeeded"), "ActionNeededPage");
const AlertsPage = lazyPage(() => import("./pages/Alerts"), "AlertsPage");
const MailPage = lazyPage(() => import("./pages/Mail"), "MailPage");
const OffersPage = lazyPage(() => import("./pages/Offers"), "OffersPage");
const RunningOffersPage = lazyPage(() => import("./pages/RunningOffers"), "RunningOffersPage");
const OffersEossPage = lazyPage(() => import("./pages/OffersEoss"), "OffersEossPage");
const OfferAuthorPage = lazyPage(() => import("./pages/OfferAuthor"), "OfferAuthorPage");
const PriceListPage = lazyPage(() => import("./pages/PriceList"), "PriceListPage");
const DiscountsPage = lazyPage(() => import("./pages/Discounts"), "DiscountsPage");
const InventoryPage = lazyPage(() => import("./pages/Inventory"), "InventoryPage");
const ApplicationRoadmapPage = lazyPage(
  () => import("./pages/ApplicationRoadmap"),
  "ApplicationRoadmapPage",
);
const StockLedger = lazyPage(() => import("./pages/StockLedger"), "default");
const StockBookmark = lazyPage(() => import("./pages/StockWorkspace"), "StockBookmark");
const VendorLedger = lazyPage(() => import("./pages/VendorLedger"), "default");
const CashLedger = lazyPage(() => import("./pages/CashLedger"), "default");
const DaySummary = lazyPage(() => import("./pages/DaySummary"), "default");
const IrnQueue = lazyPage(() => import("./pages/IrnQueue"), "default");
const BillingPage = lazyPage(() => import("./pages/sell/Billing"), "default");
const BillsPage = lazyPage(() => import("./pages/sell/Bills"), "default");
const CustomerSearchPage = lazyPage(() => import("./pages/sell/CustomerSearch"), "default");
const TillPage = lazyPage(() => import("./pages/sell/Till"), "default");
const CashCountPage = lazyPage(() => import("./pages/sell/CashCount"), "default");

export type Room = "counter";
type Screen = RouteObject & { id: string; path: string; room?: Room };

/** A Sell screen, with the counter behind it. */
function withTill(screen: ReactNode) {
  return <TillProvider>{screen}</TillProvider>;
}

/** Screens that are built. Behaviour is unchanged from before the re-housing —
 *  only the address moved. */
const BUILT: Screen[] = [
  // Home
  { id: "home", path: "/", element: <Home /> },
  // Everything waiting on you: the approvals inbox (#70, #87) beside the goods
  // exceptions (ticket 08). `/approvals` and `/goods/exceptions` redirect here.
  { id: "action-needed", path: "/action-needed", element: <ActionNeededPage /> },
  // Home's heads-up screen — in-transit aging, return-window 30/15/7 (#77) and
  // the goods notification feed
  { id: "alerts", path: "/alerts", element: <AlertsPage /> },
  // The person's own mailbox, reached from the top bar rather than the sidebar:
  // mail belongs to a person, not to a section, so it has no menu entry and no
  // RBAC cell — every logged-in person has exactly one, and it is theirs.
  { id: "mail", path: "/mail", element: <MailPage /> },
  // Offers & Price - the store's read-only view of the rulebook (#183), the
  // authoring workspace behind it, the price list and the discount pack (D11).
  { id: "offers", path: "/offers", element: <OffersPage /> },
  // Running Offers (OPS-10): before `/offers/:id`, so the word is never read as
  // an offer id - the same rank every named sub-screen in this file keeps.
  { id: "offers-running", path: "/offers/running", element: <RunningOffersPage /> },
  { id: "offers-eoss", path: "/offers/eoss", element: <OffersEossPage /> },
  { id: "offers-price-list", path: "/offers/price-list", element: <PriceListPage /> },
  // Before `/offers/:id`, so the word is never read as an offer id.
  { id: "offer-new", path: "/offers/new", element: <OfferAuthorPage /> },
  { id: "offer-detail", path: "/offers/:id", element: <OfferAuthorPage /> },
  // Booking
  { id: "booking-list", path: "/booking", element: <BookingsPage /> },
  { id: "booking-new", path: "/booking/new", element: <BookingNewPage /> },
  { id: "booking-detail", path: "/booking/:id", element: <BookingDetailPage /> },
  // Goods-v1 bookings live on the one Bookings screen above; the old
  // `/goods/bookings` address redirects there (navConfig `LEGACY_PREFIXES`).
  // The receiving inbox and its delivery workflow (OPS-04). The workflow's own
  // URLs come before the bare inbox for the reason the rest of this file gives:
  // a record id must never be read as a section word, and "history" must never
  // be read as a delivery id.
  // A delivery is addressed by its arrival (`delivery`), its GRN (`grn`) or
  // its PT (`pt`), so a link to any of its records opens the delivery (OPS-17).
  {
    id: "goods-receive-delivery",
    path: "/goods/receive/:kind/:id",
    element: <ReceiveDeliveryPage />,
  },
  { id: "goods-receive-new", path: "/goods/receive/new", element: <ReceiveNewPage /> },
  { id: "goods-receive-history", path: "/goods/receive/history", element: <ReceiveHistoryPage /> },
  { id: "goods-receive", path: "/goods/receive", element: <ReceiveInboxPage /> },
  // PT Work (OPS-17): the warehouse's To prepare, To approve and Mapping rules.
  // The separate Prepare PT and PT approvals screens are its tabs now.
  { id: "goods-pt-work", path: "/goods/pt-work", element: <PtWorkPage /> },
  // Goods-v1 stock (ticket 07; acceptance is a delivery step since OPS-17). Stock's own detail —
  // the origin journey — comes before the bare stock route only because both
  // start with "/goods/stock"; React Router's own ranking already prefers the
  // more specific path, but the ordering here matches the convention this
  // file uses everywhere else (a document id must never shadow a section word).
  // Goods-v1 opening stock for synthetic tenants (ticket 10).
  { id: "goods-opening", path: "/goods/opening", element: <GoodsOpeningPage /> },
  // Store test run: nightly stock sync to the legacy SOH (test tenant only).
  { id: "goods-stock-sync", path: "/goods/stock-sync", element: <StockSyncPage /> },
  {
    id: "goods-stock-origin-journey",
    path: "/goods/stock/origins/:id",
    element: <GoodsOriginJourneyPage />,
  },
  { id: "goods-stock", path: "/goods/stock", element: <GoodsStockPage /> },
  // Goods-v1 labels and print jobs (ticket 11).
  { id: "goods-labels", path: "/goods/labels", element: <GoodsLabelsPage /> },
  // Goods-v1 bin moves, holds and releases (ticket 12).
  { id: "goods-movements", path: "/goods/movements", element: <GoodsMovementsPage /> },
  // Goods-v1 counts at non-trading sites (ticket 17): the list, and one count.
  { id: "goods-count-detail", path: "/goods/counts/:id", element: <GoodsCountDetailPage /> },
  { id: "goods-counts", path: "/goods/counts", element: <GoodsCountsPage /> },
  // Goods-v1 transfers (OPS-06). "requests" comes before the `:id` route for
  // the reason this file gives everywhere: a section word must never be read
  // as a record id.
  {
    id: "goods-transfer-requests",
    path: "/goods/transfers/requests",
    element: <GoodsTransferRequestsPage />,
  },
  {
    id: "goods-transfer-detail",
    path: "/goods/transfers/:id",
    element: <GoodsTransferDetailPage />,
  },
  // Store operations ticket 36: a shipment's delivery challan or tax invoice, to print.
  {
    id: "goods-transfer-document",
    path: "/goods/transfers/:id/shipments/:dispatchId/document",
    element: <TransferDocumentPage />,
  },
  { id: "goods-transfers", path: "/goods/transfers", element: <GoodsTransfersPage /> },
  // Goods-v1 monitoring, durable exports and command lookup (ticket 18).
  { id: "goods-monitoring", path: "/setup/monitoring", element: <GoodsMonitoringPage /> },
  // Transfer
  { id: "transfer-list", path: "/transfer", element: <TransferListPage /> },
  { id: "transfer-new", path: "/transfer/new", element: <TransferNewPage /> },
  // Before /transfer/:id, or "in-transit"/"requests"/"distribution" would be read as a transfer id (#71).
  {
    id: "transfer-distribution",
    path: "/transfer/distribution",
    element: <DistributionGridPage />,
  },
  { id: "transfer-in-transit", path: "/transfer/in-transit", element: <InTransitPage /> },
  { id: "stock-request-list", path: "/transfer/requests", element: <StockRequestListPage /> },
  { id: "stock-request-new", path: "/transfer/requests/new", element: <StockRequestNewPage /> },
  {
    id: "stock-request-detail",
    path: "/transfer/requests/:id",
    element: <StockRequestDetailPage />,
  },
  { id: "transfer-detail", path: "/transfer/:id", element: <TransferDetailPage /> },
  // The printable PT the carton travels with (#72).
  { id: "transfer-pt", path: "/transfer/:id/pt", element: <TransferPtPage /> },
  // Stock Count — the counting sessions, and the corrections they produce
  { id: "count-list", path: "/stock-count", element: <StockCountListPage /> },
  // Store operations ticket 35: when each store's blind count is due.
  { id: "count-schedule", path: "/stock-count/schedule", element: <CountSchedulePage /> },
  { id: "adjustment-list", path: "/stock-count/adjustments", element: <AdjustmentListPage /> },
  {
    id: "adjustment-detail",
    path: "/stock-count/adjustments/:id",
    element: <AdjustmentDetailPage />,
  },
  { id: "writeoff-list", path: "/stock-count/writeoffs", element: <WriteOffListPage /> },
  { id: "writeoff-new", path: "/stock-count/writeoffs/new", element: <WriteOffNewPage /> },
  { id: "writeoff-detail", path: "/stock-count/writeoffs/:id", element: <WriteOffDetailPage /> },
  // Last in the section: a count id must never shadow "adjustments"/"writeoffs".
  { id: "count-detail", path: "/stock-count/:id", element: <StockCountDetailPage /> },
  // Return to Brand
  { id: "rtv-list", path: "/return-to-brand", element: <RTVListPage /> },
  { id: "rtv-new", path: "/return-to-brand/new", element: <RTVNewPage /> },
  { id: "rtv-detail", path: "/return-to-brand/:id", element: <RTVDetailPage /> },
  // Inventory - Stock, Stock Count and Return to Brand folded onto one page
  // (#170). It belongs to no section: it is the store persona's arrangement of
  // three of them, and its tabs carry those sections' own gates.
  { id: "inventory", path: "/inventory", element: <InventoryPage /> },
  { id: "application-roadmap", path: "/roadmap", element: <ApplicationRoadmapPage /> },
  // Sell - the counter (#181) and the till layer's own surface (#180).
  //
  // `TillProvider` wraps each screen rather than the app: opening a counter's
  // local database means holding one store's price list, credit notes and
  // manager PIN hashes, which a warehouse or head-office login has no business
  // carrying.
  //
  // Billing declares the counter room instead of wrapping itself here:
  // ProtectedRoute lifts its provider above AppShell so the live sync light can
  // occupy the top bar. The other Sell screens remain route-local providers.
  { id: "sell-billing", path: "/sell", room: "counter", element: <BillingPage /> },
  { id: "sell-till", path: "/sell/till", element: withTill(<TillPage />) },
  // Ticket 41: the day-close cash count, on the counter that took the cash.
  { id: "sell-cash-count", path: "/sell/cash-count", element: withTill(<CashCountPage />) },
  // Ticket 19: the gift vouchers this store sold. Online only; no counter needed.
  { id: "sell-gift-vouchers", path: "/sell/gift-vouchers", element: <GiftVouchersPage /> },
  // What this counter has done, and where each bill has got to (OPS-08). It
  // *is* `withTill`, unlike the customer search below, and for the opposite
  // reason: half of what it shows is this browser's own queue - the bills head
  // office has never heard of - and that is only readable with the counter's
  // database open.
  { id: "sell-bills", path: "/sell/bills", element: withTill(<BillsPage />) },
  // Find a bill and print it again (#185). No `withTill`, and that is the whole
  // shape of the screen: it reads the *server's* bills, because the counter's
  // local copy holds only what it has not yet synced - last month's bill, or one
  // from the machine that was replaced, is only ever at head office. It also
  // means somebody who may read bills but not bill (an owner, an accountant) can
  // open it without a counter's price list being opened on their laptop.
  { id: "sell-customers", path: "/sell/customers", element: <CustomerSearchPage /> },
  // Stock — V-flip is an action inside this section, not a menu item
  { id: "stock-on-hand", path: "/stock", element: <StockBookmark /> },
  { id: "stock-search", path: "/stock/search", element: <StockBookmark /> },
  // Store operations ticket 12: items with no HSN, for fixing.
  { id: "stock-missing-hsn", path: "/stock/missing-hsn", element: <MissingHsnPage /> },
  { id: "stock-ageing", path: "/stock/ageing", element: <StockAgeingPage /> },
  // Store operations ticket 32: broken-size alerts and their rules.
  { id: "stock-broken-sizes", path: "/stock/broken-sizes", element: <BrokenSizesPage /> },
  // Store operations ticket 34: transfers suggested to fill broken sizes.
  { id: "stock-size-balancing", path: "/stock/size-balancing", element: <SizeBalancingPage /> },
  { id: "stock-history", path: "/stock/history", element: <StockLedger /> },
  { id: "vflip-list", path: "/stock/vflips", element: <VFlipListPage /> },
  { id: "vflip-new", path: "/stock/vflips/new", element: <VFlipNewPage /> },
  { id: "vflip-detail", path: "/stock/vflips/:id", element: <VFlipDetailPage /> },
  // Money
  { id: "store-targets", path: "/money/store-targets", element: <StoreTargetsPage /> },
  { id: "vendor-ledger", path: "/money/vendor", element: <VendorLedger /> },
  { id: "cash-ledger", path: "/money/cash", element: <CashLedger /> },
  // Same day, split into money in vs out per account instead of one running
  // balance (#254) — reads the cash ledger, so it sits right after it.
  { id: "daily-cash", path: "/money/daily-cash", element: <DailyCashPage /> },
  { id: "irn-queue", path: "/money/irn-queue", element: <IrnQueue /> },
  // Debit notes for receiving shortages (store operations ticket 38, ST-REC-3).
  { id: "debit-notes", path: "/money/debit-notes", element: <DebitNotesPage /> },
  { id: "debit-note", path: "/money/debit-notes/:id", element: <DebitNotesPage /> },
  // Ticket 42: the store's petty cash box; `:id` is a spend opened from the inbox.
  { id: "petty-cash", path: "/money/petty-cash", element: <PettyCashPage /> },
  { id: "open-to-buy", path: "/booking/open-to-buy", element: <OpenToBuyPage /> },
  { id: "open-to-buy-ask", path: "/booking/open-to-buy/:id", element: <OpenToBuyPage /> },
  { id: "petty-cash-spend", path: "/money/petty-cash/:id", element: <PettyCashPage /> },
  // Store operations ticket 28 (ST-MNY-4): payables for outright brands.
  { id: "payables", path: "/money/payables", element: <PayablesPage /> },
  { id: "partner-billing", path: "/money/partner-billing", element: <PartnerBillingPage /> },
  { id: "partner-dues", path: "/money/partner-dues", element: <PartnerDuesPage /> },
  // Upload a bank statement, matched against the cash ledger (finledger.reconciliation).
  { id: "bank-reconciliation", path: "/money/bank", element: <BankReconciliationPage /> },
  // Customers (store operations PRD §5.1, ST-CUS-1, ticket 16): the list, and
  // each customer's page with the rights screen.
  { id: "customers-list", path: "/customers", element: <CustomerListPage /> },
  { id: "customer-page", path: "/customers/:id", element: <CustomerPage /> },
  // Brands (store operations PRD §5.1, ticket 23)
  { id: "brands-terms", path: "/brands/terms", element: <BrandTermsPage /> },
  // Store operations ticket 27 (ST-BRD-4): the monthly margin share statement.
  { id: "brands-margin-share", path: "/brands/margin-share", element: <MarginSharePage /> },
  // Store operations ticket 26 (ST-BRD-3): brand-funded discounts claimed at month end.
  { id: "brands-claims", path: "/brands/claims", element: <BrandClaimsPage /> },
  { id: "brands-claim", path: "/brands/claims/:id", element: <BrandClaimsPage /> },
  // Store operations ticket 24 (ST-BRD-5): SOR stock aged from the brand's dispatch date.
  { id: "brands-sor-ageing", path: "/brands/sor-ageing", element: <SorAgeingPage /> },
  // Store operations ticket 29 (ST-BRD-2): each brand's Sale and SOH report in its own layout.
  { id: "brands-reports", path: "/brands/reports", element: <BrandReportsPage /> },
  { id: "orders-reservations", path: "/orders/reservations", element: <ReservationsPage /> },
  { id: "orders-special", path: "/orders/special", element: <SpecialOrdersPage /> },
  { id: "orders-alterations", path: "/orders/alterations", element: <AlterationsPage /> },
  // Setup
  { id: "setup-organisation", path: "/setup/organisation", element: <OrganisationPage /> },
  { id: "setup-first-store", path: "/setup/first-store", element: <FirstStoreSetupPage /> },
  { id: "setup-people-access", path: "/setup/people-access", element: <PeopleAccessPage /> },
  {
    id: "setup-products-parties",
    path: "/setup/products-parties",
    element: <ProductsPartiesPage />,
  },
  { id: "setup-products", path: "/setup/products", element: <ProductListsPage /> },
  { id: "setup-configuration", path: "/setup/configuration", element: <ConfigurationPage /> },
  { id: "setup-stores", path: "/setup/stores", element: <StoresPage /> },
  { id: "setup-brands", path: "/setup/brands", element: <BrandsPage /> },
  { id: "setup-vendors", path: "/setup/vendors", element: <VendorsPage /> },
  { id: "setup-seasons", path: "/setup/seasons", element: <SeasonsPage /> },
  { id: "setup-gstins", path: "/setup/gstins", element: <GstinsPage /> },
  { id: "setup-settings", path: "/setup/settings", element: <SellPolicySettingsPage /> },
  {
    id: "setup-feature-switches",
    path: "/setup/feature-switches",
    element: <FeatureSwitchesPage />,
  },
  { id: "setup-feature-check", path: "/setup/feature-check", element: <FeatureCheckPage /> },
  { id: "setup-audit", path: "/setup/audit", element: <AuditLogPage /> },
  { id: "setup-tax-settings", path: "/setup/tax-settings", element: <TaxSettingsPage /> },
  { id: "setup-task-checklists", path: "/setup/checklists", element: <TaskChecklistsPage /> },
  { id: "store-checklist", path: "/checklist", element: <StoreChecklistPage /> },
  {
    id: "setup-consent-wording",
    path: "/setup/consent-wording",
    element: <ConsentWordingPage />,
  },
  {
    id: "setup-document-numbering",
    path: "/setup/document-numbering",
    element: <DocumentNumberingPage />,
  },
  {
    id: "setup-salesperson-matches",
    path: "/setup/salesperson-matches",
    element: <SalespersonMatchesPage />,
  },
  // HRMS
  { id: "staff-list", path: "/staff/list", element: <StaffListPage /> },
  // Reports (store operations ticket 10, PRD §17). The Sales report is new; the
  // day summary, discount report and store dashboard are the same screens,
  // moved here from Money, Offers & Price and Home.
  { id: "reports-sales", path: "/reports/sales", element: <SalesReportPage /> },
  // Store operations ticket 47 (ST-RPT-5): GST, prepared for Accounts to file.
  { id: "reports-gst", path: "/reports/gst", element: <GstReportPage /> },
  { id: "reports-gift-stock", path: "/reports/gift-stock", element: <GiftStockReportPage /> },
  {
    id: "reports-discount-funding",
    path: "/reports/discount-funding",
    element: <DiscountFundingPage />,
  },
  // Store operations ticket 44 (ST-INV-4): pieces and cost lost to shrinkage.
  { id: "reports-shrinkage", path: "/reports/shrinkage", element: <ShrinkageReportPage /> },
  // Store operations ticket 48 (ST-RPT-6): the counter's exceptions per store and staff member.
  { id: "reports-exceptions", path: "/reports/exceptions", element: <ExceptionsReportPage /> },
  // Store operations ticket 46 (ST-RPT-4): each salesperson's results; your own, or the team.
  { id: "reports-staff", path: "/reports/staff", element: <StaffPerformanceReportPage /> },
  // Store operations ticket 43 (ST-RPT-2): sell-through, weeks of cover, GMROI, dead stock.
  { id: "reports-inventory", path: "/reports/inventory", element: <InventoryReportPage /> },
  {
    id: "reports-brand-performance",
    path: "/reports/brand-performance",
    element: <BrandPerformanceReportPage />,
  },
  { id: "day-summary", path: "/reports/day-summary", element: <DaySummary /> },
  { id: "offers-discounts", path: "/reports/discounts", element: <DiscountsPage /> },
  { id: "store-dashboard", path: "/reports/store-dashboard", element: <StoreDashboardPage /> },
];

// Sections whose subsections aren't built yet still appear in the sidebar — so
// every one of them needs a page that honestly says what is planned there (#89).
// Generated from the manifest, so a new planned item can never 404, and the copy
// comes from pages/plannedPages, keyed by the same path.
const PLANNED: Screen[] = NAV_ITEMS.filter((i) => i.planned && !i.deepLink).map((i) => ({
  id: `planned:${itemPath(i)}`,
  path: itemPath(i),
  element: <PlannedPage />,
}));

// Old addresses are redirected by App's catch-all — every one that falls through
// the table. One that sits exactly where a dynamic route looks for a document
// number does not fall through: the route would open "document number <word>"
// and the catch-all would never see it (the first case was `/receive/upload-bill`
// beside the legacy `/receive/:id`, both gone since OPS-18).
//
// So: any legacy path a built or planned route would claim gets a redirect route
// of its own, derived rather than listed, so the next one is caught without
// anybody remembering this file. A static segment outranks a dynamic one, so
// these win over a `:id` route whatever the order here.
const CLAIMABLE: Screen[] = [...BUILT, ...PLANNED];
const LEGACY: Screen[] = LEGACY_PATHS.filter((path) => matchRoutes(CLAIMABLE, path)).map(
  (path) => ({
    id: `legacy:${path}`,
    path,
    element: <LegacyRedirect />,
  }),
);

// Each screen's chunk suspends against the one boundary `ProtectedRoute` puts
// around the outlet. Router navigations are transitions, so moving between
// screens keeps the current one on view until the next one's code is in,
// instead of flashing a fallback per route.
export const PROTECTED_ROUTES: Screen[] = [...CLAIMABLE, ...LEGACY];

/** The list a screen asks for first, where it is plain enough to warm from a
 *  sidebar hover. Each must be byte-for-byte the URL the screen's `useList`
 *  builds with its default tab and an empty search box, or the warm-up lands
 *  in a cache entry nobody reads. */
const PRIMARY_LIST: Record<string, string> = {
  alerts: "/alerts",
  offers: "/offers/",
  "transfer-list": withQuery("/outbound/transfers", { type: "inter_store" }),
  "stock-request-list": "/outbound/stock-requests",
  "count-list": "/outbound/stocktakes",
  "adjustment-list": "/outbound/adjustments",
  "writeoff-list": "/outbound/writeoffs",
  "rtv-list": withQuery("/outbound/rtvs", { return_type: "defective" }),
  "vflip-list": "/outbound/vflips",
};

function preloadOf(node: ReactNode): (() => Promise<unknown>) | undefined {
  if (!isValidElement(node)) return undefined;
  const preload = (node.type as { preload?: unknown }).preload;
  if (typeof preload === "function") return preload as () => Promise<unknown>;
  return preloadOf((node as ReactElement<{ children?: ReactNode }>).props.children);
}

/** What to warm before someone opens `pathname`: its code chunk, and its
 *  first list where `PRIMARY_LIST` knows it. */
export function routeWarmup(pathname: string): {
  preload?: () => Promise<unknown>;
  listUrl?: string;
} {
  const matched = matchRoutes(CLAIMABLE, pathname);
  const screen = matched?.[matched.length - 1]?.route as Screen | undefined;
  if (!screen) return {};
  const preload = preloadOf(screen.element);
  const listUrl = PRIMARY_LIST[screen.id];
  return { ...(preload ? { preload } : {}), ...(listUrl ? { listUrl } : {}) };
}

/** Resolve from the canonical table so shell chrome cannot drift into a second
 *  pathname list with subtly different prefix rules. */
export function roomAt(pathname: string): Room | undefined {
  const matched = matchRoutes(PROTECTED_ROUTES, pathname);
  return (matched?.[matched.length - 1]?.route as Screen | undefined)?.room;
}
