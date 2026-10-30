import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GoodsStockPage } from "./GoodsStock";

const reads = vi.hoisted(() => ({
  urls: [] as (string | null)[],
  summaryFailure: false,
  rowsRefreshFailed: false,
}));

vi.mock("../lib/goodsScreen", async (original) => ({
  ...(await original<typeof import("../lib/goodsScreen")>()),
  useGoodsFetch: (url: string | null) => {
    reads.urls.push(url);
    const failed = reads.summaryFailure && url?.includes("stockledger/summary");
    if (reads.rowsRefreshFailed && url?.includes("stockledger/quarantine")) {
      // Rows read before, then a refresh that failed behind them.
      return {
        loading: false,
        denied: false,
        deniedField: false,
        failure: "The server is busy.",
        error: { response: { status: 503, data: { detail: "The server is busy." } } },
        fetching: false,
        updatedAt: Date.now() - 2 * 60_000,
        value: { items: [], next_cursor: null, as_of: "2026-10-30T10:00:00Z" },
        reload: () => {},
      };
    }
    return {
      loading: !failed,
      denied: false,
      deniedField: false,
      failure: failed ? "Summary unavailable" : "",
      value: failed ? { basis: "quantity", totals: { physical_qty: 999 } } : null,
      reload: () => {},
    };
  },
}));

vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({
    activeStore: { id: 12 },
    session: {
      display_actions: ["stock.view", "movement.draft"],
      sites: [
        { id: "12", code: "FIRST", name: "First shop", type: "store" },
        { id: "27", code: "NEXT", name: "Next shop", type: "store" },
      ],
    },
  }),
}));

function draw(search = "") {
  return renderToStaticMarkup(
    <MemoryRouter initialEntries={[`/inventory?tab=damage${search}`]}>
      <GoodsStockPage damageWorkspace />
    </MemoryRouter>,
  );
}

describe("daily damage uses the canonical stock screen", () => {
  beforeEach(() => {
    reads.urls.length = 0;
    reads.summaryFailure = false;
    reads.rowsRefreshFailed = false;
  });

  it("starts in quarantine at the active store and offers governed physical-stock reporting", () => {
    const html = draw();
    expect(html).toContain('data-testid="canonical-damage-workspace"');
    expect(html.match(/class="page-pad operations-page/g)).toHaveLength(1);
    expect(html).toContain('value="12" selected');
    expect(html).toContain('aria-pressed="true" data-testid="stock-view-quarantine"');
    expect(html).toContain("Find physical stock to report");
    expect(html).toContain('href="/goods/movements?site_id=12"');
    expect(html).toContain("Summary totals cover all stock in the selected scope");
    expect(reads.urls).toContain("/goods-v1/stockledger/quarantine?site_id=12&basis=quantity");
    expect(reads.urls).toContain("/goods-v1/outbound/damage-reports?site=12");
    expect(reads.urls.some((url) => url?.startsWith("/stockledger/quarantine"))).toBe(false);
  });

  it("retains explicit all-site and bookmarked stable scope instead of switching back to the active store", () => {
    expect(draw("&site_id=")).toContain('value="" selected');
    expect(draw("&scope=all")).toContain('value="" selected');
    expect(draw("&site=27")).toContain('value="27" selected');
    expect(draw("&store=NEXT")).toContain('value="27" selected');
    expect(draw("&site_id=27&view=on-hand")).toContain(
      'aria-pressed="true" data-testid="stock-view-on-hand"',
    );
    expect(draw("&site=999")).toContain("Bookmarked site unavailable");
    expect(draw("&site=999")).not.toContain('value="12" selected');
  });

  it("does not display retained summary values after a failed scope read", () => {
    reads.summaryFailure = true;
    const html = draw();
    expect(html).toContain("Summary unavailable");
    expect(html).not.toContain('data-testid="stock-summary"');
    expect(html).not.toContain("999");
  });

  it("says how old the stock is, and keeps it on screen with a warning when a refresh fails", () => {
    reads.rowsRefreshFailed = true;
    const html = draw();
    expect(html).toContain('data-testid="stock-updated"');
    expect(html).toContain("2 min ago");
    expect(html).toContain('data-testid="stock-refresh-failed"');
    expect(html).toContain("may be out of date");
    // The rows' own answer stands: no failure in their place.
    expect(html).toContain("Nothing is quarantined or held for this scope.");
  });
});
