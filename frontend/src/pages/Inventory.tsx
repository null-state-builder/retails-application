/** Stock work shares one scoped workspace. Daily operations and canonical
 * count entry stay separate from review and earlier records. Every destination
 * keeps its existing route/action gate; presentation retires no writer. */

import { useMemo } from "react";
import type { ReactElement } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { canAccess } from "../auth/routeAccess";
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

  // The route guard already refuses anyone with no tab, so this is the belt to
  // its braces - and it says so rather than rendering a blank page.
  if (!active || !Panel) {
    return (
      <div className="page-pad">
        <p className="warn-note" data-testid="inventory-no-tabs">
          You have no inventory screens on your account.
        </p>
      </div>
    );
  }

  // Page-level tabs, deliberately not the `.seg` pill strip the screens
  // themselves use for their own groupings: on Stock on Hand the two sit one
  // above the other, and two identical controls in a row read as an accident.
  const daily = new Set(["stock", "damage", "returns"]);
  const tabLink = (t: (typeof tabs)[number]) => (
    <Link
      key={t.slug}
      // A goods-v1 line opens its own screen (GSA-T02); only a panel tab
      // stays on this page.
      to={t.link ? t.entry : `/inventory?${stockParams(params, { tab: t.slug })}`}
      className={`page-tab ${t.slug === active.slug ? "active" : ""}`}
      aria-current={t.slug === active.slug ? "page" : undefined}
      data-testid={`inventory-tab-${t.slug}`}
    >
      {t.label}
    </Link>
  );
  const reviewTabs = tabs.filter((tab) => !daily.has(tab.slug));
  const strip = (
    <div data-testid="inventory-tabs">
      <nav aria-label="Daily stock operations" data-testid="inventory-daily-tabs">
        <p className="eyebrow">Daily stock operations</p>
        <div className="page-tabs">{tabs.filter((tab) => daily.has(tab.slug)).map(tabLink)}</div>
        {tabs.some((tab) => tab.slug === "assigned-counts") && (
          <p className="muted" data-testid="inventory-assigned-counts-inactive">
            Assigned blind counts for trading stores are not active. Existing blind counts require
            an independently approved non-trading site; their records and count schedules are in
            Review &amp; history. Reviewed SOH inventory reconciliation is a separate workflow.
          </p>
        )}
      </nav>
      <details open={!daily.has(active.slug)} data-testid="inventory-review-tabs">
        <summary>Review &amp; history</summary>
        <div className="page-tabs">
          {reviewTabs.map(tabLink)}
          {user && canAccess("/roadmap", user, session?.display_actions ?? [], featuresOn) && (
            <Link className="page-tab" to="/roadmap">
              Application roadmap
            </Link>
          )}
        </div>
      </details>
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
