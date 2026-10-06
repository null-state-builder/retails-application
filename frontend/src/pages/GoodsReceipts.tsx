// Goods receipts — the GRN detail and its discrepancy panel (ticket 05).
//
// Since OPS-17 (store and warehouse operations PRD §5.1) not a screen of its
// own: `GrnPanel` is the delivery workflow's GRN step and `DispositionPanel` its
// Discrepancies step (`ReceiveDelivery.tsx`). A link to a GRN opens its delivery.
//
// The GRN is the quantity truth of one arrival. The panel puts four things
// beside it and lets an authorised person act on the last:
//
//   * the invoice comparison — what the vendor claimed against what was
//     counted, paired the one way the server pairs it, mismatches marked;
//   * the count history — the issued count and every counter-GRN since,
//     including one still waiting for its approver;
//   * live PT coverage — covered, held and disposed per line, so it is visible
//     that a held piece is not on a PT and cannot be put on one;
//   * the discrepancy panel — every held quantity with the reason it is held
//     and the owner and due date of the exception the receiving commands
//     already raised. Nothing here raises or closes one.
//
// A counter-GRN raises, lowers or recounts the condition of a line and goes to
// a distinct approver. Lowering below what a live PT already covers is refused
// by the server, and the refusal is shown on the line that caused it.
//
// A disposition decides what happens to disputed goods. Return and dispose
// consume the held custody without inventing a SKU, a cost or a stock layer,
// and the form says so before it is submitted.
//
// Damage is not a disposition (ticket 05C). Pieces counted damaged are held and
// reported the moment the GRN is issued; damage found later, before the PT, is
// reported here (E254) and goes to quarantine at once. Either way a different
// person decides the report in the common damage review, which this screen
// links to rather than restating.
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ClipboardCheck,
  FileText,
  History,
  Scale,
  ShieldAlert,
  X,
} from "lucide-react";

import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import {
  Denied,
  Feedback,
  Field,
  hold,
  listState,
  useGoodsFetch,
  useResourceDoc,
  useStepUp,
  type Page,
  type ResourceDTO,
} from "../lib/goodsScreen";
import {
  CONDITIONS,
  CONDITION_LABEL,
  coverableQty,
  DISPOSITION_KINDS,
  dispositionKindsFor,
  dispositionLines,
  excessByLine,
  exceptionsFor,
  heldReasons,
  NOT_GIVEN,
  orNotGiven,
  siteName,
  type Comparison,
  type Condition,
  type ExceptionRow,
  type DispositionHistory,
  type DispositionKind,
  type DispositionRequest,
  type GrnCoverage,
  type GrnDamageReport,
  type GrnLine,
} from "../lib/goodsReceiving";
import { DAMAGE_SOURCE_LABEL, DAMAGE_STATE_LABEL } from "../lib/goodsMovements";
import { NO_DUE_DATE } from "../lib/goodsExceptions";
import { useAuth } from "../auth/AuthContext";
import { formatDateTime, formatPaiseString } from "../lib/format";
import { rupeesToPaiseString } from "../lib/goodsPt";
import { receiptShortages } from "../lib/goodsReceiptShortage";
import { ThreeWayPanel, threeWayOn } from "./ThreeWayMatch";
import "./GoodsReceiving.css";

interface NamedMaster {
  code: string;
  name: string;
}

