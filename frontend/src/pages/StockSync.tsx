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
import { api, apiErrorCode, apiErrorMessage, goodsMeta } from "../lib/api";
import { sha256Hex } from "../lib/goodsPt";
import { Feedback, Field, hold } from "../lib/goodsScreen";

interface Row {
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
  outcome: Record<string, number>;
  changed_rows: number;
  exception_count: number;
  new_item_rows: number;
  top_up_import_id: string | null;
  allowed_actions: string[];
  preview?: Record<string, number>;
  rows?: Row[];
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
  const [overview, setOverview] = useState<Overview | null>(null);
  const [selected, setSelected] = useState<Selected | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [cutoff, setCutoff] = useState(localNow());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [needsOpening, setNeedsOpening] = useState(false);
  const [ok, setOk] = useState("");

  useEffect(() => {
    if (!siteId && sites[0]) setSiteId(sites[0].id);
  }, [sites, siteId]);

  async function refresh() {
    if (!siteId) return;
    const result = await api.get<Overview>(`${BASE}/legacy-sync?site_id=${siteId}`);
    setOverview(result.data);
  }

  useEffect(() => {
    setSelected(null);
    setOverview(null);
    refresh().catch((e) => setError(apiErrorMessage(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [siteId]);

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
    await refresh();
  }

  async function act(operation: "apply" | "discard") {
    if (!selected) return;
    const path = selected.kind === "soh" ? "stock-syncs" : "legacy-sales";
    const result = await api.post(`${BASE}/${path}/${selected.id}/${operation}`, goodsMeta());
    setSelected({ ...result.data, kind: selected.kind } as Selected);
    if (operation === "apply") setOk(`Applied. ${result.data.exception_count} rows need attention.`);
    await refresh();
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
      <Feedback error={error} ok={ok} />
      {needsOpening && (
        <p className="lead">
          <Link to="/goods/opening">Go to Opening stock</Link> to upload this store&apos;s PT files.
        </p>
      )}
      {overview && !overview.test_environment && (
        <div className="warn-note" role="alert">
          This store is live. Legacy sync runs only on the test environment.
        </div>
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
          className="btn btn-cta"
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
        <section className="card section-card" data-testid="sync-detail">
          <p className="eyebrow">{selected.source_name}</p>
          <h3 className="h3">
            {selected.kind === "soh"
              ? `SOH exported ${when(selected.cutoff_at)}`
              : `Sales ${selected.date_from} – ${selected.date_to}`}{" "}
            · {selected.state}
          </h3>
          <ul>
            {selected.preview && (
              <li>{selected.preview.test_bills_to_reverse} test bills will be put back on the shelf</li>
            )}
            {selected.kind === "soh" && selected.preview && (
              <>
                <li>
                  This store holds {selected.preview.book_qty} pieces; the SOH says{" "}
                  {selected.preview.soh_qty}
                </li>
                <li>{selected.preview.remove_qty} to take out</li>
                <li>{selected.preview.add_qty} to add for items already here</li>
                <li>{selected.preview.new_item_qty} of new items to review as an opening top-up</li>
              </>
            )}
            {selected.kind === "sales" && (
              <li>
                {selected.metadata.bill_count} legacy bills, {selected.metadata.sold_quantity} sold,{" "}
                {selected.metadata.returned_quantity} returned
              </li>
            )}
            {selected.state === "applied" &&
              Object.entries(selected.outcome).map(([key, value]) => (
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
              <button className="btn btn-cta" disabled={busy} onClick={() => run(() => act("apply"))}>
                Apply to stock
              </button>
              <button className="btn" disabled={busy} onClick={() => run(() => act("discard"))}>
                Discard
              </button>
            </div>
          )}
          {selected.rows && selected.rows.length > 0 && (
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
                        {row.outcome === "exception" ? `⚠ ${row.exception_code}` : row.outcome.replace("_", " ")}
                        {row.exception_note && <div className="muted">{row.exception_note}</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </OperationsTable>
          )}
        </section>
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
                    <button className="btn btn-link" onClick={() => run(() => open("soh", item.id))}>
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
                    <button className="btn btn-link" onClick={() => run(() => open("sales", item.id))}>
                      {item.date_from === item.date_to ? item.date_from : `${item.date_from} – ${item.date_to}`}
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
