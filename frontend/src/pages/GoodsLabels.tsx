// Labels and print jobs (ticket 11): print an official PT line's frozen alias
// and MRP as a real Code 128 label, record the printer's own outcome separately
// from the browser dialog attempt, and reprint only a line's evidenced missing
// quantity with a reason (GSA-T11). A printed alias then scans back at
// acceptance as this job's own evidence (design R1/R8) — proved by
// `GoodsAccept.tsx`'s own screen, not repeated here.
import { useEffect, useMemo, useState } from "react";
import { Printer, RotateCcw } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import {
  Feedback,
  Field,
  listState,
  useGoodsFetch,
  useResourceDoc,
  type Page,
  type ResourceDTO,
} from "../lib/goodsScreen";
import type { PtData, PtLine } from "../lib/goodsPt";
import {
  OUTCOME_OPTIONS,
  allowsUsableCounts,
  hasReprintableShortfall,
  latestEvent,
  missingByLine,
  statusLabel,
  verifiedScans,
  type PrintJobData,
  type PrintOutcomeKind,
} from "../lib/goodsLabels";
import { formatPaiseString } from "../lib/format";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import "./GoodsLabels.css";

const PT_FILES = "/goods-v1/ptmapper/files";
const PRINT_JOBS = "/goods-v1/ptmapper/print-jobs";

interface LabelConfigData {
  payload: { width_mm: number; height_mm: number; copies_limit: number };
  versions: { id: string; state: string }[];
}

interface TemplateOption {
  versionId: string;
  label: string;
  copiesLimit: number;
}

function templateOptions(rows: ResourceDTO<LabelConfigData>[]): TemplateOption[] {
  const out: TemplateOption[] = [];
  for (const row of rows) {
    const effective = row.data.versions.find((v) => v.state === "effective");
    if (!effective) continue;
    const { width_mm, height_mm, copies_limit } = row.data.payload;
    out.push({
      versionId: effective.id,
      label: `${width_mm} × ${height_mm} mm (up to ${copies_limit} copies)`,
      copiesLimit: copies_limit,
    });
  }
  return out;
}

export function GoodsLabelsPage() {
  const [params, setParams] = useSearchParams();
  return (
    <div className="labels-layout">
      <PageHeader
        title="Print labels"
        lead="Print a scannable Code 128 label for an official PT line, record what the printer actually did, and reprint only what is still missing."
      />
      <LabelsPanel
        ptId={params.get("pt") ?? ""}
        onPtId={(id) => setParams(id ? { pt: id } : {})}
      />
    </div>
  );
}

/** Labels for one official PT: pick the lines, print them, and record what the
 *  printer actually did.
 *
 *  The screen above and the receiving workflow's Labels step both render this,
 *  so there is one printing panel rather than two that could drift. `fixed`
 *  pins it to a PT the caller already knows (the workflow's delivery); without
 *  it the panel asks for one, which is what the standalone screen needs. */
export function LabelsPanel({
  ptId: initial,
  fixed,
  onPtId,
}: {
  ptId?: string;
  /** A PT chosen by the caller: no picker, and no way to wander off it. */
  fixed?: string;
  onPtId?: (id: string) => void;
}) {
  const { session } = useAuth();
  const [ptInput, setPtInput] = useState(initial ?? "");
  const [chosen, setChosen] = useState(initial ?? "");
  const ptId = fixed ?? chosen;
  const [jobs, setJobs] = useState<ResourceDTO<PrintJobData>[]>([]);

  const summary = useResourceDoc<PtData>(ptId ? `${PT_FILES}/${ptId}` : null);
  const versionNo = summary.doc?.version ?? null;
  const detail = useResourceDoc<PtData>(
    ptId && versionNo ? `${PT_FILES}/${ptId}?version=${versionNo}` : null,
  );
  const templates = useGoodsFetch<Page<ResourceDTO<LabelConfigData>>, ResourceDTO<LabelConfigData>[]>(
    "/goods-v1/masters/configurations?kind=label&limit=100",
    (r) => r.items ?? [],
    [],
  );

  function loadPt(e: React.FormEvent) {
    e.preventDefault();
    onPtId?.(ptInput.trim());
    setChosen(ptInput.trim());
    setJobs([]);
  }

  function addJob(job: ResourceDTO<PrintJobData>) {
    setJobs((prev) => [job, ...prev.filter((existing) => existing.id !== job.id)]);
  }

  function updateJob(job: ResourceDTO<PrintJobData>) {
    setJobs((prev) => prev.map((existing) => (existing.id === job.id ? job : existing)));
  }

  if (!session) return null;
  const officialVersionId = summary.doc?.context?.official_version_id as string | undefined;
  const lines = detail.doc?.data.lines.items ?? [];
  const options = templateOptions(templates.value);

  return (
    <>
      {!fixed && (
        <form className="card section-card form-grid" onSubmit={loadPt}>
          <Field id="labels-pt" label="Official PT">
            <input
              id="labels-pt"
              className="input"
              placeholder="Paste the PT's id"
              value={ptInput}
              onChange={(e) => setPtInput(e.target.value)}
              data-testid="labels-pt-input"
            />
          </Field>
          <button className="btn btn-cta" type="submit" data-testid="labels-pt-load">
            Load
          </button>
        </form>
      )}

      {ptId && summary.denied && (
        <div className="card section-card" data-testid="labels-pt-denied">
          <p className="lead">There is no PT here for you, or it does not exist.</p>
        </div>
      )}
      {ptId && summary.failure && <div className="warn-note">{summary.failure}</div>}

      {ptId && summary.doc && (
        <PrintForm
          ptVersionId={officialVersionId ?? null}
          lines={lines}
          loadingLines={detail.loading}
          templates={options}
          templatesLoading={templates.loading}
          onCreated={addJob}
        />
      )}

      {jobs.map((job) => (
        <PrintJobCard key={job.id} job={job} onUpdated={updateJob} onReprint={addJob} />
      ))}
    </>
  );
}