/** One approval request as E169/E170 answer it (design §6.1 ApprovalDTO). */
interface ApprovalRow {
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

function useNames(url: string) {
  const list = useGoodsFetch<Page<ResourceDTO<NamedMaster>>, ResourceDTO<NamedMaster>[]>(
    url,
    (r) => r.items ?? [],
    [],
  );
  const name = (id: string | null | undefined) => {
    if (!id) return NOT_GIVEN;
    const found = list.value.find((row) => row.id === String(id));
    return found ? `${found.data.name} (${found.data.code})` : `#${id}`;
  };
  return { ...list, name };
}

/** A line's identity in one phrase: the SKU if it resolved, otherwise the words
 *  the counter used. An unidentified piece is never given a stand-in SKU. */
function lineName(line: GrnLine): string {
  if (line.identity.sku_id)
    return line.identity.description || `SKU ${line.identity.sku_id.slice(0, 8)}`;
  return line.identity.description || line.identity.raw_alias || "Described only by its condition";
}

// --------------------------------------------------------------------------
// Counter-GRN (E118)
// --------------------------------------------------------------------------

interface Issue {
  code: string;
  line_key?: string | null;
  message?: string;
  quantity?: number | null;
}

function CounterForm({ grn, onDone }: { grn: ResourceDTO<GrnCoverage>; onDone: () => void }) {
  const lines = grn.data.lines.items;
  const [lineKey, setLineKey] = useState(lines[0]?.line_key ?? "");
  const [qty, setQty] = useState(String(lines[0]?.counted_qty ?? 0));
  const [condition, setCondition] = useState<Condition>(lines[0]?.condition ?? "good");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const [issues, setIssues] = useState<Issue[]>([]);

  const line = lines.find((row) => row.line_key === lineKey) ?? null;

  function pick(key: string) {
    setLineKey(key);
    setIssues([]);
    const chosen = lines.find((row) => row.line_key === key);
    if (chosen) {
      setQty(String(chosen.counted_qty));
      setCondition(chosen.condition);
    }
  }

  async function submit() {
    setError("");
    setOk("");
    setIssues([]);
    setBusy(true);
    try {
      await api.post(`/goods-v1/inbound/grns/${grn.id}/counter`, {
        corrections: [
          {
            line_key: lineKey,
            new_qty: Number(qty),
            new_condition: condition,
            reason_code: reason,
          },
        ],
        ...goodsMeta(grn.revision),
      });
      setOk("Counter-GRN raised. A second person has to approve it before the count moves.");
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
      const details = (e as { response?: { data?: { details?: { issues?: Issue[] } } } })?.response
        ?.data?.details?.issues;
      setIssues(details ?? []);
    } finally {
      setBusy(false);
    }
  }

  const lineIssues = issues.filter((problem) => !problem.line_key || problem.line_key === lineKey);

  return (
    <div className="gr-panel" data-testid="gg-counter">
      <h4 className="gr-h4">Correct the count</h4>
      <p className="gr-hint">
        A counter-GRN raises a line, lowers it, or says the pieces are in a different condition than
        first recorded. It never edits the original count — that stays readable — and it takes
        effect only once a second person approves it.
      </p>
      <Feedback error={error} ok={ok} />
      <div className="form-grid">
        <Field id="gg-c-line" label="Line">
          <select
            id="gg-c-line"
            className="select"
            value={lineKey}
            onChange={(e) => pick(e.target.value)}
            data-testid="gg-c-line"
          >
            {lines.map((row) => (
              <option key={row.line_key} value={row.line_key}>
                {lineName(row)} — {row.counted_qty} {CONDITION_LABEL[row.condition].toLowerCase()}
              </option>
            ))}
          </select>
        </Field>
        <Field
          id="gg-c-qty"
          label="New counted quantity"
          hint={
            line
              ? `${line.covered_qty} piece(s) on this line are already covered by a live PT and cannot be removed.`
              : undefined
          }
        >
          <input
            id="gg-c-qty"
            className="input"
            type="number"
            min={0}
            aria-describedby="gg-c-qty-hint"
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            data-testid="gg-c-qty"
          />
        </Field>
        <Field id="gg-c-condition" label="Condition">
          <select
            id="gg-c-condition"
            className="select"
            value={condition}
            onChange={(e) => setCondition(e.target.value as Condition)}
            data-testid="gg-c-condition"
          >
            {CONDITIONS.map((value) => (
              <option key={value} value={value}>
                {CONDITION_LABEL[value]}
              </option>
            ))}
          </select>
        </Field>
        <Field id="gg-c-reason" label="Reason">
          <input
            id="gg-c-reason"
            className="input"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            data-testid="gg-c-reason"
          />
        </Field>
      </div>
      {lineIssues.length > 0 && (
        <div className="warn-note" data-testid="gg-c-refusal">
          {lineIssues.map((problem, index) => (
            <p key={index}>{problem.message ?? problem.code}</p>
          ))}
        </div>
      )}
      <button
        className="btn btn-cta btn-sm"
        onClick={submit}
        disabled={busy || !reason || !lineKey}
        data-testid="gg-c-submit"
      >
        Raise counter-GRN
      </button>
    </div>
  );
}

// --------------------------------------------------------------------------
// Disposition (E119)
// --------------------------------------------------------------------------

/** Reuse of the GRN's own invoice attachment, offered for one evidence field.
 *
 *  Damage, source value and tax are three separate proofs (GSA-T09): a checker has to
 *  see which attachment proves which fact, so each field is filled by its own choice
 *  rather than by one click that quietly claims the same file proves all three. */
function ReuseInvoice({
  id,
  onUse,
  testId,
}: {
  id: string;
  onUse: (evidenceId: string) => void;
  testId: string;
}) {
  if (!id) return null;
  return (
    <button type="button" className="btn btn-sm" onClick={() => onUse(id)} data-testid={testId}>
      Use this GRN&apos;s invoice attachment
    </button>
  );
}

function DispositionForm({
  grn,
  onDone,
  kinds = DISPOSITION_KINDS,
}: {
  grn: ResourceDTO<GrnCoverage>;
  onDone: () => void;
  /** The kinds this person may ask for (`dispositionKindsFor`). */
  kinds?: typeof DISPOSITION_KINDS;
}) {
  // Pieces no PT covers; someone who may only ask to keep wrong goods is
  // offered only those lines (`dispositionLines`).
  const held = dispositionLines(kinds, grn.data.lines.items);
  const [kind, setKind] = useState<DispositionKind>(kinds[0]?.kind ?? "hold_excess");
  const [lineKey, setLineKey] = useState(held[0]?.line_key ?? "");
  const [qty, setQty] = useState("1");
  const [reason, setReason] = useState("");
  const [damageDescription, setDamageDescription] = useState("");
  const [damageEvidence, setDamageEvidence] = useState("");
  const [resolvedSku, setResolvedSku] = useState("");
  const [costEvidence, setCostEvidence] = useState("");
  const [taxEvidence, setTaxEvidence] = useState("");
  const [costRupees, setCostRupees] = useState("");
  const [mrpRupees, setMrpRupees] = useState("");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);

  const chosen = kinds.find((entry) => entry.kind === kind) ?? kinds[0];
  const line = held.find((row) => row.line_key === lineKey) ?? null;
  const valueDamage = kind === "value_damage";
  // Ticket 05D: keeping wrong or unidentified goods needs the same identity and
  // source value and tax evidence as keeping good excess (R-REC-005).
  const accepting = kind === "accept_excess" || kind === "accept_wrong";
  const naming = kind === "resolve_identity";
  const evidenced = valueDamage || accepting;
  const effectiveSku = resolvedSku || line?.identity.sku_id || "";
  const invoiceEvidenceId = grn.data.invoice?.evidence_id ?? "";

