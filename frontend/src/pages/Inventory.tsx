/**
 * Inventory - the store's goods work on one page (#170, D10 §1).
 *
 * Stock, Stock Count and Return to Brand used to be three headings in the
 * sidebar. D10 folded them: "no subsections in the sidebar, ever - anything
 * that needs dividing divides inside the page, as tabs." So this is one URL
 * with five tabs, and each tab is an *existing* screen rendered unchanged.
 *
 * It is presentation and nothing else. No section code moved, no permission key
 * changed, and each tab is gated by the very menu entry it draws - the manifest
 * answers "may this person see this tab?" with the same call the sidebar makes
 * (`foldTabs`), so a role without count rights simply has no Count & Adjust tab
 * and the guard on this URL refuses anyone with no tab at all.
 *
 * The panels keep their own `PageHeader`; `HostedPageContext` tells that header
 * which screen is showing and hands it the tab strip, so folding a screen never
 * means editing it.
 */

import { useMemo } from "react";
import type { ReactElement } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { HostedPageContext } from "../components/PageHeader";
import { INVENTORY_FOLD, foldTabsFor, resolveFoldTab } from "../shell/navConfig";
import { BrokenSizesPage } from "./BrokenSizes";
import { SizeBalancingPage } from "./SizeBalancing";
import CrossStoreSearch from "./CrossStoreSearch";
import { MissingHsnPage } from "./MissingHsn";
import { RTVListPage } from "./OutboundRTV";
import { StockAgeingPage } from "./StockAgeing";
import { CountSchedulePage } from "./CountSchedule";
import { StockCountListPage } from "./StockCount";
import StockOnHand from "./StockOnHand";

/** Tab slug → the screen it draws. Every slug in `INVENTORY_FOLD.tabs` needs one
 *  here; `Inventory.test` fails the build otherwise, because a tab the sidebar
 *  offers and this map has no answer for is a white screen. */
export const PANELS: Record<string, () => ReactElement> = {
  stock: () => <StockOnHand view="stock" />,
  search: () => <CrossStoreSearch />,
  hsn: () => <MissingHsnPage />,
  ageing: () => <StockAgeingPage />,
  broken: () => <BrokenSizesPage />,
  balance: () => <SizeBalancingPage />,
  damage: () => <StockOnHand view="quarantine" />,
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
  const strip = (
    <div className="page-tabs" data-testid="inventory-tabs">
      {tabs.map((t) => (
        <Link
          key={t.slug}
          // A goods-v1 line opens its own screen (GSA-T02); only a panel tab
          // stays on this page.
          to={t.link ? t.entry : `/inventory?tab=${t.slug}`}
          className={`page-tab ${t.slug === active.slug ? "active" : ""}`}
          aria-current={t.slug === active.slug ? "page" : undefined}
          data-testid={`inventory-tab-${t.slug}`}
        >
          {t.label}
        </Link>
      ))}
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
