// Monitoring, exports and command lookup (ticket 18; design §8.2
// "Monitoring/export/recovery", E189-E192).
//
// Three things one screen, because they are what a platform administrator does
// in the same five minutes when something looks wrong:
//
//   1. "Is the system keeping up?" — E192's checks, each with what was observed
//      and the threshold it was judged against, worst first.
//   2. "Give me the numbers." — E189 asks for a durable export and E190 polls it
//      until there is a file. The file itself is protected evidence: the link
//      goes through the ordinary evidence download, which re-checks scope, so a
//      link that worked yesterday can refuse today.
//   3. "Did my command commit?" — E191, by command id. A command identity nobody
//      recorded reads "no record", which is *not* the same as "it did not
//      happen"; the screen says so rather than reassuring anyone.
//
// Everything here needs a fresh password confirmation before it will act:
// `export.run` is a single-identity privileged action (Phase 1 §9.3), and
// `useStepUp` asks and retries the exact call that was refused.
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Download, RefreshCw, Search } from "lucide-react";

import { api, apiErrorMessage, apiUrl, goodsMeta } from "../lib/api";
import {
  Denied,
  Feedback,
  Field,
  hold,
  listState,
  useGoodsFetch,
  useStepUp,
} from "../lib/goodsScreen";
import {
  attentionCount,
  commandStatusLabel,
  exportStateLabel,
  healthLabel,
  healthStateLabel,
  isRunning,
  issuePath,
  sortHealth,
  type CommandStatus,
  type ExportJob,
  type HealthBody,
  type HealthItem,
} from "../lib/goodsMonitoring";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { formatDateTime } from "../lib/format";
import "./GoodsMonitoring.css";

const HEALTH = "/goods-v1/operations/health";
const EXPORTS = "/goods-v1/exports";
const COMMANDS = "/goods-v1/commands";
/** How often a running export job is asked about. The worker claims jobs on its
 *  own clock, so this is a poll, not a stream: slow enough to be cheap, fast
 *  enough that a small file feels immediate. */
const POLL_MS = 1_500;

