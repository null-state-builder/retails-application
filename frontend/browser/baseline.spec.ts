import { expect, test } from "@playwright/test";

test("authentication, protected access, dashboard and logout use the real backend", async ({
  page,
}) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));

  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByTestId("login-form")).toBeVisible();

  await page.getByTestId("login-email").fill("owner@kdps.demo");
  await page.getByTestId("login-password").fill("wrong-password");
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("login-error")).toBeVisible();
  await expect(page).toHaveURL(/\/login$/);

  await page.getByTestId("login-password").fill("Owner@123");
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("greeting")).toBeVisible();
  await expect(page.getByTestId("dashboard-card-grid")).toBeVisible();

  let releaseLogout!: () => void;
  const delayedLogout = new Promise<void>((resolve) => {
    releaseLogout = resolve;
  });
  await page.route("**/api/auth/logout", async (route) => {
    await delayedLogout;
    await route.continue();
  });
  await page.getByTestId("user-menu").click();
  await page.getByTestId("logout-button").click();
  await expect(page.getByTestId("logout-button")).toBeDisabled();
  await expect(page).toHaveURL(/\/$/);
  const confirmedLogout = page.waitForResponse(
    (response) => response.url().endsWith("/api/auth/logout") && response.status() === 200,
  );
  releaseLogout();
  await confirmedLogout;
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  expect(pageErrors).toEqual([]);
});

test("failed logout stays visible and can be retried before leaving the session", async ({
  page,
}) => {
  await page.goto("/login");
  await page.getByTestId("login-email").fill("owner@kdps.demo");
  await page.getByTestId("login-password").fill("Owner@123");
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("greeting")).toBeVisible();
  await page.route("**/api/auth/logout", (route) => route.abort(), { times: 1 });
  await page.getByTestId("user-menu").click();
  await page.getByTestId("logout-button").click();
  await expect(page.getByTestId("logout-error")).toContainText("Sign out was not confirmed");
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("logout-button")).toBeEnabled();
  await page.getByTestId("logout-button").click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});

test("access bookmarks use the unified editor and a scoped store login is denied", async ({
  page,
}) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));

  await page.goto("/login");
  await page.getByTestId("login-email").fill("owner@kdps.demo");
  await page.getByTestId("login-password").fill("Owner@123");
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/$/);

  await page.goto("/setup/access");
  await expect(page).toHaveURL(/\/setup\/people-access\?panel=roles$/);
  await expect(page.getByTestId("pa-policy-panel")).toBeVisible();
  await page.getByTestId("pa-policy-panel").getByLabel("Role").selectOption("owner");
  await expect(page.getByTestId("pa-policy-json")).toHaveValue(/section_access/);
  await page.getByTestId("pa-nav-workflow").click();
  await expect(page.getByTestId("pa-workflow-policy-panel")).toBeVisible();
  await expect(page.getByLabel("access.manage minimum level")).toHaveValue("manage");

  await page.getByTestId("user-menu").click();
  await page.getByTestId("logout-button").click();
  await page.getByTestId("login-email").fill("deo.manager@kdps.demo");
  await page.getByTestId("login-password").fill("Store@123");
  await page.getByTestId("login-submit").click();
  await expect(page).not.toHaveURL(/\/login$/);

  await page.goto("/setup/people-access");
  await expect(page.getByTestId("access-denied")).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test("saving workflow policy confirms identity and revokes the active session", async ({
  page,
}) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));

  await page.goto("/login");
  await page.getByTestId("login-email").fill("owner@kdps.demo");
  await page.getByTestId("login-password").fill("Owner@123");
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/$/);

  await page.goto("/setup/people-access?panel=workflow");
  await expect(page.getByTestId("pa-workflow-policy-panel")).toBeVisible();
  await expect(page.getByLabel("access.manage minimum level")).toHaveValue("manage");
  const before = await page.request.get("/api/auth/admin/workflow-policy");
  expect(before.ok()).toBe(true);
  const beforeRevision = Number((await before.json()).revision);
  await page
    .getByTestId("pa-workflow-policy-panel")
    .getByLabel("Current password")
    .fill("Owner@123");
  await page.getByTestId("pa-workflow-policy-save").click();
  await expect(page.getByTestId("login-form")).toBeVisible();
  await page.getByTestId("login-email").fill("owner@kdps.demo");
  await page.getByTestId("login-password").fill("Owner@123");
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/$/);
  const after = await page.request.get("/api/auth/admin/workflow-policy");
  expect(after.ok()).toBe(true);
  expect(Number((await after.json()).revision)).toBe(beforeRevision + 1);
  expect(pageErrors).toEqual([]);
});

