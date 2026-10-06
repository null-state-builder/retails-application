import { execFileSync } from "node:child_process";
import { appendFileSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { expect, type Page, type Response } from "@playwright/test";

export const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const CREDS = `${ROOT}/.local/first-store-receiving-credentials.json`;
export const BASE = "http://127.0.0.1:5182";
export type Actor = "manager" | "owner" | "admin" | "warehouse" | "count_checker";
export interface Snapshot {
  sellable_qty: number;
  stock_cost_paise: string;
  sale_hash: string;
  cash_hash: string;
  origins: { id: string; opening_qty: number; unit_cost: string; mrp: string }[];
  positions: {
    lot_id: string;
    origin_id: string | null;
    qty: number;
    condition: string;
    boundary: string;
    location_id: string;
    unit_cost_paise: string | null;
  }[];
  journals: { id: string; posting_kind: string }[];
  value_legs: unknown[];
  arrivals: number;
  counts: number;
  grns: number;
  damage_reports: {
    id: string;
    state: string;
    quantity: number;
    reporter_id: string;
    reviewer_id: string | null;
  }[];
  freeze_id: string | null;
}
export interface Fixture {
  synthetic_only: true;
  site_id: string;
  brand_id: string;
  vendor_id: string;
  sku_id: string;
  origin_id: string;
  floor_id: string;
  barcode: string;
}
export interface Plan {
  site_id: string;
  cutoff_at: string;
  identity_profile_id: string;
  profile_version_id: string;
  brand_mappings: Record<string, string>;
  season_mappings: Record<string, string>;
  size_mappings: Record<string, string>;
}
export interface Artifact {
  file: string;
  physical_file: string;
  evidence_file: string;
  quantity: number;
  cutoff_at: string;
}
type ReceivingProofRecord = { [K in Actor]: { email: string; password: string } } & {
  receiving_rehearsal: { database: string; system_identifier: string };
  receiving_fixture: Fixture;
  commercial_fixture: { device_token: string; synthetic_only: boolean };
};

function privateJson<T>(path: string): T {
  if ((statSync(path).mode & 0o777) !== 0o600) throw new Error("Proof record must be private.");
  return JSON.parse(readFileSync(path, "utf8")) as T;
}
export function record(): ReceivingProofRecord {
  const proof = privateJson<{ database: string; system_identifier: string }>(
    `${ROOT}/.local/first-store-receiving-proof.json`,
  );
  const people = privateJson<ReceivingProofRecord>(CREDS);
  if (
    !proof.database.startsWith("kdps_rehearsal_receiving_") ||
    proof.database !== people.receiving_rehearsal.database ||
    proof.system_identifier !== people.receiving_rehearsal.system_identifier
  )
    throw new Error("The receiving sibling identity changed.");
  return people;
}
export function fixture(): Fixture {
  const value = record().receiving_fixture;
  if (!value?.synthetic_only) throw new Error("Explicit fictional inputs are absent.");
  return value;
}
export function plan(): Plan {
  return JSON.parse(
    readFileSync(`${ROOT}/.local/first-store-opening-browser/mapping-plan.json`, "utf8"),
  ) as Plan;
}

/** Restricted helper entry points: identity/configuration inputs and read-only evidence. */
export function helper<T>(
  action: "prepare" | "snapshot" | "artifact" | "supplier-identity",
  ...args: string[]
): T {
  const python = `${ROOT}/backend/.venv/bin/python`;
  try {
    const output = execFileSync(
      python,
      [
        "scripts/first-store-receiving-proof.py",
        "run",
        "--",
        python,
        "scripts/first-store-receiving-fixture.py",
        action,
        ...args,
      ],
      { cwd: ROOT, encoding: "utf8", timeout: 120_000 },
    );
    appendFileSync(
      `${ROOT}/.local/first-store-receiving-helpers.log`,
      `${action} ${args.join(" ")}\n${output}\n`,
      { mode: 0o600 },
    );
    return JSON.parse(output.trim().split("\n").at(-1)!) as T;
  } catch (error) {
    const output = (error as { stdout?: string | Buffer }).stdout;
    if (output)
      appendFileSync(`${ROOT}/.local/first-store-receiving-helpers.log`, output, { mode: 0o600 });
    throw new Error("Owned receiving helper failed; its private log and database are retained.");
  }
}
export function snapshot(label: string): Snapshot {
  const value = helper<Snapshot>("snapshot");
  appendFileSync(
    `${ROOT}/.local/first-store-receiving-evidence.jsonl`,
    `${JSON.stringify({ label, ...value })}\n`,
    { mode: 0o600 },
  );
  return value;
}
export function decisionEvidence(
  label: string,
  status: number,
  refusal?: { code?: string; error?: string },
) {
  appendFileSync(
    `${ROOT}/.local/first-store-receiving-decisions.jsonl`,
    `${JSON.stringify({ label, database: record().receiving_rehearsal.database, status, ...refusal })}\n`,
    { mode: 0o600 },
  );
}
export async function login(page: Page, who: Actor) {
  const person = record()[who];
  if (!person?.email.endsWith(".example.test") || !person.password)
    throw new Error("Only generated fictional identities are permitted.");
  await page.goto("/login");
  await page.request.get("/api/auth/csrf");
  const response = await page.request.post("/api/auth/login", {
    headers: await csrf(page),
    data: { email: person.email, password: person.password },
  });
  expect(response.status()).toBe(200);
  await page.goto("/");
  await expect(page).not.toHaveURL(/\/login$|\/change-password$/);
}
export async function csrf(page: Page) {
  const cookie = (await page.context().cookies()).find((item) => item.name === "kdps_csrf");
  if (!cookie) throw new Error("Proof session CSRF cookie is absent.");
  return { "X-CSRF-Token": cookie.value };
}
export async function post(page: Page, path: string, body: unknown) {
  return page.request.post(`/api${path}`, { headers: await csrf(page), data: body });
}
export function responseFor(page: Page, path: string, status?: number): Promise<Response> {
  return page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === `/api${path}` &&
      r.request().method() === "POST" &&
      (status === undefined || r.status() === status),
  );
}
export async function stepUp(page: Page, who: Actor) {
  await expect(page.getByTestId("org-stepup-password")).toBeVisible();
  const dialog = page.getByRole("dialog", { name: "Confirm it's you", exact: true });
  for (const width of [1440, 1366, 768, 375]) {
    await page.setViewportSize({ width, height: 1000 });
    await noOverflow(page);
    await expect(dialog.getByLabel("Password", { exact: true })).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(page.getByTestId("org-stepup-cancel")).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(dialog.getByLabel("Password", { exact: true })).toBeFocused();
    const box = await dialog.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
  }
  try {
    await page.getByTestId("org-stepup-password").fill(record()[who].password);
    await page.getByTestId("org-stepup-password").press("Enter");
  } catch {
    throw new Error("The independent fictional reviewer could not confirm.");
  }
  await expect(page.getByTestId("org-stepup")).toHaveCount(0);
}
export async function pair(page: Page) {
  const counter = record().commercial_fixture;
  if (!counter.synthetic_only) throw new Error("The fictional counter is absent.");
  await page.goto("/sell/till");
  const input = page.getByLabel("Pairing code", { exact: true });
  try {
    await input.fill(counter.device_token);
    await page.getByRole("button", { name: "Pair counter", exact: true }).click();
    await expect(page.getByRole("status")).toContainText("This browser is paired.");
  } catch {
    throw new Error("The fictional receiving counter could not pair.");
  } finally {
    await input.fill("").catch(() => undefined);
  }
}
export async function sourceRead(page: Page, source: string) {
  const response = await page.request.get(`/api/goods-v1/ptmapper/soh-imports/${source}`);
  expect(response.status()).toBe(200);
  return (await response.json()) as {
    id: string;
    state: string;
    revision: number;
    content_hash: string;
    data: { stock_reconciliation_id: string | null; approval_request_id: string | null };
  };
}
export async function sourcePage(page: Page, name: string) {
  await page.goto("/goods/opening");
  await page.getByRole("button", { name: `${name}.xlsx`, exact: true }).click();
  await expect(page.getByTestId("soh-import")).toContainText("Source SHA-256");
}
export async function noOverflow(page: Page) {
  const size = await page.evaluate(() => ({
    width: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(size.scroll).toBeLessThanOrEqual(size.width + 1);
}
/** Read-only presentation checks on the current populated step, before any commit. */
export async function widthCheckpoint(page: Page, regions: string[] = []) {
  for (const width of [1440, 1366, 768, 375]) {
    await page.setViewportSize({ width, height: 1000 });
    await noOverflow(page);
    for (const name of regions) {
      const region = page.getByRole("region", { name, exact: true });
      await expect(region).toBeVisible();
      await expect(region).toHaveAttribute("tabindex", "0");
      await region.focus();
      await region.press("Tab");
      await page.keyboard.press("Shift+Tab");
      await expect(region).toBeFocused();
      const focus = await region.evaluate((element) => {
        const style = getComputedStyle(element);
        return { style: style.outlineStyle, width: Number.parseFloat(style.outlineWidth) };
      });
      expect(focus.style).not.toBe("none");
      expect(focus.width).toBeGreaterThan(0);
      const scroll = await region.evaluate((element) => {
        element.scrollLeft = 0;
        return { width: element.clientWidth, content: element.scrollWidth };
      });
      await region.press("ArrowRight");
      if (scroll.content > scroll.width + 1)
        await expect
          .poll(() => region.evaluate((element) => element.scrollLeft))
          .toBeGreaterThan(0);
    }
  }
}
export function unchangedStock(before: Snapshot, after: Snapshot) {
  expect(after.positions).toEqual(before.positions);
  expect(after.origins).toEqual(before.origins);
  expect(after.journals).toEqual(before.journals);
  expect(after.value_legs).toEqual(before.value_legs);
  expect(after.sale_hash).toBe(before.sale_hash);
  expect(after.cash_hash).toBe(before.cash_hash);
}
