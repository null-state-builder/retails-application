// Nightly legacy sync (store test run, test tenant only). Two ways to bring the
// store in line with the legacy software each night:
//   * SOH upload - the legacy stock-on-hand becomes this store's stock;
//   * Sales report - only what the shop really sold leaves stock.
// Either way, the day's test bills go back on the shelf first. Customer data
// in the sales report is never stored.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { OperationsPage, OperationsTable } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { FilterChip } from "../components/FilterChip";
import { Note } from "../components/Note";
import { Section } from "../components/Section";
import { api, apiErrorCode, apiErrorMessage, goodsMeta } from "../lib/api";
import { sha256Hex } from "../lib/goodsPt";
import { Feedback, Field, hold } from "../lib/goodsScreen";
import { useCachedGet } from "../lib/cachedQuery";

interface Row {
  id?: string;
  category?: Category | null;
  mrp_differs?: boolean;
  soh_mrp_paise?: number | null;
  book_mrp_paise?: number | null;
  review_state?: "open" | "acknowledged";
  barcode: string;
  outcome: string;
  applied_qty: number;
  exception_code: string;
  exception_note: string;
  soh_qty?: number;
  book_qty?: number;
  quantity?: number;
  bill_no?: string;
  business_date?: string;
}

interface SohSync {
  kind: "soh";
  id: string;
  state: string;
  source_name: string;
  cutoff_at: string;
  outcome: Record<string, number | Record<string, number>>;
  changed_rows: number;
  exception_count: number;
  new_item_rows: number;
  top_up_import_id: string | null;
  allowed_actions: string[];
  preview?: SohPreview;
  review?: ReviewCounts;
  rows?: Row[];
}

// A row's flag after an SOH sync. Stock for PT items is already brought to the
// SOH; acknowledging a row only marks it reviewed.
type Category = "SOH_ONLY" | "PT_ONLY" | "SOH_ABOVE" | "SOH_BELOW" | "MRP_DIFFERS";

const CATEGORY_LABEL: Record<Category, string> = {
  SOH_ONLY: "Not in any PT — not sellable until its PT is uploaded",
  PT_ONLY: "Not in the SOH — taken to 0",
  SOH_ABOVE: "SOH higher — added",
  SOH_BELOW: "SOH lower — taken out",
  MRP_DIFFERS: "MRP differs",
};
const CATEGORIES = Object.keys(CATEGORY_LABEL) as Category[];
const PAGE_SIZE = 100;

interface SohPreview {
  test_bills_to_reverse: number;
  book_qty: number;
  soh_qty: number;
  remove_qty: number;
  add_qty: number;
  soh_only_qty: number;
  flagged_rows: number;
  category_counts: Record<Category, number>;
}

interface ReviewCounts {
  categories: Record<Category, number>;
  total: number;
  open: number;
  acknowledged: number;
}

interface ReviewPage {
  counts: ReviewCounts;
  total: number;
  offset: number;
  limit: number;
  rows: Row[];
}

function rupees(paise: number | null | undefined): string {
  return paise == null ? "–" : `₹${(paise / 100).toLocaleString()}`;
}

interface SalesImport {
  kind: "sales";
  id: string;
  state: string;
  source_name: string;
  date_from: string;
  date_to: string;
  metadata: { bill_count: number; sold_quantity: number; returned_quantity: number };
  outcome: Record<string, number>;
  exception_count: number;
  allowed_actions: string[];
  preview?: { test_bills_to_reverse: number };
  rows?: Row[];
}

type Selected = SohSync | SalesImport;

interface Overview {
  test_environment: boolean;
  last_soh_cutoff: string | null;
  last_sales_date: string | null;
  soh_syncs: Omit<SohSync, "kind">[];
  sales_imports: Omit<SalesImport, "kind">[];
}

const BASE = "/goods-v1/ptmapper";

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "never";
}

function localNow(): string {
  const now = new Date();
  now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
  return now.toISOString().slice(0, 16);
}