// ---------------------------------------------------------------------------
// Choosing lines, copies and a label profile, then printing
// ---------------------------------------------------------------------------

function PrintForm({
  ptVersionId,
  lines,
  loadingLines,
  templates,
  templatesLoading,
  onCreated,
  reprintOf,
}: {
  ptVersionId: string | null;
  lines: PtLine[];
  loadingLines: boolean;
  templates: TemplateOption[];
  templatesLoading: boolean;
  onCreated: (job: ResourceDTO<PrintJobData>) => void;
  reprintOf?: { jobId: string; presetCopies: Map<string, number> };
}) {
  const [copies, setCopies] = useState<Record<string, number>>({});
  const [templateVersionId, setTemplateVersionId] = useState("");
  const [reasonCode, setReasonCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!templateVersionId && templates.length > 0) setTemplateVersionId(templates[0].versionId);
  }, [templates, templateVersionId]);

  useEffect(() => {
    if (reprintOf) {
      setCopies(Object.fromEntries(reprintOf.presetCopies));
    }
  }, [reprintOf]);

  const printable = lines.filter((line) => line.official_line_id);
  const selected = printable.filter((line) => (copies[line.official_line_id!] ?? 0) > 0);
  const copiesLimit = templates.find((t) => t.versionId === templateVersionId)?.copiesLimit;

  function toggle(lineId: string, checked: boolean) {
    setCopies((prev) => ({ ...prev, [lineId]: checked ? 1 : 0 }));
  }

  function setQty(lineId: string, qty: number) {
    setCopies((prev) => ({ ...prev, [lineId]: qty }));
  }

  async function submit() {
    if (!ptVersionId || selected.length === 0 || !templateVersionId) return;
    if (reprintOf && !reasonCode.trim()) {
      setError("A reprint needs a reason.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const { data } = await api.post<ResourceDTO<PrintJobData>>(PRINT_JOBS, {
        pt_version_id: ptVersionId,
        template_version_id: templateVersionId,
        lines: selected.map((line) => ({
          official_line_id: line.official_line_id,
          copies: copies[line.official_line_id!],
        })),
        ...(reprintOf ? { reprint_of_id: reprintOf.jobId, reason_code: reasonCode } : {}),
        ...goodsMeta(),
      });
      onCreated(data);
      if (!reprintOf) setCopies({});
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card section-card" data-testid={reprintOf ? "labels-reprint-form" : "labels-print-form"}>
      <h3 className="h3">{reprintOf ? "Reprint the missing copies" : "Choose what to print"}</h3>
      <Feedback error={error} ok="" />
      {listState(
        { loading: loadingLines, failure: "", empty: !loadingLines && printable.length === 0 },
        "This PT has no official lines with a frozen id yet.",
      ) ?? (
        <table className="table" data-testid="labels-lines">
          <thead>
            <tr>
              <th>Print</th>
              <th>Alias</th>
              <th>MRP</th>
              <th>Copies</th>
            </tr>
          </thead>
          <tbody>
            {printable.map((line) => {
              const lineId = line.official_line_id!;
              const qty = copies[lineId] ?? 0;
              return (
                <tr key={lineId}>
                  <td>
                    <input
                      type="checkbox"
                      checked={qty > 0}
                      onChange={(e) => toggle(lineId, e.target.checked)}
                      data-testid={`labels-line-${lineId}`}
                    />
                  </td>
                  <td className="mono">{line.alias_as_used}</td>
                  <td className="mono">{formatPaiseString(line.calculated?.mrp_paise ?? line.supplied.mrp_paise)}</td>
                  <td>
                    <input
                      type="number"
                      min={1}
                      max={copiesLimit}
                      className="input input-sm"
                      value={qty || ""}
                      disabled={qty === 0}
                      onChange={(e) => {
                        const next = Math.max(1, Number(e.target.value) || 1);
                        setQty(lineId, copiesLimit ? Math.min(next, copiesLimit) : next);
                      }}
                      data-testid={`labels-copies-${lineId}`}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <div className="form-grid">
        <Field id="labels-template" label="Label profile">
          <select
            id="labels-template"
            className="select"
            value={templateVersionId}
            onChange={(e) => setTemplateVersionId(e.target.value)}
            data-testid="labels-template"
            disabled={templatesLoading || templates.length === 0}
          >
            {templates.map((t) => (
              <option key={t.versionId} value={t.versionId}>
                {t.label}
              </option>
            ))}
          </select>
        </Field>
        {reprintOf && (
          <Field id="labels-reason" label="Reason for the reprint">
            <input
              id="labels-reason"
              className="input"
              value={reasonCode}
              onChange={(e) => setReasonCode(e.target.value)}
              data-testid="labels-reprint-reason"
            />
          </Field>
        )}
      </div>

      <button
        className="btn btn-cta"
        disabled={busy || selected.length === 0 || !templateVersionId || !ptVersionId}
        onClick={submit}
        data-testid={reprintOf ? "labels-reprint-submit" : "labels-print-submit"}
      >
        <Printer size={14} /> {reprintOf ? "Reprint" : "Print"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// A created job: its rendered labels, the printer outcome, and its reprint
// ---------------------------------------------------------------------------

function PrintJobCard({
  job,
  onUpdated,
  onReprint,
}: {
  job: ResourceDTO<PrintJobData>;
  onUpdated: (job: ResourceDTO<PrintJobData>) => void;
  onReprint: (job: ResourceDTO<PrintJobData>) => void;
}) {
  const [outcome, setOutcome] = useState<PrintOutcomeKind>("confirmed");
  const [usable, setUsable] = useState<Record<string, string>>({});
  const [reasonCode, setReasonCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reprinting, setReprinting] = useState(false);
  const [scanAlias, setScanAlias] = useState("");
  const [scanLineId, setScanLineId] = useState(
    job.data.labels[0]?.official_line_id ?? "",
  );
  const [scanError, setScanError] = useState("");
  const [scanOk, setScanOk] = useState("");
  const [scanBusy, setScanBusy] = useState(false);

  const last = latestEvent(job.data);
  const missing = missingByLine(job.data);
  // Once the printer's own outcome is in, pressing Print again would record a
  // bare `attempted` the server now ignores for status (§5.2: an afterprint
  // cannot yield `confirmed`, nor unmake one). A second copy is a reprint: a
  // new, linked, reasoned job, never a silent rerun of this one.
  const outcomeRecorded = !["prepared", "attempted"].includes(job.data.state);

  async function recordOutcome() {
    setBusy(true);
    setError("");
    try {
      const body: Record<string, unknown> = { outcome, ...goodsMeta(job.revision) };
      if (allowsUsableCounts(outcome)) {
        body.usable_counts = job.data.labels.map((label) => {
          const raw = usable[label.official_line_id];
          return {
            official_line_id: label.official_line_id,
            qty: raw === undefined || raw === "" ? null : Number(raw),
          };
        });
      }
      if (reasonCode) body.reason_code = reasonCode;
      const { data } = await api.post<ResourceDTO<PrintJobData>>(
        `${PRINT_JOBS}/${job.id}/outcome`,
        body,
      );
      onUpdated(data);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  // Design §8.2 lists scan-back for TagPrint: read one printed label back and
  // let the server say whether it is the alias it froze for that line (E183
  // step 9). A mismatch is a discrepancy to show the operator, not a crash and
  // not an automatic master change - and a match proves that one sample only,
  // which is why it never fills in an unobserved copy count and takes no job
  // status of its own. It stays available after the counted outcome is in: a
  // sample scan corroborates the count, it never replaces it.
  async function recordScan() {
    setScanBusy(true);
    setScanError("");
    setScanOk("");
    try {
      const { data } = await api.post<ResourceDTO<PrintJobData>>(
        `${PRINT_JOBS}/${job.id}/outcome`,
        {
          outcome: "scan_verified",
          scanned_alias: scanAlias,
          matched_line_id: scanLineId,
          ...goodsMeta(job.revision),
        },
      );
      setScanAlias("");
      setScanOk("That scan matches the alias frozen for this line.");
      onUpdated(data);
    } catch (e) {
      setScanError(apiErrorMessage(e));
    } finally {
      setScanBusy(false);
    }
  }

  // GSA-T11: "a browser dialog or afterprint event proves an attempt, not
  // physical success" - so the attempt itself is the signal, recorded as its
  // own weaker `attempted` outcome, separately from the operator's later
  // physical-count confirmation below. `afterprint` fires once the dialog
  // closes either way (printed or cancelled), which is exactly that weak
  // "an attempt happened" signal - not proof of a page in a tray.
  async function recordAttempt(revision: number) {
    try {
      const { data } = await api.post<ResourceDTO<PrintJobData>>(
        `${PRINT_JOBS}/${job.id}/outcome`,
        { outcome: "attempted", ...goodsMeta(revision) },
      );
      onUpdated(data);
    } catch {
      // The dialog attempt already happened regardless; a failed recording is
      // not a blocking error here - the outcome form below still lets the
      // operator record what actually happened.
    }
  }

  function printInBrowser() {
    const revision = job.revision;
    const onAfterPrint = () => {
      window.removeEventListener("afterprint", onAfterPrint);
      void recordAttempt(revision);
    };
    window.addEventListener("afterprint", onAfterPrint);
    window.print();
  }

  const canReprint = hasReprintableShortfall(job.data);
  // Memoised on the job's own identity/revision, not on `missing` (a fresh Map
  // every render): otherwise this object's identity changes on every render of
  // this card - including one triggered by typing in the outcome form above -
  // and the reprint form's own `useEffect` below would silently wipe out
  // whatever copies a person had already edited there.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const reprintOf = useMemo(
    () => ({
      jobId: job.id,
      presetCopies: new Map(
        [...missing.entries()].filter(([, qty]) => qty !== null && qty > 0) as [string, number][],
      ),
    }),
    [job.id, job.revision],
  );

  return (
    <div className="card section-card labels-job" data-testid={`labels-job-${job.id}`}>
      <h3 className="h3">
        Print job {job.id.slice(0, 8)} — <span data-testid={`labels-job-status-${job.id}`}>{statusLabel(job.data.state)}</span>
      </h3>
      {job.data.reprint_of_id && (
        <p className="muted">Reprint of {job.data.reprint_of_id.slice(0, 8)}: {job.data.reason_code}</p>
      )}

      <div className="labels-sheet" data-testid={`labels-sheet-${job.id}`}>
        {job.data.labels.flatMap((label) =>
          Array.from({ length: label.copies }, (_, copy) => (
            // `ptmapper.goods_barcode.render_label_svg` builds this itself from
            // numeric bar geometry it computed and `html.escape()`d alias/MRP
            // text - unlike Mail.tsx's rejected use of the same API for a third
            // party's raw HTML, there is no uploaded or attacker-shaped markup
            // here to sanitise against (design §8.4: "server-generated ...
            // sanitised, not uploaded markup").
            // eslint-disable-next-line react/no-danger
            <div
              key={`${label.official_line_id}-${copy}`}
              className="labels-label"
              dangerouslySetInnerHTML={{ __html: label.svg }}
            />
          )),
        )}
      </div>
      <button
        className="btn btn-sm"
        disabled={outcomeRecorded}
        onClick={printInBrowser}
        data-testid={`labels-browser-print-${job.id}`}
      >
        <Printer size={14} /> Print in browser
      </button>
      <p className="muted">
        {outcomeRecorded
          ? "This job's printer outcome is recorded. Reprint the missing copies as a new, " +
            "reasoned job rather than printing this one again."
          : "This browser dialog only proves an attempt. Record what the printer actually did below."}
      </p>

      <Feedback error={error} ok="" />
      <div className="form-grid">
        <Field id={`labels-outcome-${job.id}`} label="Printer outcome">
          <select
            id={`labels-outcome-${job.id}`}
            className="select"
            value={outcome}
            onChange={(e) => setOutcome(e.target.value as PrintOutcomeKind)}
            data-testid={`labels-outcome-${job.id}`}
          >
            {OUTCOME_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        {outcome === "failed" || outcome === "unknown" ? (
          <Field id={`labels-outcome-reason-${job.id}`} label="Reason">
            <input
              id={`labels-outcome-reason-${job.id}`}
              className="input"
              value={reasonCode}
              onChange={(e) => setReasonCode(e.target.value)}
              data-testid={`labels-outcome-reason-${job.id}`}
            />
          </Field>
        ) : null}
      </div>

      {allowsUsableCounts(outcome) && (
        <table className="table">
          <thead>
            <tr>
              <th>Alias</th>
              <th>Requested</th>
              <th>Usable copies</th>
            </tr>
          </thead>
          <tbody>
            {job.data.labels.map((label) => (
              <tr key={label.official_line_id}>
                <td className="mono">{label.alias_as_used}</td>
                <td>{label.copies}</td>
                <td>
                  <input
                    type="number"
                    min={0}
                    max={label.copies}
                    className="input input-sm"
                    value={usable[label.official_line_id] ?? ""}
                    onChange={(e) =>
                      setUsable((prev) => ({ ...prev, [label.official_line_id]: e.target.value }))
                    }
                    data-testid={`labels-usable-${job.id}-${label.official_line_id}`}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <button
        className="btn btn-cta"
        disabled={busy}
        onClick={recordOutcome}
        data-testid={`labels-outcome-submit-${job.id}`}
      >
        Record outcome
      </button>

      {last && (
        <p className="muted" data-testid={`labels-last-outcome-${job.id}`}>
          Last recorded: {statusLabel(last.outcome)} at{" "}
          {new Date(last.recorded_at).toLocaleString()}
        </p>
      )}

      <h4 className="h4">Scan one label to verify</h4>
      <p className="muted">
        Read one printed label back. The server checks it against the alias it froze for that
        line. This proves the one sample you scanned, not the copies you asked for, so it never
        fills in a usable count.
      </p>
      <Feedback error={scanError} ok={scanOk} />
      <div className="form-grid">
        <Field id={`labels-scan-alias-${job.id}`} label="Scanned alias">
          <input
            id={`labels-scan-alias-${job.id}`}
            className="input"
            value={scanAlias}
            onChange={(e) => setScanAlias(e.target.value)}
            data-testid={`labels-scan-alias-${job.id}`}
          />
        </Field>
        <Field id={`labels-scan-line-${job.id}`} label="The line it should match">
          <select
            id={`labels-scan-line-${job.id}`}
            className="select"
            value={scanLineId}
            onChange={(e) => setScanLineId(e.target.value)}
            data-testid={`labels-scan-line-${job.id}`}
          >
            {job.data.labels.map((label) => (
              <option key={label.official_line_id} value={label.official_line_id}>
                {label.alias_as_used}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <button
        className="btn btn-sm"
        disabled={scanBusy || scanAlias.trim() === "" || scanLineId === ""}
        onClick={recordScan}
        data-testid={`labels-scan-submit-${job.id}`}
      >
        Verify the scan
      </button>
      {verifiedScans(job.data).length > 0 && (
        <ul className="muted" data-testid={`labels-scan-verified-${job.id}`}>
          {verifiedScans(job.data).map((event) => (
            <li key={event.id}>
              Scan verified: <span className="mono">{event.scanned_alias}</span> at{" "}
              {new Date(event.recorded_at).toLocaleString()}
            </li>
          ))}
        </ul>
      )}

      {canReprint && (
        <button
          className="btn btn-sm"
          onClick={() => setReprinting((v) => !v)}
          data-testid={`labels-reprint-toggle-${job.id}`}
        >
          <RotateCcw size={14} /> Reprint the missing copies
        </button>
      )}
      {reprinting && (
        <PrintForm
          ptVersionId={job.data.pt_version_id}
          lines={job.data.labels.map((label) => ({
            line_key: label.official_line_id,
            official_line_id: label.official_line_id,
            sku_id: null,
            attributes: [],
            season_id: null,
            alias_id: null,
            alias_as_used: label.alias_as_used,
            qty: label.copies,
            hsn: null,
            source_ref: null,
            coverage_requests: [],
            supplied: { mrp_paise: label.mrp_paise },
            calculated: undefined,
            reviewed: false,
          }))}
          loadingLines={false}
          templates={
            job.data.template_version_id
              ? [{ versionId: job.data.template_version_id, label: "Same profile", copiesLimit: 9999 }]
              : []
          }
          templatesLoading={false}
          onCreated={(created) => {
            onReprint(created);
            setReprinting(false);
          }}
          reprintOf={reprintOf}
        />
      )}
    </div>
  );
}
