// The store's day, by tender - and what the day left open (#188, D10 step 6).
//
// One screen, two halves, because they are one question. The top is what the
// counter took, which is the number a store person counts a drawer against. The
// bottom is the day's exceptions, which is why the drawer might not agree.
//
// **Read-only about money.** There is no "confirm the day" button and there
// deliberately cannot be one: agreeing a day - counting the float, locking the
// date - is store open/close (I3), its own designed flow sequenced after this.
// The single write on this screen is against an *exception*, and clearing one is
// a statement about somebody's attention, never a correction to a bill (A7).

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Check, EyeOff, RefreshCw } from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { Money } from "../lib/format";
import { PageHeader } from "../components/PageHeader";
import { OperationsPage } from "../components/OperationsPage";
import { KpiStrip } from "../components/KpiStrip";
import type { Kpi } from "../components/KpiStrip";
import { Note } from "../components/Note";
import { Section } from "../components/Section";
import { EmptyState, Loading } from "../components/States";
import { Status } from "../components/Status";
import { useAuth } from "../auth/AuthContext";
import { tillToday } from "../till/pricing";
import "./DaySummary.css";
import { StorePicker, useNeedsStore } from "../components/StoreGate";

/** `GET /api/store/cash-summary`. */
interface SummaryT {
  store: string;
  date: string;
  modes: { cash: number; card: number; upi: number; credit_note: number; bank_offer?: number };
  /** `modes.upi`, split by how the money was proven. The only control on manual
   *  UPI: no manager PIN, just visibility the same evening. Optional because the
   *  API and PWA can be deployed independently
   *  - a browser can hold the new bundle before the field ships. */
  upi_split?: { confirmed: number; manual: number };
  bills: number;
  returns: number;
  credit_notes_issued_paise: number;
  flags_open: number;
}

/** One row of `GET /api/sell/flags`. */
interface FlagT {
  id: number;
  kind: string;
  kind_label: string;
  status: "open" | "resolved" | "ignored";
  store_code: string;
  /** Empty on a flag about no particular bill - a hole, or a seller's return
   *  count. That is a fact rather than a gap, so there is no link to draw. */
  doc_number: string;
  billed_at: string | null;
  details: Record<string, unknown>;
  created_at: string;
  cleared_note: string;
  resolved_by_name: string;
  resolved_at: string | null;
}

/** The four tender columns, in the order a summary is read. Spelled out rather
 *  than mapped off the payload so a day with no card sales still draws a card
 *  tile: a column that vanishes when unused makes a person wonder where it went. */
const MODES = [
  { key: "cash", label: "Cash" },
  { key: "card", label: "Card" },
  { key: "upi", label: "UPI" },
  { key: "credit_note", label: "Credit note" },
] as const;

/** Paise as rupees for a sentence, "?" when absent. */
function paiseText(value: unknown): string {
  return typeof value === "number" ? `₹${(value / 100).toFixed(2)}` : "?";
}

/** The finding, said the way a person would say it.
 *
 *  The server sends `details` as the machine wrote them, because a later nightly
 *  pass rewrites them and a sentence frozen at first sight would go stale. This
 *  is where they become English - one line, because this is a list somebody
 *  scans, and the whole row is there for anybody who wants the rest. */
