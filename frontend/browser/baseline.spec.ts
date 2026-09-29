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

  await page.getByTestId("user-menu").click();
  await page.getByTestId("logout-button").click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  expect(pageErrors).toEqual([]);
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
