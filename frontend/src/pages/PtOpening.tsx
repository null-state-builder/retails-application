// Store test run (test tenant only): open a store from its PT files.
// The store person uploads the store's KDPS PT workbooks. A server job then runs
// the reviewed opening path itself - mappings, cost = P RATE, the file
// quantities as the count, approvals and acceptance onto the floor - so nothing
// is scanned. With no second person needed (the store's setup) the upload starts
// it; otherwise it waits for an owner's approval and "Load" resumes it.
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { api, apiErrorMessage } from "../lib/api";
import { formatPaiseString } from "../lib/format";
import { Feedback, Field, hold, useStepUp, type Page } from "../lib/goodsScreen";

interface Problem {
  barcode: string;
  reason: string;
}
interface PtSummary {
  files: { name: string; sheets: { sheet: string; rows: number }[] }[];
  pt_rows: number;
  unreadable: { where: string; reason: string }[];
  unreadable_count: number;
  conflicts: { barcode: string; mrp_paise: string[]; used: string }[];
  conflict_count: number;
  problems: Problem[];
  problem_count: number;
}
interface QuickLoad {
  state?: "running" | "done" | "failed" | "waiting_approval";
  step?: string;
  error?: string | null;
  log?: string[];
  loaded_rows?: number;
  loaded_qty?: number;
  excluded_rows?: number;
  updated_at?: string;
}
interface Source {
  id: string;
  source_name: string;
  state: string;
  allowed_actions: string[];
  data: {
    metadata: {
      row_count: number;
      quantity: number;
      stock_mrp_paise?: string;
      source_amount_paise?: string;
      pt_opening?: PtSummary;
      stock_sync_top_up?: boolean;
    };
    included_quantity: number;
    quick_load?: QuickLoad;
  };
}

const BASE = "/goods-v1/ptmapper/soh-imports";

function status(source: Source): string {
  const load = source.data.quick_load ?? {};
  if (load.state === "done") return "Loaded – ready to sell";
  if (load.state === "running") return load.step ?? "Loading…";
  if (load.state === "failed") return "Load failed";
  if (load.state === "waiting_approval") return load.step ?? "Waiting for approval";
  return source.state === "applied" ? "Loaded" : "Waiting for owner approval";
}

