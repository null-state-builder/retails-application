// Governed configuration (ticket 04): the profile versions, formula direction,
// rates, slabs, tax rules, reason lists, document series and label settings a
// tenant runs on, each drafted, submitted and approved by a second person
// (design E085-E089, E234; change PRD §14.4 and §14.7).
//
// Three things this screen exists to keep honest:
//   * a line's own draft state is not the state of what is in force, so every
//     version is listed with its real state and dates;
//   * an overlap refusal names the exact version it clashes with, beside the
//     date field that caused it, not in a banner somewhere else;
//   * a backdated change lists the documents it reaches and will not submit
//     until that list has actually been read.
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { CalendarClock, ClipboardCheck, Save, Send, Sparkles, X } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import {
  Denied,
  Feedback,
  Field,
  hold,
  listState,
  useGoodsFetch,
  useResourceDoc,
  useResourceList,
  useStepUp,
  type Page,
} from "../lib/goodsScreen";
import {
  backdateBlocksSubmit,
  CONFIG_KINDS,
  CONFIG_KIND_LABEL,
  TENANT_SCOPE,
  type ConfigData,
  type VersionState,
} from "../lib/goodsConfig";
import {
  BarcodeRangeEditor,
  LabelEditor,
  RatesEditor,
  ReadOnlyPayload,
  ReasonsEditor,
  SeriesEditor,
  TaxRatesEditor,
  VocabularyEditor,
  type Payload,
} from "./configurationPayloads";
import { ProfileWizard } from "./ProfileWizard";
import { IdentityProfileEditor, PtProfileEditor } from "./ProfileWizard";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { formatDateTime } from "../lib/format";
import "./Configuration.css";

/** One approval request as E169/E170 answer it (design §6.1 ApprovalDTO). */
export interface ApprovalRow {
  id: string;
  subject_id: string;
  subject_kind: string;
  reviewed_hash: string;
  state: string;
  maker: { id: string; name: string };
  requested_action: string;
  title: string;
  requested_at: string | null;
}

interface EntityData {
  code: string;
  name: string;
}

/** The kinds this screen offers a starting draft for. Anything else is listed
 *  and readable; only the ones with a form can be drafted here. */
const EDITABLE_KINDS = new Set([
  "vocabulary",
  "identity_profile",
  "profile",
  "rates",
  "tax_rates",
  "reasons",
  "series",
  "label",
  "barcode_range",
]);

const BLANK_PAYLOAD: Record<string, Payload> = {
  rates: { transport_pct: "0.00", pricing_margin_pct: "0.00" },
  tax_rates: { currency: "INR", hsn_rules: [] },
  vocabulary: { dimension: "", values: [], effective_from: new Date().toISOString() },
  identity_profile: {
    family: "",
    distinguishing_dimensions: [],
    size_dimension: "",
    colour_dimension: null,
    grade_dimension: null,
    allowed_size_values: [],
    allowed_colour_values: [],
    allowed_grade_values: [],
  },
  reasons: { action: "", codes: [] },
  series: { entity_id: "", type: "", fy: "", ceiling_block_size: 1000 },
  label: {
    width_mm: 38,
    height_mm: 25,
    symbology: "code128",
    copies_limit: 100,
    fields: ["barcode", "mrp"],
    printer_dpi: 203,
    min_module_dots: 2,
    max_payload_characters: 32,
  },
  barcode_range: { issuer: "", prefix: "", start: 1, end: 100000, symbology: "code128" },
  profile: {},
};

export function versionChip(version: VersionState): ReactNode {
  const tone =
    version.state === "effective"
      ? "green"
      : version.state === "scheduled"
        ? "amber"
        : version.state === "ended"
          ? "grey"
          : "red";
  return <span className={`chip chip-${tone}`}>{version.state}</span>;
}

function draftChip(state: string): ReactNode {
  const tone = state === "approved" ? "green" : state === "submitted" ? "amber" : "grey";
  return <span className={`chip chip-${tone}`}>{state}</span>;
}

/** An RFC3339 instant as a `datetime-local` control wants it, and back. */
function toLocalInput(iso: string): string {
  const at = new Date(iso);
  const offset = at.getTimezoneOffset() * 60000;
  return new Date(at.getTime() - offset).toISOString().slice(0, 16);
}

