import type { ReactNode } from "react";

/** Render integer paise as INR with Indian Lakh/Crore grouping. Never floats on
 *  any write path — this is display only (the value is computed once server-side). */
export function formatINR(paise: number, opts: { short?: boolean } = {}): string {
  // The minus goes outside the symbol: "-₹1", never "₹-1". `formatRupeeAmount`
  // below has always written it that way, and a split-tender bill can now be
  // overpaid, which is the first screen where a negative amount is an everyday
  // sight rather than a corner of a report.
  const sign = paise < 0 ? "-" : "";
  const rupees = Math.abs(paise) / 100;
  if (opts.short) {
    if (rupees >= 1e7) return `${sign}₹${(rupees / 1e7).toFixed(2)} Cr`;
    if (rupees >= 1e5) return `${sign}₹${(rupees / 1e5).toFixed(2)} L`;
  }
  // Whole rupees show no paise at all; anything with paise shows both digits.
  // Without the minimum, ₹1,222.50 renders as "₹1,222.5", which is not money.
  const fraction = rupees % 1 === 0 ? 0 : 2;
  const grouped = new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: fraction,
    maximumFractionDigits: fraction,
  }).format(rupees);
  return `${sign}₹${grouped}`;
}

export function Money({ paise, short }: { paise: number; short?: boolean }) {
  return <span className="tabular">{formatINR(paise, short === undefined ? {} : { short })}</span>;
}

/** Render a rupee decimal string the server already computed ("72450.00") as
 *  INR. A few read endpoints project money that way rather than as paise, and
 *  the screens reading them were printing "72450.00" at the counter.
 *
 *  Read as text, not arithmetic: `72450.29 * 100` is 7245028.999… in float, and
 *  the integer-paise rule holds on the client too. Anything unparseable comes
 *  back untouched rather than silently rendering as ₹0. */
export function formatRupeeAmount(amount: string): string {
  const parsed = /^\s*(-?)(\d+)(?:\.(\d*))?\s*$/.exec(amount);
  if (!parsed) return amount;
  const [, sign, whole, fraction = ""] = parsed;
  const paise = Number(whole) * 100 + Number((fraction + "00").slice(0, 2));
  return `${sign}${formatINR(paise)}`;
}

/** A rupee amount as typed, in exact integer paise - or `null` when what was
 *  typed is not an amount.
 *
 *  Digit arithmetic on purpose: `25.51 * 100` is `2551.0000000000005` in binary
 *  floating point, and the server's money column refuses a non-integer rather
 *  than rounding it (ADR-0004), so the rounding must not happen here either. The
 *  mirror of `core.money.rupees_to_paise`, held to the same rule - only what a
 *  person would write as money, never a guess at what they meant. */
export function rupeesToPaise(text: string): number | null {
  // Commas are how an amount is *read back* (₹25,00,000), so someone editing one
  // is allowed to type them straight back in; spaces likewise.
  const typed = text.replace(/[,\s]/g, "");
  if (!/^\d+(\.\d{1,2})?$/.test(typed)) return null;
  const [whole, fraction = ""] = typed.split(".");
  return Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
}

/** Integer paise as a plain rupee amount for an *input box* - no grouping, no ₹,
 *  and the paise shown only when there are any (`250000000` -> `"2500000"`).
 *
 *  `formatINR` is for reading; this is for editing, and the difference matters
 *  because whatever it returns is what `rupeesToPaise` gets handed back on save.
 *  So it is the exact inverse of that function, by the same digit arithmetic: the
 *  obvious `String(paise / 100)` is a float divide sitting on a write path, which
 *  is what ADR-0004 exists to keep off one. */
export function paiseToRupees(paise: number): string {
  const whole = Math.trunc(paise);
  const sign = whole < 0 ? "-" : "";
  const abs = Math.abs(whole);
  const sub = abs % 100;
  // `abs - sub` divides by 100 exactly, so this quotient is never a float
  // approximation of an integer the way `abs / 100` can be.
  const rupees = (abs - sub) / 100;
  return sub === 0 ? `${sign}${rupees}` : `${sign}${rupees}.${String(sub).padStart(2, "0")}`;
}