export function PtOpeningPanel({ siteId }: { siteId: string }) {
  const { session } = useAuth();
  const stage = hold(session, "opening.import.stage");
  const approve = hold(session, "opening.manifest.approve");
  const [sources, setSources] = useState<Source[]>([]);
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  const refresh = useCallback(async () => {
    const list = await api.get<Page<Source>>(`${BASE}?site_id=${siteId}`);
    setSources(
      list.data.items.filter(
        (s) => s.data.metadata.pt_opening || s.data.metadata.stock_sync_top_up,
      ),
    );
  }, [siteId]);

  useEffect(() => {
    refresh().catch((e: unknown) => setError(apiErrorMessage(e)));
  }, [refresh]);

  const loading = sources.some((s) => s.data.quick_load?.state === "running");
  useEffect(() => {
    if (!loading) return;
    const timer = window.setInterval(() => {
      refresh().catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [loading, refresh]);

  async function run(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    setOk("");
    try {
      await work();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function upload() {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    form.append("site_id", siteId);
    form.append("command_id", crypto.randomUUID());
    form.append("contract_version", "goods-v1");
    await api.post<Source>(`${BASE}/pt-upload`, form);
    setFiles([]);
    setOk("PT files uploaded. Loading starts on its own unless an approval is needed.");
    await refresh();
  }

  async function load(source: Source) {
    await stepUp.guarded(() => api.post<Source>(`${BASE}/${source.id}/load`, {}));
    setOk("Loading started. This takes a minute or two; you can stay on this page.");
    await refresh();
  }

  if (!session?.test_environment || (!stage && !approve)) return null;
  return (
    <section className="card section-card" data-testid="pt-opening">
      <h3 className="h3">Opening from PT files (test store)</h3>
      <p className="lead">
        Upload every PT file sent to this store (KDPS 22-column .xlsx; all sheets are read). An
        owner then approves and loads: the quantities go straight onto the floor, ready to sell,
        with P RATE as cost and each row&apos;s own HSN. Afterwards, upload the legacy SOH in{" "}
        <Link to="/goods/stock-sync">Legacy sync</Link> to match what is really in the store.
      </p>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {stage && (
        <div className="form-grid">
          <Field id="pt-files" label="PT files (.xlsx, several allowed)">
            <input
              id="pt-files"
              type="file"
              accept=".xlsx"
              multiple
              onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
            />
          </Field>
          <button
            className="btn btn-primary"
            disabled={busy || files.length === 0}
            onClick={() => run(upload)}
            data-testid="pt-upload"
          >
            Upload and preview
          </button>
        </div>
      )}
      {sources.map((source) => {
        const meta = source.data.metadata;
        const pt = meta.pt_opening;
        const progress = source.data.quick_load ?? {};
        return (
          <article className="card" key={source.id} data-testid="pt-opening-source">
            <p className="eyebrow">
              {meta.stock_sync_top_up ? "New items from the SOH sync" : source.source_name}
            </p>
            <h4 className="h4">{status(source)}</h4>
            <ul>
              <li>
                {meta.row_count.toLocaleString()} barcodes · {meta.quantity.toLocaleString()} pieces
                {pt
                  ? ` from ${pt.pt_rows.toLocaleString()} PT rows in ${pt.files.length} file(s)`
                  : ""}
              </li>
              {meta.stock_mrp_paise && (
                <li>Value at MRP: {formatPaiseString(meta.stock_mrp_paise)}</li>
              )}
              {meta.source_amount_paise && (
                <li>
                  Value at {pt ? "P RATE" : "SOH rate"}:{" "}
                  {formatPaiseString(meta.source_amount_paise)}
                </li>
              )}
              {pt && pt.problem_count > 0 && (
                <li>
                  <strong>{pt.problem_count}</strong> barcodes will be left out (no MRP, no P RATE,
                  P RATE above MRP, or a bad HSN)
                </li>
              )}
              {pt && pt.unreadable_count > 0 && (
                <li>
                  <strong>{pt.unreadable_count}</strong> PT rows could not be read (no barcode or a
                  part quantity)
                </li>
              )}
              {pt && pt.conflict_count > 0 && (
                <li>
                  <strong>{pt.conflict_count}</strong> barcodes have different MRPs in different
                  rows; the last row is used
                </li>
              )}
              {progress.state === "done" && (
                <li>
                  On the floor: <strong>{progress.loaded_qty?.toLocaleString()}</strong> pieces of{" "}
                  {progress.loaded_rows?.toLocaleString()} barcodes; {progress.excluded_rows} left
                  out.
                </li>
              )}
            </ul>
            {progress.state === "failed" && progress.error && (
              <div className="warn-note" role="alert">
                {progress.error}
              </div>
            )}
            {progress.state === "running" && progress.log && progress.log.length > 0 && (
              <p className="muted-cell">{progress.log[progress.log.length - 1]}</p>
            )}
            {pt &&
              (pt.problems.length > 0 || pt.unreadable.length > 0 || pt.conflicts.length > 0) && (
                <details>
                  <summary>Rows left out or to check</summary>
                  <div
                    className="table-wrap"
                    role="region"
                    aria-label="PT rows to check"
                    tabIndex={0}
                  >
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Barcode / row</th>
                          <th>Why</th>
                        </tr>
                      </thead>
                      <tbody>
                        {pt.problems.map((p) => (
                          <tr key={`p-${p.barcode}`}>
                            <td>{p.barcode}</td>
                            <td>{p.reason} – left out</td>
                          </tr>
                        ))}
                        {pt.unreadable.map((u) => (
                          <tr key={`u-${u.where}`}>
                            <td>{u.where}</td>
                            <td>{u.reason} – left out</td>
                          </tr>
                        ))}
                        {pt.conflicts.map((c) => (
                          <tr key={`c-${c.barcode}`}>
                            <td>{c.barcode}</td>
                            <td>
                              MRPs {c.mrp_paise.map((m) => formatPaiseString(m)).join(", ")} – used{" "}
                              {c.used}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              )}
            {source.allowed_actions.includes("load") && (
              <button
                className="btn btn-cta"
                disabled={busy || loading}
                onClick={() => run(() => load(source))}
                data-testid="pt-load"
              >
                {progress.state === "failed"
                  ? "Try loading again"
                  : progress.state === "waiting_approval"
                    ? "Continue loading"
                    : "Load"}
              </button>
            )}
          </article>
        );
      })}
    </section>
  );
}
