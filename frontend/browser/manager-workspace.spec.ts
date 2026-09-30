import { expect, test, type Page, type Response } from "@playwright/test";

import { loginProof, pairProof } from "./firstStoreProof";

// These journeys use the owned proof sibling's existing FIRST store. They
// perform reads and draft-only interactions; no counts, stock, bills or
// postings are created. Synthetic failures below test presentation only.
const WIDTHS = [1440, 1366, 768, 375] as const;
const UNKNOWN_COUNT = "00000000-0000-4000-8000-000000000099";

function apiResponse(page: Page, path: string): Promise<Response> {
  return page.waitForResponse((response) => new URL(response.url()).pathname === `/api${path}`);
}

async function openRead(page: Page, url: string, apiPath: string) {
  const response = apiResponse(page, apiPath);
  await page.goto(url);
  const result = await response;
  // A genuine backend denial is reported as such; a transport/server error is
  // never accepted as a successfully loaded operation.
  expect([200, 403, 404], `${apiPath} must load or explicitly deny`).toContain(result.status());
  test
    .info()
    .annotations.push({ type: "backend-read", description: `${apiPath}: ${result.status()}` });
  return result.status();
}

async function expectFrame(page: Page, width: number) {
  const frame = page.locator(".operations-page").first();
  await expect(frame).toBeVisible();
  await expect(page.getByTestId("page-title")).toBeVisible();
  const geometry = await frame.evaluate((element) => {
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    const content = document.querySelector(".content")!;
    const contentBox = content.getBoundingClientRect();
    return {
      left: box.left,
      right: box.right,
      contentLeft: contentBox.left,
      contentRight: contentBox.right,
      width: element.clientWidth,
      scroll: element.scrollWidth,
      paddingTop: Number.parseFloat(style.paddingTop),
      paddingLeft: Number.parseFloat(style.paddingLeft),
    };
  });
  expect(geometry.left).toBeGreaterThanOrEqual(geometry.contentLeft - 1);
  expect(geometry.right).toBeLessThanOrEqual(geometry.contentRight + 1);
  expect(geometry.scroll, "wide tables must scroll internally").toBeLessThanOrEqual(
    geometry.width + 1,
  );
  expect(geometry.paddingTop).toBe(width <= 480 ? 18 : width <= 768 ? 22 : 28);
  expect(geometry.paddingLeft).toBe(width <= 480 ? 16 : width <= 768 ? 20 : 32);

  // This catches the inherited flex-basis bug that made labelled filters
  // 160px high. Table editors may intentionally be wider inside a scroll box.
  for (const control of await frame
    .locator(".form-grid > .field input, .form-grid > .field select")
    .all()) {
    if (!(await control.isVisible())) continue;
    const box = await control.boundingBox();
    expect(box!.height).toBeLessThan(80);
    expect(box!.x + box!.width).toBeLessThanOrEqual(geometry.right + 1);
  }
  return frame;
}

