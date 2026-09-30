import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GoodsStockPage } from "./GoodsStock";

const reads = vi.hoisted(() => ({ urls: [] as (string | null)[], summaryFailure: false }));

vi.mock("../lib/goodsScreen", async (original) => ({
  ...(await original<typeof import("../lib/goodsScreen")>()),
  useGoodsFetch: (url: string | null) => {
    reads.urls.push(url);
    const failed = reads.summaryFailure && url?.includes("stockledger/summary");
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
  });

  it("starts in quarantine at the active store and offers governed physical-stock reporting", () => {
    const html = draw();
    expect(html).toContain('data-testid="canonical-damage-workspace"');
    expect(html.match(/class="page-pad operations-page/g)).toHaveLength(1);
    expect(html).toContain('value="12" selected');
    expect(html).toContain('btn-active" data-testid="stock-view-quarantine"');
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
      'btn-active" data-testid="stock-view-on-hand"',
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
});