// --------------------------------------------------------------------------
// One configuration line: its versions, its draft, its submission
// --------------------------------------------------------------------------

function VersionsTable({ versions }: { versions: VersionState[] }) {
  if (versions.length === 0) {
    return (
      <p className="muted" data-testid="config-no-versions">
        Nothing approved yet, so no version of this is in force.
      </p>
    );
  }
  return (
    <div className="table-wrap">
      <table className="data" data-testid="config-versions">
        <thead>
          <tr>
            <th>Version</th>
            <th>State</th>
            <th>From</th>
            <th>Until</th>
            <th>Withdrawn</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((version) => (
            <tr key={version.id} data-testid={`config-version-${version.version}`}>
              <td>
                <b>version {version.version}</b>
              </td>
              <td>{versionChip(version)}</td>
              <td>{formatDateTime(version.effective_from)}</td>
              <td>{version.effective_to ? formatDateTime(version.effective_to) : "Open-ended"}</td>
              <td>{version.withdrawn_at ? (version.withdrawn_reason ?? "Withdrawn") : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The impact of a backdated start, which has to be read before it can be sent
 *  (change PRD §14.5 M5). Every item is ticked one at a time — a "tick them
 *  all" button would be a way of not reading them. */
function ImpactPanel({
  impact,
  reviewed,
  onReview,
}: {
  impact: { resource_key: string; effect: string; official_unchanged: boolean }[];
  reviewed: Set<string>;
  onReview: (next: Set<string>) => void;
}) {
  return (
    <div className="card section-card cfg-impact" data-testid="config-impact">
      <h4 className="h4">
        <CalendarClock size={15} /> This start is in the past
      </h4>
      <p className="lead">
        These documents were recorded on or after that moment. Read each one, then tick it. Official
        documents keep the configuration version they used — nothing here rewrites them.
      </p>
      {impact.length === 0 ? (
        <p className="muted" data-testid="config-impact-empty">
          No document falls inside that period.
        </p>
      ) : (
        <ul className="cfg-impact-list">
          {impact.map((item) => (
            <li key={item.resource_key}>
              <label htmlFor={`impact-${item.resource_key}`}>
                <input
                  id={`impact-${item.resource_key}`}
                  type="checkbox"
                  checked={reviewed.has(item.resource_key)}
                  onChange={(e) => {
                    const next = new Set(reviewed);
                    if (e.target.checked) next.add(item.resource_key);
                    else next.delete(item.resource_key);
                    onReview(next);
                  }}
                  data-testid={`impact-${item.resource_key}`}
                />
                <span>
                  <b className="mono">{item.resource_key}</b> — {item.effect}
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function PayloadForm({
  kind,
  value,
  onChange,
  entities,
}: {
  kind: string;
  value: Payload;
  onChange: (next: Payload) => void;
  entities: { id: string; name: string }[];
}) {
  switch (kind) {
    case "rates":
      return <RatesEditor value={value} onChange={onChange} />;
    case "tax_rates":
      return <TaxRatesEditor value={value} onChange={onChange} />;
    case "vocabulary":
      return <VocabularyEditor value={value} onChange={onChange} />;
    case "identity_profile":
      return <IdentityProfileEditor value={value} onChange={onChange} />;
    case "profile":
      return <PtProfileEditor value={value} onChange={onChange} />;
    case "reasons":
      return <ReasonsEditor value={value} onChange={onChange} />;
    case "series":
      return <SeriesEditor value={value} onChange={onChange} entities={entities} />;
    case "label":
      return <LabelEditor value={value} onChange={onChange} />;
    case "barcode_range":
      return <BarcodeRangeEditor value={value} onChange={onChange} />;
    default:
      return <ReadOnlyPayload value={value} kind={CONFIG_KIND_LABEL[kind] ?? kind} />;
  }
}

function DraftPanel({
  draftId,
  kind,
  onSaved,
  onClose,
}: {
  draftId: string | null;
  kind: string;
  onSaved: (id: string) => void;
  onClose: () => void;
}) {
  const { session } = useAuth();
  const canDraft = hold(session, "config.draft");
  const doc = useResourceDoc<ConfigData>(
    draftId ? `/goods-v1/masters/configurations/${draftId}` : null,
  );
  // Only the series form names a legal entity, and only an entity manager may
  // read the list — fetching it for every kind would make a configuration
  // drafter's every draft screen issue a refusal they cannot act on.
  const entities = useResourceList<EntityData>(
    kind === "series" ? "/goods-v1/masters/entities" : null,
  );
  const [payload, setPayload] = useState<Payload>(BLANK_PAYLOAD[kind] ?? {});
  const [from, setFrom] = useState(toLocalInput(new Date().toISOString()));
  const [until, setUntil] = useState("");
  const [reviewed, setReviewed] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [dateError, setDateError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const stepUp = useStepUp();
  const loaded = doc.doc;

  useEffect(() => {
    if (!loaded) return;
    setPayload(loaded.data.payload);
    setFrom(toLocalInput(loaded.data.effective_from));
    setUntil(loaded.data.effective_to ? toLocalInput(loaded.data.effective_to) : "");
  }, [loaded]);

  const backdate = loaded?.data.backdate;
  const impact = backdate?.impact ?? [];
  const needsReview = Boolean(backdate?.is_backdated);
  const blocked = backdateBlocksSubmit(backdate, reviewed);

  function applyRefusal(e: unknown) {
    const details = (
      e as {
        response?: { data?: { details?: { issues?: { field?: string; message?: string }[] } } };
      }
    )?.response?.data?.details;
    const dateIssue = (details?.issues ?? []).find(
      (issue) => issue.field === "effective_from" || issue.field === "effective_to",
    );
    setDateError(dateIssue?.message ? `This ${dateIssue.message}.` : "");
    setError(apiErrorMessage(e));
  }

  async function save() {
    setError("");
    setDateError("");
    setOk("");
    setBusy(true);
    const dates = {
      effective_from: new Date(from).toISOString(),
      effective_to: until ? new Date(until).toISOString() : null,
    };
    try {
      if (loaded) {
        await stepUp.guarded(() =>
          api.patch(`/goods-v1/masters/configurations/${loaded.id}`, {
            payload,
            ...dates,
            ...goodsMeta(loaded.revision),
          }),
        );
        setOk("Draft saved.");
        doc.reload();
      } else {
        const { data } = await stepUp.guarded(() =>
          api.post<{ id: string }>("/goods-v1/masters/configurations", {
            kind,
            scope: TENANT_SCOPE,
            payload,
            ...dates,
            ...goodsMeta(),
          }),
        );
        setOk("Draft saved.");
        onSaved(data.id);
      }
    } catch (e) {
      applyRefusal(e);
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!loaded) return;
    setError("");
    setDateError("");
    setOk("");
    setBusy(true);
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/masters/configurations/${loaded.id}/submit`, {
          reviewed_hash: loaded.content_hash,
          ...goodsMeta(loaded.revision),
        }),
      );
      setOk("Sent for approval. Someone other than you has to approve it.");
      doc.reload();
    } catch (e) {
      applyRefusal(e);
    } finally {
      setBusy(false);
    }
  }

  if (doc.denied) return <Denied what="configuration" />;
  if (draftId && doc.loading) {
    return <p className="muted">Loading…</p>;
  }

  const submitBlocked = !loaded || blocked;

  return (
    <div data-testid="config-draft">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <h3 className="h3">
          {CONFIG_KIND_LABEL[kind] ?? kind} {loaded ? draftChip(loaded.state) : draftChip("draft")}
        </h3>
        <div className="spacer" />
        <button className="btn btn-sm" onClick={onClose} data-testid="config-draft-close">
          <X size={14} /> Close
        </button>
      </div>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {!canDraft && (
        <p className="warn-note">You may read this configuration, but not draft a change to it.</p>
      )}
      {loaded && <VersionsTable versions={loaded.data.versions ?? []} />}
      <div className="card section-card">
        <div className="form-grid wide-form">
          <Field
            id="config-from"
            label="In force from"
            hint={dateError || (backdate?.blocked ? backdate.blocked.message : undefined)}
          >
            <input
              id="config-from"
              className="input"
              type="datetime-local"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
              aria-invalid={Boolean(dateError || backdate?.blocked)}
              aria-describedby="config-from-hint"
              data-testid="config-from"
            />
          </Field>
          <Field id="config-until" label="Until (optional)" hint="Leave empty for open-ended.">
            <input
              id="config-until"
              className="input"
              type="datetime-local"
              value={until}
              onChange={(e) => setUntil(e.target.value)}
              data-testid="config-until"
            />
          </Field>
        </div>
        {(dateError || backdate?.blocked) && (
          <p className="warn-note" data-testid="config-date-refusal">
            {dateError || backdate?.blocked?.message}
            {(backdate?.blocked?.issues ?? []).map((issue) => ` ${issue.message}.`).join("")}
          </p>
        )}
      </div>
      <div className="card section-card">
        <PayloadForm
          kind={kind}
          value={payload}
          onChange={setPayload}
          entities={entities.items.map((e) => ({ id: e.id, name: e.data.name }))}
        />
      </div>
      {needsReview && <ImpactPanel impact={impact} reviewed={reviewed} onReview={setReviewed} />}
      {canDraft && (
        <div className="toolbar">
          <button className="btn btn-cta" onClick={save} disabled={busy} data-testid="config-save">
            <Save size={15} /> Save draft
          </button>
          <button
            className="btn"
            onClick={submit}
            disabled={busy || submitBlocked}
            data-testid="config-submit"
          >
            <Send size={15} /> Send for approval
          </button>
          {blocked && !backdate?.blocked && (
            <span className="goods-hint" data-testid="config-submit-blocked">
              Tick every affected document above before this can be sent.
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function KindPanel({
  kind,
  onOpen,
  onNew,
}: {
  kind: string;
  onOpen: (id: string) => void;
  onNew: () => void;
}) {
  const { session } = useAuth();
  const canDraft = hold(session, "config.draft");
  const lines = useResourceList<ConfigData>(
    `/goods-v1/masters/configurations?kind=${encodeURIComponent(kind)}`,
  );

  if (lines.denied) return <Denied what="configuration" />;
  const state = listState(
    { loading: lines.loading, failure: lines.failure, empty: lines.items.length === 0 },
    "Nothing set up here yet.",
  );

  return (
    <div data-testid="config-kind-panel">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <h3 className="h3">{CONFIG_KIND_LABEL[kind] ?? kind}</h3>
        <div className="spacer" />
        {canDraft && EDITABLE_KINDS.has(kind) && (
          <button className="btn btn-cta" onClick={onNew} data-testid="config-new">
            Start a change
          </button>
        )}
      </div>
      {state ? (
        <p data-testid="config-kind-state">{state}</p>
      ) : (
        lines.items.map((line) => (
          <div className="card section-card" key={line.id} data-testid={`config-line-${line.id}`}>
            <div className="toolbar" style={{ marginBottom: 8 }}>
              <span>
                Draft {draftChip(line.state)} · in force from{" "}
                {formatDateTime(line.data.effective_from)}
              </span>
              <div className="spacer" />
              <button
                className="btn btn-sm"
                onClick={() => onOpen(line.id)}
                data-testid={`config-open-${line.id}`}
              >
                Open
              </button>
            </div>
            <VersionsTable versions={line.data.versions ?? []} />
          </div>
        ))
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Approvals of configuration changes
// --------------------------------------------------------------------------

function ApprovalsPanel() {
  const { session } = useAuth();
  const me = session?.user.human_id ?? "";
  const mine = useGoodsFetch<Page<ApprovalRow>, ApprovalRow[]>(
    "/goods-v1/approvals",
    (r) => (r.items ?? []).filter((row) => row.subject_kind === "configuration"),
    [],
  );
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const stepUp = useStepUp();

  async function decide(row: ApprovalRow, decision: "approve" | "reject") {
    setError("");
    setOk("");
    setBusy(true);
    try {
      // E234 is revision-bound and `ApprovalDTO` carries no revision of its own.
      // For a configuration the subject's current revision *is* the request's:
      // editing the draft supersedes any pending request (E087 step 10), so a
      // request that is still pending was made at the revision the draft is on.
      const draftId = row.subject_id.replace("configdraft:", "");
      const { data } = await api.get<{ revision: number }>(
        `/goods-v1/masters/configurations/${draftId}`,
      );
      await stepUp.guarded(() =>
        api.post(`/goods-v1/approvals/${row.id}/decide`, {
          decision,
          reviewed_hash: row.reviewed_hash,
          ...(decision === "reject" ? { reason_code: "NOT_RIGHT" } : {}),
          ...goodsMeta(data.revision),
        }),
      );
      setOk(decision === "approve" ? "Approved and in force." : "Sent back to the drafter.");
      mine.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const pending = mine.value.filter((row) => row.state === "pending");
  const state = listState(
    { loading: mine.loading, failure: mine.failure, empty: pending.length === 0 },
    "No configuration change is waiting.",
  );

  return (
    <div data-testid="config-approvals">
      <h3 className="h3">
        <ClipboardCheck size={15} /> Configuration changes waiting
      </h3>
      <p className="lead">
        A configuration change is approved by someone other than the person who prepared it. That is
        a fixed rule; no setting relaxes it.
      </p>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {state ? (
        <p data-testid="config-approvals-state">{state}</p>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="config-approvals-table">
            <thead>
              <tr>
                <th>What</th>
                <th>Prepared by</th>
                <th>Asked</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {pending.map((row) => {
                const ownWork = row.maker.id === me;
                return (
                  <tr key={row.id} data-testid={`config-approval-${row.id}`}>
                    <td>{row.title}</td>
                    <td>{ownWork ? `${row.maker.name} (you)` : row.maker.name}</td>
                    <td>{row.requested_at ? formatDateTime(row.requested_at) : "—"}</td>
                    <td>
                      {ownWork ? (
                        <span className="warn-note" data-testid={`config-approval-self-${row.id}`}>
                          You prepared this, so you cannot approve it. Someone else has to.
                        </span>
                      ) : (
                        <>
                          <button
                            className="btn btn-cta btn-sm"
                            onClick={() => decide(row, "approve")}
                            disabled={busy}
                            data-testid={`config-approve-${row.id}`}
                          >
                            Approve
                          </button>
                          <button
                            className="btn btn-sm"
                            onClick={() => decide(row, "reject")}
                            disabled={busy}
                            data-testid={`config-reject-${row.id}`}
                          >
                            Send back
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// The page
// --------------------------------------------------------------------------

export function ConfigurationPage() {
  const [params, setParams] = useSearchParams();
  const view = params.get("view");
  const kind = params.get("kind") ?? "profile";
  const draftId = params.get("draft");
  const isNew = params.get("draft") === "new";
  const kinds = useMemo(() => CONFIG_KINDS.filter((k) => k !== "business_profile"), []);

  return (
    <div className="page-pad">
      <PageHeader />
      <div className="org-layout cfg-layout">
        <nav className="org-nav" aria-label="Configuration">
          <div className="org-nav-group">
            <h4>Guided setup</h4>
            <ul className="org-nav-list">
              <li>
                <button
                  className={`org-nav-item ${view === "wizard" ? "active" : ""}`}
                  onClick={() => setParams({ view: "wizard" })}
                  data-testid="cfg-nav-wizard"
                >
                  <Sparkles size={15} /> Set up a PT profile
                </button>
              </li>
              <li>
                <button
                  className={`org-nav-item ${view === "approvals" ? "active" : ""}`}
                  onClick={() => setParams({ view: "approvals" })}
                  data-testid="cfg-nav-approvals"
                >
                  <ClipboardCheck size={15} /> Changes waiting
                </button>
              </li>
            </ul>
          </div>
          <div className="org-nav-group">
            <h4>Configuration</h4>
            <ul className="org-nav-list">
              {kinds.map((item) => (
                <li key={item}>
                  <button
                    className={`org-nav-item ${!view && kind === item ? "active" : ""}`}
                    onClick={() => setParams({ kind: item })}
                    data-testid={`cfg-nav-${item}`}
                  >
                    {CONFIG_KIND_LABEL[item] ?? item}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </nav>
        <div className="org-detail">
          {view === "wizard" && <ProfileWizard />}
          {view === "approvals" && <ApprovalsPanel />}
          {!view && draftId && (
            <DraftPanel
              draftId={isNew ? null : draftId}
              kind={kind}
              onSaved={(id) => setParams({ kind, draft: id })}
              onClose={() => setParams({ kind })}
            />
          )}
          {!view && !draftId && (
            <KindPanel
              kind={kind}
              onOpen={(id) => setParams({ kind, draft: id })}
              onNew={() => setParams({ kind, draft: "new" })}
            />
          )}
        </div>
      </div>
    </div>
  );
}
