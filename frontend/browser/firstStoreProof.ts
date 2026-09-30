import { readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { expect, type Page } from "@playwright/test";

/** Generated local proof identities only. Passwords never enter traces or logs. */
export async function loginProof(page: Page, role: "manager" | "owner" | "admin") {
  const path = fileURLToPath(
    new URL("../../.local/first-store-browser-credentials.json", import.meta.url),
  );
  if ((statSync(path).mode & 0o777) !== 0o600)
    throw new Error("Proof identity record is not private.");
  const record = JSON.parse(readFileSync(path, "utf8")) as Record<
    string,
    { email: string; password: string }
  >;
  const person = record[role];
  if (!person || !person.email.endsWith(".example.test") || !person.password) {
    throw new Error("Only the generated first-store proof identities may be used.");
  }
  await page.goto("/login");
  await page.request.get("/api/auth/csrf");
  const cookie = (await page.context().cookies()).find((item) => item.name === "kdps_csrf");
  if (!cookie) throw new Error("Proof CSRF cookie is missing.");
  const login = await page.request.post("/api/auth/login", {
    headers: { "X-CSRF-Token": cookie.value },
    data: { email: person.email, password: person.password },
  });
  expect(login.status(), "The generated proof identity must authenticate normally").toBe(200);
  await page.goto("/");
  await expect(page).not.toHaveURL(/\/login$|\/change-password$/);
  await expect(page.getByRole("link", { name: "Stock", exact: true })).toBeVisible();
}

export async function pairProof(page: Page) {
  const path = fileURLToPath(
    new URL("../../.local/first-store-browser-credentials.json", import.meta.url),
  );
  const record = JSON.parse(readFileSync(path, "utf8")) as {
    commercial_fixture?: { device_token?: string; synthetic_only?: boolean };
  };
  const fixture = record.commercial_fixture;
  if (!fixture?.synthetic_only || !fixture.device_token)
    throw new Error("The owned fictional counter fixture is absent.");
  await page.goto("/sell/till");
  const input = page.getByLabel("Pairing code", { exact: true });
  await expect(input).toBeVisible();
  try {
    await input.fill(fixture.device_token);
    await page.getByRole("button", { name: "Pair counter", exact: true }).click();
    await expect(page.getByRole("status")).toContainText("This browser is paired.");
  } catch {
    throw new Error("The fictional counter could not be paired normally.");
  } finally {
    await input.fill("").catch(() => undefined);
  }
}