export function GoodsMonitoringPage() {
  const { session } = useAuth();
  const canSeeHealth = hold(session, "ops.health.view");
  const canExport = hold(session, "export.run");
  const canLookUp = hold(session, "audit.view") || canExport;

  if (!session) return null;
  if (!canSeeHealth && !canExport && !canLookUp) return <Denied what="monitoring" />;

  return (
    <div className="mon-layout">
      <PageHeader
        title="Monitoring and exports"
        lead="What the queue, the evidence anchors, the numbering ceilings and the verifier are doing right now — and where to get a scoped copy of the numbers."
      />
      {canSeeHealth && <HealthPanel />}
      {canExport && <ExportPanel />}
      {canLookUp && <CommandLookup />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// E192: what the system is doing
// ---------------------------------------------------------------------------

function HealthPanel() {
  const health = useGoodsFetch<HealthBody, HealthBody | null>(HEALTH, (r) => r, null);
  const items = health.value?.items ?? [];
  const attention = attentionCount(items);
  const empty = listState(
    { loading: health.loading, failure: health.failure, empty: items.length === 0 },
    "No checks were reported.",
  );

  return (
    <section className="card section-card" data-testid="mon-health">
      <div className="toolbar">
        <div>
          <p className="eyebrow">Operations health</p>
          <h2 className="h3">
            {attention > 0
              ? `${attention} check${attention === 1 ? "" : "s"} need attention`
              : "Everything within its threshold"}
          </h2>
          {health.value && (
            // The worker's scheduled pass, not this page load: refreshing reads
            // the last recorded result again and never runs a check.
            <p className="lead" data-testid="mon-health-checked">
              {health.value.checked_at
                ? `Checked by the scheduled pass at ${formatDateTime(health.value.checked_at)}`
                : "Checked: never — no scheduled pass has run yet"}
            </p>
          )}
        </div>
        <div className="spacer" />
        <button className="btn btn-sm" onClick={health.reload} data-testid="mon-health-reload">
          <RefreshCw size={14} /> Refresh
        </button>
      </div>
      {health.denied && <Denied what="operations health" />}
      {empty ?? (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Check</th>
              <th scope="col">State</th>
              <th scope="col">Observed</th>
              <th scope="col">Threshold</th>
              <th scope="col">Checked</th>
              <th scope="col">Last passed</th>
              <th scope="col">Owner</th>
              <th scope="col">Issue</th>
            </tr>
          </thead>
          <tbody>
            {sortHealth(items).map((item: HealthItem) => (
              <tr key={item.code} data-testid={`mon-health-${item.code}`}>
                <th scope="row">{healthLabel(item.code)}</th>
                <td>
                  <span
                    className={item.state === "attention" ? "warn-note" : "muted"}
                    data-testid={`mon-health-state-${item.code}`}
                  >
                    {healthStateLabel(item.state)}
                  </span>
                </td>
                <td>
                  {item.observed}
                  {item.scope && <div className="muted">Covered: {item.scope}</div>}
                </td>
                <td className="muted">{item.threshold}</td>
                <td className="muted" data-testid={`mon-health-checked-${item.code}`}>
                  {item.checked_at ? formatDateTime(item.checked_at) : "Never"}
                </td>
                <td className="muted" data-testid={`mon-health-last-ok-${item.code}`}>
                  {item.last_ok_at ? formatDateTime(item.last_ok_at) : "Never"}
                </td>
                <td className="muted">{item.owner_role}</td>
                <td>
                  {item.issue_id ? (
                    <Link
                      to={issuePath(item.issue_id)}
                      data-testid={`mon-health-issue-${item.code}`}
                    >
                      Open the exception
                    </Link>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// E189/E190: ask for a file, wait for it, download it
// ---------------------------------------------------------------------------

function ExportPanel() {
  const { guarded, dialog } = useStepUp();
  const [siteId, setSiteId] = useState("");
  const [brandId, setBrandId] = useState("");
  const [withValue, setWithValue] = useState(true);
  const [asOf, setAsOf] = useState("");
  const [job, setJob] = useState<ExportJob | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const poll = useCallback(async (id: string) => {
    const response = await api.get<ExportJob>(`${EXPORTS}/${id}`);
    setJob(response.data);
    return response.data;
  }, []);

  // One timer, cleared on unmount and the moment the job stops moving — a
  // failed job that kept polling would hammer the API with nothing to learn.
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!job || !isRunning(job)) return;
    timer.current = setTimeout(() => {
      poll(job.id).catch((e) => setError(apiErrorMessage(e)));
    }, POLL_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [job, poll]);

  async function request(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    const scope = {
      scope_kind: siteId ? "sites" : brandId ? "brands" : "tenant",
      site_ids: siteId ? [Number(siteId)] : [],
      sbu_ids: [],
      brand_ids: brandId ? [Number(brandId)] : [],
    };
    try {
      const created = await guarded(() =>
        // E189's input is the ExportSpec's own fields beside the MutationMeta.
        api.post<ExportJob>(EXPORTS, {
          kind: "stock_csv",
          scope,
          field_set: withValue ? ["cost"] : [],
          ...(asOf ? { as_of: new Date(asOf).toISOString() } : {}),
          ...goodsMeta(),
        }),
      );
      setJob(created.data);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card section-card" data-testid="mon-export">
      <p className="eyebrow">Durable export</p>
      <h2 className="h3">Stock, as a spreadsheet</h2>
      <p className="lead">
        The file holds exactly the rows you may see. Money is whole paise; a piece whose value is
        not known says <em>unknown</em> rather than nought, and a total over a mix says so.
      </p>
      {dialog}
      <Feedback error={error} ok="" />
      <form className="form-grid" onSubmit={request}>
        <Field id="mon-export-site" label="One site (optional)">
          <input
            id="mon-export-site"
            className="input"
            inputMode="numeric"
            placeholder="Every site you may see"
            value={siteId}
            onChange={(e) => setSiteId(e.target.value)}
            data-testid="mon-export-site"
          />
        </Field>
        <Field id="mon-export-brand" label="One brand (optional)">
          <input
            id="mon-export-brand"
            className="input"
            inputMode="numeric"
            placeholder="Every brand you may see"
            value={brandId}
            onChange={(e) => setBrandId(e.target.value)}
            data-testid="mon-export-brand"
          />
        </Field>
        <Field id="mon-export-asof" label="As at (optional)">
          <input
            id="mon-export-asof"
            className="input"
            type="datetime-local"
            value={asOf}
            onChange={(e) => setAsOf(e.target.value)}
            data-testid="mon-export-asof"
          />
        </Field>
        <Field id="mon-export-value" label="Include value">
          <input
            id="mon-export-value"
            type="checkbox"
            checked={withValue}
            onChange={(e) => setWithValue(e.target.checked)}
            data-testid="mon-export-value"
          />
        </Field>
        <button className="btn btn-cta" type="submit" disabled={busy} data-testid="mon-export-run">
          Ask for the file
        </button>
      </form>

      {job && (
        <div className="mon-job" data-testid="mon-export-job">
          <p>
            <strong data-testid="mon-export-state">{exportStateLabel(job)}</strong>
            {isRunning(job) && <span className="muted"> — checking again in a moment…</span>}
          </p>
          {job.as_of && <p className="muted">As at {formatDateTime(job.as_of)}</p>}
          {job.sha256 && (
            <p className="muted" data-testid="mon-export-hash">
              SHA-256 {job.sha256}
            </p>
          )}
          {job.download_url && (
            <a
              className="btn btn-sm"
              href={apiUrl(job.download_url.replace(/^\/api/, ""))}
              data-testid="mon-export-download"
            >
              <Download size={14} /> Download
            </a>
          )}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// E191: did that command commit?
// ---------------------------------------------------------------------------

function CommandLookup() {
  const [commandId, setCommandId] = useState("");
  const [principalKey, setPrincipalKey] = useState("");
  const [status, setStatus] = useState<CommandStatus | null>(null);
  const [error, setError] = useState("");

  async function look(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setStatus(null);
    try {
      const query = principalKey.trim()
        ? `?principal_key=${encodeURIComponent(principalKey.trim())}`
        : "";
      const response = await api.get<CommandStatus>(`${COMMANDS}/${commandId.trim()}${query}`);
      setStatus(response.data);
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  return (
    <section className="card section-card" data-testid="mon-command">
      <p className="eyebrow">Command outcome</p>
      <h2 className="h3">Did that command commit?</h2>
      <Feedback error={error} ok="" />
      <form className="form-grid" onSubmit={look}>
        <Field id="mon-command-id" label="Command id">
          <input
            id="mon-command-id"
            className="input"
            placeholder="The id the client sent"
            value={commandId}
            onChange={(e) => setCommandId(e.target.value)}
            data-testid="mon-command-id"
          />
        </Field>
        <Field
          id="mon-command-principal"
          label="Whose command (optional)"
          hint="Someone else's needs the audit grant, and the key it was run under."
        >
          <input
            id="mon-command-principal"
            className="input"
            placeholder="human:… or service:…"
            value={principalKey}
            onChange={(e) => setPrincipalKey(e.target.value)}
            aria-describedby="mon-command-principal-hint"
            data-testid="mon-command-principal"
          />
        </Field>
        <button
          className="btn btn-cta"
          type="submit"
          disabled={!commandId.trim()}
          data-testid="mon-command-look"
        >
          <Search size={14} /> Look it up
        </button>
      </form>
      {status && (
        <div className="mon-job" data-testid="mon-command-result">
          <p>
            <strong data-testid="mon-command-status">{commandStatusLabel(status)}</strong>
            {status.http_status !== null && (
              <span className="muted"> — answered {status.http_status}</span>
            )}
          </p>
          {status.result && (
            <pre className="mon-result" data-testid="mon-command-payload">
              {JSON.stringify(status.result, null, 2)}
            </pre>
          )}
        </div>
      )}
    </section>
  );
}
