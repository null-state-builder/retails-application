import { execFileSync } from "node:child_process";
import { appendFileSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { expect, type Page, type Response } from "@playwright/test";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const CREDS = `${ROOT}/.local/first-store-damage-credentials.json`;
type Actor = "manager" | "owner";
interface Record {
  manager: { email: string; password: string };
  owner: { email: string; password: string };
  damage_rehearsal: { database: string; system_identifier: string };
  damage_fixture: { synthetic_only: true; site_id: string; barcode: string };
  commercial_fixture: { synthetic_only: boolean; device_token: string };
}
export interface Snapshot {
  sellable_qty: number;
  stock_cost_paise: string;
  origin_hash: string;
  money_hash: string;
  bills: number;
  rejected_intents: number;
  positions: {
    id: string;
    origin_id: string;
    accepted_event_id: string | null;
    lower: number;
    upper: number;
    condition: string;
    boundary: string;
    location_id: string | null;
    unit_cost_paise: string;
  }[];
  journals: { id: string; posting_kind: string }[];
  quantity_legs: unknown[];
  value_legs: unknown[];
  holds: unknown[];
  reservations: unknown[];
  damage_reports: {
    id: string;
    state: string;
    quantity: number;
    reporter_id: string;
    reviewer_id: string | null;
    release_movement_id: string | null;
  }[];
}
function privateJson<T>(path: string): T {
  if ((statSync(path).mode & 0o777) !== 0o600) throw new Error("Proof record must be private.");
  return JSON.parse(readFileSync(path, "utf8")) as T;
}
export function record(): Record {
  const proof = privateJson<{ database: string; system_identifier: string }>(
    `${ROOT}/.local/first-store-damage-proof.json`,
  );
  const people = privateJson<Record>(CREDS);
  if (
    !proof.database.startsWith("kdps_rehearsal_damage_") ||
    proof.database !== people.damage_rehearsal.database ||
    proof.system_identifier !== people.damage_rehearsal.system_identifier ||
    !people.damage_fixture?.synthetic_only
  )
    throw new Error("The explicit fictional damage sibling identity changed.");
  return people;
}
export function snapshot(label: string): Snapshot {
  const python = `${ROOT}/backend/.venv/bin/python`;
  try {
    const output = execFileSync(
      python,
      [
        "scripts/first-store-damage-proof.py",
        "run",
        "--",
        python,
        "scripts/first-store-damage-fixture.py",
        "snapshot",
      ],
      { cwd: ROOT, encoding: "utf8", timeout: 30_000 },
    );
    const value = JSON.parse(output.trim().split("\n").at(-1)!) as Snapshot;
    appendFileSync(
      `${ROOT}/.local/first-store-damage-evidence.jsonl`,
      `${JSON.stringify({ label, ...value })}\n`,
      { mode: 0o600 },
    );
    return value;
  } catch {
    throw new Error("Owned read-only damage snapshot failed; proof is preserved.");
  }
}
async function csrf(page: Page) {
  const cookie = (await page.context().cookies()).find((item) => item.name === "kdps_csrf");
  if (!cookie) throw new Error("Proof CSRF cookie is absent.");
  return { "X-CSRF-Token": cookie.value };
}
export async function post(page: Page, path: string, body: unknown) {
  return page.request.post(`/api${path}`, { headers: await csrf(page), data: body });
}
export async function login(page: Page, who: Actor) {
  const person = record()[who];
  if (!person.email.endsWith(".example.test")) throw new Error("Fictional people only.");
  await page.goto("/login");
  await page.request.get("/api/auth/csrf");
  const response = await post(page, "/auth/login", {
    email: person.email,
    password: person.password,
  });
  expect(response.status()).toBe(200);
  await page.goto("/");
  await expect(page).not.toHaveURL(/\/login$|\/change-password$/);
}
export function responseFor(page: Page, path: string): Promise<Response> {
  return page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/api${path}` && response.request().method() === "POST",
  );
}
export async function pair(page: Page) {
  const fixture = record().commercial_fixture;
  if (!fixture.synthetic_only) throw new Error("Fictional counter only.");
  await page.goto("/sell/till");
  const input = page.getByLabel("Pairing code", { exact: true });
  try {
    await input.fill(fixture.device_token);
    await page.getByRole("button", { name: "Pair counter", exact: true }).click();
    await expect(page.getByRole("status")).toContainText("This browser is paired.");
  } catch {
    throw new Error("Fictional damage counter pairing failed.");
  } finally {
    await input.fill("").catch(() => undefined);
  }
}
export async function stepUp(page: Page) {
  const input = page.getByTestId("org-stepup-password");
  await expect(input).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByTestId("org-stepup-cancel")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(input).toBeFocused();
  try {
    await input.fill(record().owner.password);
    await input.press("Enter");
  } catch {
    throw new Error("Independent fictional damage password confirmation failed.");
  }
  await expect(page.getByTestId("org-stepup")).toHaveCount(0);
}
export function unchanged(before: Snapshot, after: Snapshot) {
  for (const field of [
    "positions",
    "journals",
    "quantity_legs",
    "value_legs",
    "holds",
    "reservations",
    "origin_hash",
    "money_hash",
    "stock_cost_paise",
  ] as const)
    expect(after[field], field).toEqual(before[field]);
}
export async function widths(page: Page) {
  for (const width of [1440, 1366, 768, 375]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(page.getByRole("main")).toBeVisible();
    const size = await page.evaluate(() => ({
      width: document.documentElement.clientWidth,
      scroll: document.documentElement.scrollWidth,
    }));
    expect(size.scroll, `document overflow at ${width}`).toBeLessThanOrEqual(size.width + 1);
  }
}