  async function submit() {
    setError("");
    setOk("");
    const costPaise = rupeesToPaiseString(costRupees);
    const mrpPaise = rupeesToPaiseString(mrpRupees);
    if (valueDamage && (!costPaise || !mrpPaise)) {
      setError("Enter positive cost and MRP in rupees, with at most two decimal places.");
      return;
    }
    setBusy(true);
    try {
      // The request's own contract, off the generated client; `goodsMeta` adds
      // the command identity and the revision it was reviewed at.
      const body: Omit<DispositionRequest, keyof ReturnType<typeof goodsMeta>> = {
        kind,
        source_document_id: grn.id,
        source_line_key: lineKey,
        qty: Number(qty),
        reason_code: reason,
        ...(valueDamage
          ? {
              damage_description: damageDescription,
              evidence_ids: damageEvidence
                .split(",")
                .map((id) => id.trim())
                .filter(Boolean),
              resolved_sku_id: effectiveSku,
              approved_cost_evidence_id: costEvidence,
              approved_tax_evidence_id: taxEvidence,
              approved_cost_paise: costPaise ?? null,
              approved_mrp_paise: mrpPaise ?? null,
            }
          : accepting
            ? {
                resolved_sku_id: effectiveSku,
                approved_cost_evidence_id: costEvidence,
                approved_tax_evidence_id: taxEvidence,
              }
            : naming
              ? { resolved_sku_id: effectiveSku }
              : {}),
        reviewed_grn_hash: grn.content_hash,
      };
      const { data } = await api.post<ResourceDTO<unknown>>(
        `/goods-v1/inbound/grns/${grn.id}/dispositions`,
        { ...body, ...goodsMeta(grn.revision) },
      );
      setOk(
        data.state.startsWith("approval")
          ? "Sent to a checker. Someone other than you decides it; nothing has moved yet."
          : valueDamage
            ? "Valued at the approved MRP and still held in quarantine."
            : naming
              ? "Named. The pieces keep how they were counted and stay held until someone keeps them."
              : "Decided. The pieces have left their hold.",
      );
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (held.length === 0) {
    return (
      <div className="gr-panel" data-testid="gg-disposition">
        <h4 className="gr-h4">Decide what happens to disputed goods</h4>
        <p className="muted">Nothing on this receipt is held, so there is nothing to decide.</p>
      </div>
    );
  }

  return (
    <div className="gr-panel" data-testid="gg-disposition">
      <h4 className="gr-h4">
        <Scale size={15} /> Decide what happens to disputed goods
      </h4>
      <Feedback error={error} ok={ok} />
      <fieldset className="gr-conditions">
        <legend>What should happen</legend>
        {kinds.map((entry) => (
          <label key={entry.kind} className="gr-condition">
            <input
              type="radio"
              name="disposition-kind"
              checked={kind === entry.kind}
              onChange={() => setKind(entry.kind)}
              data-testid={`gg-d-kind-${entry.kind}`}
            />
            <span>
              <b>{entry.label}</b>
              <small>{entry.help}</small>
            </span>
          </label>
        ))}
      </fieldset>
      {chosen?.creates && (
        <p className="gr-hint" data-testid="gg-d-creates">
          {chosen.creates}
        </p>
      )}
      {chosen?.checker && (
        <p className="gr-hint" data-testid="gg-d-checker">
          This decision goes to a checker. It has to be someone other than you — you cannot approve
          your own.
        </p>
      )}
      <div className="form-grid">
        <Field id="gg-d-line" label="Which held pieces">
          <select
            id="gg-d-line"
            className="select"
            value={lineKey}
            onChange={(e) => setLineKey(e.target.value)}
            data-testid="gg-d-line"
          >
            {held.map((row) => (
              <option key={row.line_key} value={row.line_key}>
                {`${lineName(row)} — ${row.uncovered_qty} not on a PT (${CONDITION_LABEL[
                  row.condition
                ].toLowerCase()})`}
              </option>
            ))}
          </select>
        </Field>
        <Field
          id="gg-d-qty"
          label="How many"
          hint={line ? `${line.uncovered_qty} piece(s) on this line are not on a PT.` : undefined}
        >
          <input
            id="gg-d-qty"
            className="input"
            type="number"
            min={1}
            max={line?.uncovered_qty ?? undefined}
            aria-describedby="gg-d-qty-hint"
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            data-testid="gg-d-qty"
          />
        </Field>
        <Field id="gg-d-reason" label="Reason">
          <input
            id="gg-d-reason"
            className="input"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            data-testid="gg-d-reason"
          />
        </Field>
      </div>
      {valueDamage && (
        <div className="form-grid" data-testid="gg-d-value-damage-fields">
          <Field id="gg-d-damage-description" label="Damage description">
            <textarea
              id="gg-d-damage-description"
              className="input"
              value={damageDescription}
              onChange={(e) => setDamageDescription(e.target.value)}
              data-testid="gg-d-damage-description"
            />
          </Field>
          <Field
            id="gg-d-damage-evidence"
            label="Damage evidence IDs"
            hint="Comma-separated. A photo, signed inspection report or existing GRN attachment is accepted."
          >
            <input
              id="gg-d-damage-evidence"
              className="input"
              value={damageEvidence}
              onChange={(e) => setDamageEvidence(e.target.value)}
              data-testid="gg-d-damage-evidence"
            />
            <ReuseInvoice
              id={invoiceEvidenceId}
              onUse={setDamageEvidence}
              testId="gg-d-use-grn-evidence-damage"
            />
          </Field>
          <Field id="gg-d-cost" label="Evidenced unit cost (₹)">
            <input
              id="gg-d-cost"
              className="input"
              inputMode="decimal"
              value={costRupees}
              onChange={(e) => setCostRupees(e.target.value)}
              data-testid="gg-d-cost"
            />
          </Field>
          <Field id="gg-d-mrp" label="Evidenced ticket MRP (₹)">
            <input
              id="gg-d-mrp"
              className="input"
              inputMode="decimal"
              value={mrpRupees}
              onChange={(e) => setMrpRupees(e.target.value)}
              data-testid="gg-d-mrp"
            />
          </Field>
        </div>
      )}
      {/* Ticket 07B: accepting good excess needs its identity and its source value and
          tax evidence too (DispositionPayload), so the extra can go on a supplement PT. */}
      {naming && (
        <div className="form-grid" data-testid="gg-d-naming-fields">
          <Field id="gg-d-name-sku" label="The SKU these pieces really are">
            <input
              id="gg-d-name-sku"
              className="input"
              value={resolvedSku}
              onChange={(e) => setResolvedSku(e.target.value)}
              data-testid="gg-d-name-sku"
            />
          </Field>
        </div>
      )}
      {evidenced && (
        <div className="form-grid" data-testid="gg-d-evidence-fields">
          <Field id="gg-d-sku" label="Resolved SKU ID">
            <input
              id="gg-d-sku"
              className="input"
              value={effectiveSku}
              onChange={(e) => setResolvedSku(e.target.value)}
              data-testid="gg-d-sku"
            />
          </Field>
          <Field id="gg-d-cost-evidence" label="Source value evidence ID">
            <input
              id="gg-d-cost-evidence"
              className="input"
              value={costEvidence}
              onChange={(e) => setCostEvidence(e.target.value)}
              data-testid="gg-d-cost-evidence"
            />
            <ReuseInvoice
              id={invoiceEvidenceId}
              onUse={setCostEvidence}
              testId="gg-d-use-grn-evidence-cost"
            />
          </Field>
          <Field id="gg-d-tax-evidence" label="Tax basis evidence ID">
            <input
              id="gg-d-tax-evidence"
              className="input"
              value={taxEvidence}
              onChange={(e) => setTaxEvidence(e.target.value)}
              data-testid="gg-d-tax-evidence"
            />
            <ReuseInvoice
              id={invoiceEvidenceId}
              onUse={setTaxEvidence}
              testId="gg-d-use-grn-evidence-tax"
            />
          </Field>
        </div>
      )}
      <button
        className="btn btn-cta btn-sm"
        onClick={submit}
        disabled={
          busy ||
          !reason ||
          !lineKey ||
          (evidenced && (!effectiveSku || !costEvidence || !taxEvidence)) ||
          (naming && !resolvedSku) ||
          (valueDamage && (!damageDescription || !damageEvidence || !costRupees || !mrpRupees))
        }
        data-testid="gg-d-submit"
      >
        Request this decision
      </button>
    </div>
  );
}

// --------------------------------------------------------------------------
// Approvals waiting on this receipt
// --------------------------------------------------------------------------

function EvidenceRefs({ ids, testId }: { ids: string[]; testId: string }) {
  return (
    <span data-testid={testId}>
      {ids.map((id, index) => (
        <span key={id}>
          {index > 0 ? ", " : ""}
          <a href={`${api.defaults.baseURL}/goods-v1/files/${id}/download`}>{id.slice(0, 8)}</a>
        </span>
      ))}
    </span>
  );
}

function ValueDamageEvidence({
  row,
  compact = false,
}: {
  row: DispositionHistory;
  compact?: boolean;
}) {
  return (
    <dl
      className={compact ? "gr-approval-evidence" : "gr-facts"}
      data-testid={`gg-value-damage-${row.id}`}
    >
      <div>
        <dt>Damage evidence</dt>
        <dd>
          {row.damage_description ?? NOT_GIVEN} ·{" "}
          <EvidenceRefs ids={row.damage_evidence_ids} testId="gg-damage-evidence-shown" />
        </dd>
      </div>
      <div>
        <dt>Source value evidence</dt>
        <dd data-testid="gg-source-value-evidence-shown">
          {formatPaiseString(row.source_value_evidence.cost_paise)} cost ·{" "}
          {formatPaiseString(row.source_value_evidence.mrp_paise)} MRP ·{" "}
          {row.source_value_evidence.evidence_id ? (
            <EvidenceRefs
              ids={[row.source_value_evidence.evidence_id]}
              testId="gg-value-file-shown"
            />
          ) : (
            NOT_GIVEN
          )}
        </dd>
      </div>
      <div>
        <dt>Tax basis evidence</dt>
        <dd data-testid="gg-tax-evidence-shown">
          {row.tax_basis_evidence_id ? (
            <EvidenceRefs ids={[row.tax_basis_evidence_id]} testId="gg-tax-file-shown" />
          ) : (
            NOT_GIVEN
          )}
        </dd>
      </div>
      <div>
        <dt>{row.frozen_value_paise ? "Frozen value" : "Requested value"}</dt>
        <dd data-testid="gg-value-damage-value">
          {formatPaiseString(row.frozen_value_paise ?? row.requested_value_paise)} · {row.state}
        </dd>
      </div>
    </dl>
  );
}

/** Who may report damage on a receipt's goods: the common reporting grant, or the
 *  receiving decision grant that used to hold damage here (ticket 05C). */
function canReportDamage(session: Parameters<typeof hold>[0]): boolean {
  return hold(session, "movement.draft") || hold(session, "receipt.disposition.decide");
}

/** E254: damage found on this receipt's goods before their PT (ticket 05C).
 *
 *  The pieces go to quarantine the moment this is sent, and a pending report
 *  opens for a different person to decide. Nothing about the count changes, and
 *  the form offers only what the server says may still be reported: pieces that
 *  are here and on no PT. A missing piece is a shortage, not damage. */
function ReportDamageForm({ grn, onDone }: { grn: ResourceDTO<GrnCoverage>; onDone: () => void }) {
  const lines = grn.data.lines.items.filter((line) => line.damage_reportable_qty > 0);
  const [lineKey, setLineKey] = useState(lines[0]?.line_key ?? "");
  const [qty, setQty] = useState("1");
  const [reason, setReason] = useState("");
  const [evidence, setEvidence] = useState("");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const line = lines.find((row) => row.line_key === lineKey) ?? null;

  async function submit() {
    setError("");
    setOk("");
    setBusy(true);
    try {
      await api.post(`/goods-v1/inbound/grns/${grn.id}/damage-reports`, {
        source_line_key: lineKey,
        qty: Number(qty),
        reason_code: reason,
        evidence_ids: evidence ? [evidence.trim()] : [],
        reviewed_grn_hash: grn.content_hash,
        ...goodsMeta(grn.revision),
      });
      setOk(
        `Reported. ${qty} piece(s) are in quarantine now, waiting for a different person to review the report.`,
      );
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gr-panel" data-testid="gg-report-damage">
      <h4 className="gr-h4">
        <ShieldAlert size={15} /> Report damage
      </h4>
      <Feedback error={error} ok={ok} />
      <p className="gr-hint">
        Damaged pieces go to quarantine as soon as you report them, and cannot go on a PT. A
        different person then confirms the damage or rejects the report. The count stays as it was:
        a missing piece is short, not damaged.
      </p>
      {lines.length === 0 ? (
        <p className="muted" data-testid="gg-report-damage-none">
          No piece on this receipt can be reported damaged here: everything is on a PT, already
          held, or wrong or unidentified goods that keep their own reason.
        </p>
      ) : (
        <>
          <div className="form-grid">
            <Field id="gg-rd-line" label="Which pieces">
              <select
                id="gg-rd-line"
                className="select"
                value={lineKey}
                onChange={(e) => setLineKey(e.target.value)}
                data-testid="gg-rd-line"
              >
                {lines.map((row) => (
                  <option key={row.line_key} value={row.line_key}>
                    {`${lineName(row)} — ${row.damage_reportable_qty} can be reported`}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              id="gg-rd-qty"
              label="How many are damaged"
              hint={line ? `Up to ${line.damage_reportable_qty}.` : undefined}
            >
              <input
                id="gg-rd-qty"
                className="input"
                type="number"
                min={1}
                max={line?.damage_reportable_qty ?? undefined}
                value={qty}
                onChange={(e) => setQty(e.target.value)}
                data-testid="gg-rd-qty"
              />
            </Field>
            <Field id="gg-rd-reason" label="What is wrong with them">
              <input
                id="gg-rd-reason"
                className="input"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                data-testid="gg-rd-reason"
              />
            </Field>
            <Field id="gg-rd-evidence" label="Photo or note evidence ID (optional)">
              <input
                id="gg-rd-evidence"
                className="input"
                value={evidence}
                onChange={(e) => setEvidence(e.target.value)}
                data-testid="gg-rd-evidence"
              />
            </Field>
          </div>
          <button
            className="btn btn-cta"
            disabled={busy || !reason || !lineKey || Number(qty) < 1}
            onClick={submit}
            data-testid="gg-rd-submit"
          >
            Report damage
          </button>
        </>
      )}
    </div>
  );
}

/** Every damage report over this receipt's goods, and where it is decided.
 *
 *  The receipt names each report and how many pieces it holds; the decision is
 *  the common damage review's, on the Movements screen, which the link opens. */
function DamageReportsView({ grn }: { grn: ResourceDTO<GrnCoverage> }) {
  const reports: GrnDamageReport[] = grn.data.damage_reports ?? [];
  if (reports.length === 0) return null;
  const siteId = grn.data.grn_header.site_id ?? "";
  return (
    <section data-testid="gg-damage-reports">
      <h4 className="gr-h4">
        <ShieldAlert size={15} /> Damage reports
      </h4>
      <div className="table-wrap" role="region" aria-label="Receipt damage reports" tabIndex={0}>
        <table className="data">
          <thead>
            <tr>
              <th>Reported</th>
              <th>Where it came from</th>
              <th className="num">Pieces</th>
              <th>Reported by</th>
              <th>Review</th>
            </tr>
          </thead>
          <tbody>
            {reports.map((report) => (
              <tr
                key={report.id}
                data-testid="gg-damage-report"
                data-report={report.id}
                data-state={report.state}
              >
                <td>{formatDateTime(report.reported_at)}</td>
                <td>{DAMAGE_SOURCE_LABEL[report.source] ?? report.source}</td>
                <td className="num" data-testid="gg-damage-report-qty">
                  {report.quantity}
                </td>
                <td>{report.reported_by.name || report.reported_by.id}</td>
                <td data-testid="gg-damage-report-state">
                  {DAMAGE_STATE_LABEL[report.state] ?? report.state}
                  {report.reviewed_by
                    ? ` by ${report.reviewed_by.name || report.reviewed_by.id}`
                    : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="gr-hint">
        A different person decides each report.{" "}
        <Link
          to={`/goods/movements${siteId ? `?site_id=${siteId}` : ""}`}
          data-testid="gg-damage-review-link"
        >
          Open the damage reviews
        </Link>
        . Confirming the damage values nothing and does not put the pieces on a PT; rejecting a
        mistaken report gives them back as good goods.
      </p>
    </section>
  );
}

function ShortageForm({ grn, onDone }: { grn: ResourceDTO<GrnCoverage>; onDone: () => void }) {
  const shortages = receiptShortages(grn.data);
  const [lineKey, setLineKey] = useState("");
  const [qty, setQty] = useState("1");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const selected = shortages.find((row) => row.claim.line_key === lineKey) ?? shortages[0];

  async function submit() {
    setError("");
    setOk("");
    const quantity = Number(qty);
    if (
      !selected ||
      !Number.isSafeInteger(quantity) ||
      quantity < 1 ||
      quantity > selected.remaining ||
      !reason.trim()
    ) {
      setError("Enter a whole quantity within the remaining shortage and a decision reason.");
      return;
    }
    setBusy(true);
    try {
      const body: Omit<DispositionRequest, keyof ReturnType<typeof goodsMeta>> = {
        kind: "accept_shortage",
        source_document_id: grn.id,
        source_line_key: selected.claim.line_key,
        qty: quantity,
        reason_code: reason.trim(),
        reviewed_grn_hash: grn.content_hash,
      };
      const { data } = await api.post<ResourceDTO<unknown>>(
        `/goods-v1/inbound/grns/${grn.id}/dispositions`,
        {
          ...body,
          ...goodsMeta(grn.revision),
        },
      );
      setOk(
        data.state.startsWith("approval")
          ? "Shortage sent for independent review. The invoice and physical count stay unchanged."
          : "Shortage decision recorded. The invoice and physical count stay unchanged.",
      );
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="gr-panel" data-testid="gg-shortage">
      <h4 className="gr-h4">Decide the invoice shortage</h4>
      <Feedback error={error} ok={ok} />
      {selected ? (
        <>
          <p className="gr-hint">
            This records why fewer pieces arrived. It creates no goods, cost or value. Any required
            checker decides in the review below.
          </p>
          <Field label="Invoice claim" id="gg-shortage-line">
            <select
              id="gg-shortage-line"
              data-testid="gg-shortage-line"
              value={selected.claim.line_key}
              onChange={(e) => setLineKey(e.target.value)}
            >
              {shortages.map((row) => (
                <option key={row.claim.line_key} value={row.claim.line_key}>
                  {row.claim.description || row.claim.style_code || "Invoice line"} —{" "}
                  {row.remaining} short
                </option>
              ))}
            </select>
          </Field>
          <p data-testid="gg-shortage-comparison">
            Invoice claimed {selected.comparison.claimed_qty}; physically counted{" "}
            {selected.comparison.counted_qty}; {selected.remaining} still needs a shortage decision.
          </p>
          <Field label="Shortage quantity" id="gg-shortage-qty">
            <input
              id="gg-shortage-qty"
              data-testid="gg-shortage-qty"
              type="number"
              min={1}
              max={selected.remaining}
              step={1}
              value={qty}
              onChange={(e) => setQty(e.target.value)}
            />
          </Field>
          <Field label="Shortage decision reason" id="gg-shortage-reason">
            <input
              id="gg-shortage-reason"
              data-testid="gg-shortage-reason"
              maxLength={60}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          <button
            type="button"
            className="btn btn-cta"
            data-testid="gg-shortage-submit"
            disabled={busy}
            onClick={submit}
          >
            {busy ? "Saving…" : "Record shortage decision"}
          </button>
        </>
      ) : (
        <p className="muted">No invoice shortage is waiting for a new decision.</p>
      )}
    </section>
  );
}

function ApprovalsPanel({ grn, onDone }: { grn: ResourceDTO<GrnCoverage>; onDone: () => void }) {
  const { session } = useAuth();
  const me = session?.user.human_id ?? "";
  // This receipt's own pending decisions, and nobody else's.
  //
  // A counter-GRN's subject is its own document, which the count history lists.
  // A disposition's subject is an action draft, and `ApprovalDTO` carries no
  // reference back to the GRN — its title is `"<kind> on <GRN number>"`, which
  // the server writes, so matching the receipt's number is the only link the
  // read offers. A receipt with no number yet has no dispositions either.
  const counterRevisions = useMemo(() => {
    const out = new Map<string, number>();
    for (const entry of grn.data.count_history) {
      if (entry.kind === "counter_grn" && typeof entry.revision === "number") {
        out.set(`document:${entry.document_id}`, entry.revision);
      }
    }
    return out;
  }, [grn.data.count_history]);
  const number = grn.number ?? "";
  const approvals = useGoodsFetch<Page<ApprovalRow>, ApprovalRow[]>(
    "/goods-v1/approvals",
    (r) =>
      (r.items ?? []).filter(
        (row) =>
          row.state === "pending" &&
          ((row.subject_kind === "document" && counterRevisions.has(row.subject_id)) ||
            (row.subject_kind === "disposition" && Boolean(number) && row.title.endsWith(number))),
      ),
    [],
  );
  // A request made in a sibling form reloads the receipt, not this list. Read the
  // list again whenever the receipt itself is read again, so a decision just
  // requested is shown (and its own-request notice with it) without a page reload.
  const seenReceipt = useRef(grn);
  const { reload: reloadApprovals } = approvals;
  useEffect(() => {
    if (seenReceipt.current === grn) return;
    seenReceipt.current = grn;
    reloadApprovals();
  }, [grn, reloadApprovals]);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const stepUp = useStepUp();

  async function decide(row: ApprovalRow, decision: "approve" | "reject") {
    setError("");
    setOk("");
    setBusy(true);
    try {
      // E234 is revision-bound and `ApprovalDTO` carries no revision of its
      // own (a known gap this ticket did not invent a fix for), so each kind's
      // revision is read from the subject it was requested against: a
      // counter-GRN's request quotes that counter document's own head revision
      // (the count history carries it), a disposition's quotes the receipt's.
      // Either way a subject that has moved since refuses rather than deciding
      // something else.
      const revision =
        row.subject_kind === "document" ? counterRevisions.get(row.subject_id) : grn.revision;
      // The retry after a step-up mints a fresh command identity, and may: a
      // `STEP_UP_REQUIRED` is refused before the command runs at all, so there
      // is no first attempt to replay. Anything that could have half-happened
      // would have to carry its identity into the retry instead.
      await stepUp.guarded(() =>
        api.post(`/goods-v1/approvals/${row.id}/decide`, {
          decision,
          reviewed_hash: row.reviewed_hash,
          ...(decision === "reject" ? { reason_code: "NOT_RIGHT" } : {}),
          ...goodsMeta(revision),
        }),
      );
      setOk(decision === "approve" ? "Approved." : "Sent back.");
      approvals.reload();
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const state = listState(
    { loading: approvals.loading, failure: approvals.failure, empty: approvals.value.length === 0 },
    "Nothing on this receipt is waiting for a decision.",
  );

  return (
    <div className="gr-panel" data-testid="gg-approvals">
      <h4 className="gr-h4">
        <ClipboardCheck size={15} /> Waiting for a second person
      </h4>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {state ?? (
        <div
          className="table-wrap"
          role="region"
          aria-label="Receipt pending decisions"
          tabIndex={0}
        >
          <table className="data" data-testid="gg-approvals-table">
            <thead>
              <tr>
                <th>What</th>
                <th>Asked by</th>
                <th>Asked</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {approvals.value.map((row) => {
                const ownWork = row.maker.id === me;
                const disposition = grn.data.dispositions.find(
                  (item) => item.approval_request_id === row.id && item.kind === "value_damage",
                );
                return (
                  <tr key={row.id} data-testid={`gg-approval-${row.id}`}>
                    <td>
                      {row.title}
                      {disposition && <ValueDamageEvidence row={disposition} compact />}
                    </td>
                    <td>{ownWork ? `${row.maker.name} (you)` : row.maker.name}</td>
                    <td>{row.requested_at ? formatDateTime(row.requested_at) : "—"}</td>
                    <td>
                      {ownWork ? (
                        <span className="warn-note" data-testid={`gg-approval-self-${row.id}`}>
                          You asked for this, so you cannot decide it.
                        </span>
                      ) : (
                        <>
                          <button
                            className="btn btn-cta btn-sm"
                            onClick={() => decide(row, "approve")}
                            disabled={busy}
                            data-testid={`gg-approve-${row.id}`}
                          >
                            Approve
                          </button>
                          <button
                            className="btn btn-sm"
                            onClick={() => decide(row, "reject")}
                            disabled={busy}
                            data-testid={`gg-reject-${row.id}`}
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
// The detail
// --------------------------------------------------------------------------

function ComparisonRow({
  comparison,
  claim,
}: {
  comparison: Comparison;
  claim: { description: string; claimed_qty: number; invoice_mrp_paise: string | null } | undefined;
}) {
  const mismatch = comparison.difference !== 0;
  return (
    <tr
      className={mismatch ? "gr-mismatch" : ""}
      data-testid={`gg-compare-${comparison.claim_line_key}`}
    >
      <td>{claim?.description ?? NOT_GIVEN}</td>
      <td>{claim ? formatPaiseString(claim.invoice_mrp_paise) : "Unknown"}</td>
      <td className="num">{comparison.claimed_qty}</td>
      <td className="num">{comparison.counted_qty}</td>
      <td className="num">
        {mismatch ? (
          <b data-testid={`gg-diff-${comparison.claim_line_key}`}>
            {comparison.difference > 0 ? `+${comparison.difference}` : comparison.difference}
          </b>
        ) : (
          "—"
        )}
      </td>
      <td>
        {comparison.difference === 0
          ? "Matches"
          : comparison.difference > 0
            ? "More arrived than the invoice claims"
            : "Less arrived than the invoice claims"}
      </td>
    </tr>
  );
}

/** One issued goods receipt: the invoice beside the count, what is covered and
 *  what is held, the corrections, and the decisions on disputed goods.
 *
 *  Rendered by the Goods receipts screen and by the receiving workflow's GRN
 *  step, so the two can never tell a different story about one receipt. */
export function GrnPanel({
  grnId,
  onClose,
}: {
  grnId: string;
  /** Absent ⇒ no close button: the workflow closes the delivery, not the step. */
  onClose?: () => void;
}) {
  const { session } = useAuth();
  const canCorrect = hold(session, "receive.arrival");
  const canDecide = hold(session, "receipt.disposition.decide");
  const doc = useResourceDoc<GrnCoverage>(`/goods-v1/inbound/grns/${grnId}`);
  // Ticket 05D: the site may ask to keep wrong goods (GSA-R01), and only that.
  const kinds = dispositionKindsFor(
    canDecide,
    hold(session, "movement.draft"),
    doc.doc?.data.lines.items ?? [],
  );
  const mySites = session?.sites ?? [];
  const vendors = useNames("/goods-v1/vendors?limit=100");
  const arrival = useResourceDoc<{ vendor_id: string; invoice_number: string | null }>(
    doc.doc ? `/goods-v1/inbound/arrivals/${doc.doc.data.grn_header.arrival_id}` : null,
  );
  const canReport = canReportDamage(session);
  const [showing, setShowing] = useState<"" | "counter" | "disposition" | "damage">("");

  const excess = useMemo(() => (doc.doc ? excessByLine(doc.doc.data) : {}), [doc.doc]);

  if (doc.denied) return <Denied what="goods receipt" />;
  // Only while there is nothing to show. A refresh after a write keeps the
  // record on screen — tearing the panels down mid-reload would also take the
  // message that says what just happened, and whatever was half-typed with it.
  if (doc.loading && !doc.doc) return <p className="muted">Loading…</p>;
  if (doc.failure) return <div className="warn-note">{doc.failure}</div>;
  if (!doc.doc) return <Denied what="goods receipt" />;

  const grn = doc.doc;
  const coverage = grn.data;
  const claims = new Map((coverage.invoice?.lines ?? []).map((line) => [line.line_key, line]));

  return (
    <div data-testid="gg-detail">
      <div className="toolbar">
        <h3 className="h3">{grn.number ?? `Receipt ${grn.id.slice(0, 8)}`}</h3>
        <span className="chip chip-green">Issued</span>
        <div className="spacer" />
        {onClose && (
          <button className="btn btn-sm" onClick={onClose} data-testid="gg-detail-close">
            <X size={14} /> Close
          </button>
        )}
      </div>

      <dl className="gr-facts" data-testid="gg-facts">
        <div>
          <dt>Site</dt>
          <dd>{siteName(mySites, coverage.grn_header.site_id)}</dd>
        </div>
        <div>
          <dt>Vendor</dt>
          <dd>{vendors.name(arrival.doc?.data.vendor_id)}</dd>
        </div>
        <div>
          <dt>Invoice</dt>
          <dd>{orNotGiven(arrival.doc?.data.invoice_number)}</dd>
        </div>
      </dl>
      <p className="gr-hint">
        This receipt records quantity only. It carries no cost, no value and no effect on the books.
      </p>

      <h4 className="gr-h4">
        <FileText size={15} /> The invoice beside the count
      </h4>
      {coverage.invoice === null ? (
        <p className="muted" data-testid="gg-no-invoice">
          No invoice was recorded for this arrival, so there is nothing to compare the count
          against. That is not a mismatch — it is an unknown.
        </p>
      ) : (
        <div
          className="table-wrap"
          role="region"
          aria-label="Receipt invoice comparison"
          tabIndex={0}
        >
          <table className="data" data-testid="gg-comparison">
            <thead>
              <tr>
                <th>Invoice line</th>
                <th>MRP claimed</th>
                <th className="num">Claimed</th>
                <th className="num">Counted</th>
                <th className="num">Difference</th>
                <th>What it means</th>
              </tr>
            </thead>
            <tbody>
              {coverage.invoice_comparison.map((comparison) => (
                <ComparisonRow
                  key={comparison.claim_line_key}
                  comparison={comparison}
                  claim={claims.get(comparison.claim_line_key)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <ThreeWayPanel
        arrivalId={threeWayOn(arrival.doc) ? (coverage.grn_header.arrival_id ?? null) : null}
        refresh={grn.content_hash}
      />

      <h4 className="gr-h4">
        <Scale size={15} /> What is counted, covered and held
      </h4>
      <div className="table-wrap" role="region" aria-label="Receipt counted coverage" tabIndex={0}>
        <table className="data" data-testid="gg-lines">
          <caption className="sr-only">
            Each counted line, with how much a live PT covers, how much is held and how much has
            been disposed of.
          </caption>
          <thead>
            <tr>
              <th>What</th>
              <th>Condition</th>
              <th className="num">Counted</th>
              <th className="num">On a live PT</th>
              <th className="num">Not on a PT</th>
              <th className="num">Held</th>
              <th className="num">Held for damage</th>
              <th className="num">Disposed</th>
              <th className="num">Can go on an ordinary PT</th>
            </tr>
          </thead>
          <tbody>
            {coverage.lines.items.map((line) => (
              <tr key={line.line_key} data-testid={`gg-line-${line.line_key}`}>
                <td>{lineName(line)}</td>
                <td>{CONDITION_LABEL[line.condition]}</td>
                <td className="num" data-testid={`gg-counted-qty-${line.line_key}`}>
                  {line.counted_qty}
                </td>
                <td className="num">{line.covered_qty}</td>
                <td className="num" data-testid={`gg-uncovered-qty-${line.line_key}`}>
                  {line.uncovered_qty}
                </td>
                <td className="num" data-testid={`gg-held-qty-${line.line_key}`}>
                  {line.held_qty}
                </td>
                <td className="num" data-testid={`gg-damage-held-qty-${line.line_key}`}>
                  {line.damage_held_qty}
                </td>
                <td className="num">{line.disposed_qty}</td>
                <td className="num" data-testid={`gg-coverable-${line.line_key}`}>
                  <b>{coverableQty(line, excess[line.line_key] ?? 0)}</b>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="gr-hint">
        "Not on a PT" is every piece no live PT covers yet. Most of those are ordinary good goods
        simply waiting for their PT — that is not a hold. "Held" is the separate, smaller number:
        pieces something has actually put a hold on, such as damage or goods counted above the
        invoice that nobody has decided yet. A held piece is never offered to an ordinary PT and is
        never filled into one; the next table says which is which.
      </p>

      <HeldGoodsView grn={grn} />

      <h4 className="gr-h4">
        <History size={15} /> How this count has changed
      </h4>
      <ol className="gr-history" data-testid="gg-history">
        {coverage.count_history.map((entry) => (
          <li key={entry.document_id} data-testid={`gg-history-${entry.document_id}`}>
            <b>{entry.kind === "grn" ? "Counted and issued" : "Counter-GRN"}</b>
            {entry.number ? ` ${entry.number}` : ""} — {formatDateTime(entry.recorded_at)}
            {entry.kind === "counter_grn" && (
              <span className={`chip chip-${entry.state === "approved" ? "green" : "amber"}`}>
                {entry.state === "approved" ? "Approved" : "Waiting for a second person"}
              </span>
            )}
            <ul>
              {entry.lines.map((line, index) => (
                <li key={index}>
                  {entry.kind === "grn"
                    ? `${line.qty} ${String(line.condition)}`
                    : `${String(line.old_qty)} → ${String(line.new_qty)} ${String(line.new_condition)} (${String(line.reason_code)})`}
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ol>

      <div className="toolbar" data-testid="gg-actions">
        {canCorrect && (
          <button
            className="btn btn-sm"
            onClick={() => setShowing(showing === "counter" ? "" : "counter")}
            data-testid="gg-counter-open"
          >
            Correct the count
          </button>
        )}
        {canReport && (
          <button
            className="btn btn-sm"
            onClick={() => setShowing(showing === "damage" ? "" : "damage")}
            data-testid="gg-report-damage-open"
          >
            Report damage
          </button>
        )}
        {kinds.length > 0 && (
          <button
            className="btn btn-sm"
            onClick={() => setShowing(showing === "disposition" ? "" : "disposition")}
            data-testid="gg-disposition-open"
          >
            Decide disputed goods
          </button>
        )}
      </div>

      {/* The panel stays open after a success: it is where the message that
          says what happened lives, and closing it would take the message with
          it. The button above closes it when the reader is done. */}
      {showing === "counter" && <CounterForm grn={grn} onDone={doc.reload} />}
      {showing === "damage" && <ReportDamageForm grn={grn} onDone={doc.reload} />}
      {/* Gated on `kinds` as well as the button: a reload can leave this person
          nothing to ask for here, and the form must then go, not claim that
          nothing on the receipt is held. */}
      {showing === "disposition" && kinds.length > 0 && (
        <DispositionForm grn={grn} onDone={doc.reload} kinds={kinds} />
      )}

      <ApprovalsPanel grn={grn} onDone={doc.reload} />
    </div>
  );
}

/** What this receipt is still holding: every piece no live PT covers, why, who
 *  owns the decision and when it is due, plus the damage kept at value.
 *
 *  One view, two homes - the receipt screen and the receiving workflow's
 *  Discrepancies step - because short, excess, damaged, wrong and unidentified
 *  quantities are the thing the two must never describe differently. Nothing
 *  here offers a held piece to an ordinary PT; that rule lives in the server,
 *  and this only shows what it has held. */
/** Goods ticket 13E: where this receipt's damaged goods went. They are still
 *  its goods, uncovered and held - a transfer moved them, it did not settle
 *  them - so the receipt says where they stand and links to each movement. */
function CustodyTransfers({ coverage }: { coverage: GrnCoverage }) {
  const moves = coverage.custody_transfers ?? [];
  const away = coverage.lines.items.filter(
    (line) => (line.elsewhere_qty ?? 0) > 0 || (line.in_transit_qty ?? 0) > 0,
  );
  if (moves.length === 0 && away.length === 0) return null;
  return (
    <div data-testid="gg-custody-transfers">
      <h4 className="gr-h4">Damaged goods moved to another site</h4>
      <p className="gr-hint">
        Still this receipt&apos;s goods: not on a PT, held in quarantine wherever they stand, and
        their value is unknown.
      </p>
      {away.length > 0 && (
        <ul>
          {away.map((line) => (
            <li key={line.line_key} data-testid={`gg-away-${line.line_key}`}>
              {lineName(line)}: {line.elsewhere_qty ?? 0} at another site,{" "}
              {line.in_transit_qty ?? 0} on the road
            </li>
          ))}
        </ul>
      )}
      <ul>
        {moves.map((move) => (
          <li key={move.id} data-testid="gg-custody-transfer" data-state={move.state}>
            <Link to={`/goods/transfers/${move.id}`}>
              {move.qty} piece{move.qty === 1 ? "" : "s"} · {move.state}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function HeldGoodsView({ grn }: { grn: ResourceDTO<GrnCoverage> }) {
  const exceptions = useGoodsFetch<Page<ExceptionRow>, ExceptionRow[]>(
    "/goods-v1/exceptions?limit=100",
    (r) => r.items ?? [],
    [],
  );
  const coverage = grn.data;
  const excess = useMemo(() => excessByLine(coverage), [coverage]);
  const mine = useMemo(() => exceptionsFor(exceptions.value, grn.id), [exceptions.value, grn.id]);
  const heldLines = coverage.lines.items.filter(
    (line) => line.uncovered_qty > 0 || line.held_qty > 0,
  );

  return (
    <>
      <h4 className="gr-h4">
        <AlertTriangle size={15} /> Pieces not on a PT, why, and who owns them
      </h4>
      {heldLines.length === 0 ? (
        <p className="muted" data-testid="gg-nothing-held">
          Every counted piece on this receipt is either covered by a live PT or has been decided.
        </p>
      ) : (
        <div className="table-wrap" role="region" aria-label="Receipt held pieces" tabIndex={0}>
          <table className="data" data-testid="gg-held">
            <thead>
              <tr>
                <th>What</th>
                <th className="num">Not on a PT</th>
                <th className="num">Held</th>
                <th>Why</th>
                <th>Owner</th>
                <th>Due</th>
              </tr>
            </thead>
            <tbody>
              {heldLines.map((line) => {
                const reasons = heldReasons(line, excess[line.line_key] ?? 0);
                return (
                  <tr key={line.line_key} data-testid={`gg-held-${line.line_key}`}>
                    <td>{lineName(line)}</td>
                    <td className="num">{line.uncovered_qty}</td>
                    <td className="num">{line.held_qty}</td>
                    <td>{reasons.join("; ")}</td>
                    <td>{mine[0]?.owner_role ?? NOT_GIVEN}</td>
                    <td>
                      {mine[0] ? (
                        <span className={mine[0].overdue ? "warn-note" : ""}>
                          {mine[0].due_at ? formatDateTime(mine[0].due_at) : NO_DUE_DATE}
                        </span>
                      ) : (
                        NOT_GIVEN
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <CustodyTransfers coverage={coverage} />
      {exceptions.denied && (
        <p className="gr-hint" data-testid="gg-exceptions-denied">
          You are not entitled to read the exception records, so the owner and due date above are
          not shown.
        </p>
      )}
      {mine.length > 0 && (
        <ul className="gr-exceptions" data-testid="gg-exceptions">
          {mine.map((row) => (
            <li key={row.id}>
              <b>{row.kind.replace(/_/g, " ")}</b> — {row.reason_code.replace(/\+/g, ", ")}. Owned
              by {row.owner_role ?? NOT_GIVEN},{" "}
              {row.due_at ? `due ${formatDateTime(row.due_at)}` : NO_DUE_DATE}
              {row.overdue ? " (overdue)" : ""}.
            </li>
          ))}
        </ul>
      )}

      <DamageReportsView grn={grn} />

      {coverage.dispositions.some((row) => row.kind === "value_damage") && (
        <section data-testid="gg-value-damage-history">
          <h4 className="gr-h4">Damage kept at value</h4>
          {coverage.dispositions
            .filter((row) => row.kind === "value_damage")
            .map((row) => (
              <ValueDamageEvidence key={row.id} row={row} />
            ))}
          <p className="gr-hint">
            Approval freezes the evidenced ticket MRP. Quantity stays unchanged, the damage hold and
            quarantine remain, ATS stays zero, and no books entry is created.
          </p>
        </section>
      )}
    </>
  );
}

/** The Discrepancies step of the receiving workflow: what this receipt is
 *  holding, the damage reports over it, and the forms that report and decide.
 *
 *  It shows the same held picture the receipt screen shows and offers the same
 *  forms, so the workflow adds a way in and no second rule. Short, excess,
 *  wrong and unidentified quantities stay held until a decision is made and,
 *  where the policy says so, approved by a second person. Damaged pieces stay
 *  held under their damage report, which a different person decides in the
 *  damage review (ticket 05C). None of them is ever offered to the ordinary PT. */
export function DispositionPanel({ grnId }: { grnId: string }) {
  const { session } = useAuth();
  const canDecide = hold(session, "receipt.disposition.decide");
  const canReport = canReportDamage(session);
  const doc = useResourceDoc<GrnCoverage>(`/goods-v1/inbound/grns/${grnId}`);
  const kinds = dispositionKindsFor(
    canDecide,
    hold(session, "movement.draft"),
    doc.doc?.data.lines.items ?? [],
  );

  if (doc.denied) return <Denied what="goods receipt" />;
  if (doc.loading && !doc.doc) return <p className="muted">Loading…</p>;
  if (doc.failure) return <div className="warn-note">{doc.failure}</div>;
  if (!doc.doc) return <Denied what="goods receipt" />;

  return (
    <div data-testid="gg-discrepancies">
      <HeldGoodsView grn={doc.doc} />
      {canDecide && <ShortageForm grn={doc.doc} onDone={doc.reload} />}
      {canReport && <ReportDamageForm grn={doc.doc} onDone={doc.reload} />}
      {kinds.length > 0 ? (
        <DispositionForm grn={doc.doc} onDone={doc.reload} kinds={kinds} />
      ) : (
        <p className="gr-hint" data-testid="gg-decide-denied">
          Deciding disputed goods is the inventory controller's, and a damage report is decided in
          the damage review by a different person. The pieces above stay held, unsellable and off
          every ordinary PT until then.
        </p>
      )}
      <ApprovalsPanel grn={doc.doc} onDone={doc.reload} />
    </div>
  );
}