export function flagLine(flag: Pick<FlagT, "kind" | "details">): string {
  const d = flag.details ?? {};
  switch (flag.kind) {
    case "number_hole": {
      const missing = (d.missing as number[]) ?? [];
      const count = (d.count as number) ?? missing.length;
      const shown = missing.slice(0, 6).join(", ");
      return count === 1
        ? `Bill ${shown} never arrived.`
        : `${count} bills never arrived${shown ? ` (${shown}${count > 6 ? "…" : ""})` : ""}.`;
    }
    case "employee_returns": {
      const sellers = (d.sellers as { name: string; returns: number }[]) ?? [];
      return sellers.length
        ? sellers.map((s) => `${s.name} took back ${s.returns}`).join("; ") +
            ` (over ${d.threshold ?? "the limit"} in a day).`
        : "A seller took back more than the day's limit.";
    }
    case "gst_mismatch": {
      const lines = (d.lines as { line_no: number }[]) ?? [];
      if (!lines.length)
        return `Printed ${d.printed_split ?? "?"}, books say ${d.derived_split ?? "?"}.`;
      return `Tax on line ${lines.map((l) => l.line_no).join(", ")} is not what the tax settings say.`;
    }
    case "tax_version_mismatch":
      return `Taxed under tax settings version ${d.used ?? "?"}${
        d.known === false ? " (not known to head office)" : ""
      }, but version ${d.in_force ?? "?"} was in force.`;
    case "tax_rule_missing": {
      const lines = (d.lines as { line_no: number; hsn: string }[]) ?? [];
      return `No tax rule covers the HSN on line ${lines
        .map((l) => `${l.line_no}${l.hsn ? ` (${l.hsn})` : ""}`)
        .join(", ")}; the no-rule rate was charged.`;
    }
    case "offer_mismatch": {
      const lines = (d.lines as { line_no: number }[]) ?? [];
      return `Line ${lines.map((l) => l.line_no).join(", ")} was not charged what the rulebook says.`;
    }
    case "gstin_invalid":
      return `The buyer's GSTIN ${d.gstin ?? ""} is not well formed: ${d.reason ?? "check it"}.`;
    case "cn_unverified":
      return `A credit note the shop does not recognise was taken: ${(d.notes as string[])?.join(", ") ?? ""}.`;
    case "return_orig_missing":
      return "A piece came back against a bill we do not hold.";
    case "billed_while_paused":
      return `Billed while the counter was paused for a stock transfer (from bill ${d.pause_next_seq ?? "?"}${d.pause_fy ? ` of ${d.pause_fy}` : ""}).`;
    case "aged_uncosted":
      return `Sold before inward and still unpriced since ${d.waiting_since ?? "?"}.`;
    // Ticket 11: GST after discount.
    case "after_discount_mismatch":
      return d.bill
        ? "Priced with GST after discount, but the store's switch is off."
        : "Priced the old way, but GST after discount is on at this store.";
    case "till_server_mismatch": {
      const parts: string[] = [];
      const lines = (d.lines as { line_no: number }[]) ?? [];
      if (lines.length) parts.push(`line ${lines.map((l) => l.line_no).join(", ")}`);
      if (d.round_paise) parts.push("the round-off");
      if (d.bank_offers) parts.push("the bank offer");
      return `The till and head office worked out ${parts.join(", ") || "this bill"} differently.`;
    }
    // Ticket 13: exchange and return tax.
    case "late_credit_note":
      return `Taken back after the credit-note deadline (${d.deadline ?? "?"}) for bill ${
        d.original ?? "?"
      }: the value was given with no tax reduction.`;
    case "return_tax_mismatch":
      return d.bill
        ? "Taken back by the return tax rules, but the store's switch is off."
        : "Taken back the old way, but return tax rules are on at this store.";
    case "cn_deadline_mismatch":
      return "The till and head office judged the credit-note deadline differently.";
    case "bank_share_returned":
      return `A piece off bill ${d.original ?? "?"} came back: the bank offer's part (${paiseText(
        d.bank_offer_paise,
      )}) was reversed against the bank, not given to the customer.`;
    case "bank_share_mismatch":
      return "The till and head office worked out the bank offer's part of a returned piece differently.";
    case "credit_note_number":
      return `The credit note could not be numbered: ${d.message ?? d.reason ?? "check it"}.`;
    // Ticket 22: alterations.
    case "returned_on_job_card": {
      const lines = (d.lines as { line_no: number }[]) ?? [];
      return `Line ${lines.map((l) => l.line_no).join(", ")} came back while the garment is on an open alteration job card. Hand over or cancel the job card.`;
    }
    // Ticket 48: over a month's cap on returns without a bill. The bill stands.
    case "no_bill_return_cap": {
      const over =
        (d.over as {
          by: string;
          count: number;
          cap: number;
          phone_end?: string;
          name?: string;
        }[]) ?? [];
      const parts = over.map((o) =>
        o.by === "phone"
          ? `phone ending ${o.phone_end ?? "?"} has ${o.count} this month (cap ${o.cap})`
          : `${o.name ?? "the cashier"} has taken ${o.count} this month (cap ${o.cap})`,
      );
      return `Return without a bill over the cap: ${parts.join("; ") || "check it"}.`;
    }
    default:
      return "";
  }
}

function fmtDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (
    y === undefined ||
    m === undefined ||
    d === undefined ||
    !Number.isInteger(y) ||
    !Number.isInteger(m) ||
    !Number.isInteger(d)
  )
    return iso;
  return new Date(y, m - 1, d).toLocaleDateString("en-IN", {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** A bill's time as its day, "Thu 29 Oct", in India time. */
function fmtDay(iso: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return iso;
  return when.toLocaleDateString("en-IN", {
    timeZone: "Asia/Kolkata",
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

/** Saying something about one exception. Two answers and no way back to open -
 *  "dealt with" and "looked at, needs nothing" are different sentences, and the
 *  second takes a reason because nothing else evidences it. */
function ClearPanel({ flag, onDone }: { flag: FlagT; onDone: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function send(status: "resolved" | "ignored") {
    setError("");
    setBusy(true);
    try {
      await api.put(`/sell/flags/${flag.id}`, { status, note });
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flag-clear">
      <input
        className="input"
        value={note}
        disabled={busy}
        maxLength={240}
        placeholder="What did you do about it?"
        aria-label={`Note for ${flag.kind_label}`}
        onChange={(e) => setNote(e.target.value)}
        data-testid={`flag-note-${flag.id}`}
      />
      <button
        type="button"
        className="btn btn-primary btn-sm"
        disabled={busy}
        onClick={() => void send("resolved")}
        data-testid={`flag-resolve-${flag.id}`}
      >
        <Check size={14} aria-hidden="true" /> Dealt with
      </button>
      <button
        type="button"
        className="btn btn-sm"
        disabled={busy || !note.trim()}
        title={note.trim() ? "" : "Say why this one needs nothing doing"}
        onClick={() => void send("ignored")}
        data-testid={`flag-ignore-${flag.id}`}
      >
        <EyeOff size={14} aria-hidden="true" /> Needs nothing
      </button>
      {error && (
        <div className="flag-error">
          <Note tone="error" testId={`flag-error-${flag.id}`}>
            {error}
          </Note>
        </div>
      )}
    </div>
  );
}

const DAY_LEAD =
  "What the counter took, and what the day left open. Read-only - the day is agreed at store close.";

export default function DaySummary() {
  // Nothing to read until there is one store: no request, and no red error
  // beside a "Loading…" that never ends.
  const needsStore = useNeedsStore();
  if (needsStore)
    return (
      <OperationsPage>
        <PageHeader lead={DAY_LEAD} />
        <StorePicker purpose="the day's takings and open exceptions" />
      </OperationsPage>
    );
  return <DaySummaryScreen />;
}

function DaySummaryScreen() {
  const { activeStore } = useAuth();
  const [params, setParams] = useSearchParams();
  // The store's own day, not the browser's UTC one - the same helper the till
  // prices its offers by, because "today" flipping at 18:30 IST is the defect
  // that class of bug already produced once.
  const day = params.get("date") || tillToday();
  const [summary, setSummary] = useState<SummaryT>();
  const [open, setOpen] = useState<FlagT[]>([]);
  const [settled, setSettled] = useState<FlagT[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const requestVersion = useRef(0);

  // **Open exceptions are never narrowed to the picked day; settled ones are.**
  // They are the work, and this is the rule the IRN queue already holds to: a
  // store with three unanswered exceptions from last Tuesday still has three
  // today, and the Dashboard's own count says so with no date on it. Arriving
  // from that row and being told "nothing to look at" because the picker had
  // defaulted to this morning would be the card lying about itself. The picker
  // still governs the money above and the history behind.
  const load = useCallback(() => {
    const version = ++requestVersion.current;
    const headers = { "X-KDPS-Unit": activeStore?.id == null ? "" : String(activeStore.id) };
    setLoading(true);
    setError("");
    setSummary(undefined);
    setOpen([]);
    setSettled([]);
    Promise.all([
      api.get("/store/cash-summary", { params: { date: day }, headers }),
      api.get("/sell/flags", { params: { status: "open" }, headers }),
      api.get("/sell/flags", { params: { date: day, status: "all" }, headers }),
    ])
      .then(([s, live, ofDay]) => {
        if (version !== requestVersion.current) return;
        setSummary(s.data);
        setOpen(live.data.rows as FlagT[]);
        setSettled((ofDay.data.rows as FlagT[]).filter((row) => row.status !== "open"));
      })
      .catch((e) => {
        if (version === requestVersion.current) setError(apiErrorMessage(e));
      })
      .finally(() => {
        if (version === requestVersion.current) setLoading(false);
      });
  }, [day, activeStore?.id]);

  useEffect(load, [load]);

  return (
    <OperationsPage>
      <PageHeader
        lead={DAY_LEAD}
        actions={
          <>
            {summary && <span className="muted-cell">{summary.store}</span>}
            <label className="sr-only" htmlFor="day-summary-date">
              Day
            </label>
            <input
              id="day-summary-date"
              type="date"
              className="input day-picker"
              value={day}
              max={tillToday()}
              onChange={(e) => setParams(e.target.value ? { date: e.target.value } : {})}
              data-testid="day-picker"
            />
            <button className="btn" data-testid="day-retry" disabled={loading} onClick={load}>
              <RefreshCw size={15} aria-hidden="true" /> Read again
            </button>
          </>
        }
      />

      <div className="page">
        {error && (
          <Note tone="error" testId="day-error">
            {error}
          </Note>
        )}

        {loading || !summary ? (
          !error && <Loading label="Loading the day" />
        ) : (
          <>
            <DayMoney summary={summary} />
            <DayFlags open={open} settled={settled} onDone={load} />
          </>
        )}
      </div>
    </OperationsPage>
  );
}

/** What the counter took: the day's figures, then the money by tender. */
function DayMoney({ summary }: { summary: SummaryT }) {
  // Folded over `MODES` rather than adding the four by name: a fifth place the
  // tender list is spelled is a fifth place it can fall out of step.
  const taken =
    MODES.reduce((sum, m) => sum + summary.modes[m.key], 0) + (summary.modes.bank_offer ?? 0);
  const share = (paise: number) => (taken > 0 ? (paise / taken) * 100 : 0);
  // Ticket 11: only on a day that took one - a bank instant discount recorded
  // as a payment the bank owes, never cash in the drawer.
  const tenders: { key: string; label: string; paise: number; quiet?: boolean }[] = [
    ...MODES.map((m) => ({
      key: m.key,
      label: m.label,
      paise: summary.modes[m.key],
      quiet: m.key === "credit_note",
    })),
    ...((summary.modes.bank_offer ?? 0) > 0
      ? [
          {
            key: "bank_offer",
            label: "Bank offer",
            paise: summary.modes.bank_offer ?? 0,
            quiet: true,
          },
        ]
      : []),
  ];

  const kpis: Kpi[] = [
    { label: "Taken in all", value: <Money paise={taken} />, detail: fmtDate(summary.date) },
    { label: "Bills", value: summary.bills },
    { label: "Pieces back", value: summary.returns },
    {
      label: "Credit notes given",
      value: <Money paise={summary.credit_notes_issued_paise} />,
      detail: "Not money in the drawer",
    },
    {
      label: "Cash in the drawer for this day",
      value: <Money paise={summary.modes.cash} />,
    },
  ];

  return (
    <div className="page" data-testid="day-money">
      <div data-testid="day-counts">
        <KpiStrip items={kpis} label={`The day, ${fmtDate(summary.date)}`} />
      </div>

      <Section heading="Collected by tender">
        <div className="table-wrap day-tenders">
          <table className="data" data-testid="day-modes">
            <thead>
              <tr>
                <th scope="col">Tender</th>
                <th scope="col" className="num">
                  Amount
                </th>
                <th scope="col" className="num">
                  Share of the day
                </th>
                <th scope="col">
                  <span className="sr-only">Notes</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {tenders.map((t) => (
                <tr key={t.key} data-testid={`day-mode-${t.key}`}>
                  <td>
                    <b>{t.label}</b>
                  </td>
                  <td className="num">
                    <Money paise={t.paise} />
                  </td>
                  <td className="num">
                    <span className="day-share">
                      <span className="day-share-track" aria-hidden="true">
                        <span
                          className={`day-share-fill ${t.quiet ? "quiet" : ""}`.trim()}
                          style={{ width: `${Math.min(100, share(t.paise))}%` }}
                        />
                      </span>
                      <span className="day-share-n">{share(t.paise).toFixed(1)}%</span>
                    </span>
                  </td>
                  <td>
                    {t.key === "upi" && summary.upi_split && (
                      // The UPI row's only control: visibility, not a gate (#241).
                      <span className="day-upi-split" data-testid="day-upi-split">
                        <Status tone="green">
                          <Money paise={summary.upi_split.confirmed} /> confirmed
                        </Status>
                        <Status tone={summary.upi_split.manual > 0 ? "amber" : "grey"}>
                          <Money paise={summary.upi_split.manual} /> manual
                        </Status>
                      </span>
                    )}
                    {t.key === "credit_note" && <span className="sub">Taken as payment</span>}
                    {t.key === "bank_offer" && <span className="sub">Owed by the bank</span>}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td>Taken in all</td>
                <td className="num">
                  <Money paise={taken} />
                </td>
                <td className="num">{taken > 0 ? "100%" : "—"}</td>
                <td />
              </tr>
            </tfoot>
          </table>
        </div>
        {/* Credit notes are not money in the drawer - they close a liability
            the shop already owed - so the one figure a person counts cash
            against is said out loud rather than left to be worked out. */}
        <p className="lead" data-testid="day-drawer">
          Cash in the drawer for this day:{" "}
          <b className="tabular">
            <Money paise={summary.modes.cash} />
          </b>
          . Credit notes given, <Money paise={summary.credit_notes_issued_paise} /> on{" "}
          {summary.returns} {summary.returns === 1 ? "piece" : "pieces"} back, close what the shop
          already owed.
        </p>
      </Section>
    </div>
  );
}

/** The day's exceptions: every one still open, then what was answered on the
 *  picked day. */
function DayFlags({
  open,
  settled,
  onDone,
}: {
  open: FlagT[];
  settled: FlagT[];
  onDone: () => void;
}) {
  return (
    <Section
      ruled
      testId="day-flags"
      heading={
        open.length === 0
          ? "Exceptions, nothing to look at"
          : `Exceptions, ${open.length} to look at`
      }
      aside={
        // Said out loud, because it is the one block on this screen the day
        // picker does not govern - and a person who had just moved the picker
        // would otherwise read these as that day's.
        <span className="muted-cell day-flags-scope">
          Every exception still open at this store, whichever day it came from.
          {settled.length > 0 && " Below them, what was answered on the day picked above."}
        </span>
      }
    >
      {open.length === 0 && settled.length === 0 ? (
        <EmptyState testId="day-flags-empty">
          Nothing is waiting - every bill arrived, and the tax and offers agree with the rulebook.
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="data flag-table">
            <thead>
              <tr>
                <th scope="col">Bill</th>
                <th scope="col">Billed on</th>
                <th scope="col">What disagrees</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {[...open, ...settled].map((flag) => (
                <tr key={flag.id} data-status={flag.status} data-testid={`flag-${flag.id}`}>
                  <td>
                    {flag.doc_number ? (
                      <Link
                        className="mono"
                        to={`/sell/customers?doc=${encodeURIComponent(flag.doc_number)}`}
                        data-testid={`flag-bill-${flag.id}`}
                      >
                        {flag.doc_number}
                      </Link>
                    ) : (
                      <span className="muted-cell">—</span>
                    )}
                  </td>
                  <td className="day-flag-when">{flag.billed_at ? fmtDay(flag.billed_at) : "—"}</td>
                  <td className="flag-what">
                    <b>{flag.kind_label}</b>
                    <span className="sub">{flagLine(flag)}</span>
                    {flag.cleared_note && (
                      <span className="sub flag-note">“{flag.cleared_note}”</span>
                    )}
                    {flag.status === "open" && <ClearPanel flag={flag} onDone={onDone} />}
                  </td>
                  <td>
                    {flag.status === "open" ? (
                      <Status tone="red">Open</Status>
                    ) : (
                      <Status tone={flag.status === "resolved" ? "green" : "grey"}>
                        {flag.status === "resolved" ? "Dealt with" : "Needs nothing"}
                        {flag.resolved_by_name ? ` · ${flag.resolved_by_name}` : ""}
                      </Status>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}
