import { expect, test } from "@playwright/test";
import { loginProof, pairProof, stepUpProof } from "./firstStoreProof";

// Matching counts append actual browser evidence while preserving the fixture's
// stock, sales and money. The separate reviewer is an explicit synthetic fixture.
for (const width of [1440, 1366, 768, 375]) {
  test(`paused blind count needs a separate exact reviewer at ${width}px`, async ({
    page,
    browser,
  }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width, height: 1000 });
    await loginProof(page, "manager");
    await pairProof(page);
    await page.getByTestId("till-sync-now").click();
    await expect(page.getByTestId("till-pause-go")).toBeVisible();
    await page
      .getByTestId("till-pause-reason")
      .fill("Fictional matching blind count browser proof");
    await page.getByTestId("till-pause-go").click();
    await expect(page.getByTestId("till-pause-state")).toHaveAttribute("data-stage", "paused");
    await page.goto("/goods/counts");
    const site = await page.getByTestId("cnt-site").locator("option").last().getAttribute("value");
    await page.getByTestId("cnt-site").selectOption(site!);
    await page.getByTestId("cnt-start-go").click();
    await expect(page).toHaveURL(/\/goods\/counts\/[0-9a-f-]+$/);
    const url = page.url();
    await page.getByTestId("cnt-open").click();
    await expect(page.getByTestId("cnt-location")).toBeVisible();
    await page.getByTestId("cnt-location").selectOption({ label: "Fictional proof sales floor" });
    await page.getByTestId("cnt-tag").fill("ALPHA000123");
    await page.getByTestId("cnt-lookup").click();
    await expect(page.getByTestId("cnt-item")).toBeVisible();
    await expect(page.getByTestId("cnt-variance")).toHaveCount(0);
    await page.getByTestId("cnt-qty").fill("2");
    await page.getByTestId("cnt-record").click();
    await expect(page.getByTestId("cnt-observation")).toHaveCount(1);
    await page.getByTestId("cnt-affirm").check();
    await page.getByTestId("cnt-submit").click();
    await expect(page.getByTestId("cnt-progress")).toHaveAttribute(
      "data-progress",
      "awaiting_review",
    );
    const ownerContext = await browser.newContext({
      baseURL: "http://127.0.0.1:5178",
      viewport: { width, height: 1000 },
    });
    const checkerContext = await browser.newContext({
      baseURL: "http://127.0.0.1:5178",
      viewport: { width, height: 1000 },
    });
    try {
      const owner = await ownerContext.newPage();
      await loginProof(owner, "owner");
      await owner.goto(url);
      await expect(owner.getByTestId("cnt-review-summary")).toHaveAttribute("data-matches", "yes");
      await expect(owner.getByTestId("cnt-close")).toHaveCount(0);
      await owner.getByTestId("cnt-review-reason").fill("SOURCE_COUNT");
      await owner.getByTestId("cnt-submit-review").click();
      await expect(owner.getByTestId("cnt-detail")).toHaveAttribute("data-state", "review");
      await expect(owner.getByTestId("cnt-approve")).toHaveCount(0);
      const checker = await checkerContext.newPage();
      await loginProof(checker, "count_checker");
      await checker.goto(url);
      await expect(checker.getByTestId("cnt-approval")).toContainText("remove 0 unit(s)");
      const geometry = await checker
        .locator(".operations-page")
        .evaluate((el) => ({ width: el.clientWidth, scroll: el.scrollWidth }));
      expect(geometry.scroll).toBeLessThanOrEqual(geometry.width + 1);
      await checker.getByTestId("cnt-approve").click();
      await stepUpProof(checker, "count_checker");
      await expect(checker.getByTestId("cnt-detail")).toHaveAttribute("data-state", "closed");
      await expect(checker.getByTestId("cnt-history")).toContainText("count closed");
      await page.goto("/sell/till");
      await page.getByTestId("till-pause-resume").click();
      await expect(page.getByTestId("till-pause-state")).toHaveCount(0);
    } finally {
      await ownerContext.close();
      await checkerContext.close();
    }
  });
}