/** A moment, written the way an Indian store reads one: `1 Aug, 5:30 pm`.
 *
 *  To the minute, deliberately. Every use of this so far is a time somebody was
 *  *told* — an expected arrival quoted to a waiting customer (#175) — and the
 *  day alone does not answer the question they ring back to ask.
 *
 *  It also does a second job wherever a `datetime-local` input feeds it: that
 *  control renders in the *browser's* locale, so a person typing `08/02` meaning
 *  the eighth of February hands a US-formatted picker the second of August and
 *  gets no hint of it. Echoing the chosen instant back in this format turns a
 *  silent wrong date into a visible one, before it is committed to anything. */
export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** The single SKU-grain primitive: Brand · Style · Colour · Size — never style-only. */
export function SkuLine({
  brand,
  style,
  color,
  size,
}: {
  brand: string;
  style: string;
  color: string;
  size: string;
}) {
  return (
    <span className="sku-line">
      <b>{brand}</b>
      <span className="sku-sep">·</span>
      {style}
      <span className="sku-chip">{color}</span>
      <span className="sku-chip">{size}</span>
    </span>
  );
}

const STATUS_TONE: Record<string, string> = {
  open: "green",
  eoss: "amber",
  closed: "navy",
  ok: "green",
  matched: "green",
  pending: "amber",
  blocked: "red",
  overdue: "red",
  ai: "purple",
};

export function StatusChip({ status, tone }: { status: string; tone?: string }) {
  const t = tone ?? STATUS_TONE[status.toLowerCase()] ?? "navy";
  return <span className={`chip chip-${t}`}>{status}</span>;
}

export function CommercialBadge({ label }: { label: string }) {
  const tone =
    label === "Outright"
      ? "navy"
      : label === "Correction"
        ? "blue"
        : label === "SOR"
          ? "amber"
          : "purple";
  return <span className={`chip chip-${tone}`}>{label}</span>;
}

export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 12.5, color: "var(--muted)", fontWeight: 600 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 700, marginTop: 2 }} className="tabular">
        {value}
      </div>
    </div>
  );
}

/** Money the goods-v1 wire sends as a base-10 **integer-paise string**
 *  (design §14.4), rendered for reading. `null`/`""` means *unknown*, which is
 *  never the same thing as zero and must never be shown as ₹0 — that rule is
 *  the whole reason this is a separate function from `formatRupeeAmount`,
 *  which reads a rupee decimal instead.
 *
 *  Read as digits, never as arithmetic: a paise count can exceed what a JS
 *  number holds exactly (aggregates are NUMERIC(30,0) on the server), so the
 *  rupee/paise split is a string split. */
export function formatPaiseString(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "Unknown";
  const parsed = /^\s*(-?)(\d+)\s*$/.exec(value);
  if (!parsed?.[2]) return value;
  const [, sign, digits] = parsed;
  const padded = digits.padStart(3, "0");
  const whole = padded.slice(0, -2);
  const sub = padded.slice(-2);
  const grouped = new Intl.NumberFormat("en-IN").format(BigInt(whole));
  return `${sign}₹${grouped}${sub === "00" ? "" : `.${sub}`}`;
}

/** The inverse of `formatPaiseString` for an *input box*: a base-10
 *  integer-paise string as a plain rupee amount, no grouping and no ₹, with the
 *  paise shown only when there are any. `null`/`""` gives an empty box, which is
 *  how *unknown* is typed.
 *
 *  Digits, never `Number`: a paise count can exceed what a JS number holds
 *  exactly, and this sits on the path back to a write. */
export function paiseStringToRupees(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "";
  const parsed = /^\s*(-?)(\d+)\s*$/.exec(value);
  if (!parsed?.[2]) return value;
  const [, sign, digits] = parsed;
  const padded = digits.padStart(3, "0");
  const whole = padded.slice(0, -2).replace(/^0+(?=\d)/, "");
  const sub = padded.slice(-2);
  return sub === "00" ? `${sign}${whole}` : `${sign}${whole}.${sub}`;
}
