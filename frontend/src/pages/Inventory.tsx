/** Stock work shares one scoped workspace. Daily operations and canonical
 * count entry stay separate from review and earlier records. Every destination
 * keeps its existing route/action gate; presentation retires no writer. */

import { useMemo } from "react";
import type { ReactElement } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { canAccess } from "../auth/routeAccess";
import { Note } from "../components/Note";
import { HostedPageContext } from "../components/PageHeader";
import { INVENTORY_FOLD, foldTabsFor, resolveFoldTab } from "../shell/navConfig";
import { BrokenSizesPage } from "./BrokenSizes";
import { SizeBalancingPage } from "./SizeBalancing";
import { stockParams } from "../lib/stockWorkspace";
import { StockWorkspace } from "./StockWorkspace";
import { MissingHsnPage } from "./MissingHsn";
import { RTVListPage } from "./OutboundRTV";
import { StockAgeingPage } from "./StockAgeing";
import { CountSchedulePage } from "./CountSchedule";
import { StockCountListPage } from "./StockCount";
import StockOnHand from "./StockOnHand";
import { GoodsStockPage } from "./GoodsStock";

/** Tab slug → the screen it draws. Every slug in `INVENTORY_FOLD.tabs` needs one
 *  here; `Inventory.test` fails the build otherwise, because a tab the sidebar
 *  offers and this map has no answer for is a white screen. */
export const PANELS: Record<string, () => ReactElement> = {
  stock: () => <StockWorkspace />,
  search: () => <StockWorkspace />,
  hsn: () => <MissingHsnPage />,
  ageing: () => <StockAgeingPage />,
  broken: () => <BrokenSizesPage />,
  balance: () => <SizeBalancingPage />,
  damage: () => <GoodsStockPage damageWorkspace />,
  "damage-history": () => <StockOnHand view="quarantine" />,
  count: () => <StockCountListPage />,
  schedule: () => <CountSchedulePage />,
  returns: () => <RTVListPage />,
};

export function InventoryPage() {
  const { user, session, featuresOn } = useAuth();
  const [params] = useSearchParams();

  const tabs = useMemo(
    () => foldTabsFor(INVENTORY_FOLD, user, session?.display_actions ?? [], featuresOn),
    [user, session, featuresOn],
  );
  const active = resolveFoldTab(tabs, params.get("tab"));
  const Panel = active ? PANELS[active.slug] : undefined;

  // Damage & Quarantine is the Quarantine view of Stock on hand now (desktop
  // redesign Q3); its tab keeps its gate, and an old link lands on the view.
  if (active?.slug === "damage" && tabs.some((t) => t.slug === "stock")) {
    return (
      <Navigate
        replace
        to={`/inventory?${stockParams(params, { tab: "stock", view: "quarantine" })}`}
      />
    );
  }

  // The route guard already refuses anyone with no tab, so this is the belt to
  // its braces - and it says so rather than rendering a blank page.
  if (!active || !Panel) {
    return (
      <div className="page-pad">
        <Note tone="warn" testId="inventory-no-tabs">
          You have no inventory screens on your account.
        </Note>
      </div>
    );
  }

  // Page-level places: each one changes the route, so they are links drawn as a
  // `.seg` (design system v2, decision 5), the current one marked with
  // aria-current. Rust marks only "where you are" in the shell's nav, never
  // these. "damage" is a view of Stock on hand, not a tab of its own, wherever
  // the Stock tab is there to carry it.
  const asView = tabs.some((t) => t.slug === "stock") ? new Set(["damage"]) : new Set<string>();
  const daily = new Set(["stock", "damage", "returns"]);
  const tabLink = (t: (typeof tabs)[number]) => (
    <Link
      key={t.slug}
      // A goods-v1 line opens its own screen (GSA-T02); only a panel tab
      // stays on this page.
      to={t.link ? t.entry : `/inventory?${stockParams(params, { tab: t.slug })}`}
      className={`seg-btn${t.slug === active.slug ? " active" : ""}`}
      aria-current={t.slug === active.slug ? "page" : undefined}
      data-testid={`inventory-tab-${t.slug}`}
    >
      {t.label}
    </Link>
  );
  const reviewTabs = tabs.filter((tab) => !daily.has(tab.slug));
  const strip = (
    <div className="inventory-strip" data-testid="inventory-tabs">
      <nav
        className="inventory-strip-daily"
        aria-label="Daily stock operations"
        data-testid="inventory-daily-tabs"
      >
        <div className="seg">
          {tabs.filter((tab) => daily.has(tab.slug) && !asView.has(tab.slug)).map(tabLink)}
        </div>
      </nav>
      <details
        className="inventory-strip-review"
        open={!daily.has(active.slug)}
        data-testid="inventory-review-tabs"
      >
        <summary>Review &amp; history</summary>
        <div className="seg">
          {reviewTabs.map(tabLink)}
          {user && canAccess("/roadmap", user, session?.display_actions ?? [], featuresOn) && (
            <Link className="seg-btn" to="/roadmap">
              Application roadmap
            </Link>
          )}
        </div>
      </details>
      {active.slug === "assigned-counts" && (
        <p className="muted inventory-strip-note" data-testid="inventory-assigned-counts-inactive">
          Blind counts freeze sales and stock movements. Trading stores must sync and pause their
          online counter before starting; a different authorised person approves the reviewed result
          before closure. Count schedules and earlier evidence are in Review &amp; history.
        </p>
      )}
    </div>
  );

  return (
    <HostedPageContext.Provider
      value={{ crumb: INVENTORY_FOLD.heading, title: active.label, tabs: strip }}
    >
      {/* Keyed on the tab: the panels are whole screens with their own state and
          fetches, so switching tabs mounts a fresh one rather than leaving the
          previous tab's filters and rows behind. */}
      <div key={active.slug}>
        <Panel />
      </div>
    </HostedPageContext.Provider>
  );
}