test("scheduled assignment editing keeps its real backend identity and start date", async ({
  page,
}) => {
  await page.goto("/login");
  await page.getByTestId("login-email").fill("owner@kdps.demo");
  await page.getByTestId("login-password").fill("Owner@123");
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/$/);

  const endpoint = "/api/auth/admin/users/10/assignments";
  const first = await page.request.get(endpoint);
  expect(first.ok()).toBe(true);
  const initial = (await first.json()) as {
    revision: number;
    items: Array<{
      id: string;
      role_code: string;
      effective_from: string;
      effective_to: string | null;
    }>;
  };
  const index = initial.items.findIndex(
    (item) => item.role_code === "warehouse" && new Date(item.effective_from) > new Date(),
  );
  expect(index).toBeGreaterThanOrEqual(0);
  const future = initial.items[index];
  expect(future).toBeDefined();

  await page.goto("/setup/people-access");
  await page.getByTestId("person-row-deo.manager").click();
  await page.getByTestId("person-tab-grants").click();
  const row = page.getByTestId(`pa-assignment-${index}`);
  await expect(row).toBeVisible();
  const end = new Date(new Date(future!.effective_from).getTime() + 2 * 86400_000);
  const local = new Date(end.getTime() - end.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16);
  await row.locator('input[type="datetime-local"]').fill(local);
  await page.getByTestId("pa-assignments-tab").getByLabel("Current password").fill("Owner@123");
  await page.getByTestId("pa-assignments-save").click();
  await expect(page.getByText("Assignments saved.", { exact: false })).toBeVisible();
  const edited = (await (await page.request.get(endpoint)).json()) as typeof initial;
  const after = edited.items.find((item) => item.id === future!.id);
  expect(after?.effective_from).toBe(future!.effective_from);
  expect(after?.effective_to).not.toBeNull();

  await page
    .getByTestId(`pa-assignment-${index}`)
    .getByRole("button", { name: "Remove assignment" })
    .click();
  await page.getByTestId("pa-assignments-tab").getByLabel("Current password").fill("Owner@123");
  await page.getByTestId("pa-assignments-save").click();
  await expect(page.getByText("Assignments saved.", { exact: false })).toBeVisible();
  const removed = (await (await page.request.get(endpoint)).json()) as typeof initial;
  expect(removed.items.some((item) => item.id === future!.id)).toBe(false);
});

test("built icons are served and the app shell reloads offline without serving cached API data", async ({
  page,
  context,
}) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));

  for (const path of [
    "/kdps-mark.svg",
    "/icon-192.png",
    "/icon-512.png",
    "/icon-maskable-512.png",
    "/apple-touch-icon.png",
    "/manifest.webmanifest",
  ]) {
    const response = await page.request.get(path);
    expect(response.ok(), path).toBe(true);
  }

  await page.goto("/login");
  await expect(page.getByTestId("login-form")).toBeVisible();
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.reload();
  await expect(page.getByTestId("login-form")).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => Boolean(navigator.serviceWorker.controller)))
    .toBe(true);

  const online = await page.evaluate(async () => {
    const response = await fetch("/api/health");
    return { ok: response.ok, type: response.headers.get("content-type") };
  });
  expect(online.ok).toBe(true);
  expect(online.type).toContain("application/json");

  await context.setOffline(true);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("login-form")).toBeVisible();
  const offlineApi = await page.evaluate(async () => {
    try {
      const response = await fetch("/api/health");
      return { networkError: false, type: response.headers.get("content-type") };
    } catch {
      return { networkError: true, type: null };
    }
  });
  expect(offlineApi).toEqual({ networkError: true, type: null });
  expect(pageErrors).toEqual([]);
});
