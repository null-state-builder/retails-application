import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { StockWorkspace } from "./StockWorkspace";

const stores = [
  { id: 12, code: "FIRST", name: "First shop" },
  { id: 27, code: "NEXT", name: "Next shop" },
];

vi.mock("../auth/AuthContext", () => ({
  allowedUnits: (user: { business_units: typeof stores }) => user.business_units,
  useAuth: () => ({
    user: { business_units: stores },
    activeStore: stores[0],
    session: { display_actions: ["stock.view", "transfer.allocate"] },
  }),
}));

function draw(search: string) {
  return renderToStaticMarkup(
    <MemoryRouter initialEntries={[`/inventory${search}`]}>
      <StockWorkspace />
    </MemoryRouter>,
  );
}

describe("one operator-facing stock frame", () => {
  it("renders one header, frame and search control with the active store selected", () => {
    const html = draw("");
    expect(html.match(/class="page-pad operations-page/g)).toHaveLength(1);
    expect(html.match(/data-testid="page-title"/g)).toHaveLength(1);
    expect(html.match(/data-testid="stock-workspace-search"/g)).toHaveLength(1);
    expect(html).toContain('value="12" selected');
    expect(html).toContain("All authorised stores");
    expect(html).toContain('href="/goods/transfers/requests"');
    expect(html).toContain("Loading…");
  });

  it("shows a missing bookmarked store without a stock table or fabricated zero totals", () => {
    const html = draw("?site=999&q=shirt");
    expect(html).toContain("Bookmarked store unavailable");
    expect(html).toContain("no longer in your available store choices");
    expect(html).not.toContain('data-testid="onhand-summary"');
  });

  it("keeps availability in the same frame and explains its field and request limits", () => {
    const html = draw("?scope=all&view=availability&q=Shirt&group=brand");
    expect(html.match(/class="page-pad operations-page/g)).toHaveLength(1);
    expect(html).toContain("Costs, margins and values are unavailable in this view");
    expect(html).toContain("A request reserves nothing");
    expect(html).toContain("group=brand");
    expect(html).not.toContain('data-testid="availability-search"');
  });
});