export function StockSyncPage() {
  const { session } = useAuth();
  const sites = (session?.sites ?? []).filter(
    (s) => s.type !== "warehouse" && s.stock_contract === "goods_v1",
  );
  const allowed = hold(session, "opening.import.stage");
  const [siteId, setSiteId] = useState("");
  const [mode, setMode] = useState<"soh" | "sales">("soh");
  const [selected, setSelected] = useState<Selected | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [cutoff, setCutoff] = useState(localNow());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [needsOpening, setNeedsOpening] = useState(false);
  const [ok, setOk] = useState("");
  const [category, setCategory] = useState<Category | "">("");
  const [onlyOpen, setOnlyOpen] = useState(true);
  const [offset, setOffset] = useState(0);
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const reviewId = selected?.kind === "soh" && selected.state === "applied" ? selected.id : null;

  // The flagged rows of an applied SOH sync, a page at a time, through the
  // shared cache.
  const reviewRead = useCachedGet<ReviewPage>(
    reviewId ? `${BASE}/stock-syncs/${reviewId}/rows` : null,
    {
      params: {
        offset,
        limit: PAGE_SIZE,
        category,
        review_state: onlyOpen ? "open" : undefined,
      },
      keepPrevious: false,
    },
  );
  const review = reviewRead.data ?? null;

  useEffect(() => {
    setPicked(new Set());
  }, [reviewId, category, onlyOpen, offset]);

  async function acknowledge(ids: string[]) {
    if (!reviewId || ids.length === 0) return;
    const result = await api.post<{ acknowledged: number; open_rows: number }>(
      `${BASE}/stock-syncs/${reviewId}/rows/acknowledge`,
      { ...goodsMeta(), row_ids: ids },
    );
    setOk(
      `Marked ${result.data.acknowledged} reviewed. ${result.data.open_rows} still to review. Stock is not changed.`,
    );
    setPicked(new Set());
    reviewRead.reload();
  }

  function togglePick(id: string) {
    setPicked((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  useEffect(() => {
    if (!siteId && sites[0]) setSiteId(sites[0].id);
  }, [sites, siteId]);

  // The site's sync overview, through the shared cache. An action's POST
  // already refreshes every goods-v1 read; `refresh` asks again regardless.
  const overviewRead = useCachedGet<Overview>(siteId ? `${BASE}/legacy-sync` : null, {
    params: { site_id: siteId },
    kind: "doc",
    keepPrevious: false,
  });
  const overview = overviewRead.data ?? null;
  const refresh = overviewRead.reload;

  useEffect(() => {
    setSelected(null);
  }, [siteId]);

  const readFailure = overviewRead.error ?? reviewRead.error;
  const shownError = error || (readFailure ? apiErrorMessage(readFailure) : "");

  async function run(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNeedsOpening(false);
    setOk("");
    try {
      await work();
    } catch (e) {
      setError(apiErrorMessage(e));
      setNeedsOpening(apiErrorCode(e) === "OPENING_REQUIRED");
    } finally {
      setBusy(false);
    }
  }

  async function open(kind: "soh" | "sales", id: string) {
    const path = kind === "soh" ? `stock-syncs/${id}` : `legacy-sales/${id}`;
    const result = await api.get(`${BASE}/${path}`);
    setOffset(0);
    setSelected({ ...result.data, kind } as Selected);
  }

  async function upload() {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    form.append("site_id", siteId);
    form.append("command_id", crypto.randomUUID());
    form.append("contract_version", "goods-v1");
    let path = `${BASE}/legacy-sales/upload`;
    if (mode === "soh") {
      form.append("cutoff_at", new Date(cutoff).toISOString());
      form.append("expected_sha256", await sha256Hex(file));
      path = `${BASE}/stock-syncs/upload`;
    }
    const result = await api.post(path, form);
    await open(mode, result.data.id);
    refresh();
  }

  async function act(operation: "apply" | "discard") {
    if (!selected) return;
    const path = selected.kind === "soh" ? "stock-syncs" : "legacy-sales";
    const result = await api.post(`${BASE}/${path}/${selected.id}/${operation}`, goodsMeta());
    setSelected({ ...result.data, kind: selected.kind } as Selected);
    if (operation === "apply" && selected.kind === "soh") {
      const flagged = result.data.review?.total ?? 0;
      setOk(`Applied. Stock now matches the SOH; ${flagged} rows are flagged for review below.`);
    } else if (operation === "apply") {
      setOk(`Applied. ${result.data.exception_count} rows need attention.`);
    }
    refresh();
  }

  if (!allowed) {
    return (
      <OperationsPage>
        <PageHeader title="Legacy sync" />
        <p className="lead">You do not have access to the nightly legacy sync.</p>
      </OperationsPage>
    );
  }

  return (
    <OperationsPage>
      <PageHeader
        title="Legacy sync"
        lead="Each night, bring this store in line with the legacy software. Today's test bills go back on the shelf first."
      />
      <Feedback error={shownError} ok={ok} />
      {needsOpening && (
        <p className="lead">
          <Link to="/goods/opening">Go to Opening stock</Link> to upload this store&apos;s PT files.
        </p>
      )}
      {overview && !overview.test_environment && (
        <Note tone="warn" role="alert">
          This store is live. Legacy sync runs only on the test environment.
        </Note>
      )}
      <div className="form-grid">
        <Field id="sync-site" label="Store">
          <select
            id="sync-site"
            className="input"
            value={siteId}
            onChange={(e) => setSiteId(e.target.value)}
          >
            {sites.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </Field>
        <Field id="sync-mode" label="What are you uploading?">
          <select
            id="sync-mode"
            className="input"
            value={mode}
            onChange={(e) => setMode(e.target.value as "soh" | "sales")}
          >
            <option value="soh">SOH – replace the store's stock</option>
            <option value="sales">Sales report – take out what was sold</option>
          </select>
        </Field>
        {mode === "soh" && (
          <Field id="sync-cutoff" label="When was the SOH exported?">
            <input
              id="sync-cutoff"
              className="input"
              type="datetime-local"
              value={cutoff}
              onChange={(e) => setCutoff(e.target.value)}
            />
          </Field>
        )}
        <Field id="sync-file" label={mode === "soh" ? "SOH (.xlsx)" : "Sale report (.xlsx)"}>
          <input
            id="sync-file"
            type="file"
            accept=".xlsx"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </Field>
        <button
          className="btn btn-primary"
          disabled={busy || !file || !siteId}
          onClick={() => run(upload)}
        >
          Upload and preview
        </button>
      </div>
      {overview && (
        <p className="lead" data-testid="sync-last">
          Last SOH: <strong>{when(overview.last_soh_cutoff)}</strong> · Last sales report:{" "}
          <strong>{overview.last_sales_date ?? "never"}</strong>
        </p>
      )}

      {selected && (
        <Section
          ruled
          testId="sync-detail"
          heading={`${
            selected.kind === "soh"
              ? `SOH exported ${when(selected.cutoff_at)}`
              : `Sales ${selected.date_from} – ${selected.date_to}`
          } · ${selected.state}`}
        >
          <p className="muted">{selected.source_name}</p>
          <ul>
            {selected.preview && (
              <li>
                {selected.preview.test_bills_to_reverse} test bills will be put back on the shelf
              </li>
            )}
            {selected.kind === "soh" && selected.preview && (
              <>
                <li>
                  This store holds {selected.preview.book_qty} pieces; the SOH says{" "}
                  {selected.preview.soh_qty}
                </li>
                <li>{selected.preview.remove_qty} to take out</li>
                <li>{selected.preview.add_qty} to add for items already here</li>
                <li>
                  {selected.preview.soh_only_qty} pieces are in no PT: they will be flagged, not
                  added, and are not sellable until their PT is uploaded
                </li>
                <li>
                  {selected.preview.flagged_rows} rows will be flagged for review:{" "}
                  {CATEGORIES.map(
                    (c) => `${selected.preview?.category_counts[c] ?? 0} ${CATEGORY_LABEL[c]}`,
                  ).join(" · ")}
                </li>
              </>
            )}
            {selected.kind === "sales" && (
              <li>
                {selected.metadata.bill_count} legacy bills, {selected.metadata.sold_quantity} sold,{" "}
                {selected.metadata.returned_quantity} returned
              </li>
            )}
            {selected.state === "applied" &&
              Object.entries(selected.outcome)
                .flatMap(([key, value]) =>
                  typeof value === "number" ? [[key, value] as const] : [],
                )
                .map(([key, value]) => (
                  <li key={key}>
                    {key.replaceAll("_", " ")}: {value}
                  </li>
                ))}
            <li>
              <strong>{selected.exception_count}</strong> rows need attention
            </li>
            {selected.kind === "soh" && selected.top_up_import_id && (
              <li>
                New items are waiting in <a href="/goods/opening">Opening stock</a> for review.
              </li>
            )}
          </ul>
          {selected.allowed_actions.includes("apply") && (
            <div className="btn-row">
              <button
                className="btn btn-cta"
                disabled={busy}
                onClick={() => run(() => act("apply"))}
              >
                Apply to stock
              </button>
              <button className="btn" disabled={busy} onClick={() => run(() => act("discard"))}>
                Discard
              </button>
            </div>
          )}
          {reviewId && review && (
            <OperationsTable label="Flagged rows">
              <p className="lead" data-testid="sync-review-counts">
                Stock for PT items now matches the SOH. {review.counts.open} of{" "}
                {review.counts.total} flagged rows still to review. Acknowledging a row only marks
                it seen; nobody adds or edits items or stock here.
              </p>
              <div className="btn-row" role="group" aria-label="Filter by category">
                <button
                  className={category === "" ? "btn btn-primary" : "btn"}
                  onClick={() => {
                    setOffset(0);
                    setCategory("");
                  }}
                >
                  All ({review.counts.total})
                </button>
                {CATEGORIES.map((c) => (
                  <button
                    key={c}
                    className={category === c ? "btn btn-primary" : "btn"}
                    onClick={() => {
                      setOffset(0);
                      setCategory(c);
                    }}
                  >
                    {CATEGORY_LABEL[c]} ({review.counts.categories[c] ?? 0})
                  </button>
                ))}
              </div>
              <FilterChip
                pressed={onlyOpen}
                onClick={() => {
                  setOffset(0);
                  setOnlyOpen(!onlyOpen);
                }}
              >
                Only rows still to review
              </FilterChip>
              <div className="btn-row">
                <button
                  className="btn"
                  disabled={busy || picked.size === 0}
                  onClick={() => run(() => acknowledge([...picked]))}
                >
                  Acknowledge selected ({picked.size})
                </button>
                <button
                  className="btn"
                  disabled={busy || !review.rows.some((r) => r.review_state === "open")}
                  onClick={() =>
                    run(() =>
                      acknowledge(
                        review.rows.filter((r) => r.review_state === "open").map((r) => r.id ?? ""),
                      ),
                    )
                  }
                >
                  Acknowledge this page
                </button>
              </div>
              <table className="table">
                <thead>
                  <tr>
                    <th aria-label="Select" />
                    <th>Barcode</th>
                    <th>Here before</th>
                    <th>SOH</th>
                    <th>Applied</th>
                    <th>Flag</th>
                    <th>MRP (SOH / here)</th>
                    <th>Review</th>
                  </tr>
                </thead>
                <tbody>
                  {review.rows.map((row) => (
                    <tr key={row.id}>
                      <td>
                        {row.review_state === "open" && row.id && (
                          <input
                            type="checkbox"
                            aria-label={`Select ${row.barcode}`}
                            checked={picked.has(row.id)}
                            onChange={() => row.id && togglePick(row.id)}
                          />
                        )}
                      </td>
                      <td>{row.barcode}</td>
                      <td>{row.book_qty}</td>
                      <td>{row.soh_qty}</td>
                      <td>{row.applied_qty}</td>
                      <td>
                        {row.category ? CATEGORY_LABEL[row.category] : "–"}
                        {row.mrp_differs && row.category !== "MRP_DIFFERS" && (
                          <div>MRP differs</div>
                        )}
                        {row.outcome === "exception" && <div>⚠ {row.exception_code}</div>}
                        {row.exception_note && row.category !== "SOH_ONLY" && (
                          <div className="muted">{row.exception_note}</div>
                        )}
                      </td>
                      <td>
                        {row.mrp_differs || row.soh_mrp_paise != null
                          ? `${rupees(row.soh_mrp_paise)} / ${rupees(row.book_mrp_paise)}`
                          : "–"}
                      </td>
                      <td>{row.review_state === "acknowledged" ? "Reviewed" : "To review"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="btn-row">
                <button
                  className="btn"
                  disabled={busy || offset === 0}
                  onClick={() => setOffset(Math.max(offset - PAGE_SIZE, 0))}
                >
                  Previous
                </button>
                <span className="muted">
                  {review.total === 0
                    ? "No rows"
                    : `${offset + 1}–${Math.min(offset + PAGE_SIZE, review.total)} of ${review.total}`}
                </span>
                <button
                  className="btn"
                  disabled={busy || offset + PAGE_SIZE >= review.total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Next
                </button>
              </div>
            </OperationsTable>
          )}
          {!reviewId && selected.rows && selected.rows.length > 0 && (
            <OperationsTable label="Changed rows">
              <table className="table">
                <thead>
                  <tr>
                    <th>Barcode</th>
                    {selected.kind === "soh" ? (
                      <>
                        <th>Here</th>
                        <th>SOH</th>
                      </>
                    ) : (
                      <>
                        <th>Bill</th>
                        <th>Qty</th>
                      </>
                    )}
                    <th>Applied</th>
                    <th>Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {selected.rows.map((row, index) => (
                    <tr key={`${row.barcode}-${index}`}>
                      <td>{row.barcode}</td>
                      {selected.kind === "soh" ? (
                        <>
                          <td>{row.book_qty}</td>
                          <td>{row.soh_qty}</td>
                        </>
                      ) : (
                        <>
                          <td>{row.bill_no}</td>
                          <td>{row.quantity}</td>
                        </>
                      )}
                      <td>{row.applied_qty}</td>
                      <td>
                        {row.outcome === "exception"
                          ? `⚠ ${row.exception_code}`
                          : row.outcome.replace("_", " ")}
                        {row.exception_note && <div className="muted">{row.exception_note}</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </OperationsTable>
          )}
        </Section>
      )}

      {overview && (
        <OperationsTable label="Sync history">
          <table className="table">
            <thead>
              <tr>
                <th>Type</th>
                <th>Covers</th>
                <th>File</th>
                <th>State</th>
                <th>Exceptions</th>
              </tr>
            </thead>
            <tbody>
              {overview.soh_syncs.map((item) => (
                <tr key={item.id}>
                  <td>SOH</td>
                  <td>
                    <button
                      className="btn btn-link"
                      onClick={() => run(() => open("soh", item.id))}
                    >
                      {when(item.cutoff_at)}
                    </button>
                  </td>
                  <td>{item.source_name}</td>
                  <td>{item.state}</td>
                  <td>{item.exception_count}</td>
                </tr>
              ))}
              {overview.sales_imports.map((item) => (
                <tr key={item.id}>
                  <td>Sales</td>
                  <td>
                    <button
                      className="btn btn-link"
                      onClick={() => run(() => open("sales", item.id))}
                    >
                      {item.date_from === item.date_to
                        ? item.date_from
                        : `${item.date_from} – ${item.date_to}`}
                    </button>
                  </td>
                  <td>{item.source_name}</td>
                  <td>{item.state}</td>
                  <td>{item.exception_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </OperationsTable>
      )}
    </OperationsPage>
  );
}