for (const width of WIDTHS) {
  test(`real manager operations remain coherent at ${width}px`, async ({ page }) => {
    test.setTimeout(120_000);
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.setViewportSize({ width, height: 1000 });
    await loginProof(page, "manager");

    await test.step("populated stock and a genuine empty search", async () => {
      expect(await openRead(page, "/inventory?tab=stock", "/stockledger/on-hand")).toBe(200);
      await expect(page.getByTestId("onhand-table")).toBeVisible();
      await expectFrame(page, width);
      const stockTable = page.getByRole("region", { name: "Stock on hand", exact: true });
      await stockTable.focus();
      await expect(stockTable).toBeFocused();
      await stockTable.press("ArrowRight");
      await expect(page.getByTestId("stock-workspace-store")).not.toHaveValue("");
      const search = page.getByTestId("stock-workspace-search");
      await search.fill("__no_match_first_store_alpha__");
      await search.press("Enter");
      await expect(page.getByTestId("onhand-empty")).toBeVisible();
      await expect(page).toHaveURL(/q=__no_match_first_store_alpha__/);
      await expectFrame(page, width);
      await search.fill("");
      await search.press("Enter");
      await expect(page.getByTestId("onhand-table")).toBeVisible();
    });

    await test.step("damage filters stay compact", async () => {
      await openRead(page, "/inventory?tab=damage", "/goods-v1/stockledger/quarantine");
      await expect(page.getByTestId("canonical-damage-workspace")).toBeVisible();
      await expectFrame(page, width);
      for (const id of ["stock-site", "stock-state", "stock-basis"]) {
        const box = await page.getByTestId(id).boundingBox();
        expect(box!.height).toBeLessThan(80);
      }
    });

    await test.step("canonical counts explain their contract", async () => {
      const status = await openRead(page, "/goods/counts", "/goods-v1/outbound/stocktakes");
      await expectFrame(page, width);
      if (status === 200) {
        await expect(page.getByText("No count has been started at your sites.")).toBeVisible();
        await expect(page.getByText(/Blind counts at sites that do not trade/)).toBeVisible();
      } else {
        await expect(page.getByTestId("org-denied")).toBeVisible();
      }
    });

    await test.step("receiving keeps an honest empty or denied state", async () => {
      const status = await openRead(page, "/goods/receive", "/goods-v1/inbound/inbox");
      await expectFrame(page, width);
      if (status === 200) {
        await expect(page.getByText("Nothing is on its way in to this site.")).toBeVisible();
      } else {
        await expect(page.getByTestId("inbox-denied")).toBeVisible();
      }
    });

    await test.step("transfers keep filters and preparation separate", async () => {
      const status = await openRead(page, "/goods/transfers", "/goods-v1/outbound/transfers");
      await expectFrame(page, width);
      await expect(page.getByTestId("transfers-direction")).toBeVisible();
      if (status === 200) {
        await expect(page.getByText("No transfer here matches this filter.")).toBeVisible();
      } else {
        await expect(page.getByTestId("transfers-denied")).toBeVisible();
      }
    });

    await test.step("inventory reports load or explain field/scope denial", async () => {
      await page.goto("/reports/inventory");
      await expect(page.getByTestId("access-denied")).toBeVisible();
      // Navigation is a hint: the backend independently refuses this manager.
      expect((await page.request.get("/api/reports/inventory")).status()).toBe(403);
    });

    await test.step("the manager sees their active store's targets without Setup access", async () => {
      const status = await openRead(
        page,
        "/money/store-targets",
        "/masters/store-targets/locations",
      );
      expect(status).toBe(200);
      await expectFrame(page, width);
      await expect(page.getByText("No stores yet.", { exact: true })).toHaveCount(0);
      await expect(page.getByTestId("target-row-FIRST")).toContainText("First proof shop");
    });

    await test.step("the established POS supports a draft-only scan without preview features", async () => {
      const issued: string[] = [];
      page.on("request", (request) => {
        if (
          request.method() === "POST" &&
          new URL(request.url()).pathname === "/api/sell/sales/finalise-online"
        ) {
          issued.push("sale issue");
        }
      });
      await pairProof(page);
      await page.goto("/sell");
      await expect(page.getByTestId("plus-rail")).toHaveCount(0);
      await expect(page.locator(".operations-page")).toHaveCount(0);
      const scan = page.getByTestId("bill-scan");
      await expect(scan).toBeEnabled();
      await scan.fill("ALPHA000123");
      await scan.press("Enter");
      const line = page.getByTestId("bill-line-1");
      await expect(line).toContainText("ALPHA000123");
      const content = await page.locator(".content").evaluate((element) => ({
        width: element.clientWidth,
        scroll: element.scrollWidth,
      }));
      expect(content.scroll).toBeLessThanOrEqual(content.width + 1);
      await page.getByTestId("bill-remove-1").click();
      await expect(page.getByTestId("bill-empty")).toBeVisible();
      expect(issued).toEqual([]);
    });

    expect(errors).toEqual([]);
    await test.info().attach(`manager-${width}px`, {
      body: await page.screenshot(),
      contentType: "image/png",
    });
  });

  test(`manager loading, backend denial and simulated errors retain the frame at ${width}px`, async ({
    page,
  }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width, height: 1000 });
    await loginProof(page, "manager");

    await test.step("delayed real count response", async () => {
      let release!: () => void;
      const paused = new Promise<void>((resolve) => {
        release = resolve;
      });
      await page.route("**/api/goods-v1/outbound/stocktakes", async (route) => {
        await paused;
        await route.continue();
      });
      await page.goto("/goods/counts");
      try {
        await expect(page.getByText("Loading…", { exact: true })).toBeVisible();
        await expectFrame(page, width);
      } finally {
        release();
      }
      await expect(page.getByText("Loading…", { exact: true })).toBeHidden();
      await page.unroute("**/api/goods-v1/outbound/stocktakes");
    });

    await test.step("unknown count receives a real backend denial", async () => {
      const response = apiResponse(page, `/goods-v1/outbound/stocktakes/${UNKNOWN_COUNT}`);
      await page.goto(`/goods/counts/${UNKNOWN_COUNT}`);
      expect((await response).status()).toBe(404);
      await expect(page.getByTestId("org-denied")).toBeVisible();
      await expectFrame(page, width);
      await expect(page.getByTestId("cnt-my-pass")).toHaveCount(0);
      await expect(page.getByTestId("cnt-review")).toHaveCount(0);
    });

    await test.step("simulated stock server failure is not presented as empty stock", async () => {
      await page.route("**/api/stockledger/on-hand**", (route) =>
        route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({
            detail: "Simulated stock service unavailable for layout verification.",
          }),
        }),
      );
      await page.goto("/inventory?tab=stock");
      await expect(page.getByTestId("onhand-error")).toBeVisible();
      await expect(page.getByTestId("onhand-empty")).toHaveCount(0);
      await expectFrame(page, width);
      await page.unroute("**/api/stockledger/on-hand**");
    });

    await test.step("simulated report transport failure offers recovery", async () => {
      await page.route("**/api/store/cash-summary**", (route) => route.abort());
      await page.goto("/reports/day-summary");
      await expect(page.getByTestId("day-error")).toBeVisible();
      await expect(page.getByTestId("day-money")).toHaveCount(0);
      await expect(page.getByTestId("day-retry")).toBeVisible();
      await expectFrame(page, width);
      await page.unroute("**/api/store/cash-summary**");
      const recovered = apiResponse(page, "/store/cash-summary");
      await page.getByTestId("day-retry").click();
      expect((await recovered).status()).toBe(200);
      await expect(page.getByTestId("day-error")).toHaveCount(0);
      await expect(page.getByTestId("day-money")).toBeVisible();
      await expectFrame(page, width);
    });
  });
}
