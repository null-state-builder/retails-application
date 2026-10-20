import { afterEach, describe, expect, it, vi } from "vitest";

import { TEST_MARK, setTestEnvironment } from "../lib/testEnvironment";
import { browserPrintAdapter } from "./print";
import { markTestDocument, receiptHtml } from "./receipt";
import type { QueuedBill } from "./types";

const bill = {
  idempotency_uuid: "u-1",
  store: "S01",
  fy: "26-27",
  till_seq: 1,
  attempts: 0,
  origin: "online",
  doc_number: "S01-1",
  billed_at: "2026-10-20T10:00:00+05:30",
  customer: { name: "", mobile: "" },
  lines: [
    {
      line_no: 1,
      direction: "sale",
      barcode: "8900000000017",
      season: "",
      qty: 1,
      mrp_paise: 99900,
      disc_paise: 0,
      net_paise: 99900,
      gst_rate: "5",
      gst_paise: 4757,
    },
  ],
  tenders: [{ mode: "cash", amount_paise: 99900 }],
  totals: {
    gross_paise: 99900,
    discount_paise: 0,
    net_paise: 99900,
    gst_paise: 4757,
    round_paise: 0,
  },
} as unknown as QueuedBill;

const store = { code: "S01", gstin: "29ABCDE1234F1Z5", state_code: "29" };

function marks(html: string): number {
  return html.split("data-test-mark").length - 1;
}

describe("test-mode receipt banner", () => {
  it("puts the banner at the very top and the very bottom of the receipt", () => {
    const marked = markTestDocument(receiptHtml(bill, store));
    expect(marks(marked)).toBe(2);
    const body = marked.slice(marked.indexOf("<body>") + "<body>".length).trim();
    expect(body.startsWith('<div class="test-mark"')).toBe(true);
    const beforeClose = marked.slice(0, marked.lastIndexOf("</body>")).trimEnd();
    expect(beforeClose.endsWith(`${TEST_MARK}</div>`)).toBe(true);
    expect(marked).toContain("TEST – NOT A TAX INVOICE");
  });

  it("leaves a real store's receipt unmarked", () => {
    expect(marks(receiptHtml(bill, store))).toBe(0);
  });

  it("marks a document with no body tags too", () => {
    expect(marks(markTestDocument("<p>slip</p>"))).toBe(2);
  });
});

describe("print adapter", () => {
  afterEach(() => {
    setTestEnvironment(false);
    vi.unstubAllGlobals();
  });

  async function printed(): Promise<string> {
    let srcdoc = "";
    const frame = {
      style: {},
      setAttribute() {},
      addEventListener(_: string, done: () => void) {
        queueMicrotask(done);
      },
      remove() {},
      contentWindow: { focus() {}, print() {} },
      set srcdoc(value: string) {
        srcdoc = value;
      },
    };
    vi.stubGlobal("window", { print() {}, setTimeout: () => 0 });
    vi.stubGlobal("document", {
      createElement: () => frame,
      body: { appendChild() {} },
    });
    const outcome = await browserPrintAdapter.print(receiptHtml(bill, store));
    expect(outcome.ok).toBe(true);
    return srcdoc;
  }

  it("marks every printed document on a test tenant", async () => {
    setTestEnvironment(true);
    expect(marks(await printed())).toBe(2);
  });

  it("prints a real tenant's document unchanged", async () => {
    setTestEnvironment(false);
    expect(marks(await printed())).toBe(0);
  });
});
