import { expect, test } from "@playwright/test";
import { loginProof, pairProof, stepUpProof } from "./firstStoreProof";

// Matching counts append actual browser evidence while preserving the fixture's
// stock, sales and money. The separate reviewer is an explicit synthetic fixture.
test("resume an interrupted exact review without replacing its count", async ({
  page,
  browser,
}) => {
  await loginProof(page, "manager");
  await page.goto("/goods/counts");
  // The list exists only once the shop has counts; the start panel is always there.
  await expect(page.getByTestId("cnt-start")).toBeVisible();
  const pending = page.locator('[data-testid="cnt-row"][data-state="review"]');
  const count = await pending.count();
  if (count === 0) {
    test.skip(true, "No interrupted review remains on this owned proof.");
    return;
  }
  expect(count, "Only one frozen review may be resumed on the owned shop").toBe(1);
  const href = await pending.getByTestId("cnt-open-link").getAttribute("href");
  expect(href).toMatch(/^\/goods\/counts\/[0-9a-f-]+$/);
  const checkerContext = await browser.newContext({
    baseURL: "http://127.0.0.1:5178",
    viewport: { width: 1440, height: 1000 },
  });
  try {
    const checker = await checkerContext.newPage();
    await loginProof(checker, "count_checker");
    await checker.goto(href!);
    await expect(checker.getByTestId("cnt-detail")).toHaveAttribute("data-state", "review");
    await expect(checker.getByTestId("cnt-approval")).toContainText("remove 0 unit(s)");
    await checker.getByTestId("cnt-approve").click();
    const confirmation = checker.getByRole("dialog", { name: "Confirm it's you" });
    await expect(confirmation).toBeVisible();
    await checker.keyboard.press("Escape");
    await expect(confirmation).toHaveCount(0);
    await expect(checker.getByTestId("cnt-detail")).toHaveAttribute("data-state", "review");
    await expect(checker.getByTestId("cnt-approve")).toBeFocused();
    await checker.getByTestId("cnt-approve").click();
    await expect
      .poll(async () => {
        if ((await checker.getByTestId("cnt-detail").getAttribute("data-state")) === "closed")
          return "closed";
        if (await checker.getByTestId("org-stepup").isVisible()) return "stepup";
        return "pending";
      })
      .not.toBe("pending");
    if (await checker.getByTestId("org-stepup").isVisible())
      await stepUpProof(checker, "count_checker");
    await expect(checker.getByTestId("cnt-detail")).toHaveAttribute("data-state", "closed");
    await expect(checker.getByTestId("cnt-history")).toContainText("count closed");
  } finally {
    await checkerContext.close();
  }
  await pairProof(page);
  await page.getByTestId("till-pause-resume").click();
  await expect(page.getByTestId("till-pause-state")).toHaveCount(0);
});

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
      const confirmation = checker.getByRole("dialog", { name: "Confirm it's you" });
      await expect(confirmation).toBeVisible();
      await expect(confirmation.getByLabel("Password", { exact: true })).toBeFocused();
      await checker.keyboard.press("Shift+Tab");
      await expect(checker.getByTestId("org-stepup-cancel")).toBeFocused();
      await checker.keyboard.press("Tab");
      await expect(confirmation.getByLabel("Password", { exact: true })).toBeFocused();
      if (width === 1440) {
        await confirmation.getByLabel("Password", { exact: true }).fill("wrong-proof-password");
        await confirmation.getByLabel("Password", { exact: true }).press("Enter");
        await expect(confirmation.getByRole("alert")).toBeVisible();
        await expect(checker.getByTestId("cnt-detail")).toHaveAttribute("data-state", "review");
      }
      await checker.keyboard.press("Escape");
      await expect(confirmation).toHaveCount(0);
      await expect(checker.getByTestId("cnt-detail")).toHaveAttribute("data-state", "review");
      await expect(checker.getByTestId("cnt-approve")).toBeEnabled();
      await expect(checker.getByTestId("cnt-approve")).toBeFocused();
      await checker.getByTestId("cnt-approve").click();
      let releaseConfirmation = () => {};
      const confirmationGate = new Promise<void>((resolve) => {
        releaseConfirmation = resolve;
      });
      await checker.route("**/api/auth/step-up", async (route) => {
        await confirmationGate;
        await route.continue();
      });
      let releaseCommand = () => {};
      const commandGate = new Promise<void>((resolve) => {
        releaseCommand = resolve;
      });
      if (width === 1440) {
        await checker.route("**/api/goods-v1/approvals/*/decide", async (route) => {
          await commandGate;
          await route.fulfill({
            status: 503,
            contentType: "application/json",
            body: JSON.stringify({ error: "Synthetic retry error for focus recovery." }),
          });
        });
      }
      const confirmed = stepUpProof(checker, "count_checker", true);
      try {
        await expect(confirmation.getByLabel("Password", { exact: true })).toBeDisabled();
        await expect(confirmation).toBeFocused();
        await checker.keyboard.press("Tab");
        await expect(confirmation).toBeFocused();
        await checker.keyboard.press("Shift+Tab");
        await expect(confirmation).toBeFocused();
      } finally {
        releaseConfirmation();
        await confirmed;
        await checker.unroute("**/api/auth/step-up");
      }
      if (width === 1440) {
        await expect(checker.getByTestId("cnt-approve")).toBeDisabled();
        releaseCommand();
        await expect(checker.getByTestId("org-error")).toContainText(
          "Synthetic retry error for focus recovery.",
        );
        await expect(checker.getByTestId("cnt-detail")).toHaveAttribute("data-state", "review");
        await expect(checker.getByTestId("cnt-approve")).toBeEnabled();
        await expect(checker.getByTestId("cnt-approve")).toBeFocused();
        await checker.unroute("**/api/goods-v1/approvals/*/decide");
        await checker.getByTestId("cnt-approve").click();
      }
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
